#!/usr/bin/env python3
"""v2.3 P1：Touchstone 指标（S11/S21、插损、回损、-3 dB 带宽、谐振、峰值衰减）。

这些指标直接从 .sNp 数据算，不需要 ngspice —— 这也是"用实测数据校准模型"的第一步：
先看器件本身长什么样，再谈电路里表现如何。
"""
import cmath
import math


def _db(x):
    return 20 * math.log10(max(abs(x), 1e-30))


def _local_peaks(vals, freqs, min_db=None):
    out = []
    for i in range(1, len(vals) - 1):
        if vals[i] > vals[i - 1] and vals[i] >= vals[i + 1]:
            if min_db is None or vals[i] >= min_db:
                out.append({"f": freqs[i], "value": vals[i]})
    return out


def impedance_1port(ts):
    """S11 → Z（1 端口）。"""
    out = []
    for k, f in enumerate(ts.frequencies):
        s11 = ts.s[k][0][0]
        z = ts.z0 * (1 + s11) / (1 - s11) if abs(1 - s11) > 1e-12 else complex(1e12, 0)
        out.append({"f": f, "z": z, "mag": abs(z)})
    return out


def metrics(ts):
    """返回结构化指标 dict（2 端口给插损/回损/带宽，1 端口给 |Z|）。"""
    out = {"file": ts.name, "num_ports": ts.num_ports,
           "n_points": len(ts.frequencies), "z0": ts.z0}
    f = ts.frequencies
    if not f:
        return out
    out["f_start_hz"] = f[0]
    out["f_stop_hz"] = f[-1]

    if ts.num_ports == 1:
        zs = impedance_1port(ts)
        mags = [z["mag"] for z in zs]
        imax = max(range(len(mags)), key=lambda i: mags[i])
        out.update({
            "s11_db": ts.db(0, 0),
            "return_loss_db": -min(ts.db(0, 0)),
            "worst_return_loss_db": -max(ts.db(0, 0)),
            "|z|_ohm": mags,
            "z_max_ohm": mags[imax], "z_max_f": f[imax],
            "z_min_ohm": min(mags), "z_min_f": f[mags.index(min(mags))],
            "resonances": _local_peaks(mags, f),
        })
        return out

    # 2 端口
    s21 = ts.db(1, 0)
    s11 = ts.db(0, 0)
    s12 = ts.db(0, 1)
    s22 = ts.db(1, 1)
    p = ts.num_ports
    out.update({"s11_db": s11, "s21_db": s21, "s12_db": s12, "s22_db": s22,
                "s_parameters": {f"S{i + 1}{j + 1}": ts.db(i, j)
                                 for i in range(p) for j in range(p)}})
    peak = max(s21)
    ipk = s21.index(peak)
    out["insertion_loss_db"] = -peak                      # 最小损耗（passband 里最好的一点）
    out["peak_s21_db"] = peak
    out["peak_s21_f"] = f[ipk]
    out["peak_attenuation_db"] = -min(s21)                # 最大衰减
    out["stopband_f"] = f[s21.index(min(s21))]
    out["return_loss_db"] = -min(s11)                     # 最好的回损
    out["worst_return_loss_db"] = -max(s11)
    out["is_reciprocal"] = all(abs(abs(ts.s[k][1][0]) - abs(ts.s[k][0][1])) < 1e-3
                               for k in range(len(f)))
    # -3 dB 带宽（相对峰值）
    thr = peak - 3.0
    above = [i for i in range(len(s21)) if s21[i] >= thr]
    if above:
        lo, hi = above[0], above[-1]
        bw = {"f_low": f[lo], "f_high": f[hi], "bandwidth": f[hi] - f[lo],
              "definition": "peak - 3 dB"}
        if lo == 0:
            bw["note"] = "低端未回落到 -3 dB，可能是低通/高通而非带通"
        if hi == len(f) - 1:
            bw["note"] = bw.get("note", "") + "；高端未回落到 -3 dB"
        out["bandwidth_3db"] = bw
    # 谐振：S21 的局部极大（带通）与局部极小（陷波）
    out["resonances"] = _local_peaks(s21, f)
    notches = [{"f": f[i], "value": s21[i]} for i in range(1, len(s21) - 1)
               if s21[i] < s21[i - 1] and s21[i] <= s21[i + 1]]
    out["notches"] = notches
    return out


def compare(models, reference="file"):
    """A/B：把若干模型的 S21 曲线放在一起对比（.s2p vs lumped）。"""
    ref = models.get(reference)
    if ref is None:
        return {}
    out = {"reference": reference, "items": []}
    for name, m in models.items():
        if m is None:
            continue
        row = {"model": name, "max_err_db": None, "rms_err_db": None}
        if "s21_db" in m and "s21_db" in ref and len(m["s21_db"]) == len(ref["s21_db"]):
            errs = [a - b for a, b in zip(m["s21_db"], ref["s21_db"])]
            row["max_err_db"] = max(abs(e) for e in errs)
            row["rms_err_db"] = math.sqrt(sum(e * e for e in errs) / len(errs))
        out["items"].append(row)
    return out


def summary_lines(m, limit=6):
    lines = [f"{m['file']}: {m['num_ports']}-port, {m['n_points']} pts, "
             f"{m.get('f_start_hz', 0):g}..{m.get('f_stop_hz', 0):g} Hz"]
    if m["num_ports"] == 1:
        lines.append(f"  |Z|max = {m.get('z_max_ohm', 0):.4g} Ω @ {m.get('z_max_f', 0):g} Hz")
        lines.append(f"  return loss = {m.get('return_loss_db', 0):.2f} dB "
                     f"(worst {m.get('worst_return_loss_db', 0):.2f} dB)")
    else:
        lines.append(f"  insertion loss = {m.get('insertion_loss_db', 0):.2f} dB "
                     f"@ {m.get('peak_s21_f', 0):g} Hz")
        lines.append(f"  return loss = {m.get('return_loss_db', 0):.2f} dB "
                     f"(worst {m.get('worst_return_loss_db', 0):.2f} dB)")
        bw = m.get("bandwidth_3db")
        if bw:
            lines.append(f"  -3 dB BW = {bw['bandwidth'] / 1e6:.3f} MHz "
                         f"({bw['f_low'] / 1e6:.3f}..{bw['f_high'] / 1e6:.3f} MHz)")
        if m.get("resonances"):
            lines.append("  resonances: " + ", ".join(
                f"{r['f'] / 1e6:.2f} MHz" for r in m["resonances"][:limit]))
    return lines
