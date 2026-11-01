#!/usr/bin/env python3
"""v2.2 P1-B：从 raw 波形自动提取可量化指标。

v2.1 只把 raw 导成 JSON/CSV，agent 得自己长眼睛看波形。v2.2 把这些判断
变成数字：过冲/下冲/上升/下降、阈值穿越次数（振铃）、skew、
PDN 阻抗 |Z(f)| 与谐振峰、PWM 谐波。

设计约束：只用标准库（本机 python 未必装 numpy），对几千点级别的
tran/ac 结果完全够用。

指标 spec 形态见 SKILL.md「Metrics」一节；核心支持：

  {"metric": "tran_stats", "node": "CLK_ADC"}
  {"metric": "threshold_crossings", "node": "CLK_ADC", "threshold": 1.65}
  {"metric": "time_delta", "a": "CLK_ADC@1.65", "b": "CLK_FPGA@1.65"}
  {"metric": "impedance", "node": "AVDD", "amplitude": 1}
  {"metric": "fft_harmonics", "node": "MAIN_RAIL", "f0": 1000, "n": 5}
"""
import cmath
import math


class MetricError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _norm(name):
    return str(name or "").strip().lower().replace("$", "").lstrip("_")


def find_var(plot, name):
    """在 plot 里定位变量：支持 'AVDD' / '$AVDD' / 'v(avdd)' / 'I(V1)'。"""
    want = str(name or "").strip()
    low = want.lower()
    vars_ = [v.lower() for v in plot.get("vars") or []]
    if low in vars_:
        return vars_.index(low)
    cands = []
    if low.startswith(("v(", "i(", "v$", "i$")):
        cands.append(low)
    base = _norm(want)
    cands += [f"v({base})", base, f"i({base})", f"v({low})", f"i({low})"]
    try:                                  # 3V3 → n_3v3（spice_node 会给数字开头补 N_）
        from sim_config import spice_node
        sn = spice_node(want).lower()
        cands += [f"v({sn})", f"i({sn})", sn]
    except Exception:
        pass
    for c in cands:
        if c in vars_:
            return vars_.index(c)
    # 宽松兜底：忽略大小写与括号里的 $/__
    for i, v in enumerate(vars_):
        if _norm(v.strip("vi()")) == base:
            return i
    raise MetricError("METRIC_NODE_NOT_FOUND",
                      f"raw 中没有变量 {name!r}（现有: {plot.get('vars')}）")


def _xval(row):
    """raw 的 X 轴：complex plot 里连频率也成对存储（[f, 0]），要拆开。"""
    x = row[0]
    if isinstance(x, list):
        return float(x[0])
    return float(x)


def _xy(plot, name):
    idx = find_var(plot, name)
    xs, ys = [], []
    for row in plot.get("points") or []:
        x = _xval(row)
        y = row[idx]
        if isinstance(y, list):
            y = math.hypot(float(y[0]), float(y[1]))   # complex（AC）取模
        xs.append(float(x))
        ys.append(float(y))
    return xs, ys


def _window(xs, ys, spec):
    t0 = spec.get("t0")
    t1 = spec.get("t1")
    if t0 is None and t1 is None:
        return xs, ys
    lo = float(t0) if t0 is not None else None
    hi = float(t1) if t1 is not None else None
    ox, oy = [], []
    for x, y in zip(xs, ys):
        if lo is not None and x < lo:
            continue
        if hi is not None and x > hi:
            continue
        ox.append(x)
        oy.append(y)
    if len(ox) < 2:
        raise MetricError("METRIC_WINDOW_EMPTY", f"时间窗口 ({t0}, {t1}) 内没有采样点")
    return ox, oy


def _percentile(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    f = int(math.floor(k))
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _crossings(xs, ys, threshold, rising_only=None):
    out = []
    for i in range(1, len(xs)):
        a, b = ys[i - 1], ys[i]
        if a == b:
            continue
        if (a - threshold) * (b - threshold) > 0:
            continue
        rising = b > a
        if rising_only is True and not rising:
            continue
        if rising_only is False and rising:
            continue
        # 线性插值到穿越时刻
        t = xs[i - 1] + (threshold - a) * (xs[i] - xs[i - 1]) / (b - a)
        out.append({"t": t, "dir": "rise" if rising else "fall"})
    return out


def _tran_stats(xs, ys, spec):
    v_low = spec.get("v_low")
    v_high = spec.get("v_high")
    n = len(ys)
    tail = ys[int(n * 0.8):] or ys
    ref_high = float(v_high) if v_high is not None else _percentile(tail, 90)
    ref_low = float(v_low) if v_low is not None else _percentile(tail, 10)
    amax, amin = max(ys), min(ys)
    pp = amax - amin
    mean = sum(ys) / n
    rms = math.sqrt(sum(y * y for y in ys) / n)
    span = ref_high - ref_low
    out = {
        "n_points": n,
        "max": amax, "min": amin, "peak_to_peak": pp,
        "mean": mean, "rms": rms,
        "final": ys[-1],
        "ref_low": ref_low, "ref_high": ref_high,
        "ref_source": "explicit" if (v_low is not None or v_high is not None) else "p10/p90",
    }
    if span > 1e-12:
        # 过冲/下冲只对"摆幅可观"的信号有意义；DC 电源轨的 pp 只有几 mV，
        # 拿它当 100% 会造成 120% 这种荒谬数字，所以改成纹波百分比。
        level = max(abs(amax), abs(amin), abs(mean), 1e-12)
        # 判定"这是跳变信号还是带纹波的直流轨"：纹波 < 电平的 10% 视为电源轨
        edge_like = (v_high is not None and v_low is not None) or span > 0.1 * level
        out["signal_kind"] = "edge" if edge_like else "rail"
        if edge_like:
            out["overshoot_pct"] = max(0.0, (amax - ref_high) / span * 100.0)
            out["undershoot_pct"] = max(0.0, (ref_low - amin) / span * 100.0)
            out["rise_time"] = _edge_time(xs, ys, ref_low + 0.1 * span,
                                          ref_low + 0.9 * span, True)
            out["fall_time"] = _edge_time(xs, ys, ref_high - 0.1 * span,
                                          ref_high - 0.9 * span, False)
        else:
            out["ripple_pct"] = pp / abs(mean) * 100.0 if abs(mean) > 1e-15 else None
            out["deviation_pct"] = (amax - mean) / abs(mean) * 100.0 if abs(mean) > 1e-15 else None
    return out


def _edge_time(xs, ys, lo, hi, rising):
    if rising:
        i0 = next((i for i in range(1, len(ys)) if ys[i - 1] < lo <= ys[i]), None)
        if i0 is None:
            return None
        i1 = next((i for i in range(i0, len(ys)) if ys[i] >= hi), None)
    else:
        i0 = next((i for i in range(1, len(ys)) if ys[i - 1] > lo >= ys[i]), None)
        if i0 is None:
            return None
        i1 = next((i for i in range(i0, len(ys)) if ys[i] <= hi), None)
    if i1 is None:
        return None
    return xs[i1] - xs[i0]


def _threshold_crossings(xs, ys, spec):
    thr = spec.get("threshold")
    if thr is None:
        raise MetricError("METRIC_PARAM_INVALID", "threshold_crossings 需要 threshold")
    cr = _crossings(xs, ys, float(thr))
    rises = [c for c in cr if c["dir"] == "rise"]
    falls = [c for c in cr if c["dir"] == "fall"]

    # 把穿越按"同一条边沿"聚类：同一次跳变里的多次穿越（振铃）间隔远小于
    # 相邻两次跳变的间隔，所以用中位间隔的 1/4 作为聚类门限。
    gaps = [cr[i + 1]["t"] - cr[i]["t"] for i in range(len(cr) - 1)]
    gap = spec.get("cluster_gap")
    if gap is None:
        # 用"最大间隔"（≈一个时钟周期）的 1/4 作为聚类门限：
        # 同一次边沿内部的振铃重穿越间隔远小于 1/4 周期，而相邻边沿必然大于它。
        gap = 0.25 * max(gaps) if gaps else 0.0
    clusters = []
    for c in cr:
        if clusters and (c["t"] - clusters[-1][-1]["t"]) <= gap:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    multi = [cl for cl in clusters if len(cl) > 1]
    return {
        "threshold": float(thr),
        "total": len(cr),
        "rises": len(rises),
        "falls": len(falls),
        "edges": len(clusters),
        "crossings_per_edge": (len(cr) / len(clusters)) if clusters else 0.0,
        "ringing": bool(multi),
        "ringing_edges": len(multi),
        "max_crossings_in_edge": max((len(cl) for cl in clusters), default=0),
        "cluster_gap": gap,
        "first_crossings": [{"t": c["t"], "dir": c["dir"]} for c in cr[:8]],
        "first_rise": rises[0]["t"] if rises else None,
        "first_fall": falls[0]["t"] if falls else None,
    }


def _parse_node_at(s):
    s = str(s)
    if "@" in s:
        node, thr = s.rsplit("@", 1)
        return node.strip(), float(thr)
    raise MetricError("METRIC_PARAM_INVALID", f"time_delta 端点需要写成 NODE@阈值，收到 {s!r}")


def _time_delta(plot, spec):
    a = spec.get("a")
    b = spec.get("b")
    if not a or not b:
        raise MetricError("METRIC_PARAM_INVALID", "time_delta 需要 a 与 b")
    na, ta = _parse_node_at(a)
    nb, tb = _parse_node_at(b)
    xa, ya = _xy(plot, na)
    xb, yb = _xy(plot, nb)
    ca = _crossings(xa, ya, ta, rising_only=True)
    cb = _crossings(xb, yb, tb, rising_only=True)
    t0, t1 = spec.get("t0"), spec.get("t1")
    if t0 is not None:
        ca = [c for c in ca if c["t"] >= float(t0)]
        cb = [c for c in cb if c["t"] >= float(t0)]
    if t1 is not None:
        ca = [c for c in ca if c["t"] <= float(t1)]
        cb = [c for c in cb if c["t"] <= float(t1)]
    if not ca or not cb:
        raise MetricError("METRIC_NO_CROSSING",
                          f"{a} 或 {b} 在仿真窗口内没有上升沿穿越")
    return {"a": a, "b": b, "t_a": ca[0]["t"], "t_b": cb[0]["t"],
            "skew": cb[0]["t"] - ca[0]["t"],
            "skew_abs": abs(cb[0]["t"] - ca[0]["t"])}


def _impedance(plot, spec):
    node = spec.get("node")
    amp = float(spec.get("amplitude", 1.0) or 1.0)
    idx = find_var(plot, node)
    xs = [_xval(row) for row in plot["points"]]
    zs = []
    for row in plot["points"]:
        v = row[idx]
        if isinstance(v, list):
            re_, im_ = float(v[0]), float(v[1])
        else:
            re_, im_ = float(v), 0.0
        zs.append(math.hypot(re_, im_) / abs(amp))
    if not zs:
        raise MetricError("METRIC_EMPTY", f"{node} 没有 AC 数据")
    imax = max(range(len(zs)), key=lambda i: zs[i])
    out = {"node": node, "amplitude": amp,
           "max_impedance": zs[imax], "peak_frequency": xs[imax],
           "min_impedance": min(zs),
           "z_at": [{"f": xs[i], "z": zs[i]} for i in range(len(zs))],
           "n_points": len(zs)}
    for f in spec.get("at") or []:
        z = _interp_log(xs, zs, float(f))
        if z is not None:
            out[f"z_at_{float(f):g}"] = z
    return out


def _interp_log(xs, zs, f):
    """在 log-log 上线性插值取某个频点的 |Z|（明显超出范围返回 None）。"""
    if not xs:
        return None
    if f < xs[0] * 0.999 or f > xs[-1] * 1.001:
        return None
    for i in range(1, len(xs)):
        if xs[i] >= f:
            x0, x1, y0, y1 = xs[i - 1], xs[i], zs[i - 1], zs[i]
            if x0 <= 0 or f <= 0 or y0 <= 0 or y1 <= 0:
                return y0 + (y1 - y0) * (f - x0) / (x1 - x0) if x1 != x0 else y0
            t = (math.log10(f) - math.log10(x0)) / (math.log10(x1) - math.log10(x0))
            return 10 ** (math.log10(y0) + t * (math.log10(y1) - math.log10(y0)))
    return zs[-1]


def _uniform(xs, ys, n):
    t0, t1 = xs[0], xs[-1]
    if t1 <= t0:
        raise MetricError("METRIC_EMPTY", "时间窗口无效")
    dt = (t1 - t0) / (n - 1)
    out = []
    j = 0
    for i in range(n):
        t = t0 + i * dt
        while j < len(xs) - 2 and xs[j + 1] < t:
            j += 1
        x0, x1 = xs[j], xs[min(j + 1, len(xs) - 1)]
        y0, y1 = ys[j], ys[min(j + 1, len(xs) - 1)]
        out.append(y0 if x1 == x0 else y0 + (y1 - y0) * (t - x0) / (x1 - x0))
    return dt, out


def _fft_harmonics(plot, spec):
    node = spec.get("node")
    f0 = float(spec.get("f0") or 0)
    if f0 <= 0:
        raise MetricError("METRIC_PARAM_INVALID", "fft_harmonics 需要 f0 > 0")
    n_h = int(spec.get("n", 5))
    xs, ys = _xy(plot, node)
    xs, ys = _window(xs, ys, spec)
    n = 1024
    dt, u = _uniform(xs, ys, n)
    mean = sum(u) / n
    u = [y - mean for y in u]
    res = []
    for h in range(1, n_h + 1):
        f = f0 * h
        acc = 0j
        for i, y in enumerate(u):
            acc += y * cmath.exp(-2j * math.pi * f * i * dt)
        res.append({"harmonic": h, "freq": f, "magnitude": abs(acc) * 2.0 / n})
    base = res[0]["magnitude"] or 1e-30
    for r in res:
        r["relative_db"] = 20 * math.log10(max(r["magnitude"], 1e-30) / base)
    return {"node": node, "f0": f0, "harmonics": res, "dc": mean}


def compute(plots, specs, mode=None):
    """按 specs 计算指标。返回 {指标名: 值} 与失败清单。"""
    out, errors = {}, []
    if not specs:
        return {"metrics": out}, []
    for i, spec in enumerate(specs):
        kind = str((spec or {}).get("metric") or "").lower()
        label = spec.get("name") or f"{kind}#{i + 1}"
        try:
            if kind in ("tran_stats", "stats", "transient"):
                xs, ys = _xy_pick(plots, spec, mode, complex_ok=False)
                xs, ys = _window(xs, ys, spec)
                out[label] = _tran_stats(xs, ys, spec)
            elif kind == "threshold_crossings":
                xs, ys = _xy_pick(plots, spec, mode, complex_ok=False)
                xs, ys = _window(xs, ys, spec)
                out[label] = _threshold_crossings(xs, ys, spec)
            elif kind == "time_delta":
                out[label] = _time_delta(_pick(plots, mode, complex_ok=False), spec)
            elif kind in ("impedance", "z"):
                out[label] = _impedance(_pick(plots, mode, complex_ok=True), spec)
            elif kind in ("fft_harmonics", "fft"):
                out[label] = _fft_harmonics(_pick(plots, mode, complex_ok=False), spec)
            else:
                raise MetricError("METRIC_UNKNOWN", f"未知指标类型 {kind!r}")
            out[label]["metric"] = kind
        except MetricError as e:
            out[label] = {"metric": kind, "error": e.code, "message": e.message}
            errors.append({"metric": label, "code": e.code, "message": e.message})
    return {"metrics": out}, errors


def _pick(plots, mode=None, complex_ok=False):
    if not plots:
        raise MetricError("METRIC_NO_PLOT", "没有可用的 raw plot")
    wants_ac = complex_ok or (mode or "").lower() == "ac"
    for p in plots:
        is_complex = "complex" in str(p.get("flags") or "")
        if wants_ac == is_complex:
            return p
    return plots[0]


def _xy_pick(plots, spec, mode, complex_ok=False):
    p = _pick(plots, mode, complex_ok=complex_ok)
    return _xy(p, spec.get("node") or spec.get("var"))


def summary_line(name, m):
    """人读的一行摘要（stdout / 汇总表用）。"""
    if not m:
        return ""
    if m.get("error"):
        return f"{name}: ERROR {m['error']} ({m.get('message')})"
    kind = m.get("metric")
    if kind in ("tran_stats", "stats", "transient"):
        if m.get("signal_kind") == "rail":
            return (f"{name}: ripple={m['peak_to_peak']:.4g} "
                    f"({m.get('ripple_pct', 0):.3f}%) max={m['max']:.6g} min={m['min']:.6g}")
        return (f"{name}: pp={m['peak_to_peak']:.4g} max={m['max']:.4g} "
                f"min={m['min']:.4g} overshoot={m.get('overshoot_pct', 0):.1f}% "
                f"undershoot={m.get('undershoot_pct', 0):.1f}%")
    if kind == "threshold_crossings":
        return (f"{name}: crossings={m['total']} on {m['edges']} edges "
                f"(rise {m['rises']} / fall {m['falls']}) "
                f"ringing={'yes' if m['ringing'] else 'no'}"
                + (f" max {m['max_crossings_in_edge']}/edge" if m["ringing"] else ""))
    if kind == "time_delta":
        return f"{name}: skew={m['skew'] * 1e12:.1f} ps ({m['a']} vs {m['b']})"
    if kind in ("impedance", "z"):
        return (f"{name}: |Z|max={m['max_impedance']:.4g} Ω @ {m['peak_frequency']:.4g} Hz")
    if kind in ("fft_harmonics", "fft"):
        return (f"{name}: fundamental={m['harmonics'][0]['magnitude']:.4g} "
                f"@ {m['f0']:.4g} Hz")
    return f"{name}: {m}"
