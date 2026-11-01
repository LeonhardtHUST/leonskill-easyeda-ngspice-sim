#!/usr/bin/env python3
"""v2.3 P0-F(2)：谐波自动识别 + 互调（birdies）候选匹配。

目标：给一个 f0（PWM / buck / clock），自动报出
  期望频率、实测最近峰、幅度、dBc
再给多个已知频率（f_clk / f_sw / f_pwm），生成 |n·f1 ± m·f2| 候选并与实测峰匹配——
SDR 板上的 birdie 往往就是这么来的。
"""
import math

import fft_analysis as fft


def harmonics(spec, f0, n=8, tol_bins=2.0, include_dc=False):
    """逐次谐波匹配。返回 [{harmonic, expected_hz, measured_hz, mag_v, dbv, dbc, offset_hz}]"""
    dbv_all = fft.to_dbv(spec)
    ref = fft.peak_at(spec, f0, tol=tol_bins * spec["bin_hz"])
    ref_dbv = ref["dbv"] if ref else None
    out = []
    start = 0 if include_dc else 1
    for h in range(start, n + 1):
        f = f0 * h
        if f > spec["freqs_hz"][-1] * 1.001:
            break
        p = fft.peak_at(spec, f, tol=tol_bins * spec["bin_hz"])
        rec = {"harmonic": h, "expected_hz": f}
        if p:
            rec.update({"measured_hz": p["f_hz"], "mag_v": p["mag_v"],
                        "dbv": p["dbv"], "offset_hz": p["f_hz"] - f,
                        "found": True})
            if ref_dbv is not None:
                rec["dbc"] = p["dbv"] - ref_dbv
        else:
            rec.update({"found": False,
                        "note": f"±{tol_bins * spec['bin_hz']:g} Hz 内没有峰"
                                f"（分辨率不足时可增大 nfft）"})
        out.append(rec)
    return out


def intermod_candidates(freqs, max_order=3, f_max=None, limit=200):
    """生成 |n·f1 ± m·f2| 候选（n,m ≤ max_order，n+m ≥ 2）。"""
    freqs = [f for f in freqs if f and f > 0]
    cands = []
    for i in range(len(freqs)):
        for j in range(i + 1, len(freqs)):
            f1, f2 = freqs[i], freqs[j]
            for n in range(1, max_order + 1):
                for m in range(1, max_order + 1):
                    if n + m < 2:
                        continue
                    for sign in (+1, -1):
                        f = abs(n * f1 + sign * m * f2)
                        if f <= 0 or (f_max and f > f_max):
                            continue
                        cands.append({
                            "f_hz": f, "n": n, "m": m, "sign": "+" if sign > 0 else "-",
                            "f1": f1, "f2": f2,
                            "formula": f"|{n}×{f1 / 1e6:g}MHz "
                                       f"{'+' if sign > 0 else '−'} {m}×{f2 / 1e6:g}MHz|",
                            "order": n + m,
                        })
    cands.sort(key=lambda c: (c["f_hz"], c["order"]))
    # 去重（同一频率多个组合只留最低阶）
    seen, uniq = set(), []
    for c in cands:
        key = round(c["f_hz"] / 1e3)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq[:limit]


def match_candidates(spec, cands, tol_bins=1.5, min_dbv=None):
    """把候选频率与实测峰匹配上。"""
    out = []
    for c in cands:
        p = fft.peak_at(spec, c["f_hz"], tol=tol_bins * spec["bin_hz"])
        if not p:
            continue
        if min_dbv is not None and p["dbv"] < min_dbv:
            continue
        rec = dict(c)
        rec.update({"measured_hz": p["f_hz"], "dbv": p["dbv"], "mag_v": p["mag_v"],
                    "offset_hz": p["f_hz"] - c["f_hz"]})
        out.append(rec)
    out.sort(key=lambda r: -r["dbv"])
    return out


def peak_list(spec, n=10, min_rel_db=-80.0):
    """全局最强峰（不含 DC）。"""
    dbv = fft.to_dbv(spec)
    if not dbv:
        return []
    top = max(dbv[1:]) if len(dbv) > 1 else dbv[0]
    idx = sorted(range(1, len(dbv)), key=lambda i: -dbv[i])[:n]
    return [{"f_hz": spec["freqs_hz"][i], "dbv": dbv[i], "mag_v": spec["mag_v"][i],
             "rel_db": dbv[i] - top} for i in idx if dbv[i] - top >= min_rel_db]


def summary_lines(harm, cands=None, limit=8):
    lines = []
    for h in harm[:limit]:
        if h.get("found"):
            lines.append(f"  H{h['harmonic']:<2} {h['expected_hz'] / 1e6:9.4f} MHz → "
                         f"{h['measured_hz'] / 1e6:9.4f} MHz  {h['dbv']:7.1f} dBV"
                         + (f"  {h['dbc']:6.1f} dBc" if "dbc" in h else ""))
        else:
            lines.append(f"  H{h['harmonic']:<2} {h['expected_hz'] / 1e6:9.4f} MHz → "
                         f"not found")
    if cands:
        lines.append("  intermodulation candidates matched:")
        for c in cands[:limit]:
            lines.append(f"    {c['formula']} = {c['f_hz'] / 1e6:.4f} MHz → "
                         f"{c['measured_hz'] / 1e6:.4f} MHz {c['dbv']:.1f} dBV "
                         f"(order {c['order']})")
    return lines
