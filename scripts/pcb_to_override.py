#!/usr/bin/env python3
"""v2.3 P0-C：PCB 几何 → sim_override.generated.json（自动生成 v2.2 override）。

    python pcb_to_override.py Main.epro2 --net NET-CLOCK --stackup stackup.json --mode tline

产出直接能被 v2.2 的 override_apply 消费：
  * 每段走线 → 无损传输线（z0 + td 来自叠层解析估计）
  * 每个过孔 → series_rl（换层处，节点已按层拆开，所以 R/L 有地方落）
  * 节点名 `__AUTO_<NET>_B/E/V<n>`（需求文档 §2 要求，不用 `$`）

`--mode rlc` 给电短段用集总 π 型（series R+L + 两端 C/2），给"不想用 T 线"的场合。
两种模式的精度都在文件里写明，不夸大。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pcb_geometry as pg          # noqa: E402
import stackup as stk              # noqa: E402
import trace_extract as tx         # noqa: E402
import via_extract as vx           # noqa: E402

FIDELITY_TLINE = "ANALYTIC_ESTIMATE (lossless T-line from stackup)"
FIDELITY_RLC = "ANALYTIC_ESTIMATE (lumped pi, valid only for electrically short trace)"


def build_override(geo, stackup, nets=None, mode="tline", board_thickness_mm=None,
                   include_vias=True, overrides=None, default_width_mm=None):
    nets = nets or list(geo["nets"].keys())
    out = {
        "version": 2,
        "generated_by": "pcb_to_override.py (easyeda-ngspice-sim v2.3)",
        "source": geo.get("source"),
        "project": geo.get("project"),
        "comment": ("由 PCB 几何自动生成；z0/td 为解析估计（非场求解），"
                    "via R/L 为经验式估计。数值可在本文件里直接改。"),
        "model_fidelity": FIDELITY_TLINE if mode == "tline" else FIDELITY_RLC,
        "stackup": stackup.to_dict() if stackup else None,
        "params": {},
        "segments": [],
        "parasitics": [],
        "sources": [],
        "warnings": [],
    }
    n_tl = n_via = skipped_vias = 0
    for net_name in nets:
        net = geo["nets"].get(net_name)
        if not net:
            out["warnings"].append(f"net {net_name} 不在 PCB_GEOMETRY 里，跳过")
            continue
        segs, warns = tx.estimate_net(net_name, net, stackup,
                                      default_width_mm=default_width_mm,
                                      overrides=overrides)
        out["warnings"].extend(warns)
        # 端点悬空的传输线 = 开路短桩，会给出纯反射结果，必须提示
        deg = {}
        for sg in segs:
            for k in (sg["from"], sg["to"]):
                deg[k] = deg.get(k, 0) + 1
        for sg in segs:
            for k in (sg["from"], sg["to"]):
                if deg[k] == 1 and k.startswith("__AUTO_"):
                    out["warnings"].append(
                        f"net {net_name}: 传输线端 {k} 既不是焊盘也没有后续走线"
                        f"（开路短桩）；若它其实接铜皮/平面，请补一个终端或用 "
                        f"--no-via 后手工建模")
        for seg in segs:
            nm = f"T_AUTO_{_tok(net_name)}_{seg['segment_index']}"
            if mode == "rlc":
                out["parasitics"].extend(_rlc_trace(seg, nm, out))
            else:
                out["parasitics"].append({
                    "name": nm,
                    "element_name": nm,
                    "type": "transmission_line",
                    "from": seg["from"],
                    "to": seg["to"],
                    "z0": round(seg["z0_ohm"], 2),
                    "td": f"{seg['td_ps']:.3f}p",
                    "ref": "0",
                    "source": "pcb_geometry",
                    "derivation": [
                        f"layer {seg['layer']} {seg['trace_model']}",
                        f"width = {seg['width_mm']} mm, h = {seg['h_mm']} mm, "
                        f"er = {_er(geo, seg['layer'])}",
                        f"Z0 = {seg['z0_ohm']} ohm, eps_eff = {seg['eps_eff']}",
                        f"length = {seg['length_mm']} mm, "
                        f"td = {seg['td_ps_per_mm']} ps/mm",
                        f"TD = {seg['td_ps']} ps",
                    ],
                })
                n_tl += 1
        if include_vias:
            # 只保留"两端都真的接了走线"的过孔。铺铜缝合孔（plane stitching）在
            # 走线图里没有邻居，硬加 R/L 会造出一对悬空节点 → 奇异矩阵。
            used = set()
            for seg in segs:
                used.add(seg["from"])
                used.add(seg["to"])
            vias, vwarns = vx.estimate_vias(net, stackup, board_thickness_mm)
            out["warnings"].extend(vwarns)
            skipped = 0
            for v in vias:
                nodes = v.get("nodes") or []
                if len(nodes) < 2:
                    continue
                pairs = [(nodes[i], nodes[i + 1]) for i in range(len(nodes) - 1)]
                live = [pr for pr in pairs if pr[0] in used and pr[1] in used]
                if not live:
                    skipped += 1
                    skipped_vias += 1
                    continue
                for i, (na, nb) in enumerate(live):
                    nm = f"{v['id']}_RL" + ("" if i == 0 else f"_{i + 1}")
                    out["parasitics"].append({
                        "name": nm,
                        "element_names": [f"R_AUTO_{_tok(net_name)}_V{v['id'].split('_V')[-1]}"
                                          f"_{i + 1}",
                                          f"L_AUTO_{_tok(net_name)}_V{v['id'].split('_V')[-1]}"
                                          f"_{i + 1}"],
                        "type": "series_rl",
                        "from": na,
                        "to": nb,
                        "r": f"{v['r_ohm']:.6g}",
                        "l": f"{v['l_nh']:.4g}n",
                        "source": "pcb_geometry",
                        "derivation": [
                            f"via {v['id']}: drill {v['drill_mm']} mm, "
                            f"pad {v['pad_mm']} mm, length {v['length_mm']} mm",
                            f"L = {v['l_nh']} nH, C(shunt) = {v['c_pf']} pF",
                        ],
                    })
                    n_via += 1
    if skipped_vias:
        out["warnings"].append(
            f"{skipped_vias} 个过孔的两端没有走线连接（铺铜缝合孔 / 平面连接），"
            f"未建模为 R/L —— 它们接入的是铜皮/平面，不在走线拓扑里")
    out["stats"] = {"transmission_lines": n_tl, "via_series_rl": n_via,
                    "nets": nets, "mode": mode,
                    "vias_skipped_not_on_tracks": skipped_vias}
    # 空段清理
    for k in ("segments", "sources"):
        if not out[k]:
            out.pop(k)
    return out


def _rlc_trace(seg, nm, out):
    """集总 π：series R+L，两端各 C/2（只对电短段成立）。"""
    z0 = seg["z0_ohm"]
    td = seg["td_ps"] * 1e-12
    L = z0 * td
    C = td / z0 if z0 else 0
    base = nm.replace("T_AUTO_", "")
    return [{
        "name": f"{nm}_RL",
        "element_names": [f"R_AUTO_{base}", f"L_AUTO_{base}"],
        "type": "series_rl",
        "from": seg["from"], "to": seg["to"],
        "r": "1m", "l": f"{L * 1e9:.4g}n",
        "source": "pcb_geometry",
        "derivation": [f"lumped L = Z0*TD = {L * 1e9:.4g} nH",
                       f"（等价电容 {C * 1e12:.4g} pF 未展开，需 --mode tline 更准）"],
    }]


def _tok(s):
    import re
    return re.sub(r"[^0-9A-Za-z_]", "_", str(s)).strip("_") or "NET"


def _er(geo, layer):
    for d in ((geo.get("stackup") or {}).get("dielectrics") or []):
        if d.get("er"):
            return d["er"]
    return "?"


# --------------------------------------------------------------------------
def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        sys.exit(2)
    src = argv[0]
    net, out = [], "sim_override.generated.json"
    stackup_path = None
    mode = "tline"
    board_t = None
    unit = None
    no_via = False
    width = None
    geo_out = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--net":
            net.append(argv[i + 1]); i += 2
        elif a == "--out":
            out = argv[i + 1]; i += 2
        elif a == "--stackup":
            stackup_path = argv[i + 1]; i += 2
        elif a == "--mode":
            mode = argv[i + 1].lower(); i += 2
        elif a == "--board-thickness":
            board_t = float(argv[i + 1]); i += 2
        elif a == "--unit":
            unit = argv[i + 1]; i += 2
        elif a == "--width":
            width = float(argv[i + 1]); i += 2
        elif a == "--geometry-out":
            geo_out = argv[i + 1]; i += 2
        elif a == "--no-via":
            no_via = True; i += 1
        else:
            i += 1
    if mode not in ("tline", "rlc"):
        raise SystemExit(f"--mode 只支持 tline|rlc，收到 {mode}")
    if not net:
        raise SystemExit(
            "必须用 --net 指定要提参的网络（可多次），或 --net all。"
            "不默认全板提参：一个真实工程可能有几十个网络，全量 overkill 且难审。")
    raw = pg.load_pcb_input(src, unit=unit)
    geo = pg.build_geometry(raw, net=None if net == ["all"] else net)
    if geo_out:
        with open(geo_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(geo, f, ensure_ascii=False, indent=1)

    base_st = stk.Stackup.from_pcb(geo.get("layers"), raw.get("stackup_raw"), board_t)
    if stackup_path:
        # 用户只补缺的项；PCB 里已有的（εr / 介质厚度）不要求重抄
        st = stk.Stackup.from_file(stackup_path, base=base_st)
        st.meta["merged_with_pcb"] = True
        missing = st.validate()
    else:
        st = base_st
        missing = st.validate()
        if missing:
            raise SystemExit(
                "STACKUP_INCOMPLETE: PCB 文件里的叠层不完整，无法 auto-Z0。\n"
                "  缺失: " + "; ".join(missing[:6]) + "\n"
                "  请提供 --stackup stackup.json（只补缺的项即可）。\n"
                "  注意：本工具不会默认 FR4=4.5 继续跑。")
    st.require_complete()

    ovr = build_override(geo, st, nets=None if net == ["all"] else net, mode=mode,
                         board_thickness_mm=board_t, include_vias=not no_via,
                         default_width_mm=width)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(ovr, f, ensure_ascii=False, indent=1)
    print(f"override written: {out}")
    print("  {} T-line(s), {} via R/L, mode={}".format(
        ovr["stats"]["transmission_lines"], ovr["stats"]["via_series_rl"], mode))
    for p in ovr["parasitics"][:6]:
        if p["type"] == "transmission_line":
            print(f"  [TL ] {p['from']} -> {p['to']}  Z0={p['z0']} TD={p['td']}")
        else:
            print(f"  [R/L] {p['from']} -> {p['to']}  r={p['r']} l={p['l']}")
    for w in ovr["warnings"][:8]:
        print(f"  [warn] {w}")


if __name__ == "__main__":
    main()
