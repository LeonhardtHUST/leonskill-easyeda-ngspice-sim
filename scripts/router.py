#!/usr/bin/env python3
"""仿真线路图布局布线器：IR + 网络 → 器件坐标 + 导线几何（"竖井"通道布线）。

供两个出口共用：
  - epro2_writeback.py：把 plan 写进工程文件的 SIMULATION 页面
  - js/build_sim_page.js：按 plan 在 EDA 仿真页上放置/布线（经 bridge）

用法: python router.py <ir_with_nets.json> <plan.json>

布局模型（确定性、无短路）：
  - 器件排一行，列距 90；R/C/L/V 竖放（rot 90 / 0），Q 横放（rot 0）
  - 每个引脚有一根"竖井"（专用 x 列）：水平引出到竖井，再竖直到本网络的轨道
      R/C/L: 上脚 x+25，下脚 x-25；Q: 集电极 x+25、基极 x-25、发射极 x+40；源: 上 x+25 下 x-25
    竖井 x 永不与任何引脚 x 重合（引脚 x 均为整列 x 或 x±10），故不会穿过别的引脚
  - 每网络一条水平轨道：电源带顶部（+120 起）、GND 带底部（-120 起）、信号带中间（±40 起，交替上下，间距 12）
  - 网络标志放在轨道左端点上（引脚与轨道端点重合）
"""
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

COL_SPACING = 120
RISER = {"top": 25.0, "bottom": -25.0}
Q_RISER = {"C": 25.0, "B": -25.0, "E": 40.0}

# 仿真符号引脚偏移（EDA 坐标系，符号 rot=0；R/C/L 实际竖放 rot=90）
SIM_PIN_OFFSETS = {
    "R": {"1": (20, 0), "2": (-20, 0)},
    "C": {"1": (20, 0), "2": (-20, 0)},
    "L": {"1": (20, 0), "2": (-20, 0)},
    "QNPN": {"1": (10, 20), "2": (-10, 0), "3": (10, -20)},   # 1=C 2=B 3=E
    "QPNP": {"1": (10, 20), "2": (-10, 0), "3": (10, -20)},
    "VSRC": {"1": (0, -25), "2": (0, 25)},                    # 2=N+
}


def rot_pt(px, py, deg):
    th = math.radians(deg)
    return px * math.cos(th) - py * math.sin(th), px * math.sin(th) + py * math.cos(th)


def pin_pos(comp, offset):
    ox, oy = rot_pt(offset[0], offset[1], comp["rotation"])
    if comp.get("mirror"):
        ox = -ox
    return comp["x"] + ox, comp["y"] + oy


def classify_sim(comp):
    prefix = (comp.get("pinPrefix") or "").upper()
    title = (comp.get("deviceTitle") or "") + " " + (comp.get("manufacturerPart") or "")
    pins = comp.get("pins") or []
    if prefix in ("R", "C", "L") and len(pins) == 2:
        return prefix
    if prefix == "Q" and len(pins) >= 3:
        return "QPNP" if ("PNP" in title.upper() or "9012" in title or "9015" in title
                          or "8550" in title or "3906" in title) else "QNPN"
    if prefix == "V" and len(pins) == 2 and ("电压源" in title or "VOLTAGE" in title.upper()
                                             or "BATTERY" in title.upper()):
        return "VSRC"
    return None


def is_gnd(net):
    s = str(net)
    return s in ("0",) or s.upper().startswith("GND") or s.upper() in ("VSS", "AGND", "DGND")


def is_power(net):
    s = str(net)
    return bool(re.match(r"^[+\-]?\d+(?:\.\d+)?V\d*$", s)) or s.upper() in ("VCC", "VDD", "VEE")


def parse_voltage(net):
    m = re.match(r"^[+\-]?(\d+)(?:V(\d+))?V?$", str(net))
    if not m:
        return None
    v = float(f"{m.group(1)}.{m.group(2)}") if m.group(2) else float(m.group(1))
    return -v if str(net).startswith("-") else v


def build_plan(ir, pin_net):
    comps = [c for c in ir.get("components", []) if c.get("componentType") == "part"]
    plan = {"components": [], "wires": [], "netflags": [], "skipped": [],
            "supplies": [], "meta": {"page": (ir.get("page") or {}).get("name")}}

    # 1) 选器件 + 排布
    chosen = []
    col = 0
    for c in comps:
        des = c.get("designator")
        if not des:
            continue
        sim_kind = classify_sim(c)
        if not sim_kind:
            plan["skipped"].append({"designator": des, "reason": "非 R/C/L/Q/电压源，无法映射仿真符号"})
            continue
        rot = 90 if sim_kind in ("R", "C", "L") else 0
        chosen.append({
            "designator": des, "simKind": sim_kind,
            "x": col * COL_SPACING, "y": 0, "rotation": rot, "mirror": False,
            "value": c.get("value"),
            "netPins": pin_net.get(des, {}),
            "offsets": SIM_PIN_OFFSETS[sim_kind],
        })
        col += 1
    if not chosen:
        plan["error"] = "没有可仿真的器件"
        return plan

    # 2) 引脚绝对坐标 + 竖井 x
    for c in chosen:
        c["pins"] = {}
        c["risers"] = {}
        for num, off in c["offsets"].items():
            px, py = pin_pos(c, off)
            c["pins"][num] = (px, py)
            if c["simKind"] in ("QNPN", "QPNP"):
                role = {"1": "C", "2": "B", "3": "E"}[num]
                c["risers"][num] = (px + Q_RISER[role], py)
            else:
                c["risers"][num] = (px + (RISER["top"] if py >= 0 else RISER["bottom"]), py)

    # 3) 网络 → 成员
    net_members = {}
    for c in chosen:
        for num in c["pins"]:
            net = c["netPins"].get(str(num))
            if net is None:
                continue
            net_members.setdefault(net, []).append({
                "des": c["designator"], "pin": num,
                "px": c["pins"][num][0], "py": c["pins"][num][1],
                "rx": c["risers"][num][0], "ry": c["risers"][num][1],
            })

    # 4) 轨道分配
    gnd_nets = sorted(n for n in net_members if is_gnd(n))
    pwr_nets = sorted(n for n in net_members if is_power(n) and not is_gnd(n))
    sig_nets = sorted(n for n in net_members if n not in gnd_nets and n not in pwr_nets)
    tracks = {}
    y = 140.0
    for n in pwr_nets:
        tracks[n] = y; y += 18
    y = -140.0
    for n in gnd_nets:
        tracks[n] = y; y -= 18
    # 信号：引脚多者靠近器件行，交替上下；轨道带自适应压缩，
    # 保证不侵入电源带（pwr 最低轨上方 -18）与 GND 带（gnd 最高轨下方 +18）
    sig_nets.sort(key=lambda n: -len(net_members[n]))
    pwr_low = min((tracks[n] for n in pwr_nets), default=1000.0)
    gnd_high = max((tracks[n] for n in gnd_nets), default=-1000.0)
    top_limit = pwr_low - 18.0
    bot_limit = gnd_high + 18.0
    n_up = (len(sig_nets) + 1) // 2
    n_down = len(sig_nets) // 2
    up_step = (top_limit - 50.0) / n_up if n_up else 0.0
    down_step = (bot_limit + 50.0) / n_down if n_down else 0.0
    for i, n in enumerate(sig_nets):
        if i % 2 == 0:
            tracks[n] = 50.0 + up_step * (i // 2)
        else:
            tracks[n] = -50.0 + down_step * (i // 2)

    # 5) 导线：每网络 = 每个引脚的 (横引 + 竖井 + 轨道横线)
    net_segs = {n: [] for n in net_members}
    for net, ms in net_members.items():
        ty = tracks[net]
        segs = net_segs[net]
        for m in ms:
            if abs(m["px"] - m["rx"]) > 1e-6:
                segs.append([m["px"], m["py"], m["rx"], m["py"]])
            if abs(m["py"] - ty) > 1e-6:
                segs.append([m["rx"], m["py"], m["rx"], ty])
        xs = sorted(m["rx"] for m in ms)
        if len(xs) >= 2 and abs(xs[-1] - xs[0]) > 1e-6:
            segs.append([xs[0], ty, xs[-1], ty])

    # 6) 网标：轨道向左延伸到 x=-40 的网标引脚
    for n in pwr_nets:
        ty = tracks[n]
        xs = sorted(m["rx"] for m in net_members[n])
        if xs[0] > -40.0:
            net_segs[n].append([-40.0, ty, xs[0], ty])
        plan["netflags"].append({"net": n, "flagType": "Power", "x": -40.0, "y": ty})
    for n in gnd_nets:
        ty = tracks[n]
        xs = sorted(m["rx"] for m in net_members[n])
        if xs[0] > -40.0:
            net_segs[n].append([-40.0, ty, xs[0], ty])
        plan["netflags"].append({"net": "GND", "flagType": "Ground", "x": -40.0, "y": ty})

    # 7) 补源：每个电源网一个直流源，放在器件行下方独立列
    #    源引脚（实测）：pin2(+)=+30, pin1(-)=-20；+ 接电源轨道，- 接 GND 轨道
    supplies = []
    for k, n in enumerate(pwr_nets):
        v = parse_voltage(n)
        # 安全列：器件竖井列为 COL_SPACING*k±25 与 +40；电源用 +55 / +45，永不与任何 riser 重合
        xs = 55.0 + k * COL_SPACING
        y0 = -200.0 - k * 140.0
        supplies.append({"net": n, "voltage": v, "x": xs, "y": y0})
        if v is None or v <= 0:
            plan["supplies"].append({"net": n, "voltage": v, "skipped": "负压/无法解析，未自动加源"})
            continue
        pwr_y = tracks[n]
        gnd_y = tracks[gnd_nets[0]] if gnd_nets else -120.0
        # pin1(N+) 实测在 y+30，pin2(N-) 在 y-20；两脚各用独立列（相隔 20），避免同列重叠短路
        net_segs[n].append([xs, y0 + 30.0, xs + 10.0, y0 + 30.0])
        net_segs[n].append([xs + 10.0, y0 + 30.0, xs + 10.0, pwr_y])
        net_segs[gnd_nets[0] if gnd_nets else n].append([xs, y0 - 20.0, xs - 10.0, y0 - 20.0])
        net_segs[gnd_nets[0] if gnd_nets else n].append([xs - 10.0, y0 - 20.0, xs - 10.0, gnd_y])
        plan["supplies"].append({"net": n, "voltage": v, "x": xs, "y": y0})
    plan["supply_components"] = supplies

    for net, segs in net_segs.items():
        segs = [s for s in segs if abs(s[0] - s[2]) > 1e-6 or abs(s[1] - s[3]) > 1e-6]
        if segs:
            plan["wires"].append({"net": net, "segments": segs,
                                  "trackY": tracks[net], "xmin": min(s[0] for s in segs)})

    plan["components"] = [{k: c[k] for k in ("designator", "simKind", "x", "y", "rotation", "value")}
                          for c in chosen]
    return plan


def main():
    if len(sys.argv) < 3:
        print("usage: router.py <ir_with_nets.json> <plan.json>")
        sys.exit(2)
    ir = json.load(open(sys.argv[1], encoding="utf-8"))
    pin_net = {}
    for n in ir.get("nets", []):
        for m in n.get("members", []):
            pin_net.setdefault(m["component"], {})[str(m["pin"])] = n["name"]
    plan = build_plan(ir, pin_net)
    json.dump(plan, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"plan written: {sys.argv[2]}")
    print(f"  components: {len(plan['components'])}  nets: {len(plan['wires'])}  "
          f"netflags: {len(plan['netflags'])}  supplies: {len(plan['supplies'])}  skipped: {len(plan['skipped'])}")


if __name__ == "__main__":
    main()
