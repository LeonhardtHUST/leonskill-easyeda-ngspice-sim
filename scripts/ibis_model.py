#!/usr/bin/env python3
"""v2.3 P0-D(2)：IBIS 语义层——把解析结果变成可用的电参数。

  resolve(doc, component, pin)        → 该引脚的模型 + 封装参数
  output_drive(model, vcc)            → 由 [Pullup]/[Pulldown] 反推 Rout
  edge_times(model, vcc, corner)      → 由 [Ramp] 或 [Rising/Falling Waveform] 得 tr/tf
  input_capacitance(model, pin)       → C_comp + C_pkg + C_pin

关于 Ramp 的口径：IBIS 的 dV/dt 是 **20%~80%** 的斜率，而 SPICE 的 PULSE tr/tf 是
**0~100%** 的过渡时间。这里按经验关系 tr(10-90) ≈ 1.4 × tr(20-80)、
tr(0-100) ≈ 1.8 × tr(20-80) 换算，并在返回值里标 `edge_basis`，报告里如实写出。
"""
import math

IBIS_FIDELITY = "IBIS_DERIVED_BEHAVIORAL_MODEL"


class IbisModelError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def resolve(doc, component=None, pin=None, model=None):
    """定位 (component, pin) 对应的模型。三者至少要给 model 或 (component, pin)。"""
    comp = None
    if component:
        for name, c in (doc.get("components") or {}).items():
            if name.upper() == str(component).upper():
                comp = c
                break
        if comp is None:
            raise IbisModelError(
                "IBIS_COMPONENT_NOT_FOUND",
                f"IBIS 里没有 [Component] {component!r}；现有: "
                f"{list((doc.get('components') or {}))}")
    pin_entry = None
    if comp is not None and pin is not None:
        pin_entry = (comp.get("pins") or {}).get(str(pin))
        if pin_entry is None:
            raise IbisModelError(
                "IBIS_PIN_NOT_FOUND",
                f"[Component] {component} 里没有引脚 {pin!r}；"
                f"现有: {list((comp.get('pins') or {}))[:20]}")
    if model is None:
        if pin_entry is None:
            raise IbisModelError("IBIS_MODEL_NOT_SPECIFIED",
                                 "既没给 model 名，也没给可解析的 component/pin")
        model = pin_entry.get("model")
        if not model:
            raise IbisModelError(
                "IBIS_PIN_HAS_NO_MODEL",
                f"{component}.{pin} 是电源/地/NC 引脚（{pin_entry.get('keyword')}），没有 [Model]")
    m = (doc.get("models") or {}).get(model)
    if m is None:
        raise IbisModelError(
            "IBIS_MODEL_NOT_FOUND",
            f"IBIS 里没有 [Model] {model!r}；现有: {list((doc.get('models') or {}))}")
    pkg = (comp or {}).get("package") or doc.get("package") or {}
    rows = pkg.get("rows") or []
    r_pkg = rows[0].get("r") if rows else None
    l_pkg = rows[0].get("l") if rows else None
    c_pkg = rows[0].get("c") if rows else None
    if pin_entry:
        # 引脚级的 R/L/C 更精确：优先用它们（IBIS 允许 pin 覆盖 package）
        r = pin_entry.get("r_pin") if pin_entry.get("r_pin") is not None else r_pkg
        l = pin_entry.get("l_pin") if pin_entry.get("l_pin") is not None else l_pkg
        c = pin_entry.get("c_pin") if pin_entry.get("c_pin") is not None else c_pkg
    else:
        r, l, c = r_pkg, l_pkg, c_pkg
    return {
        "ibis_file": doc.get("file"),
        "ibis_path": doc.get("path"),
        "component": component,
        "pin": str(pin) if pin is not None else None,
        "signal_name": (pin_entry or {}).get("signal_name"),
        "model": model,
        "model_type": (m.get("model_type") or "").strip(),
        "c_comp": (m.get("c_comp") or {}),
        "r_pkg": r, "l_pkg": l, "c_pkg": c,
        "vinl": m.get("vinl"), "vinh": m.get("vinh"), "vmeas": m.get("vmeas"),
        "ramp": m.get("ramp") or {},
        "pullup": m.get("pullup") or [],
        "pulldown": m.get("pulldown") or [],
        "rising": m.get("rising") or [],
        "falling": m.get("falling") or [],
        "fidelity": IBIS_FIDELITY,
    }


def resolve_diff(doc, component, positive, negative=None, vcc=3.3):
    """[Diff Pin] 支持：定位一对差分引脚并给出两侧模型。

    返回 {pair, positive: spec, negative: spec, vdiff, model_type}
    高级特性（series switch / aggregator / submodel）直接报 IBIS_FEATURE_UNSUPPORTED。
    """
    if doc.get("unsupported"):
        raise IbisModelError(
            "IBIS_FEATURE_UNSUPPORTED",
            f"文件含暂不支持的 IBIS 段: {sorted(set(doc['unsupported']))}；"
            f"差分建模请先导出成基础 [Model] + [Diff Pin] 形式")
    comp = (doc.get("components") or {}).get(component)
    if comp is None:
        for k, v in (doc.get("components") or {}).items():
            if k.upper() == str(component).upper():
                comp, component = v, k
                break
    if comp is None:
        raise IbisModelError("IBIS_COMPONENT_NOT_FOUND",
                             f"没有 [Component] {component!r}")
    pair = None
    for p in comp.get("diff_pins") or doc.get("diff_pins") or []:
        if p.get("component") not in (None, component):
            continue
        if positive in (p["positive"], p["negative"]):
            pair = p
            break
    if pair is None:
        raise IbisModelError(
            "IBIS_DIFF_PIN_NOT_FOUND",
            f"[Component] {component} 的 [Diff Pin] 里没有 {positive!r}；"
            f"现有: {[(p['positive'], p['negative']) for p in comp.get('diff_pins') or []]}")
    pos = pair["positive"]
    neg = pair["negative"]
    if negative and negative != neg:
        raise IbisModelError(
            "IBIS_DIFF_PIN_MISMATCH",
            f"{positive} 的互补脚是 {neg}，但映射里写的是 {negative}")
    sp = resolve(doc, component, pos)
    sn = resolve(doc, component, neg)
    return {"component": component, "pair": pair, "positive": sp, "negative": sn,
            "vdiff": pair.get("vdiff"),
            "model_type": sp.get("model_type"),
            "tdelay_s": pair.get("tdelay_typ")}


def _interp(points, x):
    """在 (V, I) 折线上插值（点按 V 升序）。"""
    if not points:
        return None
    pts = sorted(points, key=lambda p: p[0])
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        if pts[i][0] >= x:
            x0, y0 = pts[i - 1]
            x1, y1 = pts[i]
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def output_drive(spec, vcc=3.3, corner="typ"):
    """由 IV 曲线反推上/下拉等效电阻（在 Vout = vcc/2 处取斜率点）。"""
    key = {"typ": "i_typ", "min": "i_min", "max": "i_max"}.get(corner, "i_typ")
    vhalf = vcc / 2.0
    up = [(p["v"], p.get(key) if p.get(key) is not None else p.get("i_typ"))
          for p in spec.get("pullup") or []]
    dn = [(p["v"], p.get(key) if p.get(key) is not None else p.get("i_typ"))
          for p in spec.get("pulldown") or []]
    up = [(v, i) for v, i in up if i is not None]
    dn = [(v, i) for v, i in dn if i is not None]
    i_up = _interp(up, vhalf)      # IBIS 约定：流出器件为正，上管灌出为负
    i_dn = _interp(dn, vhalf)
    r_up = abs((vcc - vhalf) / i_up) if i_up not in (None, 0) else None
    r_dn = abs(vhalf / i_dn) if i_dn not in (None, 0) else None
    if r_up is None and r_dn is None:
        return {"rout_ohm": None, "note": "IV 曲线缺失，无法反推输出电阻"}
    r = None
    if r_up and r_dn:
        r = (r_up + r_dn) / 2.0
    else:
        r = r_up or r_dn
    return {
        "rout_ohm": round(r, 3),
        "r_pullup_ohm": round(r_up, 3) if r_up else None,
        "r_pulldown_ohm": round(r_dn, 3) if r_dn else None,
        "i_pullup_a": i_up, "i_pulldown_a": i_dn,
        "at_vout": round(vhalf, 3),
        "corner": corner,
    }


def edge_times(spec, vcc=3.3, corner="typ"):
    """返回 {tr, tf, basis}（0→100% 口径，用于 SPICE PULSE）。"""
    ramp = spec.get("ramp") or {}
    suffix = {"typ": "", "min": "_min", "max": "_max"}.get(corner, "")

    def from_ramp(key):
        r = ramp.get(key)
        if not r:
            return None
        dv = r.get("dv" + suffix) or r.get("dv")
        dt = r.get("dt" + suffix) or r.get("dt")
        if not dv or not dt:
            return None
        frac = float(dv) / abs(vcc)          # 这段 dV 占满摆幅的比例
        if frac <= 0.02:
            return None
        t_2080 = dt / frac                   # 折算成整摆幅时间
        return t_2080 * 1.8                  # 20-80% → 0-100%

    tr = from_ramp("dv/dt_r")
    tf = from_ramp("dv/dt_f")
    if tr and tf:
        return {"tr": tr, "tf": tf, "basis": "ramp(20-80% ×1.8 → 0-100%)"}

    def from_wave(waves, rising):
        if not waves:
            return None
        w = waves[0]
        pts = w.get("typ") or []
        if len(pts) < 3:
            return None
        vs = [p[1] for p in pts]
        lo, hi = min(vs), max(vs)
        span = hi - lo
        if span <= 1e-9:
            return None
        def t_at(frac):
            target = lo + span * frac
            for i in range(1, len(pts)):
                v0, v1 = pts[i - 1][1], pts[i][1]
                if (v0 - target) * (v1 - target) <= 0 and v0 != v1:
                    t0, t1 = pts[i - 1][0], pts[i][0]
                    return t0 + (target - v0) * (t1 - t0) / (v1 - v0)
            return None
        if rising:
            t10, t90 = t_at(0.1), t_at(0.9)
            if t10 is None or t90 is None:
                t10, t90 = pts[0][0], pts[-1][0]
            return (t90 - t10) * 1.25        # 10-90% → 0-100% 近似
        t90, t10 = t_at(0.9), t_at(0.1)
        if t10 is None or t90 is None:
            t90, t10 = pts[0][0], pts[-1][0]
        return (t10 - t90) * 1.25

    if not tr:
        tr = from_wave(spec.get("rising"), True)
    if not tf:
        tf = from_wave(spec.get("falling"), False)
    if tr or tf:
        return {"tr": tr, "tf": tf, "basis": "waveform(10-90% ×1.25 → 0-100%)"}
    return {"tr": None, "tf": None, "basis": "none"}


def input_capacitance(spec, corner="typ"):
    cc = (spec.get("c_comp") or {})
    c_comp = cc.get(corner) or cc.get("typ")
    c_pkg = spec.get("c_pkg") or 0.0
    total = (c_comp or 0.0) + (c_pkg or 0.0)
    return {"c_comp_pf": round((c_comp or 0) * 1e12, 4),
            "c_pkg_pf": round((c_pkg or 0) * 1e12, 4),
            "c_total_pf": round(total * 1e12, 4)}


def classify(spec):
    t = (spec.get("model_type") or "").strip().lower()
    if t in ("input", "input_diff"):
        return "input"
    if t in ("output", "output_diff"):
        return "output"
    if t.startswith("i/o") or t in ("io", "3-state", "3_state", "bidirectional"):
        return "io"
    if t in ("series", "series_switch"):
        return "series"
    if t in ("open_drain", "open_sink", "open_source", "open_drain_diff"):
        return "open"
    return "unknown"


def describe(spec, vcc=3.3):
    kind = classify(spec)
    lines = [f"{spec.get('component')}.{spec.get('pin')} "
             f"{spec.get('model')} ({spec.get('model_type')})",
             f"fidelity: {IBIS_FIDELITY}"]
    if kind in ("input", "io", "output"):
        c = input_capacitance(spec)
        lines.append(f"C: C_comp={c['c_comp_pf']} pF + C_pkg={c['c_pkg_pf']} pF "
                     f"= {c['c_total_pf']} pF")
    if kind in ("output", "io"):
        d = output_drive(spec, vcc)
        e = edge_times(spec, vcc)
        lines.append(f"drive: Rout={d.get('rout_ohm')} Ω @ Vout={d.get('at_vout')} V "
                     f"(pullup {d.get('r_pullup_ohm')} / pulldown {d.get('r_pulldown_ohm')})")
        lines.append(f"edges: tr={_ps(e.get('tr'))} tf={_ps(e.get('tf'))}  [{e.get('basis')}]")
    if spec.get("vinl") is not None or spec.get("vinh") is not None:
        lines.append(f"thresholds: Vinl={spec.get('vinl')} Vinh={spec.get('vinh')} "
                     f"Vmeas={spec.get('vmeas')}")
    return lines


def _ps(t):
    return f"{t * 1e12:.1f} ps" if t else "?"
