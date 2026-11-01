#!/usr/bin/env python3
"""v2.5 P0-B：PCB → openEMS coupon 工程（**只生成几何/端口，不做求解**）。

    python pcb_em.py Main.epro2 --net CLK_ADC --fmax 3e9 --out em/
    python pcb_em.py geo.json --net CLK_ADC --net GND --stackup stackup.json --out em/

产出（`--out` 目录）：
  em_coupon.py    openEMS/CSXCAD 脚本（真实铜多边形 + 多层介质 + lumped ports）
  EM_PLAN.json    裁剪区域、网格、端口、叠层快照、限制说明

为什么不能"整板 full-wave"：FDTD 网格量随尺寸立方增长，一块 100 mm 板在 3 GHz
下要 TB 级内存。所以必须裁一个 coupon（参考 gerber2ems 的做法）。

状态恒为 EXPERIMENTAL：本机没有 openEMS，本脚本从不求解，
产出的工程必须由用户自己的 openEMS 跑（GPL-3.0，外部进程，见 REFERENCE_PROJECTS.md）。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import em_bridge                     # noqa: E402
import pcb_geometry as pg            # noqa: E402
import stackup as stk                # noqa: E402


def _load_geometry(src, nets, unit=None, board_t=None):
    if src.lower().endswith(".json"):
        with open(src, encoding="utf-8") as f:
            geo = json.load(f)
        raw = {}
        if not geo.get("nets"):
            raise SystemExit("geometry.json 里没有 nets，请先用 pcb_to_override.py 生成")
        return geo, raw
    try:
        raw = pg.load_pcb_input(src, unit=unit)
    except (ValueError, OSError) as e:
        raise SystemExit(f"PCB_INPUT_INVALID: {e}")
    try:
        geo = pg.build_geometry(raw, net=nets or None)
    except pg.PcbError as e:
        raise SystemExit(f"{e.code}: {e.message}\n"
                         "  --net 必须是 PCB 上真实存在的铜网络名（不是原理图网标）。")
    return geo, raw


def _pick_ports(geo, nets, stack, reference=None):
    """生成端口：每个网络取一个端点做一条垂直于走线的端口面。

    经验规则（写进 plan 的 limitations，避免误导）：
      * 给两个以上网络 → 每个网络一个端口（port i = nets[i] 的端点）；
      * 只给一个网络 → 该网络几何上最远的两个端点各一个端口（2 端口 coupon）。
    端口竖向跨度 = 「走线铜层的表面 → **选定的参考平面层**」。
    v2.5 用的是"相邻下一片铜"，既不检查是不是地、也不检查那层有没有几何；
    v2.5.1 改为使用 `em_bridge.select_reference()` 的结果。
    """
    cu = stack["copper"]

    def idx_of(name):
        for i, c in enumerate(cu):
            if c["name"] == name:
                return i
        return None

    def term_xy(net_name, key):
        t = ((geo.get("nets") or {}).get(net_name) or {}).get("terminals") or {}
        p = t.get(key)
        return (p["x_mm"], p["y_mm"]) if p else None

    def layer_of(net_name):
        for sg in ((geo.get("nets") or {}).get(net_name) or {}).get("segments") or []:
            return sg.get("layer")
        return None

    def z_span(net_name):
        """返回 (走线侧 z, 参考平面 z)。优先用已选定的参考层。"""
        i = idx_of(layer_of(net_name))
        if i is None:
            return cu[0]["z_bottom_mm"], cu[-1]["z_bottom_mm"]
        if reference and reference.get("layer"):
            j = idx_of(reference["layer"])
            if j is not None and j != i:
                z_ref = (cu[j]["z_top_mm"] if j < i else cu[j]["z_bottom_mm"])
                return cu[i]["z_bottom_mm"], z_ref
        if i + 1 < len(cu):
            return cu[i]["z_bottom_mm"], cu[i + 1]["z_bottom_mm"]
        return cu[i]["z_top_mm"], cu[i - 1]["z_top_mm"]

    ports = []
    if len(nets) >= 2:
        for i, nm in enumerate(nets, start=1):
            terms = list((((geo.get("nets") or {}).get(nm) or {})
                          .get("terminals") or {}).keys())
            if not terms:
                continue
            xy = term_xy(nm, terms[0])
            z0, z1 = z_span(nm)
            ports.append({"name": f"P{i}_{nm}", "net": nm,
                          "positive": f"{nm}:{terms[0]}",
                          "negative": f"plane@{z1:.4g}mm",
                          "x0": xy[0], "y0": xy[1], "z0": z0,
                          "x1": xy[0], "y1": xy[1], "z1": z1})
        return ports
    # 单网络 → 最远两端
    nm = nets[0]
    terms = (((geo.get("nets") or {}).get(nm) or {}).get("terminals") or {})
    keys = list(terms.keys())
    if len(keys) < 2:
        raise SystemExit(f"网络 {nm} 只有 {len(keys)} 个端点，无法生成 2 端口 coupon")
    p0 = terms[keys[0]]
    far = max(keys, key=lambda k: (terms[k]["x_mm"] - p0["x_mm"]) ** 2 +
              (terms[k]["y_mm"] - p0["y_mm"]) ** 2)
    z0, z1 = z_span(nm)
    for i, k in enumerate([keys[0], far], start=1):
        xy = (terms[k]["x_mm"], terms[k]["y_mm"])
        ports.append({"name": f"P{i}_{nm}", "net": nm,
                      "positive": f"{nm}:{k}",
                      "negative": f"plane@{z1:.4g}mm",
                      "x0": xy[0], "y0": xy[1], "z0": z0,
                      "x1": xy[0], "y1": xy[1], "z1": z1})
    return ports


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        sys.exit(2)
    src = argv[0]
    nets, out = [], "em"
    stackup_path = None
    fmax = 3e9
    board_t = None
    unit = None
    margin = None
    ref_net = None
    plane_box = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--net":
            nets.append(argv[i + 1]); i += 2
        elif a == "--out":
            out = argv[i + 1]; i += 2
        elif a == "--stackup":
            stackup_path = argv[i + 1]; i += 2
        elif a == "--fmax":
            fmax = float(argv[i + 1]); i += 2
        elif a == "--board-thickness":
            board_t = float(argv[i + 1]); i += 2
        elif a == "--unit":
            unit = argv[i + 1]; i += 2
        elif a == "--margin":
            margin = float(argv[i + 1]); i += 2
        elif a == "--reference-net":
            ref_net = argv[i + 1]; i += 2
        elif a == "--reference-plane-box":
            plane_box = [float(x) for x in argv[i + 1].replace(" ", "").split(",")]
            if len(plane_box) != 4:
                raise SystemExit("--reference-plane-box 需要 x0,y0,x1,y1（mm）")
            i += 2
        else:
            i += 1
    if not nets:
        raise SystemExit("必须用 --net 指定要仿真的网络（EM coupon 只能覆盖一两条线）")

    # v2.5.1：几何要包含 GND 等参考网络，才能判断真实的参考导体。
    # 所以这里按「全板」建几何，再只用 nets 做 ROI/端口。
    geo, raw = _load_geometry(src, None, unit=unit)
    missing_nets = [n for n in nets if n not in (geo.get("nets") or {})]
    if missing_nets:
        raise SystemExit(f"网络不在几何里: {missing_nets}；可用: "
                         f"{list((geo.get('nets') or {}).keys())[:10]}")

    base = stk.Stackup.from_pcb(geo.get("layers"), raw.get("stackup_raw"), board_t)
    if stackup_path:
        st = stk.Stackup.from_file(stackup_path, base=base)
        st.meta["merged_with_pcb"] = True
    else:
        st = base
    try:
        st.require_complete()
    except stk.StackupError as e:
        raise SystemExit(
            f"{e.code}: {e.message}\n"
            "  EM 需要知道铜层位置与介质厚度才能摆几何，本工具不会默认 FR4=4.5 继续跑。\n"
            "  用 --stackup stackup.json 只补缺的项即可（铜厚 + 缺失的介质厚度）。")
    reference = reference_error = None
    try:
        stack = em_bridge.layer_stack(st.to_dict())
        reference = em_bridge.select_reference(geo, nets, stack, ref_net=ref_net)
        ports = _pick_ports(geo, nets, stack, reference)
    except em_bridge.EmBridgeError as e:
        raise SystemExit(f"{e.code}: {e.message}")
    region = em_bridge.plan_region(geo, nets, fmax, margin_mm=margin)

    plan, plan_path, script = em_bridge.write_plan(
        geo, st.to_dict(), region, ports, out, reference=reference,
        plane_box=plane_box, reference_error=reference_error)
    plan["limitations"] = [
        "只生成几何与端口，不做任何求解（需要用户自己的 openEMS，GPL-3.0 外部进程）",
        "铜 = 「中心线 + 线宽」沿法向偏移的旋转矩形，用 metal.AddLinPoly(...) 拉伸铜厚",
        "参考平面按 REFERENCE_PLANE_APPROXIMATION 处理：铺铜(POUR) 未展开，"
        "默认铺一片覆盖 ROI 的整块铜；可用 --reference-plane-box 显式给范围",
        "介质按叠层逐层建 slab，未做频率相关 εr / 宽带损耗拟合",
        "网格为均匀估计，未做 thirds-rule 边缘加密；细线（<3 格）会被网格吞掉",
        "过孔只作为端点坐标参与 ROI，未在 EM 里建 3D 孔壁",
    ]
    with open(plan_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)

    print(f"EM plan written: {plan_path}")
    print(f"  script : {script}")
    print(f"  nets   : {region['nets']}")
    print(f"  bbox   : x{region['bbox_mm']['x']}  y{region['bbox_mm']['y']} mm")
    print(f"  f_max  : {region['f_max_hz'] / 1e9:.3g} GHz  "
          f"λ={region['lambda_mm']:.2f} mm  cell={region['cell_mm']} mm  "
          f"cells≈{region['cells_estimate']['x']}×{region['cells_estimate']['y']}")
    print(f"  stack  : {len(stack['copper'])} 铜层 / {len(stack['dielectrics'])} 介质层, "
          f"总厚 {stack['total_thickness_mm']:.3f} mm")
    if reference:
        print(f"  参考面 : {reference['net']} @ {reference['layer']}  "
              f"z {reference['z_bottom_mm']:.4g}..{reference['z_top_mm']:.4g} mm  "
              f"[{plan['reference_plane']}]")
    for p in ports:
        print(f"  port {p['name']}: ({p['x0']:.3f}, {p['y0']:.3f}) "
              f"z {p['z0']:.4g}→{p['z1']:.4g} mm")
    n_poly = sum(len(g.get("segments") or [])
                 for g in (geo.get("nets") or {}).values()
                 if g and True)
    print(f"  segments in geometry: {n_poly}（会按网络过滤）")
    print(f"  status : {em_bridge.STATUS} —— 未求解，结果不可进工程决策")


if __name__ == "__main__":
    main()
