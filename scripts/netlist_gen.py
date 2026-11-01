#!/usr/bin/env python3
"""网表生成器：IR + 网络归组 → NGspice 网表文本 + 警告报告。

用法:
  python netlist_gen.py <ir_with_nets.json> <out.cir> [--keep-open]

分类规则（优先级序）:
  1. R/C/L（2脚）        直接按 Value 出实例
  2. Q（BJT 3脚）        查模型库(MPN/器件名) → 泛化 NPN/PNP 兜底
  3. D/LED               查库 → 通用二极管兜底
  4. M（MOSFET 3脚）     泛化 NMOS/PMOS
  5. 电压源器件          按属性直出
  6. Power 网标自动补源  "+12V"/"5V"/"3V3" 等命名且无真实源驱动 → 自动加 V_PWR
  7. 无模型器件（继电器/排针/端子/IC）默认排除并告警（--keep-open 保留为注释）
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(HERE, "models")

VOLT_RE = re.compile(r"^[+\-]?(\d+)(?:[Vv](\d+))?[Vv]?$")


def load_models():
    disc = json.load(open(os.path.join(MODELS_DIR, "discrete.json"), encoding="utf-8"))
    gen = json.load(open(os.path.join(MODELS_DIR, "generic.json"), encoding="utf-8"))
    alias_map = {}
    for name, part in disc.get("parts", {}).items():
        for a in part.get("aliases", []) + [name]:
            alias_map[str(a).upper()] = name
    return disc, gen, alias_map


def sanitize_net(name, used):
    """网名清洗：GND→0；非法字符→_；去重加后缀。"""
    if name is None:
        return None
    s = str(name).strip()
    if re.fullmatch(r"(?i)gnd\d*|ground|agnd|dgnd|0", s):
        return "0"
    s = re.sub(r"[^0-9A-Za-z_+\-.$]", "_", s)
    if not s:
        s = "N_unnamed"
    base = s
    i = 2
    while s in used and used[s] != base:
        s = f"{base}_{i}"
        i += 1
    used[s] = base
    return s


def parse_voltage(netname):
    """从网络名解析电压值：+12V→12，-15V→-15，5V→5，3V3→3.3。解析失败返回 None。"""
    if not netname:
        return None
    s = str(netname).strip()
    neg = s.startswith("-")
    m = VOLT_RE.match(s)
    if not m:
        return None
    whole = m.group(1)
    dec = m.group(2)
    v = float(f"{whole}.{dec}") if dec else float(whole)
    if v <= 0 or v > 1000:
        return None
    return -v if neg else v


def sanitize_des(des, used=None):
    """器件位号 → SPICE 安全名：C2' 之类的撇号会破坏 ngspice 解析（被当成表达式引号）。"""
    s = re.sub(r"[^0-9A-Za-z_]", "_", str(des or "X"))
    if not re.match(r"^[A-Za-z]", s):
        s = "X" + s
    if used is not None:
        base = s
        i = 2
        while s in used:
            s = f"{base}_{i}"
            i += 1
        used.add(s)
    return s


def _pin_net(pin_net, des, num):
    return pin_net.get(des, {}).get(str(num))


def _two_pin_nets(comp, pin_net):
    """取 2 脚元件的两个网，返回 (net1, net2, pin1, pin2)。"""
    pins = comp.get("pins") or []
    if len(pins) != 2:
        return None
    a, b = pins[0], pins[1]
    na = _pin_net(pin_net, comp["designator"], a.get("number"))
    nb = _pin_net(pin_net, comp["designator"], b.get("number"))
    return na, nb, a.get("number"), b.get("number")


def _find_pin_by_role(comp, role_names):
    """按引脚名找引脚（C/B/E 等）。"""
    for p in comp.get("pins") or []:
        nm = (p.get("name") or "").upper()
        for r in role_names:
            if r in nm:
                return p
    return None


def resolve_bjt_pins(comp, model_pins, report):
    """确定 BJT 的 C/B/E 三个引脚，返回 {role: pin_number} 或 None。

    优先级：引脚名 → 模型库 pins 映射 → 几何（基极=与另两脚不同列的脚）→ 编号惯序(1=B,2=E,3=C)
    """
    pins = comp.get("pins") or []
    if len(pins) < 3:
        return None
    by_name = {
        "C": _find_pin_by_role(comp, ["C", "COLLECTOR", "集电极"]),
        "B": _find_pin_by_role(comp, ["B", "BASE", "基极"]),
        "E": _find_pin_by_role(comp, ["E", "EMITTER", "发射极"]),
    }
    if all(by_name.values()):
        return {r: p.get("number") for r, p in by_name.items()}
    if model_pins and all(k in model_pins for k in ("C", "B", "E")):
        return dict(model_pins)
    nums = [str(p.get("number")) for p in pins if p.get("number") is not None]
    if sorted(nums) == ["1", "2", "3"]:
        report["warnings"].append(
            f"{comp['designator']}: 无引脚名/模型映射，按惯序 1=B 2=E 3=C 处理（请核对）")
        return {"B": "1", "E": "2", "C": "3"}
    xs = {}
    for p in pins:
        xs.setdefault(round(float(p.get("x") or 0), 1), []).append(p)
    if len(xs) == 2:
        # 两脚同列（竖排），第三脚在另一列 = 基极
        vert = [grp for grp in xs.values() if len(grp) == 2]
        base = [grp for grp in xs.values() if len(grp) == 1]
        if vert and base:
            v = sorted(vert[0], key=lambda p: float(p.get("y") or 0))
            report["warnings"].append(
                f"{comp['designator']}: 按几何推断 B/C/E（上C 下E），请核对极性与符号方向")
            return {"C": v[-1].get("number"), "E": v[0].get("number"), "B": base[0].get("number")}
    report["warnings"].append(f"{comp['designator']}: 无法确定 C/B/E 引脚，已跳过")
    return None


def generate(ir, pin_net, keep_open=False):
    disc, gen, alias_map = load_models()
    report = {"included": [], "excluded": [], "generic": [], "powers": [],
              "dangling_nets": [], "warnings": []}
    lines = []
    model_cards = {}
    used_nets = {}
    used_des = set()

    def net_or(name, comp):
        if name is None:
            report["warnings"].append(f"{comp}: 存在悬空引脚（无网络），已跳过该器件")
            return None
        return sanitize_net(name, used_nets)

    # 参与仿真的引脚 → 是否已有源
    sourced_nets = set()
    for c in ir.get("components", []):
        if c.get("componentType") != "part":
            continue
        des = c.get("designator") or ""
        sdes = sanitize_des(des, used_des)
        prefix = (c.get("pinPrefix") or "").upper()
        title = (c.get("deviceTitle") or "")
        mpn = (c.get("manufacturerPart") or "")
        value = c.get("value")
        pins = c.get("pins") or []

        # --- 电压源类器件 ---
        if prefix == "V" or "电压源" in title or "VOLTAGE SOURCE" in title.upper():
            nets = _two_pin_nets(c, pin_net)
            if not nets:
                report["warnings"].append(f"{des}: 电压源引脚数异常，跳过")
                continue
            na, nb, pa, pb = nets
            na, nb = net_or(na, des), net_or(nb, des)
            if na is None or nb is None:
                continue
            vval = value
            for k, v in (c.get("attrs") or {}).items():
                if "voltage" in str(k).lower() and v:
                    vval = v
            is_ac = "AC" in title.upper() or "正弦" in title
            if is_ac:
                lines.append(f"{sdes} {na} {nb} SIN(0 10mV 1k) AC 1 0")
            else:
                vv = vval or "5V"
                lines.append(f"{sdes} {na} {nb} DC {vv} AC 0 0")
            sourced_nets.update([na, nb])
            report["included"].append({"designator": des, "as": "V"})
            continue

        kind = prefix if prefix in ("R", "C", "L") else (
            "D" if prefix in ("D", "LED") else (
                "Q" if prefix == "Q" else ("M" if prefix in ("M", "Q_NMOS", "Q_PMOS") else None)))
        if prefix and prefix.upper() in ("H", "J", "P", "K", "CN", "SW", "U", "IC", "TP", "F"):
            kind = None

        if kind in ("R", "C", "L"):
            nets = _two_pin_nets(c, pin_net)
            if not nets:
                report["warnings"].append(f"{des}: {kind} 引脚数不是 2，跳过")
                continue
            na, nb = net_or(nets[0], des), net_or(nets[1], des)
            if na is None or nb is None:
                continue
            val = value
            if not val:
                dflt = gen.get("valueDefaults", {}).get(kind, {})
                val = dflt.get("value", {"R": "1k", "C": "100n", "L": "1m"}.get(kind))
                report["warnings"].append(f"{des}: 无 Value，按 {val} 兜底")
            lines.append(f"{sdes} {na} {nb} {val}")
            report["included"].append({"designator": des, "as": kind})
            continue

        if kind == "Q":
            model_name = None
            model_pins = None
            card = None
            for key in (mpn, title):
                if not key:
                    continue
                hit = alias_map.get(str(key).upper())
                if hit:
                    part = disc["parts"][hit]
                    model_name, model_pins, card = hit, part.get("pins"), part.get("card")
                    break
            if not model_name:
                is_pnp = ("PNP" in (title + mpn).upper()) or ("9012" in mpn) or ("9015" in mpn) or ("8550" in mpn)
                g = gen["generic"]["PNP" if is_pnp else "NPN"]
                model_name, card = g["modelName"], g["card"]
                model_pins = None
                report["generic"].append({"designator": des, "model": model_name,
                                          "why": f"未在库中匹配 {mpn or title}"})
            role_pins = resolve_bjt_pins(c, model_pins, report)
            if not role_pins:
                continue
            nc = net_or(_pin_net(pin_net, des, role_pins["C"]), des)
            nb = net_or(_pin_net(pin_net, des, role_pins["B"]), des)
            ne = net_or(_pin_net(pin_net, des, role_pins["E"]), des)
            if None in (nc, nb, ne):
                continue
            lines.append(f"{sdes} {nc} {nb} {ne} {model_name}")
            model_cards[model_name] = card
            report["included"].append({"designator": des, "as": "Q", "model": model_name})
            continue

        if kind in ("D", "M") or prefix in ("LED",):
            nets = _two_pin_nets(c, pin_net) if kind == "D" else None
            if kind == "D":
                if not nets:
                    report["warnings"].append(f"{des}: 二极管引脚数异常，跳过")
                    continue
                na, nb = net_or(nets[0], des), net_or(nets[1], des)
                if na is None or nb is None:
                    continue
                hit = alias_map.get((mpn or "").upper()) or alias_map.get((title or "").upper())
                if hit:
                    part = disc["parts"][hit]
                    model_name, card = hit, part["card"]
                else:
                    g = gen["generic"]["D" if "LED" not in (title or "").upper() else "LED"]
                    model_name, card = g["modelName"], g["card"]
                    report["generic"].append({"designator": des, "model": model_name,
                                              "why": f"未在库中匹配 {mpn or title}"})
                lines.append(f"{sdes} {na} {nb} {model_name}")
                model_cards[model_name] = card
                report["included"].append({"designator": des, "as": "D", "model": model_name})
                continue
            # MOSFET：引脚名 G/D/S 或编号 1=G 2=S 3=D
            p3 = c.get("pins") or []
            role = {}
            for p in p3:
                nm = (p.get("name") or "").upper()
                for r in ("G", "D", "S"):
                    if r in nm and r not in role:
                        role[r] = p.get("number")
            if len(role) < 3 and len(p3) == 3 and all(str(p.get("number")) in ("1", "2", "3") for p in p3):
                role = {"G": "1", "S": "2", "D": "3"}
                report["warnings"].append(f"{des}: MOSFET 按惯序 1=G 2=S 3=D 处理（请核对）")
            if len(role) < 3:
                report["excluded"].append({"designator": des, "title": title, "reason": "MOSFET 引脚无法确定"})
                continue
            ng = net_or(_pin_net(pin_net, des, role["G"]), des)
            nd = net_or(_pin_net(pin_net, des, role["D"]), des)
            ns = net_or(_pin_net(pin_net, des, role["S"]), des)
            if None in (ng, nd, ns):
                continue
            is_pmos = "PMOS" in (title + mpn).upper() or "P沟道" in (title or "")
            g = gen["generic"]["PMOS" if is_pmos else "NMOS"]
            lines.append(f"{sdes} {nd} {ng} {ns} 0 {g['modelName']}")
            model_cards[g["modelName"]] = g["card"]
            report["generic"].append({"designator": des, "model": g["modelName"], "why": "MOSFET 泛化模型"})
            report["included"].append({"designator": des, "as": "M", "model": g["modelName"]})
            continue

        # --- 其余（继电器/排针/端子/IC 等）---
        shown = mpn or title
        if not shown or str(shown).startswith("="):
            shown = "未知型号"
        reason = f"无仿真模型（{shown}），已排除"
        if keep_open:
            lines.append(f"* [skipped] {sdes} {shown}")
        report["excluded"].append({"designator": des, "title": title, "mpn": mpn, "reason": reason})

    # --- Power 网标自动补源 ---
    for net in ir.get("nets", []):
        nm = net.get("name")
        if net.get("origin") != "netflag":
            continue
        v = parse_voltage(nm)
        if v is None:
            continue
        s = sanitize_net(nm, used_nets)
        if s in sourced_nets or s == "0":
            continue
        dev = ("VPWR_NEG_" + s.lstrip("-")) if s.startswith("-") else ("VPWR_" + s.lstrip("+"))
        lines.append(f"{dev} {s} 0 {v:g} DC AC 0 0")
        report["powers"].append({"net": nm, "voltage": v})
        sourced_nets.add(s)

    # --- 悬空网络检测（只有 1 个引脚的网络）---
    for net in ir.get("nets", []):
        if net.get("pinCount") == 1 and net.get("origin") == "auto":
            m = net["members"][0]
            report["dangling_nets"].append(f"{net['name']} 仅连接 {m['component']}.{m['pin']}")

    # --- 组装网表 ---
    header_like = [e for e in report["excluded"]
                   if str(e.get("designator", ""))[:2].upper().startswith(("H", "SW", "JP"))
                   or "PZ2.54" in str(e.get("mpn") or "")
                   or "HEADER" in str(e.get("mpn") or "").upper()]
    if header_like:
        report["warnings"].append(
            f"检测到 {len(header_like)} 个跳线/排针（{', '.join(e['designator'] for e in header_like)}）位于信号路径中，"
            f"网表按『未插跳线』状态生成，放大/滤波链路可能处于断开状态。"
            f"如需仿真闭合状态，请手工以 0Ω 导线替代或在自定义控制块中处理")
    head = ["* NGspice netlist generated by easyeda-ngspice-sim",
            f"* project: {ir.get('project') or '-'}  page: {(ir.get('page') or {}).get('name') or '-'}  source: {ir.get('source')}"]
    if report["excluded"]:
        head.append(f"* WARNING: {len(report['excluded'])} 个无模型器件被排除: " +
                    ", ".join(e["designator"] for e in report["excluded"]))
    if report["generic"]:
        head.append("* WARNING: 使用泛化模型: " +
                    ", ".join(f"{g['designator']}→{g['model']}" for g in report["generic"]))
    text = "\n".join(head) + "\n" + "\n".join(lines)
    if model_cards:
        text += "\n\n* ---- models ----\n"
        for name, card in model_cards.items():
            if card:
                text += card + "\n"
    text += "\n.end\n"
    return text, report


def main():
    if len(sys.argv) < 3:
        print("usage: netlist_gen.py <ir_with_nets.json> <out.cir> [--keep-open]")
        sys.exit(2)
    ir = json.load(open(sys.argv[1], encoding="utf-8"))
    pin_net = {}
    for n in ir.get("nets", []):
        for m in n.get("members", []):
            pin_net.setdefault(m["component"], {})[str(m["pin"])] = n["name"]
    text, report = generate(ir, pin_net, keep_open="--keep-open" in sys.argv)
    open(sys.argv[2], "w", encoding="utf-8", newline="\n").write(text)
    print(f"netlist written: {sys.argv[2]}")
    print(f"  included: {len(report['included'])}, excluded: {len(report['excluded'])}, "
          f"generic: {len(report['generic'])}, power sources: {len(report['powers'])}")
    for e in report["excluded"]:
        print(f"  [excluded] {e['designator']}: {e['reason']}")
    for g in report["generic"]:
        print(f"  [generic ] {g['designator']} -> {g['model']} ({g['why']})")
    for p in report["powers"]:
        print(f"  [power   ] {p['net']} = {p['voltage']:g}V 自动补源")
    for w in report["warnings"]:
        print(f"  [warn    ] {w}")
    if report["dangling_nets"]:
        print("  [dangling] " + "; ".join(report["dangling_nets"]))


if __name__ == "__main__":
    main()
