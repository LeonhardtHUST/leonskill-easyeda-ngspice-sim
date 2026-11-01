#!/usr/bin/env python3
"""v2.4 P0-G/J：耦合传输线解析估计 + 差分对自动识别。

两件事：
  1. `coupled_microstrip()/coupled_stripline()`：由 w/s/h/t/εr 估 Zodd/Zeven/Zdiff/
     Zcommon 与模间时延。标 `ANALYTIC_COUPLED_LINE_ESTIMATE`（不是场求解）。
  2. `identify_diff_pairs()`：从 PCB_GEOMETRY 里按**几何**找耦合对（平行、同层、
     间距稳定、投影重叠），net 名 `_P/_N` 只作为加分证据——需求文档明确禁止只看名字。

精度说明（必须写进报告）：
  * Zdiff 用 IPC-2141 的经验耦合因子 `Zodd = Z0se·(1 − 0.48·e^(−0.96·s/h))`，
    常规 w/s/h 下误差 ~±10%；s/h > 3 时收敛到 2×Z0se（正确极限）。
  * 奇模/偶模的有效介电常数只有一阶修正，模间时延误差可达 ±20%。
"""
import math

import numpy as np

FIDELITY = "ANALYTIC_COUPLED_LINE_ESTIMATE"
C_PS_PER_MM = 3.33564


class CoupledLineError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _single(stk, layer, w):
    """单端基线：Z0 与 εeff（复用 stackup 的微带/带状线解析式）。"""
    import stackup as stk_mod
    tr = stk_mod.trace_params(stk, layer, w)
    return tr["z0_ohm"], tr["eps_eff"], tr["model"]


def coupled_microstrip(stk, layer, w_mm, s_mm, length_mm=None, t_mm=None):
    """边沿耦合微带：返回 Zodd/Zeven/Zdiff/Zcommon + td_odd/td_even。"""
    if s_mm <= 0:
        raise CoupledLineError("COUPLED_GEOMETRY_INVALID", f"间距必须 > 0（{s_mm}）")
    z0se, eps_se, model = _single(stk, layer, w_mm)
    h = stk.dielectric_for(layer)[0]
    k = math.exp(-0.96 * (s_mm / h))
    z_odd = z0se * (1.0 - 0.48 * k)
    # 对称耦合对：Zodd·Zeven ≈ Z0se²  （s→∞ 时两者都收敛到 Z0se）
    z_even = (z0se ** 2) / z_odd if z_odd > 0 else z0se
    z_diff = 2.0 * z_odd
    z_comm = z_even / 2.0
    # 一阶模间介电常数修正：奇模场更集中在介质里 → εeff 略高
    eps_odd = eps_se + (max(eps_se * 1.0, 1.0001) - 1.0) * 0.12 * k
    eps_even = eps_se - (eps_se - 1.0) * 0.08 * k
    td_odd = C_PS_PER_MM * math.sqrt(max(eps_odd, 1.0))
    td_even = C_PS_PER_MM * math.sqrt(max(eps_even, 1.0))
    out = {
        "type": "edge_coupled_microstrip", "layer": layer,
        "width_mm": round(w_mm, 4), "spacing_mm": round(s_mm, 4),
        "height_mm": round(h, 4), "model": model,
        "z0_single_ohm": round(z0se, 2),
        "z_odd_ohm": round(z_odd, 2), "z_even_ohm": round(z_even, 2),
        "z_diff_ohm": round(z_diff, 2), "z_common_ohm": round(z_comm, 2),
        "eps_eff_se": round(eps_se, 4),
        "eps_eff_odd": round(eps_odd, 4), "eps_eff_even": round(eps_even, 4),
        "td_odd_ps_per_mm": round(td_odd, 4), "td_even_ps_per_mm": round(td_even, 4),
        "model_fidelity": FIDELITY,
        "coupling_k": round(k, 5),
        "assumptions": [
            "奇模用 IPC-2141 经验耦合因子 Zodd = Z0se·(1 − 0.48·e^(−0.96·s/h))",
            "Zeven 由对称性 Zodd·Zeven ≈ Z0se² 反推",
            "奇/偶模 εeff 仅一阶修正，模间时延误差可达 ±20%",
        ],
    }
    if length_mm:
        out["length_mm"] = round(length_mm, 4)
        out["td_odd_ps"] = round(length_mm * td_odd, 3)
        out["td_even_ps"] = round(length_mm * td_even, 3)
        out["skew_ps"] = round(abs(out["td_odd_ps"] - out["td_even_ps"]), 3)
    return out


def coupled_stripline(stk, layer, w_mm, s_mm, length_mm=None):
    """内层边沿耦合带状线：Zdiff 用 IPC-2141 的带宽耦合近似。"""
    z0se, eps_se, model = _single(stk, layer, w_mm)
    b = stk.dielectric_for(layer)[0]
    k = math.exp(-1.0 * max(s_mm / max(b, 1e-6), 1e-6))
    z_odd = z0se * (1.0 - 0.45 * k)
    z_even = (z0se ** 2) / z_odd if z_odd > 0 else z0se
    td = C_PS_PER_MM * math.sqrt(eps_se)          # 带状线两模 εeff 近似相同
    out = {
        "type": "edge_coupled_stripline", "layer": layer,
        "width_mm": round(w_mm, 4), "spacing_mm": round(s_mm, 4),
        "height_mm": round(b, 4), "model": model,
        "z0_single_ohm": round(z0se, 2),
        "z_odd_ohm": round(z_odd, 2), "z_even_ohm": round(z_even, 2),
        "z_diff_ohm": round(2 * z_odd, 2), "z_common_ohm": round(z_even / 2, 2),
        "td_odd_ps_per_mm": round(td, 4), "td_even_ps_per_mm": round(td, 4),
        "model_fidelity": FIDELITY,
        "assumptions": ["带状线两模 εeff 视为相同（介质对称）"],
    }
    if length_mm:
        out["length_mm"] = round(length_mm, 4)
        out["td_odd_ps"] = out["td_even_ps"] = round(length_mm * td, 3)
        out["skew_ps"] = 0.0
    return out


# --------------------------------------------------------------------------
# 差分对识别（几何优先）
# --------------------------------------------------------------------------
def _seg_vec(seg, terminals):
    a = terminals.get(seg["from"])
    b = terminals.get(seg["to"])
    if not a or not b:
        return None
    return np.array([b["x_mm"] - a["x_mm"], b["y_mm"] - a["y_mm"]]), a, b


def _angle_deg(v):
    return math.degrees(math.atan2(v[1], v[0])) % 180.0


def _parallel_gap(seg_a, seg_b, ta, tb):
    """两条平行线段的中位间距（垂直距离），返回 (gap_mm, overlap_ratio)。"""
    va, a1, a2 = seg_a
    vb, b1, b2 = seg_b
    na = va / (np.linalg.norm(va) or 1.0)
    normal = np.array([-na[1], na[0]])
    pa1 = np.array([a1["x_mm"], a1["y_mm"]])
    pa2 = np.array([a2["x_mm"], a2["y_mm"]])
    pb1 = np.array([b1["x_mm"], b1["y_mm"]])
    pb2 = np.array([b2["x_mm"], b2["y_mm"]])
    gaps = [abs(float(np.dot(p - pa1, normal))) for p in (pb1, pb2)]
    # 沿 a 方向的投影重叠
    ta_ = [(0.0, float(np.linalg.norm(pa2 - pa1)))]
    proj = [float(np.dot(p - pa1, na)) for p in (pb1, pb2)]
    lo, hi = max(0.0, min(proj)), min(ta_[0][1], max(proj))
    overlap = max(0.0, hi - lo) / max(ta_[0][1], 1e-9)
    return sum(gaps) / 2.0, overlap


def identify_diff_pairs(geo, max_gap_mm=1.0, min_gap_mm=0.05, angle_tol_deg=8.0,
                        min_overlap=0.3, terminal_tol_mm=0.02):
    """从 PCB_GEOMETRY 里按几何找耦合对。

    判定（全部基于几何，net 名只加分）：
      * 同层；
      * 走向夹角 < angle_tol_deg；
      * 中线间距 ∈ [min_gap, max_gap]；
      * 沿走线方向的投影重叠 > min_overlap。
    输出每对还带：长度/失配、过孔不对称、置信度与依据。
    """
    pairs = []
    for net_a, na in (geo.get("nets") or {}).items():
        segs_a = [(s, _seg_vec(s, na.get("terminals") or {}))
                  for s in na.get("segments") or []]
        for net_b, nb in (geo.get("nets") or {}).items():
            if net_b <= net_a:
                continue
            segs_b = [(s, _seg_vec(s, nb.get("terminals") or {}))
                      for s in nb.get("segments") or []]
            best = None
            for sa, va in segs_a:
                if not va:
                    continue
                for sb, vb in segs_b:
                    if not vb:
                        continue
                    if sa["layer"] != sb["layer"]:
                        continue
                    ang = abs(_angle_deg(va[0]) - _angle_deg(vb[0]))
                    ang = min(ang, 180.0 - ang)
                    if ang > angle_tol_deg:
                        continue
                    gap, overlap = _parallel_gap(va, vb, None, None)
                    if not (min_gap_mm <= gap <= max_gap_mm) or overlap < min_overlap:
                        continue
                    score = overlap / (1.0 + abs(gap - (min_gap_mm + max_gap_mm) / 2))
                    if best is None or score > best[0]:
                        best = (score, sa, sb, gap, overlap, ang)
            if not best:
                continue
            _, sa, sb, gap, overlap, ang = best
            len_a = sum(s["length_mm"] for s in na["segments"])
            len_b = sum(s["length_mm"] for s in nb["segments"])
            vias_a = len(na.get("vias") or [])
            vias_b = len(nb.get("vias") or [])
            evidence = [
                f"同层 {sa['layer']}",
                f"走向夹角 {ang:.1f}°",
                f"间距 {gap:.3f} mm（重叠 {overlap * 100:.0f}%）",
            ]
            conf = 0.55 + 0.25 * min(overlap, 1.0) + (0.1 if ang < 2 else 0.0)
            if _name_hint(net_a, net_b):
                evidence.append("net 名含 _P/_N 线索（**仅作辅助**）")
                conf += 0.10
            if abs(len_a - len_b) > 1.0:
                evidence.append(f"长度失配 {abs(len_a - len_b):.2f} mm")
            if vias_a != vias_b:
                evidence.append(f"过孔不对称（{vias_a} vs {vias_b}）")
            pairs.append({
                "net_p": net_a if len_a >= len_b else net_b,
                "net_n": net_b if len_a >= len_b else net_a,
                "layer": sa["layer"],
                "spacing_mm": round(gap, 4),
                "width_p_mm": sa["width_mm"], "width_n_mm": sb["width_mm"],
                "length_p_mm": round(max(len_a, len_b), 4),
                "length_n_mm": round(min(len_a, len_b), 4),
                "mismatch_mm": round(abs(len_a - len_b), 4),
                "vias_p": vias_a, "vias_n": vias_b,
                "overlap": round(overlap, 3),
                "angle_deg": round(ang, 2),
                "confidence": round(min(conf, 0.95), 2),
                "evidence": evidence,
            })
    pairs.sort(key=lambda p: -p["confidence"])
    return {
        "version": 1,
        "source": geo.get("source"),
        "criteria": {"max_gap_mm": max_gap_mm, "min_gap_mm": min_gap_mm,
                     "angle_tol_deg": angle_tol_deg, "min_overlap": min_overlap},
        "note": ("按几何判定，net 名仅作辅助证据；"
                 "间距/长度/过孔都来自 PCB_GEOMETRY，未做场求解"),
        "pairs": pairs,
    }


def _name_hint(a, b):
    """`XXX_P`/`XXX_N`、`_DP`/`_DN`、`+`/`-` 之类的命名线索（只加分不判定）。"""
    import re
    a, b = str(a).upper(), str(b).upper()
    for pa, pb in (("_P", "_N"), ("+", "-"), ("_DP", "_DN"), ("_POS", "_NEG")):
        if a.endswith(pa) and b.endswith(pb):
            return a[: -len(pa)] == b[: -len(pb)]
    m = re.match(r"^(.*?)(\d+)$", a), re.match(r"^(.*?)(\d+)$", b)
    return bool(m[0] and m[1] and m[0].group(1) == m[1].group(1))


def estimate_pairs(geo_report, stackup, length_of=None):
    """给识别出来的差分对补上 Zdiff/时延估计。"""
    out = []
    for p in geo_report.get("pairs") or []:
        try:
            if p["layer"].lower().startswith(("top", "bottom")) or True:
                res = coupled_microstrip(stackup, p["layer"],
                                         max(p["width_p_mm"], p["width_n_mm"]),
                                         p["spacing_mm"], p["length_p_mm"])
        except Exception as e:
            res = {"error": f"{getattr(e, 'code', 'COUPLED_LINE_ERROR')}: {e}"}
        merged = dict(p)
        merged["coupled_line"] = res
        out.append(merged)
    rep = dict(geo_report)
    rep["pairs"] = out
    return rep


def summary_lines(rep):
    out = [f"diff pairs identified: {len(rep.get('pairs') or [])}"]
    for p in (rep.get("pairs") or [])[:6]:
        c = p.get("coupled_line") or {}
        zd = c.get("z_diff_ohm")
        out.append(f"  {p['net_p']}/{p['net_n']} on {p['layer']}: "
                   f"s={p['spacing_mm']} mm, mismatch={p['mismatch_mm']} mm, "
                   f"Zdiff≈{zd} Ω, conf={p['confidence']}")
        out.append(f"    依据: {'; '.join(p['evidence'])}")
    return out
