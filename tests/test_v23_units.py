"""v2.3 单元测试：PCB 几何 / 叠层 / IBIS / Touchstone / FFT / PDN / Monte Carlo。

这些测试不启动 ngspice（除了极少数），全部用合成数据，保证 < 10 s。
"""
import json
import math
import os

import pytest

from conftest import FIXTURES, SCRIPTS, write_json


# =========================================================================
# P0-A PCB 几何
# =========================================================================
def _pcb_fixture():
    """合成一块 2 层板：一个 T 型分支 + 一个换层过孔 + 两个焊盘。"""
    return {
        "source": "synthetic", "project": "p", "unit": "mil",
        "layers": [{"id": 1, "name": "Top", "type": "TOP"},
                   {"id": 2, "name": "Bottom", "type": "BOTTOM"}],
        "pads": [
            {"component": "X1", "pin": "1", "net": "CLK", "x": 0.0, "y": 0.0, "layer_id": 1},
            {"component": "U1", "pin": "7", "net": "CLK", "x": 1000.0, "y": 0.0, "layer_id": 1},
            {"component": "U2", "pin": "9", "net": "CLK", "x": 500.0, "y": 400.0, "layer_id": 1},
        ],
        "tracks": [
            # X1.1 → 分支点 (500,0) → U1.7
            {"net": "CLK", "layer_id": 1, "x1": 0, "y1": 0, "x2": 500, "y2": 0, "width": 10},
            {"net": "CLK", "layer_id": 1, "x1": 500, "y1": 0, "x2": 1000, "y2": 0, "width": 10},
            # 分支点 → U2.9（不在分支点断开，靠 T-junction 检测）
            {"net": "CLK", "layer_id": 1, "x1": 500, "y1": 0, "x2": 500, "y2": 400, "width": 10},
        ],
        "vias": [],
        "stackup_raw": [],
        "extent": {"x": [0, 1000], "y": [0, 400]},
    }


def test_pcb_geometry_t_junction_and_length(tmp_path):
    import pcb_geometry as pg
    raw = _pcb_fixture()
    geo = pg.build_geometry(raw, net=["CLK"])
    net = geo["nets"]["CLK"]
    # 3 段：X1.1→B1、B1→U1.7、B1→U2.9
    assert len(net["segments"]) == 3
    total = sum(s["length_mm"] for s in net["segments"])
    # 1000 mil + 400 mil = 35.56 mm
    assert total == pytest.approx(35.56, abs=0.02)
    ends = {s["from"] for s in net["segments"]} | {s["to"] for s in net["segments"]}
    assert "U1.7" in ends and "U2.9" in ends
    assert any(e.startswith("__AUTO_CLK_B") for e in ends)   # T 型分支自动命名
    assert net["segments"][0]["width_mm"] == pytest.approx(0.254, abs=1e-3)


def test_pcb_geometry_via_kept_as_series(tmp_path):
    import pcb_geometry as pg
    raw = _pcb_fixture()
    raw["tracks"] = [
        {"net": "CLK", "layer_id": 1, "x1": 0, "y1": 0, "x2": 500, "y2": 0, "width": 10},
        {"net": "CLK", "layer_id": 2, "x1": 500, "y1": 0, "x2": 1000, "y2": 0, "width": 10},
    ]
    raw["vias"] = [{"net": "CLK", "x": 500, "y": 0, "drill": 12, "pad": 24,
                    "type": "NORMAL", "unused": []}]
    geo = pg.build_geometry(raw, net=["CLK"])
    n = geo["nets"]["CLK"]
    # 换层 → 两个层各一个过孔节点（不合并），这样才能插 R/L
    assert len(n["vias"]) == 1
    assert len(n["vias"][0]["nodes"]) == 2
    assert n["vias"][0]["nodes"][0] != n["vias"][0]["nodes"][1]
    layers = {s["layer"] for s in n["segments"]}
    assert layers == {"Top", "Bottom"}


def test_pcb_unit_switch_mm():
    import pcb_geometry as pg
    raw = _pcb_fixture()
    raw["unit"] = "mm"
    raw["tracks"] = [{"net": "CLK", "layer_id": 1, "x1": 0, "y1": 0,
                      "x2": 10, "y2": 0, "width": 0.2}]
    raw["pads"] = []
    geo = pg.build_geometry(raw, net=["CLK"])
    assert geo["nets"]["CLK"]["segments"][0]["length_mm"] == pytest.approx(10.0)


def test_pcb_unknown_net_raises():
    import pcb_geometry as pg
    with pytest.raises(pg.PcbError) as e:
        pg.build_geometry(_pcb_fixture(), net=["NOPE"])
    assert e.value.code == "PCB_NET_NOT_FOUND"


# =========================================================================
# P0-B 叠层 + Z0/TD
# =========================================================================
def _stackup(tmp_path, copper=True, er=4.2, h=0.18):
    layers = [{"name": "Top", "type": "signal", "copper_um": 35 if copper else None},
              {"name": "Inner1", "type": "plane", "copper_um": 35 if copper else None}]
    return write_json(tmp_path / "stackup.json", {
        "layers": layers,
        "dielectrics": [{"between": ["Top", "Inner1"], "thickness_mm": h, "er": er,
                         "loss_tangent": 0.02}]})


def test_stackup_incomplete_when_copper_missing(tmp_path):
    import stackup as stk
    st = stk.Stackup.from_file(_stackup(tmp_path, copper=False))
    missing = st.validate()
    assert any("copper thickness" in m for m in missing)
    with pytest.raises(stk.StackupError) as e:
        st.require_complete()
    assert e.value.code == "STACKUP_INCOMPLETE"


def test_stackup_never_defaults_er(tmp_path):
    """没有 εr 时必须报缺失，绝不默认 4.5。"""
    import stackup as stk
    p = write_json(tmp_path / "s.json", {
        "layers": [{"name": "Top", "type": "signal", "copper_um": 35},
                   {"name": "Inner1", "type": "plane", "copper_um": 35}],
        "dielectrics": [{"between": ["Top", "Inner1"], "thickness_mm": 0.18}]})
    st = stk.Stackup.from_file(p)
    assert any("er missing" in m for m in st.validate())


def test_microstrip_z0_and_td(tmp_path):
    import stackup as stk
    st = stk.Stackup.from_file(_stackup(tmp_path))
    tr = stk.trace_params(st, "Top", 0.2, 45.08)
    assert tr["model"] == "microstrip"
    assert 50 < tr["z0_ohm"] < 70
    # εeff≈3.1 → 5.9 ps/mm → 45.08mm ≈ 266ps（绝不能出现 ns 量级）
    assert 250 < tr["td_ps"] < 290
    assert tr["model_fidelity"] == "ANALYTIC_ESTIMATE"


def test_stripline_model_selected_for_inner_layer(tmp_path):
    import stackup as stk
    p = write_json(tmp_path / "s.json", {
        "layers": [{"name": "Top", "type": "signal", "copper_um": 35},
                   {"name": "In1", "type": "signal", "copper_um": 35},
                   {"name": "Bot", "type": "signal", "copper_um": 35}],
        "dielectrics": [
            {"between": ["Top", "In1"], "thickness_mm": 0.2, "er": 4.2},
            {"between": ["In1", "Bot"], "thickness_mm": 0.2, "er": 4.2}]})
    st = stk.Stackup.from_file(p)
    tr = stk.trace_params(st, "In1", 0.15, 10)
    assert tr["model"] == "stripline"
    assert tr["z0_ohm"] > 20


def test_unit_conversion_exact():
    """45.08 mm × 6 ps/mm = 270.48 ps —— 浮点级一致性。"""
    from parasitics import td_from_length
    td, deriv = td_from_length(45.08, 6.0)
    assert td * 1e12 == pytest.approx(270.48, abs=1e-9)
    assert "270.5 ps" in deriv[2]


def test_via_parasitics_magnitude(tmp_path):
    import stackup as stk
    import via_extract as vx
    st = stk.Stackup.from_file(_stackup(tmp_path))
    p = vx.via_parasitics({"id": "V1", "drill_mm": 0.3, "pad_mm": 0.6}, st,
                          board_thickness_mm=1.6)
    assert 0.8 < p["l_nh"] < 2.0          # 1.6mm 通孔典型 1~1.5 nH
    assert 0.1 < p["c_pf"] < 1.5
    assert p["r_ohm"] < 0.01
    assert p["model_fidelity"] == "ENGINEERING_ESTIMATE"


# =========================================================================
# P0-D IBIS
# =========================================================================
def _ibs():
    import ibis_parser
    return ibis_parser.parse_ibs(os.path.join(FIXTURES, "ibis", "fixture_lvcmos.ibs"))


def test_ibis_pin_mapping():
    doc = _ibs()
    pins = doc["components"]["FIXTURE_FPGA"]["pins"]
    assert pins["89"]["model"] == "3V3_LVCMOS_IN"
    assert pins["12"]["keyword"] == "POWER"
    assert pins["45"]["r_pin"] == pytest.approx(0.08)


def test_ibis_c_comp_and_ramp():
    import ibis_model as im
    doc = _ibs()
    spec = im.resolve(doc, "FIXTURE_FPGA", "89")
    cap = im.input_capacitance(spec)
    assert cap["c_comp_pf"] == pytest.approx(4.0)
    # 引脚级 c_pin（0.6p）优先于 Package 的 0.9p —— 这是 IBIS 的层次规则
    assert cap["c_total_pf"] == pytest.approx(4.6)
    out = im.resolve(doc, "FIXTURE_FPGA", "45")
    e = im.edge_times(out, 3.3)
    # Ramp 是 20-80% 斜率，换算成 PULSE 的 0-100% 会放大 ~1.8×；
    # 这里只验证"换算被应用且量级合理"，具体数值取决于 dv/vcc 的比例
    assert e["tr"] and 500e-12 < e["tr"] < 2500e-12
    assert "ramp" in e["basis"]


def test_ibis_output_resistance():
    import ibis_model as im
    spec = im.resolve(_ibs(), "FIXTURE_FPGA", "45")
    d = im.output_drive(spec, 3.3)
    # IV 曲线在 1.65V 处 ±33mA → 50Ω
    assert d["rout_ohm"] == pytest.approx(50.0, abs=2.0)


def test_ibis_missing_pin_is_error():
    import ibis_model as im
    with pytest.raises(im.IbisModelError) as e:
        im.resolve(_ibs(), "FIXTURE_FPGA", "999")
    assert e.value.code == "IBIS_PIN_NOT_FOUND"


def test_ibis_malformed_file(tmp_path):
    import ibis_parser as ip
    bad = write_json(tmp_path / "x.json", {})       # 不是 IBIS
    p = tmp_path / "bad.ibs"
    p.write_text("not an ibis file at all\n", encoding="utf-8")
    with pytest.raises(ip.IbisError):
        ip.parse_ibs(str(p))


def test_ibis_unsupported_model_warns(tmp_path):
    import ibis_to_spice as its
    m = write_json(tmp_path / "map.json", {"parts": [
        {"designator": "U9", "pin": "1", "ibis_file": "nope.ibs"}]})
    s = its.IbisSet(m)
    from sim_config import Namer
    from parasitics import NetCtx
    lines, infos = s.build(NetCtx(Namer()), {"designator": "U9", "pins": [{"number": "1"}]},
                           lambda c, p: "N1")
    assert lines == [] and infos == []
    assert s.errors and s.errors[0]["code"] == "IBIS_FILE_MISSING"


def test_ibis_behavioral_emission(tmp_path):
    import ibis_to_spice as its
    from parasitics import NetCtx
    from sim_config import Namer
    m = write_json(tmp_path / "map.json", {"parts": [
        {"designator": "U5", "pin": "89",
         "ibis_file": os.path.join(FIXTURES, "ibis", "fixture_lvcmos.ibs"),
         "model": "3V3_LVCMOS_IN"}]})
    s = its.IbisSet(m)
    lines, infos = s.build(NetCtx(Namer()), {"designator": "U5", "pins": [{"number": "89"}]},
                           lambda c, p: "CLK")
    assert infos and infos[0]["level"] == "IBIS_DERIVED_BEHAVIORAL_MODEL"
    assert any(l.startswith("C_") and "p" in l for l in lines)
    assert infos[0]["ibis_sha256"]


# =========================================================================
# P0-E Touchstone
# =========================================================================
def _ts(name):
    import touchstone
    return touchstone.parse(os.path.join(FIXTURES, "touchstone", name))


@pytest.mark.parametrize("fname", ["fixture_lc_lpf.s2p", "fixture_lc_lpf_db_mhz.s2p"])
def test_touchstone_formats_equivalent(fname):
    a = _ts("fixture_lc_lpf.s2p")
    b = _ts(fname)
    assert len(a.frequencies) == len(b.frequencies)
    for x, y in zip(a.frequencies, b.frequencies):
        assert x == pytest.approx(y, rel=1e-9)
    for x, y in zip(a.db(1, 0), b.db(1, 0)):
        assert x == pytest.approx(y, abs=1e-3)


def test_touchstone_s1p_and_metrics():
    import sparam_metrics
    ts = _ts("fixture_bead_1port.s1p")
    assert ts.num_ports == 1
    m = sparam_metrics.metrics(ts)
    assert m["|z|_ohm"][0] > 0
    assert m["return_loss_db"] > 0


def test_touchstone_metrics_2port():
    import sparam_metrics
    m = sparam_metrics.metrics(_ts("fixture_lc_lpf.s2p"))
    assert m["insertion_loss_db"] < 0.5
    assert m["bandwidth_3db"]["bandwidth"] > 10e6
    assert m["resonances"]


def test_touchstone_lumped_fit_recovers_lc():
    import sparam_element as se
    from parasitics import NetCtx
    from sim_config import Namer
    ts = _ts("fixture_lc_lpf.s2p")
    lines, info = se.emit(ts, NetCtx(Namer()), "LC", "A", "B")
    m = info["model"]
    assert m["topology"] == "t"
    assert m["series1"]["L"] == pytest.approx(220e-9, rel=0.02)
    assert m["shunt"]["C"] == pytest.approx(100e-12, rel=0.02)
    assert m["fit"]["s21_max_err_db"] < 0.1


def test_touchstone_native_probe_is_false_here():
    import sparam_element as se
    # 本机 ngspice-47 的 S 元件不可用（实测），adapter 必须能识别并降级
    assert se.probe_native() is False


def test_touchstone_rejects_non_snp(tmp_path):
    import touchstone
    p = tmp_path / "x.txt"
    p.write_text("# Hz S RI R 50\n1e6 1 0\n", encoding="utf-8")
    with pytest.raises(touchstone.TouchstoneError) as e:
        touchstone.parse(str(p))
    assert e.value.code == "TOUCHSTONE_NOT_SPARAM"


# =========================================================================
# P0-F FFT
# =========================================================================
def _tone(f0, amp=1.0, fs=1e-5, n=2000):
    xs = [i * fs for i in range(n)]
    ys = [amp * math.sin(2 * math.pi * f0 * t) for t in xs]
    return xs, ys


def test_fft_single_tone_amplitude():
    import fft_analysis as fa
    xs, ys = _tone(1000.0)
    spec = fa.spectrum(xs, ys, nfft=1024, window="hann")
    peak = fa.peak_at(spec, 1000.0)
    assert peak["mag_v"] == pytest.approx(1.0, abs=0.05)
    assert peak["f_hz"] == pytest.approx(1000.0, rel=0.02)


@pytest.mark.parametrize("window", ["rect", "hann", "blackman-harris"])
def test_fft_windows_keep_amplitude(window):
    import fft_analysis as fa
    xs, ys = _tone(2000.0)
    spec = fa.spectrum(xs, ys, nfft=1024, window=window)
    assert fa.peak_at(spec, 2000.0)["mag_v"] == pytest.approx(1.0, abs=0.08)


def test_dbm_requires_impedance():
    import fft_analysis as fa
    xs, ys = _tone(1000.0)
    spec = fa.spectrum(xs, ys)
    with pytest.raises(ValueError):
        fa.to_dbm(spec, None)
    dbm = fa.to_dbm(spec, 50.0)
    # 1V 幅度 → 0.707Vrms → 10mW → +10 dBm
    peak_dbm = max(dbm[1:])
    assert peak_dbm == pytest.approx(10.0, abs=0.6)


def test_harmonic_detection():
    import fft_analysis as fa
    import spectral_metrics as sm
    f0 = 10000.0
    xs = [i * 1e-6 for i in range(4000)]
    ys = [1.0 * math.sin(2 * math.pi * f0 * t)
          + 0.1 * math.sin(2 * math.pi * 3 * f0 * t) for t in xs]
    spec = fa.spectrum(xs, ys, nfft=2048, window="blackman-harris")
    harm = sm.harmonics(spec, f0, n=4)
    h1 = next(h for h in harm if h["harmonic"] == 1)
    h3 = next(h for h in harm if h["harmonic"] == 3)
    assert h1["found"] and h3["found"]
    assert h3["dbc"] == pytest.approx(-20.0, abs=1.5)


def test_intermod_candidates():
    import spectral_metrics as sm
    c = sm.intermod_candidates([61.44e6, 2e6], max_order=3, f_max=200e6)
    assert c
    assert any(abs(x["f_hz"] - (61.44e6 - 2 * 2e6)) < 1e3 for x in c)


# =========================================================================
# P0-G PDN 端口定义
# =========================================================================
def test_pdn_ports_validation():
    import pdn_multiport as pm
    with pytest.raises(pm.PdnError):
        pm.normalize_ports({"ports": [{"name": "A", "node": "X"}]})   # 只有 1 个
    with pytest.raises(pm.PdnError):
        pm.normalize_ports({"ports": [
            {"name": "A", "node": "X", "return": "X"},
            {"name": "B", "node": "Y", "return": "0"}]})


def test_pdn_detects_ideal_source_short():
    import pdn_multiport as pm
    ports = pm.normalize_ports({"ports": [
        {"name": "A", "node": "1V2", "return": "0"},
        {"name": "B", "node": "3V3", "return": "0"}]})
    deck = "V1 1V2 0 DC 1.2\nR1 3V3 0 1k\n.end\n"
    problems = pm.check_ideal_source_short(deck, ports)
    assert problems and "V1" in problems[0]


def test_pdn_inject_ports():
    import pdn_multiport as pm
    ports = pm.normalize_ports({"ports": [
        {"name": "A", "node": "1V2", "return": "0", "z0": 50},
        {"name": "B", "node": "3V3", "return": "0"}]})
    deck = pm.inject_ports("R1 1V2 0 1k\n.end\n", ports)
    assert "portnum=1" in deck and "portnum=2" in deck
    assert deck.index("portnum=1") < deck.index(".end")


# =========================================================================
# P0-H Monte Carlo
# =========================================================================
def test_mc_seed_reproducible():
    import monte_carlo as mc
    specs = {"R1": {"nominal": "22", "distribution": "gaussian", "sigma_percent": 5}}
    a = mc.expand(specs, 8, seed=42)
    b = mc.expand(specs, 8, seed=42)
    c = mc.expand(specs, 8, seed=43)
    assert [x["params"] for x in a] == [x["params"] for x in b]
    assert [x["params"] for x in a] != [x["params"] for x in c]


def test_mc_distributions():
    import monte_carlo as mc
    import random
    rng = random.Random(1)
    from parasitics import parse_eng
    v = [parse_eng(mc.sample_one({"nominal": "100n", "distribution": "uniform",
                                 "tolerance_percent": 10}, rng), "mc")
         for _ in range(50)]
    assert all(isinstance(x, float) for x in v)
    assert all(85e-9 < x < 115e-9 for x in v)
    d = mc.sample_one({"nominal": "100", "distribution": "discrete",
                       "values": ["91", "100", "110"]}, rng)
    assert d in ("91", "100", "110")


def test_mc_case_limit():
    import monte_carlo as mc
    with pytest.raises(mc.MonteCarloError) as e:
        mc.expand({"R": {"nominal": "1", "distribution": "uniform",
                         "tolerance_percent": 5}}, 500, max_cases=100)
    assert e.value.code == "MC_TOO_MANY_CASES"


def test_mc_corner_cartesian():
    import monte_carlo as mc
    cases = mc.corner({"VIN": [11.5, 13.8], "TEMP": [0, 25, 60]})
    assert len(cases) == 6
    assert cases[0]["params"] == {"VIN": 11.5, "TEMP": 0}


def test_mc_stats_and_criteria():
    import monte_carlo as mc
    st = mc.stats([1, 2, 3, 4, 5])
    assert st["mean"] == 3 and st["p50"] == 3 and st["min"] == 1 and st["max"] == 5
    res = mc.criteria_result({"a.b": 0.02}, [{"metric": "a.b", "op": "<", "value": 0.05}])
    assert res["verdict"] == "PASS"
    res = mc.criteria_result({"a.b": 0.08}, [{"metric": "a.b", "op": "<", "value": 0.05}])
    assert res["verdict"] == "FAIL"
    res = mc.criteria_result({}, [{"metric": "zzz", "op": "<", "value": 1}])
    assert res["verdict"] == "INCONCLUSIVE"


def test_mc_apply_case_temp():
    import monte_carlo as mc
    deck = "R1 a 0 1k\n.temp 27\n.end\n"
    out = mc.apply_case(deck, {"R1": "2k", "TEMP": 60})
    assert "R1 a 0 2k" in out and ".temp 60" in out


# =========================================================================
# P1 对比 / 判据 / 置信度
# =========================================================================
def test_compare_runs_table(tmp_path):
    import compare_runs as cr
    before = {"flat": {"clk.overshoot_pct": 18.0, "avdd.mean": 5.0}}
    after = {"flat": {"clk.overshoot_pct": 6.0, "avdd.mean": 4.9}}
    rows = {r["metric"]: r for r in cr.compare(before, after)}
    assert rows["clk.overshoot_pct"]["verdict"] == "better"   # 越小越好
    assert rows["avdd.mean"]["verdict"] == "changed"


def test_criteria_verdicts():
    import compare_runs as cr
    assert cr.criteria_check({"a": 1}, [{"metric": "a", "op": "<", "value": 2}])["verdict"] == "PASS"
    assert cr.criteria_check({"a": 3}, [{"metric": "a", "op": "<", "value": 2}])["verdict"] == "FAIL"
    assert cr.criteria_check({}, [{"metric": "x", "op": "<", "value": 2}])["verdict"] == "INCONCLUSIVE"


def test_confidence_rules():
    import compare_runs as cr
    low = cr.confidence({"behavioral_models": [{"designator": "U1", "type": "x"}]})
    assert low["level"] == "LOW"
    high = cr.confidence({"models": [{"designator": "U1", "type": "SUBCKT"}],
                          "ibis_models": [{"designator": "U2"}, {"designator": "U3"}],
                          "geometry": {"nets": {"CLK": {}}},
                          "stackup": {"layers": [1], "dielectrics": [1]}})
    assert high["level"] == "HIGH"
    assert all(isinstance(r, tuple) for r in high["rules"])
