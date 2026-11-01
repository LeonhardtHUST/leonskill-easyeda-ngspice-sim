#!/usr/bin/env python3
"""v2.4 P0-H(2)：仿真 vs 实测相关分析。

流程（需求文档 §17）：
  时间对齐（可选互相关找时延）→ DC 偏移归一化 → 可选幅度归一化 →
  插值到公共网格 → RMSE / NRMSE / 峰值误差 / 上升时间误差 / 过冲误差。

频域（VNA）走另一条路：|Sij| 与相位逐点比，给 RMS/max 误差与相对误差。

**纪律**：这里的每个数字都是"仿真 vs 实测"，不是"拟合残差"。如果测量本身只用于
拟合（同一份数据），结论必须标 `IN_SAMPLE`，不能当验证。
"""
import json
import os

import numpy as np


class CorrelateError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# 时间域
# --------------------------------------------------------------------------
def resample(t_from, y_from, t_to):
    """线性插值（超界返回 NaN，避免外推造假）。"""
    return np.interp(t_to, t_from, y_from, left=np.nan, right=np.nan)


def _half_crossing(t, y):
    """50% 穿越时刻（阶跃类信号用它算时延，比互相关稳得多）。"""
    if len(y) < 4:
        return None
    lo, hi = float(np.min(y)), float(np.max(y))
    if hi - lo <= 1e-12:
        return None
    thr = lo + 0.5 * (hi - lo)
    for i in range(1, len(y)):
        if (y[i - 1] - thr) * (y[i] - thr) <= 0 and y[i] != y[i - 1]:
            return float(t[i - 1] + (thr - y[i - 1]) * (t[i] - t[i - 1])
                         / (y[i] - y[i - 1]))
    return None


def find_delay(t_sim, y_sim, t_meas, y_meas, max_delay_s=None, coarse=1):
    """估计时延。

    阶跃/脉冲类信号用 50% 穿越差；否则退回互相关。
    （实测：两个几乎相同的阶跃做互相关，峰值永远在 lag=0 —— 除边沿外完全重合。）
    """
    dt = float(np.median(np.diff(t_meas)))
    n = min(len(y_sim), len(y_meas))
    if n < 8:
        return 0.0
    sa, sb = float(np.ptp(y_sim)), float(np.ptp(y_meas))
    if sa > 1e-12 and sb > 1e-12 and abs(sa - sb) / max(sa, sb) < 0.5:
        ca = _half_crossing(t_sim, y_sim)
        cb = _half_crossing(t_meas, y_meas)
        if ca is not None and cb is not None:
            d = cb - ca
            if max_delay_s is None or abs(d) <= max_delay_s:
                return float(d)
    a = (y_sim[:n] - np.mean(y_sim[:n]))
    b = (y_meas[:n] - np.mean(y_meas[:n]))
    a = a[::coarse]
    b = b[::coarse]
    corr = np.correlate(a, b, mode="full")
    lag = int(np.argmax(corr) - (len(b) - 1))
    delay = lag * dt * coarse
    if max_delay_s is not None and abs(delay) > max_delay_s:
        return 0.0
    return float(delay)


def _rise_time(t, y, lo_frac=0.1, hi_frac=0.9):
    if len(y) < 4:
        return None
    lo, hi = np.min(y), np.max(y)
    span = hi - lo
    if span <= 1e-15:
        return None
    lo_t, hi_t = lo + lo_frac * span, lo + hi_frac * span
    i0 = next((i for i in range(1, len(y)) if y[i - 1] < lo_t <= y[i]), None)
    if i0 is None:
        return None
    i1 = next((i for i in range(i0, len(y)) if y[i] >= hi_t), None)
    if i1 is None:
        return None
    return float(t[i1] - t[i0])


def correlate_waveform(sim, meas, spec=None):
    """sim/meas: {"time_s", "voltage"}。返回指标 + 对齐后的曲线（用于画图）。"""
    spec = spec or {}
    t_s = np.asarray(sim["time_s"], dtype=float)
    y_s = np.asarray(sim["voltage"], dtype=float)
    t_m = np.asarray(meas["time_s"], dtype=float)
    y_m = np.asarray(meas["voltage"], dtype=float)

    # 1) 时间窗
    t0 = spec.get("t0", max(t_s.min(), t_m.min()))
    t1 = spec.get("t1", min(t_s.max(), t_m.max()))
    if not (t1 > t0):
        raise CorrelateError("CORR_WINDOW_INVALID",
                             f"仿真与测量的时间窗不重叠（sim {t_s.min():.3g}..{t_s.max():.3g}, "
                             f"meas {t_m.min():.3g}..{t_m.max():.3g}）")

    # 2) 时延对齐
    delay = spec.get("delay_s")
    if delay is None and spec.get("auto_delay", True):
        k = (t_s >= t0) & (t_s <= t1)
        k2 = (t_m >= t0) & (t_m <= t1)
        delay = find_delay(t_s[k], y_s[k], t_m[k2], y_m[k2],
                           spec.get("max_delay_s"))
    delay = float(delay or 0.0)

    # 3) 公共网格
    dt = float(spec.get("dt_s") or np.median(np.diff(t_m)))
    tt = np.arange(t0, t1, dt)
    ys = resample(t_s, y_s, tt + delay)
    ym = resample(t_m, y_m, tt)
    ok = np.isfinite(ys) & np.isfinite(ym)
    if ok.sum() < 8:
        raise CorrelateError("CORR_NO_OVERLAP", "对齐后有效点不足 8 个")
    tt, ys, ym = tt[ok], ys[ok], ym[ok]

    # 4) 归一化
    if spec.get("remove_dc", True):
        ys = ys - np.mean(ys)
        ym = ym - np.mean(ym)
    gain = 1.0
    if spec.get("normalize_amplitude"):
        rs, rm = np.ptp(ys), np.ptp(ym)
        if rm > 1e-15:
            gain = rs / rm
            ym = ym * gain

    # 5) 指标
    err = ys - ym
    rms = float(np.sqrt(np.mean(err ** 2)))
    denom = np.ptp(ym) if np.ptp(ym) > 1e-15 else 1.0
    rt_s = _rise_time(tt, ys)
    rt_m = _rise_time(tt, ym)

    def overshoot(y):
        lo, hi = np.min(y), np.max(y)
        lvl = y[-1]
        span = abs(lvl - lo) if abs(lvl - lo) > 1e-15 else 1.0
        return float(max(0.0, (hi - lvl) / span * 100.0))

    return {
        "n_points": int(len(tt)),
        "window_s": [float(t0), float(t1)],
        "dt_s": dt,
        "delay_s": delay,
        "gain_applied": float(gain),
        "dc_removed": bool(spec.get("remove_dc", True)),
        "rmse_v": rms,
        "nrmse": float(rms / denom),
        "peak_error_v": float(np.max(np.abs(err))),
        "peak_error_pct": float(np.max(np.abs(err)) / denom * 100.0),
        "mean_error_v": float(np.mean(err)),
        "rise_time_sim_s": rt_s,
        "rise_time_meas_s": rt_m,
        "rise_time_error_s": (None if (rt_s is None or rt_m is None)
                              else float(rt_s - rt_m)),
        "overshoot_sim_pct": overshoot(ys),
        "overshoot_meas_pct": overshoot(ym),
        "series": {"t": tt.tolist(), "sim": ys.tolist(), "meas": ym.tolist()},
    }


# --------------------------------------------------------------------------
# 频域
# --------------------------------------------------------------------------
def correlate_sparam(sim_net, meas_net, tol_interp=0.02):
    f_m = np.asarray(meas_net.freq, dtype=float)
    f_s = np.asarray(sim_net.freq, dtype=float)
    lo, hi = max(f_m.min(), f_s.min()), min(f_m.max(), f_s.max())
    if not (hi > lo):
        raise CorrelateError("CORR_BAND_MISMATCH", "仿真与测量的频段不重叠")
    P = min(sim_net.nports, meas_net.nports)
    keep = (f_m >= lo) & (f_m <= hi)
    f = f_m[keep]
    mag_err, ph_err = [], []
    for i in range(P):
        for j in range(P):
            ref = meas_net.s[keep, i, j]
            idx = [int(np.argmin(np.abs(f_s - fk))) for fk in f]
            got = sim_net.s[idx, i, j]
            mag_err.append(20 * np.log10(np.maximum(np.abs(got), 1e-30))
                           - 20 * np.log10(np.maximum(np.abs(ref), 1e-30)))
            d = np.angle(got) - np.angle(ref)
            ph_err.append(np.degrees(np.mod(d + np.pi, 2 * np.pi) - np.pi))
    mag = np.concatenate(mag_err)
    ph = np.concatenate(ph_err)
    return {
        "domain": "sparam",
        "n_points": int(len(f)),
        "band_hz": [float(lo), float(hi)],
        "nports_compared": P,
        "rms_mag_error_db": float(np.sqrt(np.mean(mag ** 2))),
        "max_mag_error_db": float(np.max(np.abs(mag))),
        "rms_phase_error_deg": float(np.sqrt(np.mean(ph ** 2))),
        "max_phase_error_deg": float(np.max(np.abs(ph))),
    }


# --------------------------------------------------------------------------
def report(metrics, meta=None, out=None):
    rep = {"version": 1, "kind": "sim_vs_measurement",
           "meta": meta or {}, "metrics": metrics,
           "note": ("这是仿真与实测的偏差，不是模型拟合残差。"
                    "若测量数据同时用于参数拟合，请把 sample 标为 IN_SAMPLE。")}
    if out:
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
    return rep


def summary_lines(m):
    if m.get("domain") == "sparam":
        return [f"correlation (S-param, {m['n_points']} pts "
                f"{m['band_hz'][0]:.4g}..{m['band_hz'][1]:.4g} Hz)",
                f"  |S| error rms {m['rms_mag_error_db']:.3f} dB / "
                f"max {m['max_mag_error_db']:.3f} dB",
                f"  phase error rms {m['rms_phase_error_deg']:.2f}° / "
                f"max {m['max_phase_error_deg']:.2f}°"]
    return [f"correlation (waveform, {m['n_points']} pts, delay {m['delay_s'] * 1e9:.2f} ns)",
            f"  RMSE {m['rmse_v'] * 1e3:.3f} mV (NRMSE {m['nrmse'] * 100:.2f}%)",
            f"  peak error {m['peak_error_v'] * 1e3:.3f} mV ({m['peak_error_pct']:.2f}%)",
            f"  rise time: sim {_ps(m['rise_time_sim_s'])} meas {_ps(m['rise_time_meas_s'])}",
            f"  overshoot: sim {m['overshoot_sim_pct']:.1f}% "
            f"meas {m['overshoot_meas_pct']:.1f}%"]


def _ps(t):
    return f"{t * 1e12:.1f} ps" if t else "?"
