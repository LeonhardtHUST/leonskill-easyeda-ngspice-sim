"""P1-B：定量指标提取。"""
import math

import pytest

from metrics import compute


def mk_plot(times, values, name="v(a)", flags="real", xname="time"):
    return {"plotname": "Test", "flags": flags, "vars": [xname, name],
            "points": [[t, v] for t, v in zip(times, values)],
            "n_points": len(times)}


def step_with_overshoot():
    ts, ys = [], []
    t = 0.0
    while t < 10.0001:
        ts.append(round(t, 6))
        if t < 2.0:
            ys.append(0.0)
        elif t < 2.5:
            ys.append((t - 2.0) / 0.5)
        elif t < 3.0:
            ys.append(1.0 + 0.2 * (t - 2.5) / 0.5)      # 过冲到 1.2
        elif t < 3.5:
            ys.append(1.2 - 0.2 * (t - 3.0) / 0.5)      # 回落到 1.0
        else:
            ys.append(1.0)
        t += 0.01
    return mk_plot(ts, ys)


def test_tran_stats_basic():
    res, errs = compute([step_with_overshoot()], [
        {"name": "s", "metric": "tran_stats", "node": "A", "v_low": 0, "v_high": 1}])
    m = res["metrics"]["s"]
    assert not errs
    assert m["max"] == pytest.approx(1.2)
    assert m["min"] == pytest.approx(0.0)
    assert m["peak_to_peak"] == pytest.approx(1.2)
    assert m["overshoot_pct"] == pytest.approx(20.0, abs=0.5)
    assert m["rise_time"] == pytest.approx(0.4, abs=0.02)   # 10%..90% of 0..1


def test_rail_signal_reports_ripple_not_overshoot():
    ts = [i * 1e-6 for i in range(1000)]
    ys = [5.0 + 0.001 * math.sin(i / 10.0) for i in range(1000)]
    res, _ = compute([mk_plot(ts, ys, name="v(avdd)")],
                     [{"name": "avdd", "metric": "tran_stats", "node": "AVDD"}])
    m = res["metrics"]["avdd"]
    assert m["signal_kind"] == "rail"
    assert "overshoot_pct" not in m
    assert m["ripple_pct"] == pytest.approx(0.04, abs=0.02)


def test_threshold_crossings_detects_ringing():
    ts, ys = [], []
    dt = 0.0005
    t = 0.0
    # 时钟：边沿在 1.0，随后 1.002 / 1.004 处抖动重穿越，下一个边沿在 1.1
    while t < 1.5:
        if t < 1.0:
            v = 0.0
        elif t < 1.002:
            v = 1.0
        elif t < 1.004:
            v = 0.0
        elif t < 1.1:
            v = 1.0
        else:
            v = 0.0 if (t % 0.1) < 0.001 else 1.0
        ts.append(round(t, 6))
        ys.append(v)
        t += dt
    res, _ = compute([mk_plot(ts, ys, name="v(clk)")], [
        {"name": "c", "metric": "threshold_crossings", "node": "CLK", "threshold": 0.5}])
    m = res["metrics"]["c"]
    assert m["ringing"] is True
    assert m["max_crossings_in_edge"] >= 3
    assert m["crossings_per_edge"] > 1.0


def test_time_delta_skew():
    ts = [i * 1e-12 for i in range(2000)]
    a = [0.0 if t < 500e-12 else 1.0 for t in ts]
    b = [0.0 if t < 620e-12 else 1.0 for t in ts]
    plot = {"plotname": "T", "flags": "real", "vars": ["time", "v(a)", "v(b)"],
            "points": [[t, x, y] for t, x, y in zip(ts, a, b)], "n_points": len(ts)}
    res, errs = compute([plot], [
        {"name": "skew", "metric": "time_delta", "a": "A@0.5", "b": "B@0.5"}])
    assert not errs
    assert res["metrics"]["skew"]["skew"] == pytest.approx(120e-12, rel=1e-6)


def test_impedance_from_complex_ac():
    pts = [[[1e3, 0.0], [0.1, 0.0]],
           [[1e5, 0.0], [0.2, 0.0]],
           [[1e7, 0.0], [0.05, 0.05]]]        # |0.05+0.05j| = 0.0707
    plot = {"plotname": "AC", "flags": "complex", "vars": ["frequency", "v(avdd)"],
            "points": pts, "n_points": 3}
    res, _ = compute([plot], [{"name": "z", "metric": "impedance", "node": "AVDD",
                               "amplitude": 1, "at": [1e5]}], mode="ac")
    m = res["metrics"]["z"]
    assert m["max_impedance"] == pytest.approx(0.2)
    assert m["peak_frequency"] == pytest.approx(1e5)
    assert m["z_at_100000"] == pytest.approx(0.2, rel=1e-3)


def test_impedance_divides_by_amplitude():
    plot = {"plotname": "AC", "flags": "complex", "vars": ["frequency", "v(x)"],
            "points": [[[1e3, 0.0], [2.0, 0.0]]], "n_points": 1}
    res, _ = compute([plot], [{"name": "z", "metric": "impedance", "node": "X",
                               "amplitude": 2}], mode="ac")
    assert res["metrics"]["z"]["max_impedance"] == pytest.approx(1.0)


def test_fft_finds_fundamental():
    f0 = 1000.0
    ts = [i * 1e-5 for i in range(2000)]
    ys = [math.sin(2 * math.pi * f0 * t) for t in ts]
    res, _ = compute([mk_plot(ts, ys)], [
        {"name": "h", "metric": "fft_harmonics", "node": "A", "f0": f0, "n": 3}])
    hs = res["metrics"]["h"]["harmonics"]
    assert hs[0]["magnitude"] > 0.5
    assert hs[1]["relative_db"] < -30
    assert hs[2]["relative_db"] < -30


def test_missing_node_is_reported_not_crashed():
    res, errs = compute([mk_plot([0, 1], [0, 1])], [
        {"name": "x", "metric": "tran_stats", "node": "NOPE"}])
    assert res["metrics"]["x"]["error"] == "METRIC_NODE_NOT_FOUND"
    assert errs
