#!/usr/bin/env python3
"""v2.3 P0-B(2)：走线 → 传输线参数（Z0 / TD）。

只做"几何 → 电参数"，不碰 ngspice 语法。精度声明：ANALYTIC_ESTIMATE。
过孔在 via_extract.py。
"""
from stackup import StackupError, trace_params

TRACE_FIDELITY = "ANALYTIC_ESTIMATE"


class ExtractError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _widths(w):
    return w if isinstance(w, list) else [w]


def estimate_net(net_name, geo_net, stackup, default_width_mm=None, overrides=None):
    """给一个网络的所有 segment 补上 z0/td。

    overrides: {layer_name: {"z0":.., "td_ps_per_mm":..}} 用户覆盖
    """
    overrides = overrides or {}
    out, warnings = [], []
    for i, seg in enumerate(geo_net.get("segments") or [], start=1):
        widths = _widths(seg.get("width_mm"))
        if not widths or widths[0] in (None, 0):
            if default_width_mm:
                widths = [default_width_mm]
                warnings.append(f"{net_name} seg{i}: 走线宽度缺失，按 {default_width_mm} mm 兜底")
            else:
                raise ExtractError("TRACE_WIDTH_MISSING",
                                   f"{net_name} seg{i}: 走线宽度为 0/缺失，无法估 Z0")
        if len(widths) > 1:
            warnings.append(f"{net_name} seg{i}: 段内混有多种线宽 {widths}，按最宽估计"
                            f"（如需精度请拆分该段）")
        w = max(widths)
        rec = dict(seg)
        rec["segment_index"] = i
        try:
            tr = trace_params(stackup, seg["layer"], w, seg.get("length_mm"))
        except StackupError as e:
            raise ExtractError(e.code, f"{net_name} seg{i}: {e.message}")
        rec.update({
            "z0_ohm": tr["z0_ohm"],
            "td_ps_per_mm": tr["td_ps_per_mm"],
            "td_ps": tr.get("td_ps", round((seg.get("length_mm") or 0)
                                          * tr["td_ps_per_mm"], 3)),
            "eps_eff": tr["eps_eff"],
            "h_mm": tr["h_mm"],
            "trace_model": tr["model"],
            "model_fidelity": TRACE_FIDELITY,
        })
        ov = overrides.get(seg["layer"]) or {}
        if "z0" in ov:
            warnings.append(f"{net_name} seg{i}: Z0 被用户覆盖 {rec['z0_ohm']} → {ov['z0']}")
            rec["z0_ohm"] = ov["z0"]
            rec["model_fidelity"] = "USER_OVERRIDE"
        if "td_ps_per_mm" in ov:
            rec["td_ps_per_mm"] = ov["td_ps_per_mm"]
            rec["td_ps"] = round((seg.get("length_mm") or 0) * ov["td_ps_per_mm"], 3)
            rec["model_fidelity"] = "USER_OVERRIDE"
        out.append(rec)
    return out, warnings


def estimate_geometry(geo, stackup, nets=None, default_width_mm=None, overrides=None):
    """整个 PCB_GEOMETRY → 带 Z0/TD 的副本（不改原对象）。"""
    result = {k: v for k, v in geo.items() if k != "nets"}
    result["nets"] = {}
    result["warnings"] = list(geo.get("warnings") or [])
    targets = nets or list((geo.get("nets") or {}).keys())
    for nm in targets:
        net = (geo.get("nets") or {}).get(nm)
        if not net:
            continue
        segs, warns = estimate_net(nm, net, stackup, default_width_mm, overrides)
        result["nets"][nm] = {**net, "segments": segs}
        result["warnings"].extend(warns)
    result["stackup"] = stackup.to_dict()
    return result


def summary_line(seg):
    return (f"{seg['from']} → {seg['to']} ({seg['layer']}) "
            f"{seg['length_mm']:g} mm  Z0={seg['z0_ohm']:g} Ω  "
            f"TD={seg['td_ps']:g} ps  w={seg['width_mm']} mm [{seg['model_fidelity']}]")
