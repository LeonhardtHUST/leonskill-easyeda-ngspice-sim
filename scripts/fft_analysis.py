#!/usr/bin/env python3
"""v2.3 P0-F：FFT / 频谱分析（含 window 与单位纪律）。

单位纪律（需求文档 §13 明确要求）：
  dBV      20·log10(V_amplitude / 1 V)        —— 只需要电压
  dBFS     20·log10(V_amplitude / full_scale) —— 需要 Vpp/Vref
  dBc      20·log10(V_h / V_fundamental)      —— 需要基波
  dBm      10·log10(P / 1 mW)，P = V²/(2R)    —— **必须给阻抗**，否则拒绝输出
  dBm/Hz   dBm − 10·log10(ENBW)               —— 同样必须给阻抗

窗口函数用**幅度归一化**（除以 sum(w)），保证单音幅度 A 在频谱里读出来还是 A，
否则不同窗之间不可比。ENBW 一并给出，用于噪声密度换算。
"""
import cmath
import math

WINDOWS = ("rect", "rectangular", "hann", "hanning", "blackman-harris",
           "blackmanharris", "bh")


def _window(name, n):
    name = (name or "hann").lower()
    if name in ("rect", "rectangular", "none"):
        return [1.0] * n, "rect"
    if name in ("hann", "hanning"):
        return [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)], "hann"
    if name in ("blackman-harris", "blackmanharris", "bh"):
        w = []
        for i in range(n):
            x = 2 * math.pi * i / (n - 1)
            w.append(0.35875 - 0.48829 * math.cos(x) + 0.14128 * math.cos(2 * x)
                     - 0.01168 * math.cos(3 * x))
        return w, "blackman-harris"
    raise ValueError(f"未知窗函数 {name!r}（支持 rect / hann / blackman-harris）")


def _resample(xs, ys, n):
    """线性插值到 n 点均匀网格（非均匀 tran 采样必须先重采样）。"""
    t0, t1 = xs[0], xs[-1]
    if t1 <= t0:
        raise ValueError("时间窗口无效")
    dt = (t1 - t0) / (n - 1)
    out, j = [], 0
    for i in range(n):
        t = t0 + i * dt
        while j < len(xs) - 2 and xs[j + 1] < t:
            j += 1
        x0, x1 = xs[j], xs[min(j + 1, len(xs) - 1)]
        y0, y1 = ys[j], ys[min(j + 1, len(ys) - 1)]
        out.append(y0 if x1 == x0 else y0 + (y1 - y0) * (t - x0) / (x1 - x0))
    return dt, out


def spectrum(xs, ys, nfft=1024, window="hann", remove_dc=True, single_sided=True):
    """时间序列 → 频谱（幅度已按窗归一化）。"""
    if len(xs) < 8:
        raise ValueError(f"采样点太少（{len(xs)}），无法做 FFT")
    dt, u = _resample(xs, ys, nfft)
    if remove_dc:
        mean = sum(u) / len(u)
        u = [v - mean for v in u]
    w, wname = _window(window, nfft)
    wsum = sum(w)
    x = [u[i] * w[i] for i in range(nfft)]
    # 直接 DFT（nfft 通常 ≤ 4096，纯 Python 够用，避免 numpy 依赖）
    half = nfft // 2 if single_sided else nfft
    scale = 2.0 / wsum if single_sided else 1.0 / wsum
    freqs, mags = [], []
    for k in range(half + (0 if single_sided else -1) + 1):
        acc = 0j
        for i in range(nfft):
            acc += x[i] * cmath.exp(-2j * math.pi * k * i / nfft)
        freqs.append(k / (nfft * dt))
        mags.append(abs(acc) * scale)
    # 直流与 Nyquist bin 不该乘 2
    if single_sided and mags:
        mags[0] /= 2.0
        if nfft % 2 == 0 and len(mags) > 1:
            mags[-1] /= 2.0
    enbw_bins = (wsum ** 2) / sum(v * v for v in w) if sum(v * v for v in w) else 1.0
    return {
        "freqs_hz": freqs,
        "mag_v": mags,
        "window": wname,
        "nfft": nfft,
        "dt_s": dt,
        "bin_hz": 1.0 / (nfft * dt),
        "enbw_hz": enbw_bins * (1.0 / (nfft * dt)),
        "remove_dc": bool(remove_dc),
        "single_sided": bool(single_sided),
    }


def to_dbv(spec, floor_db=-300.0):
    return [20 * math.log10(max(m, 1e-15)) for m in spec["mag_v"]]


def to_dbfs(spec, full_scale_v):
    if not full_scale_v or full_scale_v <= 0:
        raise ValueError("dBFS 需要 full_scale（Vpp 或 Vref），未提供则拒绝输出")
    return [20 * math.log10(max(m, 1e-15) / full_scale_v) for m in spec["mag_v"]]


def to_dbc(spec, f0, tol=None):
    """相对基波的 dB。"""
    f0_bin = _nearest_bin(spec, f0, tol)
    ref = spec["mag_v"][f0_bin] if f0_bin is not None and spec["mag_v"][f0_bin] > 0 else None
    if not ref:
        raise ValueError(f"基波 {f0:g} Hz 处没有能量，无法给 dBc")
    return [20 * math.log10(max(m, 1e-15) / ref) for m in spec["mag_v"]]


def to_dbm(spec, impedance_ohm):
    """只有给了阻抗才允许 dBm（V²/(2R) 假定正弦、峰值幅度）。"""
    if not impedance_ohm or impedance_ohm <= 0:
        raise ValueError(
            "dBm 需要负载阻抗（V²/2R → mW）。任意 SPICE 电压频谱不能直接标 dBm，"
            "请提供 impedance_ohm")
    out = []
    for m in spec["mag_v"]:
        v = m / math.sqrt(2.0) if False else m          # 幅度 → RMS 用 /√2
        v_rms = m / math.sqrt(2.0)
        p_w = (v_rms ** 2) / impedance_ohm
        out.append(10 * math.log10(max(p_w, 1e-30) / 1e-3))
    return out


def to_dbm_per_hz(spec, impedance_ohm):
    dbm = to_dbm(spec, impedance_ohm)
    corr = 10 * math.log10(spec["enbw_hz"]) if spec["enbw_hz"] > 0 else 0.0
    return [v - corr for v in dbm]


def _nearest_bin(spec, f, tol=None):
    if not spec["freqs_hz"]:
        return None
    binw = spec["bin_hz"]
    tol = tol if tol is not None else binw * 1.5
    best, bd = None, None
    for i, fr in enumerate(spec["freqs_hz"]):
        d = abs(fr - f)
        if d <= tol and (bd is None or d < bd):
            best, bd = i, d
    return best


def peak_at(spec, f, tol=None, interpolate=True):
    """取 f 附近的峰（可用抛物线插值提高分辨率）。"""
    i = _nearest_bin(spec, f, tol)
    if i is None:
        return None
    mags = spec["mag_v"]
    a, b, c = (mags[max(i - 1, 0)], mags[i], mags[min(i + 1, len(mags) - 1)])
    amp, freq = b, spec["freqs_hz"][i]
    if interpolate and a > 0 and c > 0 and a + c > 0:
        denom = (a - 2 * b + c)
        if abs(denom) > 1e-30:
            delta = 0.5 * (a - c) / denom
            if -0.5 < delta < 0.5:
                freq = freq + delta * spec["bin_hz"]
                amp = b - 0.25 * (a - c) * delta
    return {"f_hz": freq, "mag_v": amp,
            "dbv": 20 * math.log10(max(amp, 1e-15))}


def from_plot(plot, node, t0=None, t1=None, **kw):
    """直接从 parse_raw 的 plot 取某节点的时域数据（t0/t1 选窗口）。"""
    import metrics as metrics_mod
    xs, ys = metrics_mod._xy(plot, node)
    if t0 is not None or t1 is not None:
        ox, oy = [], []
        for x, y in zip(xs, ys):
            if t0 is not None and x < t0:
                continue
            if t1 is not None and x > t1:
                continue
            ox.append(x)
            oy.append(y)
        xs, ys = ox, oy
    return xs, ys


def summary(spec, top=5):
    dbv = to_dbv(spec)
    order = sorted(range(1, len(dbv)), key=lambda i: -dbv[i])[:top]
    lines = [f"window={spec['window']} nfft={spec['nfft']} bin={spec['bin_hz']:.3g} Hz "
             f"ENBW={spec['enbw_hz']:.3g} Hz"]
    for i in order:
        lines.append(f"  {spec['freqs_hz'][i] / 1e6:.4g} MHz : {dbv[i]:.2f} dBV")
    return lines
