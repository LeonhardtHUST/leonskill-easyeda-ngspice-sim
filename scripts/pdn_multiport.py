#!/usr/bin/env python3
"""v2.3 P0-G：多电源轨 PDN 分析（Z11 / Z21 / 传递阻抗）。

实现方式：ngspice-47 支持 `.sp` S 参数分析 + 带 `portnum`/`z0` 的电压源作为 RF 端口
（本机实测：`V1 in 0 AC 1 portnum=1 z0=50` + `sp dec 5 1meg 100meg` 可跑，
输出 `Z_1_1 / Z_2_1 / …`）。所以多端口 PDN 不需要自己拼 AC 激励矩阵，直接把每个
rail 定义成一个端口即可。

端口定义（§18）::

    {"ports": [
       {"name": "FPGA_CORE", "node": "1V2",  "return": "GND"},
       {"name": "ADC_AVDD",  "node": "3V3AD", "return": "AGND"}
    ]}

注意：
  * `sp` 是交流小信号分析，端口源在直流上是短路——如果某条 rail 上已经挂着**理想**
    电压源，Z11 会恒等于 0（没有意义）。这里会主动检查并报
    `PDN_PORT_SHORTED_BY_IDEAL_SOURCE`（strict 下致命）。
  * 至少需要 2 个端口（ngspice 要求）。
"""
import json
import math
import os
import re

import parse_raw


class PdnError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def normalize_ports(cfg):
    ports = (cfg or {}).get("ports") if isinstance(cfg, dict) else cfg
    if not ports:
        raise PdnError("PDN_PORTS_MISSING", "没有端口定义（config.ports）")
    out = []
    for i, p in enumerate(ports, start=1):
        node = p.get("node")
        ret = p.get("return") or "0"
        if not node:
            raise PdnError("PDN_PORT_INVALID", f"第 {i} 个端口缺少 node: {p}")
        if str(node) == str(ret):
            raise PdnError("PDN_PORT_INVALID",
                           f"端口 {p.get('name')}: node 与 return 相同（{node}）")
        out.append({"index": i, "name": p.get("name") or f"PORT{i}",
                    "node": node, "return": ret,
                    "z0": float(p.get("z0") or 50.0)})
    if len(out) < 2:
        raise PdnError("PDN_PORTS_TOO_FEW",
                       "至少需要 2 个端口（ngspice 的 sp 分析要求）")
    return out


def check_ideal_source_short(deck_text, ports):
    """rail 上若有理想电压源直接短接 → Z11 恒为 0，必须提前告警。"""
    problems = []
    vsrc = re.findall(r"(?im)^\s*(V\w+)\s+(\S+)\s+(\S+)\s+(.+)$", deck_text)
    for p in ports:
        want = {str(p["node"]).lower(), str(p["return"]).lower()}
        if want == {"0"}:
            continue
        for name, n1, n2, rest in vsrc:
            if "portnum" in rest.lower():
                continue
            if {n1.lower(), n2.lower()} == want:
                problems.append(
                    f"端口 {p['name']}（{p['node']}→{p['return']}）被理想电压源 {name} 直接短接，"
                    f"Z11 会恒为 0；请给该 rail 一个带输出阻抗的真实源（regulator .SUBCKT / "
                    f"series R）")
    return problems


def _deck_node(deck, name):
    """把端口配置里的网络名映射到 deck 里实际使用的节点名。

    netlist_gen 会把 `1V2` 规范化成 `N_1V2`；端口配置若照抄原理图网络名，
    直接拼进 deck 就会指向一个不存在的新节点（sp 分析于是没有数据）。
    """
    import re
    tokens = set(re.findall(r"[^\s]+", deck))
    if name in tokens:
        return name
    try:
        from sim_config import spice_node
        cand = spice_node(name)
        if cand in tokens:
            return cand
    except Exception:
        pass
    return name


def inject_ports(deck_text, ports):
    """插入端口电压源 + 把分析换成 sp。"""
    lines = deck_text.splitlines()
    has_control = any(l.strip().lower().startswith(".control") for l in lines)
    port_lines = ["* ---- PDN RF ports (ngspice sp analysis) ----"]
    for p in ports:
        n = _deck_node(deck_text, str(p["node"]))
        r = _deck_node(deck_text, str(p["return"]))
        port_lines.append(f"VPDN{p['index']} {n} {r} DC 0 AC 1 "
                          f"portnum={p['index']} z0={p['z0']:g}")
    # 去掉原有 .control 块
    if has_control:
        out, skip = [], False
        for l in lines:
            s = l.strip().lower()
            if s.startswith(".control"):
                skip = True
                continue
            if s.startswith(".endc"):
                skip = False
                continue
            if not skip:
                out.append(l)
        lines = out
    # 在 .end 之前插端口
    idx = None
    for i, l in enumerate(lines):
        if l.strip().lower() == ".end":
            idx = i
            break
    if idx is None:
        lines.append(".end")
        idx = len(lines) - 1
    lines = lines[:idx] + port_lines + lines[idx:]
    return "\n".join(lines) + "\n"


def build_sp_control(nports, f_start="1k", f_stop="100meg", ppd=20):
    return (f".control\nsp dec {ppd} {f_start} {f_stop}\n"
            f"write pdn.raw all\n.endc\n")


def _var_index(plot, name):
    want = name.lower().replace("(", "").replace(")", "")
    for i, v in enumerate(plot.get("vars") or []):
        vv = v.lower()
        if vv == want or vv == f"{want}":
            return i
    return None


def parse_z(plots, nports):
    """从 raw plot 里取出 Z 矩阵。返回 {freqs, Z[{"1,1": [...]}]}。"""
    plot = None
    for p in plots or []:
        if "complex" in str(p.get("flags") or ""):
            plot = p
            break
    if plot is None and plots:
        plot = plots[0]
    if plot is None:
        raise PdnError("PDN_NO_DATA", "没有可用的 sp 分析结果")
    freqs = []
    for row in plot["points"]:
        x = row[0]
        freqs.append(float(x[0]) if isinstance(x, list) else float(x))
    zmat = {}
    for i in range(1, nports + 1):
        for j in range(1, nports + 1):
            idx = _var_index(plot, f"z_{i}_{j}")
            if idx is None:
                continue
            vals = []
            for row in plot["points"]:
                v = row[idx]
                vals.append(complex(v[0], v[1]) if isinstance(v, list) else complex(v, 0))
            zmat[f"{i},{j}"] = vals
    if not zmat:
        avail = list(plot.get("vars") or [])[:20]
        raise PdnError("PDN_NO_Z_MATRIX",
                       f"raw 里没有 Z_i_j 变量（现有: {avail}）。"
                       f"确认 ngspice 版本支持 sp 分析且端口 >= 2")
    return freqs, zmat


def analyze(freqs, zmat, ports, resonance_min_ratio=1.5):
    """把 Z 矩阵变成可决策的指标。"""
    out = {"ports": [{"name": p["name"], "node": p["node"], "return": p["return"]}
                     for p in ports],
           "n_points": len(freqs),
           "freq_hz": freqs,
           "impedance": {}}
    for key, vals in zmat.items():
        i, j = (int(x) for x in key.split(","))
        mags = [abs(v) for v in vals]
        rec = {"from": ports[i - 1]["name"], "to": ports[j - 1]["name"],
               "|z|_ohm": mags}
        if i == j:
            imax = max(range(len(mags)), key=lambda k: mags[k]) if mags else 0
            rec.update({"z_max_ohm": mags[imax] if mags else None,
                        "z_max_f": freqs[imax] if mags else None,
                        "z_min_ohm": min(mags) if mags else None,
                        "z_dc_ohm": mags[0] if mags else None})
        out["impedance"][key] = rec
    # 谐振/反谐振候选（Z11 的局部极大 / 相邻极小）
    res, anti = [], []
    for key, rec in out["impedance"].items():
        i, j = (int(x) for x in key.split(","))
        if i != j:
            continue
        mags = rec["|z|_ohm"]
        for k in range(1, len(mags) - 1):
            if mags[k] > mags[k - 1] * resonance_min_ratio and \
                    mags[k] > mags[k + 1] * resonance_min_ratio:
                res.append({"port": rec["from"], "f_hz": freqs[k], "z_ohm": mags[k]})
            if mags[k] * resonance_min_ratio < mags[k - 1] and \
                    mags[k] * resonance_min_ratio < mags[k + 1]:
                anti.append({"port": rec["from"], "f_hz": freqs[k], "z_ohm": mags[k]})
    out["resonances"] = res
    out["anti_resonances"] = anti
    out["note"] = "simulation result, verify with measurement"
    return out


def run(deck_text, ports_cfg, ngspice_bin, workdir, f_start="1k", f_stop="100meg",
        ppd=20, strict=True, timeout=600):
    """完整流程：注入端口 → 跑 sp → 解析 → 指标。返回 (result, log)。"""
    import sweep_runner
    ports = normalize_ports(ports_cfg)
    problems = check_ideal_source_short(deck_text, ports)
    if problems:
        if strict:
            raise PdnError("PDN_PORT_SHORTED_BY_IDEAL_SOURCE", "; ".join(problems))
    deck = inject_ports(deck_text, ports)
    ctrl = build_sp_control(len(ports), f_start, f_stop, ppd)
    m = re.search(r"(?im)^[ \t]*\.end[ \t]*$", deck)
    if m:
        deck = deck[:m.start()] + ctrl + ".end\n" + deck[m.end():]
    else:
        deck = deck + ctrl + ".end\n"
    os.makedirs(workdir, exist_ok=True)
    path = os.path.join(workdir, "pdn_deck.cir")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(deck)
    rc, out, err = sweep_runner.run_ngspice(ngspice_bin, path, workdir, timeout=timeout)
    log = out + "\n" + err
    if rc != 0:
        raise PdnError("PDN_SIM_FAILED", f"ngspice 返回 {rc}: " + log[-500:])
    raw = os.path.join(workdir, "pdn.raw")
    if not os.path.exists(raw):
        raise PdnError("PDN_NO_RAW", "没有生成 pdn.raw: " + log[-500:])
    plots = parse_raw.parse_all(raw)
    freqs, zmat = parse_z(plots, len(ports))
    res = analyze(freqs, zmat, ports)
    res["warnings"] = problems
    return res, log


def summary_lines(res, limit=6):
    lines = ["PDN ports: " + ", ".join(
        f"{p['name']}({p['node']}→{p['return']})" for p in res["ports"])]
    for key, rec in res["impedance"].items():
        i, j = (int(x) for x in key.split(","))
        if i == j:
            lines.append(f"  Z{i}{i} [{rec['from']}]: max {rec['z_max_ohm']:.4g} Ω "
                         f"@ {rec['z_max_f'] / 1e6:.3g} MHz, min {rec['z_min_ohm']:.4g} Ω")
        elif i < j:
            lo = min(rec["|z|_ohm"])
            hi = max(rec["|z|_ohm"])
            lines.append(f"  Z{i}{j} [{rec['from']}→{rec['to']}] transfer: "
                         f"{lo:.4g}..{hi:.4g} Ω")
    for r in res["resonances"][:limit]:
        lines.append(f"  resonance: {r['port']} @ {r['f_hz'] / 1e6:.3g} MHz "
                     f"({r['z_ohm']:.4g} Ω)")
    for r in res["anti_resonances"][:limit]:
        lines.append(f"  anti-resonance: {r['port']} @ {r['f_hz'] / 1e6:.3g} MHz "
                     f"({r['z_ohm']:.4g} Ω)")
    lines.append("  " + res.get("note", ""))
    return lines


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print("usage: pdn_multiport.py <deck.cir> <ports.json> <workdir> [ngspice]")
        sys.exit(2)
    with open(sys.argv[2], encoding="utf-8") as f:
        cfg = json.load(f)
    ng = os.environ.get("NGSPICE_BIN") or os.path.expanduser(
        "~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe")
    res, _ = run(open(sys.argv[1], encoding="utf-8").read(), cfg, ng, sys.argv[3])
    for ln in summary_lines(res):
        print(ln)
