#!/usr/bin/env python3
"""v2.5 P0-B：openEMS bridge（**EXPERIMENTAL**，只生成几何）。

定位（需求文档 §26–§30）：**只生成 openEMS/CSXCAD 工程脚本**，不链接、不分发、
不修改 openEMS（GPL-3.0，见 REFERENCE_PROJECTS.md）。用户用自己的 openEMS 跑完，
得到 .sNp，再回到 scikit-rf → vector fitting → ngspice 宏模型。

刻意不做：全板 3D、自动网格加密、FEM/FDTD 自研求解器。

ROI crop 的思路参考 gerber2ems（Apache-2.0）：整板 full-wave 不可行，必须裁一小块。
"""
import json
import math
import os

STATUS = "EXPERIMENTAL"
FIDELITY = "EM_CROPPED_COUPON"


class EmBridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def plan_region(geo, nets, freq_max_hz, margin_mm=None, fringing=True):
    """决定 EM coupon 的裁剪区域与网格上限。"""
    if not nets:
        raise EmBridgeError("EM_REGION_INVALID", "必须指定要仿真的网络（--net）")
    xs, ys = [], []
    used = []
    for nm in nets:
        net = (geo.get("nets") or {}).get(nm)
        if not net:
            raise EmBridgeError("EM_NET_NOT_FOUND", f"网络 {nm} 不在几何里")
        used.append(nm)
        for t in (net.get("terminals") or {}).values():
            xs.append(t["x_mm"]); ys.append(t["y_mm"])
        for v in net.get("vias") or []:
            xs.append(v["x_mm"]); ys.append(v["y_mm"])
    if not xs:
        raise EmBridgeError("EM_REGION_INVALID", f"{used} 没有可用的端点/过孔坐标")
    if margin_mm is None:
        # 5% 空间余量 + 至少 3 mm（FDTD 边界要留 PML 空间）
        w = max(max(xs) - min(xs), 1.0)
        margin_mm = max(3.0, 0.05 * w)
    lam_mm = 299.792458 / (freq_max_hz / 1e9) if freq_max_hz else 100.0
    # 每个波长至少 20 个网格（常用经验值），上限 0.2 mm 避免网格爆炸
    cell_mm = min(lam_mm / 20.0, 0.2)
    return {
        "nets": used,
        "bbox_mm": {"x": [min(xs) - margin_mm, max(xs) + margin_mm],
                    "y": [min(ys) - margin_mm, max(ys) + margin_mm]},
        "margin_mm": round(margin_mm, 3),
        "f_max_hz": float(freq_max_hz),
        "lambda_mm": round(lam_mm, 3),
        "cell_mm": round(max(cell_mm, 0.02), 4),
        "cells_estimate": {
            "x": int(math.ceil((max(xs) - min(xs) + 2 * margin_mm) / max(cell_mm, 0.02))),
            "y": int(math.ceil((max(ys) - min(ys) + 2 * margin_mm) / max(cell_mm, 0.02))),
        },
        "status": STATUS,
    }


def segment_polygon(a, b, width_mm):
    """中心线 + 宽度 → 铜走线多边形（4 个角点）。

    v2.4 直接用 AddBox(x1,y1→x2,y2) 是错的：斜线会变成轴对齐大矩形、水平/垂直线可能退化。
    这里沿法向 ±w/2 偏移中心线，得到真正的旋转矩形。
    """
    ax, ay = float(a["x_mm"]), float(a["y_mm"])
    bx, by = float(b["x_mm"]), float(b["y_mm"])
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    w = float(width_mm) if isinstance(width_mm, (int, float)) else 0.2
    w = max(w, 0.02)
    if length < 1e-9:
        raise EmBridgeError("EM_SEGMENT_DEGENERATE",
                            f"零长度走线（{ax},{ay}），无法生成多边形")
    nx, ny = -dy / length * (w / 2.0), dx / length * (w / 2.0)
    return [[ax + nx, ay + ny], [bx + nx, by + ny],
            [bx - nx, by - ny], [ax - nx, ay - ny]]


def polygon_area(poly):
    """鞋带公式（用于自检：面积应 ≈ 长度 × 线宽）。"""
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def layer_stack(stackup, board_z0_mm=0.0):
    """叠层 → 每层铜的 z 区间 + 每层介质的 z 区间（多层，不压成单 slab）。"""
    layers = (stackup or {}).get("layers") or []
    diels = (stackup or {}).get("dielectrics") or []
    if not layers or not diels:
        raise EmBridgeError("STACKUP_INCOMPLETE",
                            "EM 需要完整叠层（layers + dielectrics）才能摆铜层和介质")
    # 按 layers 顺序，逐层累加"下方介质厚度"
    cu = []
    diel_ranges = []
    z = float(board_z0_mm)
    for i, l in enumerate(layers):
        t = float(l.get("copper_um") or 35) / 1000.0
        cu.append({"name": l["name"], "z_bottom_mm": z, "z_top_mm": z + t,
                   "copper_um": l.get("copper_um")})
        z += t
        if i < len(diels):
            d = diels[i]
            h = float(d.get("thickness_mm") or 0)
            if h <= 0:
                raise EmBridgeError("STACKUP_INCOMPLETE",
                                    f"介质 {i} 厚度为 0，无法生成 EM 几何")
            diel_ranges.append({"between": d.get("between") or [l["name"], "?"],
                                "z_bottom_mm": z, "z_top_mm": z + h,
                                "er": d.get("er"), "loss_tangent": d.get("loss_tangent")})
            z += h
    return {"copper": cu, "dielectrics": diel_ranges,
            "total_thickness_mm": z - float(board_z0_mm)}


GROUNDISH = ("gnd", "agnd", "dgnd", "pgnd", "gnd_can", "earth", "chassis", "vss",
             "0v", "ground")


def is_groundish(name):
    n = str(name or "").strip().lower().replace("-", "_")
    if not n:
        return False
    base = n.split("_")[0] if n.split("_")[0] in ("agnd", "dgnd", "pgnd", "chassis") else n
    return n in GROUNDISH or base in GROUNDISH or n.startswith("gnd")


def nets_on_layer(geo, layer_name):
    out = []
    for nm, net in (geo.get("nets") or {}).items():
        if not net:
            continue
        for sg in net.get("segments") or []:
            if sg.get("layer") == layer_name:
                out.append(nm)
                break
    return out


def select_reference(geo, nets, stack, ref_net=None):
    """确定这些走线的**真实参考导体**（层 + 网络），v2.5.1。

    v2.5 直接取"相邻下一片铜层"，既不检查那层是不是地，也不检查它存不存在
    几何里 —— 端口可能连到一个根本没建的假参考面。

    规则：
      1. 走线层的相邻层（先下后上）；
      2. 该层上必须有 groundish 网络（或用户 `--reference-net` 指定的网络）；
      3. 找不到 → `EM_REFERENCE_PLANE_UNRESOLVED`。
    """
    cu = stack["copper"]
    idx = {c["name"]: i for i, c in enumerate(cu)}
    trace_layers = []
    for nm in nets:
        for sg in ((geo.get("nets") or {}).get(nm) or {}).get("segments") or []:
            lay = sg.get("layer")
            if lay in idx and lay not in trace_layers:
                trace_layers.append(lay)
    if not trace_layers:
        raise EmBridgeError("EM_REFERENCE_PLANE_UNRESOLVED",
                            f"{nets} 没有任何走线层，无法确定参考面")
    wanted = [ref_net] if ref_net else None
    tried = []
    for lay in trace_layers:
        i = idx[lay]
        for j in (i + 1, i - 1):
            if not (0 <= j < len(cu)):
                continue
            cand = cu[j]["name"]
            on_layer = nets_on_layer(geo, cand)
            tried.append(f"{lay}→{cand}: {on_layer or '（该层无网络）'}")
            if wanted:
                hit = [n for n in on_layer if n in wanted]
            else:
                hit = [n for n in on_layer if is_groundish(n)]
            if hit:
                return {
                    "layer": cand, "net": hit[0],
                    "z_bottom_mm": cu[j]["z_bottom_mm"],
                    "z_top_mm": cu[j]["z_top_mm"],
                    "trace_layer": lay,
                    "source": ("--reference-net" if wanted else "ground-ish net"),
                    "candidates": tried,
                }
    raise EmBridgeError(
        "EM_REFERENCE_PLANE_UNRESOLVED",
        f"找不到 {nets} 的参考地平面。试过：{'; '.join(tried)}。"
        f"请把 GND 网络加进几何（--net GND），或用 --reference-net 指定，"
        f"或用 --reference-plane-box 明确给出平面范围")


def _layer_z(stack, layer_name):
    for l in stack["copper"]:
        if l["name"] == layer_name:
            return l
    raise EmBridgeError("EM_LAYER_NOT_FOUND",
                        f"叠层里没有铜层 {layer_name!r}（有 {[l['name'] for l in stack['copper']]}）")


def csxcad_script(geo, stackup, region, ports, out_path=None, reference=None,
                  plane_box=None):
    """生成 openEMS Python 脚本：铜（AddLinPoly）+ 多层介质 + 参考平面 + lumped ports。

    v2.5.1 的两处修正：
      * API：`CSX = CSXCAD.ContinuousStructure()` / `CSX.AddMetal(...)` /
        `metal.AddLinPoly(points, 'z', elevation, length)`。
        v2.5 生成的 `CSX.AddPolygon` / `CSXCAD.Polygon` / `SetElevation(b,t)`
        在当前 openEMS/CSXCAD 里并不存在。
      * 参考面：必须真的建进几何（否则端口连到一个不存在的导体），
        并如实标 `REFERENCE_PLANE_APPROXIMATION`。
    """
    stack = layer_stack(stackup)
    lines = [
        "#!/usr/bin/env python3",
        f"# openEMS coupon generated by easyeda-ngspice-sim (v2.6.0, {STATUS})",
        "# 需要你自己安装 openEMS + CSXCAD；本脚本不做任何求解，只搭几何与端口。",
        "import os, numpy as np",
        "import CSXCAD",
        "from openEMS import openEMS",
        "",
        "FDTD = openEMS(NrTS=20000, EndCriteria=1e-4)",
        f"FDTD.SetGaussExcite({region['f_max_hz'] / 2:.6g}, {region['f_max_hz'] / 2:.6g})",
        "FDTD.SetBoundaryCond(['PML_8'] * 6)",
        "CSX = CSXCAD.ContinuousStructure()",
        "FDTD.SetCSX(CSX)",
        "mesh = CSX.GetGrid()",
        "mesh.SetDeltaUnit(1e-3)   # 单位 mm",
        "",
        f"# ROI: {region['nets']}  margin={region['margin_mm']} mm  "
        f"cell={region['cell_mm']} mm",
    ]
    if out_path:
        lines.append(f'OUT = r"{out_path}"')
    lines += ["", "# ---- 多层介质（每层一个 material + 一个 slab，不压成单层）----"]
    for i, d in enumerate(stack["dielectrics"]):
        er = d.get("er") or 4.2
        name = f"diel{i + 1}"
        extra = ""
        if d.get("loss_tangent"):
            extra = (f", kappa={2 * math.pi * region['f_max_hz'] * 8.854e-12 * er * d['loss_tangent']:.4g}")
        lines += [
            f"{name} = CSX.AddMaterial('{name}_er{er:g}', epsilon={er:g}{extra})",
            f"{name}.AddBox([{region['bbox_mm']['x'][0]:.4g}, {region['bbox_mm']['y'][0]:.4g}, "
            f"{d['z_bottom_mm']:.4g}],",
            f"            [{region['bbox_mm']['x'][1]:.4g}, {region['bbox_mm']['y'][1]:.4g}, "
            f"{d['z_top_mm']:.4g}])",
        ]
    lines += ["", "# ---- 铜：中心线 + 线宽 → 多边形（沿法向偏移），再按铜厚拉伸 ----",
              "# CSXCAD 的 API: metal.AddLinPoly(points, norm_dir, elevation, length)",
              "#   points = 2D 多边形顶点；elevation = 起始 z；length = 沿法向的厚度",
              ""]
    n_poly = 0
    for nm in region["nets"]:
        net = (geo.get("nets") or {}).get(nm) or {}
        terms = net.get("terminals") or {}
        for i, seg in enumerate(net.get("segments") or [], start=1):

            a = terms.get(seg["from"])
            b = terms.get(seg["to"])
            if not a or not b:
                continue
            w = seg["width_mm"] if isinstance(seg["width_mm"], (int, float)) else \
                max(seg["width_mm"]) if isinstance(seg["width_mm"], list) else 0.2
            pts_xy = segment_polygon(a, b, w)
            lay = _layer_z(stack, seg["layer"])
            t_mm = lay["z_top_mm"] - lay["z_bottom_mm"]
            pts = ", ".join(f"[{x:.5g}, {y:.5g}]" for x, y in pts_xy)
            var = f"metal{n_poly}"
            lines += [
                f"# 面积自检：{polygon_area(pts_xy):.4g} mm^2"
                f"（中心线 {math.hypot(b['x_mm'] - a['x_mm'], b['y_mm'] - a['y_mm']):.4g}"
                f" mm × 线宽 {w:.4g} mm）",
                f"{var} = CSX.AddMetal('{nm}_{i}')",
                f"{var}.AddLinPoly([{pts}], 'z', "
                f"{lay['z_bottom_mm']:.5g}, {t_mm:.5g})",
            ]
            n_poly += 1
    lines += ["", f"# 共 {n_poly} 条铜多边形；多层介质 {len(stack['dielectrics'])} 层"]
    if reference:
        bx0, by0, bx1, by1 = plane_box or (
            region["bbox_mm"]["x"][0], region["bbox_mm"]["y"][0],
            region["bbox_mm"]["x"][1], region["bbox_mm"]["y"][1])
        rnet = reference["net"]
        how = ("平面范围由用户 --reference-plane-box 显式给出"
               if plane_box else
               "EasyEDA 的铺铜(POUR) 暂未展开，这里按 ROI 全幅铺一片整块铜作为参考导体")
        lines += [
            "",
            f"# ---- 参考平面：{rnet} @ {reference['layer']}"
            f"（REFERENCE_PLANE_APPROXIMATION）----",
            f"# 说明：{how}",
            f"ref = CSX.AddMetal('REF_{rnet}')",
            f"ref.AddBox([{bx0:.5g}, {by0:.5g}, {reference['z_bottom_mm']:.5g}],",
            f"            [{bx1:.5g}, {by1:.5g}, {reference['z_top_mm']:.5g}])",
        ]
    lines += ["", "# ---- 端口（竖向跨度 = 走线层 → 选定参考平面层）----"]
    for i, p in enumerate(ports, start=1):
        z0 = p.get("z0")
        z1 = p.get("z1")
        if z0 is None or z1 is None:
            top = stack["copper"][0]
            z0, z1 = top["z_bottom_mm"], top["z_top_mm"]
        lines += [
            f"# port {i}: {p.get('name', '')}  ({p.get('positive')} / {p.get('negative')})"
            f"  net={p.get('net', '')}",
            f"P{i} = FDTD.AddLumpedPort({i}, 50, "
            f"[{p.get('x0', 0):.4g}, {p.get('y0', 0):.4g}, {z0:.4g}], "
            f"[{p.get('x1', 0):.4g}, {p.get('y1', 0):.4g}, {z1:.4g}], "
            f"'z', 1.0, excite={'True' if i == 1 else 'False'})",
        ]
    lines += [
        "",
        "mesh.AddLine('x', np.arange("
        f"{region['bbox_mm']['x'][0]:.4g}, {region['bbox_mm']['x'][1]:.4g}, "
        f"{region['cell_mm']:.4g}))",
        "mesh.AddLine('y', np.arange("
        f"{region['bbox_mm']['y'][0]:.4g}, {region['bbox_mm']['y'][1]:.4g}, "
        f"{region['cell_mm']:.4g}))",
        f"mesh.AddLine('z', np.linspace(-3, {stack['total_thickness_mm']:.4g} + 3, 12))",
        "",
        "# FDTD.Run(os.path.dirname(os.path.abspath(__file__)))",
        "# 运行后用 openEMS 的 port 数据导出 .sNp，再交给 vector fitting：",
        "#   import rf_network as rn, vector_fit as vf",
        "#   net = rn.RfNetwork.from_touchstone('coupon.s2p')",
        "#   vf.fit(net, order='auto').write_spice('coupon.sp')",
    ]
    return "\n".join(lines) + "\n"


def write_plan(geo, stackup, region, ports, outdir, reference=None,
               plane_box=None, reference_error=None):
    os.makedirs(outdir, exist_ok=True)
    script = os.path.join(outdir, "em_coupon.py")
    with open(script, "w", encoding="utf-8", newline="\n") as f:
        f.write(csxcad_script(geo, stackup, region, ports,
                              reference=reference, plane_box=plane_box))
    plan = {"version": 4, "status": STATUS, "fidelity": FIDELITY,
            "region": region, "ports": ports,
            "stack": layer_stack(stackup),
            "reference": reference,
            "reference_plane": ("REFERENCE_PLANE_APPROXIMATION" if reference else None),
            "reference_resolution": reference_error,
            "script": script,
            "howto": ["1) 安装 openEMS + CSXCAD（外部，GPL-3.0）",
                      "2) python em_coupon.py（先取消最后一行注释）",
                      "3) 导出 S 参数 → rf_network / vector_fit（Python API）"],
            "limitations": ["只生成几何与端口，不做求解（需用户自己的 openEMS）",
                            "铜为「中心线+线宽」的旋转矩形（AddLinPoly 拉伸）；"
                            "铺铜(POUR) 未展开，参考平面按 ROI 全幅近似",
                            "介质按叠层逐层建成 slab，但未做频率相关 εr/损耗拟合",
                            "网格是均匀估计，未做 thirds-rule 边缘加密"]}
    path = os.path.join(outdir, "EM_PLAN.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    return plan, path, script


def summary_lines(plan):
    r = plan["region"]
    out = [f"EM coupon ({STATUS}): nets={r['nets']} margin={r['margin_mm']} mm",
           f"  bbox x{r['bbox_mm']['x']} y{r['bbox_mm']['y']}",
           f"  f_max={r['f_max_hz'] / 1e9:.3g} GHz  λ={r['lambda_mm']:.2f} mm  "
           f"cell={r['cell_mm']} mm"]
    ref = plan.get("reference")
    if ref:
        out.append(f"  参考平面: {ref['net']} @ {ref['layer']} "
                   f"(z {ref['z_bottom_mm']:.4g}..{ref['z_top_mm']:.4g} mm, "
                   f"{plan.get('reference_plane')})")
    else:
        out.append(f"  ⚠ 参考平面未确定: {plan.get('reference_resolution')}")
    out += [f"  script: {plan['script']}",
            "  注意：本步骤只生成 openEMS 工程，求解需用户自己的 openEMS"]
    return out
