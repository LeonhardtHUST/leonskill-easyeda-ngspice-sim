#!/usr/bin/env python3
"""SVG 仿真线路图生成器 v2：IR+网络 → 可读性优先的仿真线路图 SVG。

用法: python svg_schematic.py <ir_with_nets.json> <out.svg> [--plan]

两种模式：
  默认   —— 按原理图原始坐标绘制（紧凑、与 EDA 原图布局一致）：符号随元件旋转、
             引脚引线补齐、网标符号、标签自动避让
  --plan —— 布线器自动布局（器件单行拉开、轨道分层），与写回工程文件的仿真页一致；
             含网标符号、悬空引脚标记、排除器件图例（图较宽，适合单独看）

v1 渲染缺陷（本次修复）：
  - 符号不随元件旋转（竖直引脚的电容画成横向极板 → 看似悬空）
  - 符号体固定尺寸与实际引脚位置脱节（引脚引线缺失）
  - 网络标志未绘制（导线到标志处悬空）
  - 标签固定偏移 + 原理图坐标拥挤 → 文字重叠
"""
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from router import build_plan, SIM_PIN_OFFSETS, pin_pos  # noqa: E402

SIG_BLUE = "#2b6cb0"
PWR_RED = "#c0392b"
GND_BLACK = "#222222"
GRAY = "#9a9a9a"
SCALE = 1.6
FONT = "Consolas, monospace"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def txt(x, y, s, size=12, color="#222", anchor="middle", weight="normal"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}"'
            f' fill="{color}" text-anchor="{anchor}" font-weight="{weight}">{esc(s)}</text>')


def line(x1, y1, x2, y2, color="#333", w=1.4):
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"'
            f' stroke="{color}" stroke-width="{w}"/>')


def net_color(net):
    s = str(net)
    if s == "0" or s.upper().startswith("GND"):
        return GND_BLACK
    if re.match(r"^[+\-]?\d+(\.\d+)?V\d*$", s) or s.upper() in ("VCC", "VDD", "VEE", "VSS"):
        return PWR_RED
    return SIG_BLUE


def symbol_r(x, y, vertical=True):
    if vertical:
        return (f'<rect x="{x-7:.1f}" y="{y-14:.1f}" width="14" height="28" fill="none"'
                f' stroke="#222" stroke-width="1.5"/>')
    return (f'<rect x="{x-14:.1f}" y="{y-7:.1f}" width="28" height="14" fill="none"'
            f' stroke="#222" stroke-width="1.5"/>')


def symbol_c(x, y, vertical=True):
    if vertical:
        return (f'<line x1="{x-12:.1f}" y1="{y-5:.1f}" x2="{x+12:.1f}" y2="{y-5:.1f}" stroke="#222" stroke-width="2"/>'
                f'<line x1="{x-12:.1f}" y1="{y+5:.1f}" x2="{x+12:.1f}" y2="{y+5:.1f}" stroke="#222" stroke-width="2"/>')
    return (f'<line x1="{x-5:.1f}" y1="{y-12:.1f}" x2="{x-5:.1f}" y2="{y+12:.1f}" stroke="#222" stroke-width="2"/>'
            f'<line x1="{x+5:.1f}" y1="{y-12:.1f}" x2="{x+5:.1f}" y2="{y+12:.1f}" stroke="#222" stroke-width="2"/>')


def symbol_q(x, y):
    return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="16" fill="none" stroke="#222" stroke-width="1.5"/>'
            f'<line x1="{x-16:.1f}" y1="{y-8:.1f}" x2="{x-16:.1f}" y2="{y+8:.1f}" stroke="#222" stroke-width="2.4"/>')


def symbol_v(x, y, label="V"):
    return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="14" fill="none" stroke="#222" stroke-width="1.5"/>'
            + txt(x, y + 4, label, 11, "#444"))


def symbol_box(x, y, w=44, h=26):
    return (f'<rect x="{x-w/2:.1f}" y="{y-h/2:.1f}" width="{w}" height="{h}" rx="3" fill="#f7f7f7"'
            f' stroke="{GRAY}" stroke-width="1.2" stroke-dasharray="4 3"/>')


def netflag_ground(x, y, dirx=0, diry=-1):
    """GND 符号。dir = 引脚指向导线的方向；符号画在反方向（避免压线）。"""
    bx, by = -dirx, -diry
    g = (f'<g stroke="{GND_BLACK}" stroke-width="2">'
         f'<line x1="{x-12:.1f}" y1="{y+4:.1f}" x2="{x+12:.1f}" y2="{y+4:.1f}"/>'
         f'<line x1="{x-8:.1f}" y1="{y+9:.1f}" x2="{x+8:.1f}" y2="{y+9:.1f}"/>'
         f'<line x1="{x-4:.1f}" y1="{y+14:.1f}" x2="{x+4:.1f}" y2="{y+14:.1f}"/></g>'
         + line(x, y, x, y + 4, GND_BLACK, 2))
    if abs(bx) > abs(by):
        # 水平方向：符号平移到反侧，避免压线
        ox = 26 if bx > 0 else -26
        return (f'<g transform="translate({ox:.1f},0)">{g}</g>'
                + line(x, y, x + ox, y, GND_BLACK, 2))
    if by < 0:
        # 导线在下方 → 符号应在上方（当前默认朝下画会压线）
        return (f'<g transform="translate(0,{-18:.1f}) scale(1,-1)">{g}</g>'
                + line(x, y, x, y - 4, GND_BLACK, 2))
    return g


def netflag_power(x, y, net, dirx=0, diry=1):
    """电源标志：横杠 + 网名。dir = 引脚指向导线的方向；符号画在反方向，文字保持正立。"""
    bx, by = -dirx, -diry
    if abs(by) >= abs(bx):
        bar_y = y - 4 if by < 0 else y + 4
        stub = line(x, y, x, bar_y, PWR_RED, 2)
        bar = line(x - 14, bar_y, x + 14, bar_y, PWR_RED, 2.6)
        ty = bar_y - 6 if by < 0 else bar_y + 16
        return stub + bar + txt(x, ty, net, 12, PWR_RED, weight="bold")
    # 水平方向：竖杠 + 侧向文字
    bar_x = x - 4 if bx < 0 else x + 4
    stub = line(x, y, bar_x, y, PWR_RED, 2)
    bar = line(bar_x, y - 14, bar_x, y + 14, PWR_RED, 2.6)
    if bx < 0:
        return stub + bar + txt(x - 12, y + 4, net, 12, PWR_RED, "end", "bold")
    return stub + bar + txt(x + 12, y + 4, net, 12, PWR_RED, "start", "bold")


def leads(comp_pins, cx, cy, body=14.0, fx=None, fy=None):
    """从符号体边缘(body 半径处)向每个引脚画引线——整段覆盖 body→pin，无空隙。

    输入为 EDA 坐标；fx/fy 为 SVG 变换函数（X()/Y()），None 则直接使用原值。
    引脚在符号体内(d <= body)的（如灰盒排针）不画引线。
    """
    fx = fx or (lambda v: v)
    fy = fy or (lambda v: v)
    out = []
    for (px, py) in comp_pins:
        d = math.hypot(px - cx, py - cy)
        if d <= body:
            continue
        t = body / d
        out.append(line(fx(cx + (px - cx) * t), fy(cy + (py - cy) * t),
                        fx(px), fy(py), "#222", 1.5))
    return "".join(out)


def _sim_pins(c):
    offs = SIM_PIN_OFFSETS[c["simKind"]]
    holder = {"x": c["x"], "y": c["y"], "rotation": c["rotation"], "mirror": False}
    return [pin_pos(holder, o) for o in offs.values()]


# ---------------------------------------------------------------- 默认：plan 布局
def render_plan(ir, out_path):
    pin_net = {}
    for n in ir.get("nets", []):
        for m in n.get("members", []):
            pin_net.setdefault(m["component"], {})[str(m["pin"])] = n["name"]
    plan = build_plan(ir, pin_net)

    if plan.get("error"):
        open(out_path, "w", encoding="utf-8").write(
            f"<svg xmlns='http://www.w3.org/2000/svg' width='300' height='80'>"
            f"<text x='10' y='30' font-size='14'>{esc(plan['error'])}</text></svg>")
        return plan

    comps = plan["components"]
    xs, ys = [-40.0], []
    for c in comps:
        xs.append(c["x"]); ys.append(c["y"])
    for f in plan["netflags"]:
        xs.append(f["x"]); ys.append(f["y"])
    for w in plan["wires"]:
        for s in w.get("segments", []):
            xs += [s[0], s[2]]; ys += [s[1], s[3]]
    for sup in plan.get("supply_components", []):
        xs.append(sup["x"]); ys += [sup["y"] - 40, sup["y"] + 40]
    n_skip_rows = max(1, (len(plan["skipped"]) + 7) // 8)
    minx, maxx = min(xs) - 70, max(xs) + 70
    miny, maxy = min(ys) - 50, max(ys) + 50 + 28 * n_skip_rows

    def X(v):
        return (v - minx) * SCALE

    def Y(v):
        return (maxy - v) * SCALE

    W, H = (maxx - minx) * SCALE, (maxy - miny) * SCALE
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" '
           f'width="{W:.0f}" height="{H:.0f}" role="img">',
           f'<title>仿真线路图 {esc((ir.get("page") or {}).get("name") or "")}</title>',
           f'<desc>easyeda-ngspice-sim 自动布局仿真线路图：{len(comps)} 个可仿真器件、'
           f'{len(plan["wires"])} 个网络、{len(plan["skipped"])} 个器件因无模型未包含</desc>',
           f'<rect width="{W:.0f}" height="{H:.0f}" fill="#ffffff"/>',
           txt(W / 2, 26, f"仿真线路图 · {(ir.get('page') or {}).get('name') or ''}"
                          f" · {ir.get('project') or ''}", 16, "#333", weight="bold")]

    # 悬空统计（先于导线绘制：单成员网络不画导线，只画悬空标记）
    member_count = {}
    pin_owner = {}
    for c in comps:
        offs = SIM_PIN_OFFSETS[c["simKind"]]
        holder = {"x": c["x"], "y": c["y"], "rotation": c["rotation"], "mirror": False}
        for num, o in offs.items():
            for n in ir.get("nets", []):
                if any(m["component"] == c["designator"] and str(m["pin"]) == num
                       for m in n["members"]):
                    member_count[n["name"]] = member_count.get(n["name"], 0) + 1
                    pin_owner[(c["designator"], num)] = (n["name"], pin_pos(holder, o))

    # 导线（单成员网络跳过——那是悬空引脚，不画半空竖井）
    for w in plan["wires"]:
        if member_count.get(w.get("net"), 99) <= 1:
            continue
        c = net_color(w["net"])
        width = 2.6 if c == GND_BLACK else (2.2 if c == PWR_RED else 1.6)
        for s in w["segments"]:
            if abs(s[0] - s[2]) < 1e-6 and abs(s[1] - s[3]) < 1e-6:
                continue
            out.append(line(X(s[0]), Y(s[1]), X(s[2]), Y(s[3]), c, width))

    # 器件
    for c in comps:
        x, y = X(c["x"]), Y(c["y"])
        pts = _sim_pins(c)
        if c["simKind"] == "R":
            out.append(symbol_r(x, y, vertical=True))
            out.append(line(x, y - 14, x, y - 20, "#222", 1.5))
            out.append(line(x, y + 14, x, y + 20, "#222", 1.5))
        elif c["simKind"] in ("C", "L"):
            out.append(symbol_c(x, y, vertical=True))
            out.append(line(x, y - 5, x, y - 20, "#222", 1.5))
            out.append(line(x, y + 5, x, y + 20, "#222", 1.5))
        elif c["simKind"] in ("QNPN", "QPNP"):
            out.append(symbol_q(x, y))
            out.append(leads(pts, c["x"], c["y"], body=13, fx=X, fy=Y))
            out.append(line(X(c["x"] - 16), Y(c["y"]), X(c["x"] - 10), Y(c["y"]), "#222", 1.5))
        else:
            out.append(symbol_v(x, y))
        for (px, py) in pts:
            out.append(f'<circle cx="{X(px):.1f}" cy="{Y(py):.1f}" r="2.4" fill="#222"/>')
        label = c["designator"] + (f'  {c["value"]}' if c.get("value") else "")
        out.append(txt(x, y - 44, label, 12.5, "#111", weight="bold"))

    # 补源
    for sup in plan.get("supply_components", []):
        v = sup.get("voltage")
        if v is None or v <= 0:
            continue
        x, y = X(sup["x"]), Y(sup["y"])
        out.append(symbol_v(x, y, f"{v:g}V"))
        for py in (sup["y"] + 30, sup["y"] - 20):
            out.append(f'<circle cx="{X(sup["x"]):.1f}" cy="{Y(py):.1f}" r="2.4" fill="#222"/>')

    # 网标
    for f in plan["netflags"]:
        x, y = X(f["x"]), Y(f["y"])
        if f["flagType"] == "Ground":
            out.append(netflag_ground(x, y))
        else:
            out.append(netflag_power(x, y, f["net"]))

    # 网络标签（取每网最长的水平轨道，标在中点上方）
    for w in plan["wires"]:
        segs = [s for s in w["segments"]
                if abs(s[0] - s[2]) > 1e-6 or abs(s[1] - s[3]) > 1e-6]
        if not segs or not w.get("net"):
            continue
        hor = [s for s in segs if abs(s[1] - s[3]) < 1e-6 and abs(s[0] - s[2]) > 40]
        c = net_color(w["net"])
        if hor:
            s = max(hor, key=lambda sg: abs(sg[2] - sg[0]))
            out.append(txt(X((s[0] + s[2]) / 2), Y(s[1]) - 7, w["net"], 11.5, c, weight="bold"))

    # 悬空引脚标记：所在网络只有这一个引脚 → 引脚处画空心圈
    for (des, num), (net, (px, py)) in pin_owner.items():
        if member_count.get(net, 0) > 1:
            continue
        out.append(f'<circle cx="{X(px):.1f}" cy="{Y(py):.1f}" r="4.5" fill="none"'
                   f' stroke="#e0a030" stroke-width="2"/>')
        out.append(txt(X(px) + 8, Y(py) + 4, "悬空", 10, "#e0a030", "start"))

    # 排除器件图例
    if plan["skipped"]:
        ly = H - 30 * n_skip_rows
        out.append(txt(24, ly, "未包含（无仿真模型，已从仿真中排除）:", 11.5, "#777", "start", "bold"))
        names = [s["designator"] for s in plan["skipped"]]
        for r in range(n_skip_rows):
            chunk = ", ".join(names[r * 8:(r + 1) * 8])
            if chunk:
                out.append(txt(24, ly + 18 + r * 16, chunk, 11.5, "#999", "start"))

    out.append("</svg>")
    open(out_path, "w", encoding="utf-8", newline="\n").write("\n".join(out))
    return plan


# ---------------------------------------------------------------- mirror 模式
def render_mirror(ir, out_path):
    parts = [c for c in ir.get("components", []) if c.get("componentType") == "part"]
    flags = [c for c in ir.get("components", []) if c.get("componentType") == "netflag"]
    wires = ir.get("wires", [])

    xs, ys = [0.0], [0.0]
    for c in ir.get("components", []):
        xs.append(float(c.get("x") or 0)); ys.append(float(c.get("y") or 0))
        for p in c.get("pins") or []:
            xs.append(float(p.get("x") or 0)); ys.append(float(p.get("y") or 0))
    for w in wires:
        ln = w.get("line") or []
        xs.extend(ln[0::2]); ys.extend(ln[1::2])
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    pad = 80
    W = (maxx - minx + 2 * pad) * SCALE
    H = (maxy - miny + 2 * pad) * SCALE

    def X(v):
        return (float(v) - minx + pad) * SCALE

    def Y(v):
        return (maxy - float(v) + pad) * SCALE

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" '
           f'width="{W:.0f}" height="{H:.0f}" role="img">',
           f'<title>仿真线路图（原理图坐标镜像）</title>',
           f'<rect width="{W:.0f}" height="{H:.0f}" fill="#ffffff"/>']

    for w in wires:
        ln = w.get("line") or []
        pts = [(X(ln[i]), Y(ln[i + 1])) for i in range(0, len(ln) - 1, 2)]
        if len(pts) < 2:
            continue
        c = net_color(w.get("net"))
        width = 2.6 if c == GND_BLACK else (2.2 if c == PWR_RED else 1.5)
        d = " ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in pts)
        out.append(f'<polyline points="{d}" fill="none" stroke="{c}" stroke-width="{width}"/>')

    def flag_dir(f):
        """网标引脚处导线的走向（EDA 坐标），符号画在其反方向。"""
        px, py = float(f.get("x") or 0), float(f.get("y") or 0)
        for w in wires:
            ln = w.get("line") or []
            for i in range(0, len(ln) - 3, 2):
                a, b = (ln[i], ln[i + 1]), (ln[i + 2], ln[i + 3])
                for p, q in ((a, b), (b, a)):
                    if abs(p[0] - px) < 0.05 and abs(p[1] - py) < 0.05:
                        vx, vy = q[0] - p[0], q[1] - p[1]
                        if abs(vx) > abs(vy):
                            return (1 if vx > 0 else -1, 0)
                        return (0, 1 if vy > 0 else -1)
        return (0, -1)

    # ---- 障碍物：导线段（缓冲 4px）、器件符号 bbox、已放标签 ----
    wire_rects = []
    for w in wires:
        ln = w.get("line") or []
        for i in range(0, len(ln) - 3, 2):
            ax, ay, bx, by = X(ln[i]), Y(ln[i + 1]), X(ln[i + 2]), Y(ln[i + 3])
            wire_rects.append((min(ax, bx) - 3, min(ay, by) - 4, max(ax, bx) + 3, max(ay, by) + 4))

    def rect_overlap(a, b):
        return (max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
                * max(0.0, min(a[3], b[3]) - max(a[1], b[1])))

    placed_labels = []   # [(x0,y0,x1,y1)]

    def text_bbox(cx, cy, text, size, anchor):
        w = len(text) * size * 0.62
        if anchor == "middle":
            x0 = cx - w / 2
        elif anchor == "start":
            x0 = cx
        else:
            x0 = cx - w
        return (x0, cy - size * 0.85, x0 + w, cy + size * 0.25)

    def place_label(px, py, text, obstacles_extra=()):
        """在器件四周按统一距离(34px)选重叠最小的标签位。"""
        D = 34.0
        cands = [(D, 4, "start"), (-D, 4, "end"), (0, -D, "middle"), (0, D + 8, "middle"),
                 (D, -22, "start"), (-D, -22, "end"),
                 (0, -D - 16, "middle"), (0, D + 24, "middle")]
        best = None
        for off in cands:
            bx = text_bbox(px + off[0], py + off[1], text, 12, off[2])
            sc = 0.0
            for r in wire_rects:
                sc += rect_overlap(bx, r)
            for r in symbol_rects:
                sc += rect_overlap(bx, r) * 2.5
            for r in placed_labels:
                sc += rect_overlap(bx, r) * 4.0
            for r in obstacles_extra:
                sc += rect_overlap(bx, r) * 4.0
            if best is None or sc < best[0] - 1e-9:
                best = (sc, off, bx)
        placed_labels.append(best[2])
        return best[1]

    symbol_rects = []

    def part_bbox(px, py, pre, pts):
        if pre in ("R", "C", "V", "Q", "L"):
            return (px - 26, py - 26, px + 26, py + 26)
        xs2 = [p[0] for p in pts] or [px]
        ys2 = [p[1] for p in pts] or [py]
        bw = max(44, (max(xs2) - min(xs2)) + 18) / 2
        bh = max(26, (max(ys2) - min(ys2)) + 18) / 2
        return (px - bw - 4, py - bh - 4, px + bw + 4, py + bh + 4)

    # 器件标签延后统一放置（需要先收集全部符号 bbox）
    label_jobs = []
    for c in parts:
        px, py = X(c.get("x")), Y(c.get("y"))
        pins = c.get("pins") or []
        pts = [(X(p.get("x")), Y(p.get("y"))) for p in pins]
        pre = (c.get("pinPrefix") or "")[:1].upper()
        n_pins = len(pins)
        if pre in ("R", "C", "L") and n_pins == 2:
            vertical = abs(pts[0][0] - pts[1][0]) < abs(pts[0][1] - pts[1][1])
            out.append(symbol_r(px, py, vertical) if pre == "R" else symbol_c(px, py, vertical))
        elif pre == "Q" and n_pins >= 3:
            out.append(symbol_q(px, py))
        elif pre == "V" and n_pins == 2:
            out.append(symbol_v(px, py))
        else:
            xs2 = [p[0] for p in pts] or [px]
            ys2 = [p[1] for p in pts] or [py]
            bw = max(44, (max(xs2) - min(xs2)) + 18)
            bh = max(26, (max(ys2) - min(ys2)) + 18)
            out.append(symbol_box(px, py, bw, bh))
        body = 14 if pre == "R" else (5 if pre == "C" else (14 if pre == "V" else 15))
        out.append(leads([(float(p.get("x")), float(p.get("y"))) for p in pins],
                         float(c.get("x")), float(c.get("y")),
                         body=body, fx=X, fy=Y))
        for p in pts:
            out.append(f'<circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="2.4" fill="#222"/>')
        symbol_rects.append(part_bbox(px, py, pre, pts))
        label = c.get("designator") or ""
        if c.get("value"):
            label += " " + str(c.get("value"))
        if label:
            label_jobs.append((px, py, label))

    # 网标：方向自适应（符号在导线反侧，文字正立）
    for f in flags:
        px, py = X(f.get("x")), Y(f.get("y"))
        nm = f.get("net") or f.get("name")
        dx, dy = flag_dir(f)
        # Y 轴翻转：EDA 方向 → SVG 方向
        sdx, sdy = dx, -dy
        if str(nm).upper().startswith("GND") or nm == "0":
            out.append(netflag_ground(px, py, sdx, sdy))
        else:
            out.append(netflag_power(px, py, nm, sdx, sdy))
            placed_labels.append(text_bbox(px, py - 6, str(nm), 12, "middle"))

    # 统一放置器件标签（与走线/符号/标签重叠打分避让）
    for (px, py, label) in label_jobs:
        off = place_label(px, py, label)
        out.append(txt(px + off[0], py + off[1], label, 12, "#111", off[2], "bold"))

    out.append(txt(16, 22, "（原理图坐标镜像模式，含无模型器件）", 11, "#aaa", "start"))
    out.append("</svg>")
    open(out_path, "w", encoding="utf-8", newline="\n").write("\n".join(out))


def main():
    if len(sys.argv) < 3:
        print("usage: svg_schematic.py <ir_with_nets.json> <out.svg> [--mirror]")
        sys.exit(2)
    ir = json.load(open(sys.argv[1], encoding="utf-8"))
    if "--plan" in sys.argv:
        plan = render_plan(ir, sys.argv[2])
        print(f"svg written (plan layout): {sys.argv[2]}  "
              f"components={len(plan.get('components', []))} skipped={len(plan.get('skipped', []))}")
    else:
        render_mirror(ir, sys.argv[2])
        print(f"svg written (schematic coords): {sys.argv[2]}")


if __name__ == "__main__":
    main()
