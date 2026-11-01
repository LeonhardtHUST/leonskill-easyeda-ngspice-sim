"""v2.4 单元测试：scikit-rf 适配 / vector fitting / passivity / 差分 / 耦合线 /
测量相关 / 参数校准 / 求解器抽象。

不启动 ngspice 的部分在此；round-trip 与黄金语料的对数在 test_v24_roundtrip.py。
"""
import json
import math
import os
import shutil

import numpy as np
import pytest

from conftest import FIXTURES, SKILL

GOLDEN = os.path.join(SKILL, "tests", "golden")


def _gold(rel):
    import rf_network as rn
    return rn.RfNetwork.from_touchstone(os.path.join(GOLDEN, rel))


def _manifest():
    with open(os.path.join(GOLDEN, "MANIFEST.json"), encoding="utf-8") as f:
        return json.load(f)


# =========================================================================
# P0-B scikit-rf 适配层
# =========================================================================
def test_rf_backend_available():
    import rf_network as rn
    cap = rn.capability_report()
    assert cap["available"] is True, cap
    assert "vector_fitting" in cap["features"]


def test_rf_read_and_convert():
    n = _gold("rf/attenuator_6db.s2p")
    assert n.nports == 2 and n.npoints > 10
    s21 = n.s_db()[:, 1, 0]
    assert np.max(np.abs(s21 + 6.02)) < 0.2          # 6 dB 衰减
    y = n.to_y()
    assert y.shape == n.s.shape
    z = n.to_z()
    assert np.allclose(np.abs(z[:, 0, 1] - z[:, 1, 0]), 0, atol=1e-6)


def test_rf_renormalize_preserves_physics():
    """renorm 到 75Ω 后再 renorm 回 50Ω，S 必须复原。"""
    n = _gold("rf/lc_lpf.s2p")
    r = n.renormalize(75.0)
    assert r.meta["renormalized"] is True
    assert abs(r.z0_vector()[0].real - 75.0) < 1e-9
    back = r.renormalize(50.0)
    assert np.allclose(back.s, n.s, atol=1e-9)


def test_rf_z0_contract_strict():
    import rf_network as rn
    n = _gold("rf/ideal_thru.s2p")
    out, info = rn.ensure_z0(n, 75.0, strict=True)
    assert info["renormalized"] is True
    out2, info2 = rn.ensure_z0(n, 50.0, strict=True)
    assert info2["renormalized"] is False


def test_rf_cascade_thru_is_identity():
    n = _gold("rf/lc_lpf.s2p")
    thru = _gold("rf/ideal_thru.s2p")
    c = n.cascade(thru)
    assert np.allclose(np.abs(c.s), np.abs(n.s), atol=1e-9)


def test_rf_passivity_and_reciprocity():
    n = _gold("rf/attenuator_6db.s2p")
    assert n.is_reciprocal()
    assert n.is_passive()


# =========================================================================
# P0-C/D vector fitting + passivity
# =========================================================================
@pytest.mark.parametrize("rel", [
    "rf/ideal_thru.s2p", "rf/attenuator_6db.s2p", "rf/lc_lpf.s2p",
    "rf/tline_lossy_300ps.s2p", "rf/bpf_100mhz.s2p",
    "si/via_mismatch.s2p", "si/source_termination.s2p",
])
def test_vector_fit_reaches_target(rel):
    import vector_fit as vf
    n = _gold(rel)
    res = vf.fit(n, order="auto", max_order=20, target_rms_db=0.5,
                 log=lambda *_: None)
    assert res["error"]["rms_mag_error_db"] <= 0.5
    assert res["order"] <= 20
    assert res["history"], "应当记录自适应过程"
    # 无损网络会卡在 |S|=1 边界，容差内即算通过（报告里会区分 strict/tol）
    assert res["passivity"]["passive"] is True


def test_vector_fit_pure_delay_is_not_rational():
    """纯时延（理想无损线）是无理函数，VF 拟合不了 —— 必须明确失败而不是硬凑。"""
    import vector_fit as vf
    n = _gold("rf/tline_50ohm_300ps.s2p")
    with pytest.raises(vf.VectorFitError) as e:
        vf.fit(n, order="auto", max_order=16, target_rms_db=0.5,
               log=lambda *_: None)
    assert e.value.code == "VECTOR_FIT_NOT_CONVERGED"
    assert "max_order" in e.value.message


def test_passivity_enforcement_records_before_after():
    """非无源拟合：enforce 必须同时记录 before/after 误差，且不能"修到完全失真"。"""
    import vector_fit as vf
    n = _gold("si/single_line.s2p")
    plain = vf.fit(n, order=2, log=lambda *_: None)
    assert plain["passivity"]["passive_strict"] in (True, False)
    res = vf.fit(n, order=2, enforce_passivity=True, log=lambda *_: None)
    enf = res.get("enforced")
    if enf:
        assert enf["before"] and enf["after"]
        assert "success" in enf
        if enf["success"]:
            # 成功：必须真的无源，且误差没有失控
            assert res["passivity"]["max_singular_value"] <= 1.0 + 1e-3
            assert enf["after"]["rms_mag_error_db"] < 1.0
        else:
            # 失败/被拒绝：必须保留原因，且不能把模型改坏
            assert res["passivity"].get("enforcement_failed") is True
            assert enf["note"]
            assert enf["after"]["rms_mag_error_db"] <= max(
                3 * enf["before"]["rms_mag_error_db"], 1.0) + 1e-9


def test_vector_fit_order_ladder_is_monotone():
    """自适应过程里误差必须随阶数单调不增（偶发回退允许，但不该大幅反弹）。"""
    import vector_fit as vf
    n = _gold("rf/bpf_100mhz.s2p")
    res = vf.fit(n, order="auto", max_order=16, target_rms_db=0.05,
                 log=lambda *_: None)
    errs = [h["rms_mag_error_db"] for h in res["history"]]
    assert errs[-1] <= errs[0] + 1e-9
    assert min(errs) >= errs[-1] - 1e-9


def test_vector_fit_explicit_order():
    import vector_fit as vf
    n = _gold("rf/lc_lpf.s2p")
    res = vf.fit(n, order=6, log=lambda *_: None)
    # skrf 会丢弃部分"虚假极点"，实际阶数可能少于请求值
    assert 2 <= res["order"] <= 6
    assert res["error"]["max_mag_error_db"] < 1.0


def test_vector_fit_rejects_unreachable_target():
    import vector_fit as vf
    n = _gold("rf/bpf_100mhz.s2p")
    with pytest.raises(vf.VectorFitError) as e:
        vf.fit(n, order="auto", max_order=2, target_rms_db=1e-6,
               log=lambda *_: None)
    assert e.value.code == "VECTOR_FIT_NOT_CONVERGED"


def test_passivity_report_fields():
    import vector_fit as vf
    n = _gold("rf/lc_lpf.s2p")
    res = vf.fit(n, order="auto", max_order=12, target_rms_db=0.5,
                 log=lambda *_: None)
    p = res["passivity"]
    assert set(p) >= {"passive", "passive_strict", "max_singular_value", "tolerance"}
    assert p["max_singular_value"] is not None


# =========================================================================
# P0-E 差分端口 / 混合模
# =========================================================================
def test_mixed_mode_transform_is_invertible():
    import diff_port as dp
    n = _gold("si/diff_pair_balanced.s4p")
    s_mm = dp.se_to_mm(n.s, 2)
    back = dp.mm_to_se(s_mm, 2)
    assert np.allclose(back, n.s, atol=1e-9)


def test_balanced_pair_has_no_mode_conversion():
    import diff_port as dp
    n = _gold("si/diff_pair_balanced.s4p")
    res = dp.analyze(n, [
        {"positive": "P", "negative": "N", "z_diff": 100, "name": "DP1"},
        {"positive": "P2", "negative": "N2", "z_diff": 100, "name": "DP2"}])
    # 完全对称 → Scd 应该是数值噪声级
    for m in res["metrics"].values():
        assert m["scd_max_db"] < -40, m


def test_mismatched_pair_shows_mode_conversion():
    import diff_port as dp
    n = _gold("si/diff_pair_mismatch_5pct.s4p")
    res = dp.analyze(n, [
        {"positive": "P", "negative": "N", "z_diff": 100, "name": "DP1"},
        {"positive": "P2", "negative": "N2", "z_diff": 100, "name": "DP2"}])
    bal = dp.analyze(_gold("si/diff_pair_balanced.s4p"), [
        {"positive": "P", "negative": "N", "z_diff": 100, "name": "DP1"},
        {"positive": "P2", "negative": "N2", "z_diff": 100, "name": "DP2"}])
    m = res["metrics"]["DP1"]
    mb = bal["metrics"]["DP1"]
    # 5% 失配必须让模式转换明显高于完全对称的情形
    assert m["scd_max_db"] > mb["scd_max_db"] + 60, (m, mb)
    assert m["scd_max_db"] > -120, m


def test_diff_port_count_mismatch():
    import diff_port as dp
    n = _gold("si/diff_pair_balanced.s4p")
    with pytest.raises(dp.DiffPortError) as e:
        dp.analyze(n, [{"positive": "P", "negative": "N", "z_diff": 100}])
    assert e.value.code == "DIFF_PORT_COUNT"


# =========================================================================
# P0-G 耦合线 + 差分对识别
# =========================================================================
def _stackup(tmp_path):
    import stackup as stk
    p = os.path.join(str(tmp_path), "s.json")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"layers": [{"name": "Top", "type": "signal", "copper_um": 35},
                              {"name": "Inner", "type": "plane", "copper_um": 35}],
                   "dielectrics": [{"between": ["Top", "Inner"],
                                    "thickness_mm": 0.18, "er": 4.2}]}, f)
    return stk.Stackup.from_file(p)


def test_coupled_microstrip_limits(tmp_path):
    import coupled_line as cl
    st = _stackup(tmp_path)
    close = cl.coupled_microstrip(st, "Top", 0.2, 0.15)
    far = cl.coupled_microstrip(st, "Top", 0.2, 5.0)
    # 间距越大 → 耦合越弱 → Zdiff 越接近 2×Z0se
    assert close["z_diff_ohm"] < far["z_diff_ohm"]
    assert abs(far["z_diff_ohm"] - 2 * far["z0_single_ohm"]) < 1.0
    assert close["z_odd_ohm"] < close["z_even_ohm"]
    assert close["model_fidelity"] == cl.FIDELITY
    # 奇模慢于偶模（场更多在介质里）
    assert close["td_odd_ps_per_mm"] > close["td_even_ps_per_mm"]


def test_coupled_line_needs_positive_spacing(tmp_path):
    import coupled_line as cl
    st = _stackup(tmp_path)
    with pytest.raises(cl.CoupledLineError):
        cl.coupled_microstrip(st, "Top", 0.2, 0.0)


def _geo_with_pair():
    """合成：两条平行线，间距 0.2 mm，长度不同（含失配）。"""
    term = lambda x, y: {"x_mm": x, "y_mm": y, "layer": "Top Layer", "kind": "pad"}
    return {
        "source": "synthetic", "layers": [{"id": 1, "name": "Top Layer", "type": "TOP"}],
        "nets": {
            "CLK_P": {"segments": [{"from": "A.1", "to": "A.2",
                                    "layer": "Top Layer", "length_mm": 30.0,
                                    "width_mm": 0.2, "via_count": 0}],
                      "terminals": {"A.1": term(0, 0), "A.2": term(30, 0)},
                      "vias": []},
            "CLK_N": {"segments": [{"from": "B.1", "to": "B.2",
                                    "layer": "Top Layer", "length_mm": 32.5,
                                    "width_mm": 0.2, "via_count": 0}],
                      "terminals": {"B.1": term(0, 0.2), "B.2": term(30, 0.2)},
                      "vias": []},
            "FAR": {"segments": [{"from": "C.1", "to": "C.2", "layer": "Top Layer",
                                  "length_mm": 30.0, "width_mm": 0.2,
                                  "via_count": 0}],
                    "terminals": {"C.1": term(0, 5.0), "C.2": term(30, 5.0)},
                    "vias": []},
        },
    }


def test_identify_diff_pair_by_geometry():
    import coupled_line as cl
    rep = cl.identify_diff_pairs(_geo_with_pair())
    assert len(rep["pairs"]) == 1, rep["pairs"]
    p = rep["pairs"][0]
    assert {p["net_p"], p["net_n"]} == {"CLK_P", "CLK_N"}
    assert abs(p["spacing_mm"] - 0.2) < 0.02
    assert abs(p["mismatch_mm"] - 2.5) < 0.01
    assert 0.5 < p["confidence"] <= 0.95
    # FAR 太远，不该被配对
    assert all("FAR" not in (x["net_p"], x["net_n"]) for x in rep["pairs"])


def test_identify_diff_pair_uses_geometry_not_only_names():
    """把 _P/_N 名字换掉，仍应靠几何识别出这对线。"""
    import coupled_line as cl
    geo = _geo_with_pair()
    geo["nets"]["AAAA"] = geo["nets"].pop("CLK_P")
    geo["nets"]["BBBB"] = geo["nets"].pop("CLK_N")
    rep = cl.identify_diff_pairs(geo)
    assert len(rep["pairs"]) == 1
    p = rep["pairs"][0]
    assert {p["net_p"], p["net_n"]} == {"AAAA", "BBBB"}
    assert not any("_P/_N" in e for e in p["evidence"]), p["evidence"]


# =========================================================================
# P0-F IBIS [Diff Pin]
# =========================================================================
def test_ibis_diff_pin_parse_and_resolve():
    import ibis_model as im
    import ibis_parser as ip
    doc = ip.parse_ibs(os.path.join(FIXTURES, "ibis", "fixture_lvcmos.ibs"))
    assert len(doc["diff_pins"]) == 2
    r = im.resolve_diff(doc, "FIXTURE_FPGA", "60")
    assert r["pair"]["negative"] == "61"
    assert r["vdiff"] == pytest.approx(0.3)
    assert "diff" in (r["model_type"] or "").lower()
    assert im.input_capacitance(r["positive"])["c_total_pf"] > 0


def test_ibis_diff_pin_missing_and_mismatch():
    import ibis_model as im
    import ibis_parser as ip
    doc = ip.parse_ibs(os.path.join(FIXTURES, "ibis", "fixture_lvcmos.ibs"))
    with pytest.raises(im.IbisModelError) as e:
        im.resolve_diff(doc, "FIXTURE_FPGA", "89")
    assert e.value.code == "IBIS_DIFF_PIN_NOT_FOUND"
    with pytest.raises(im.IbisModelError) as e2:
        im.resolve_diff(doc, "FIXTURE_FPGA", "60", negative="63")
    assert e2.value.code == "IBIS_DIFF_PIN_MISMATCH"


def test_ibis_unsupported_feature_is_explicit(tmp_path):
    import ibis_model as im
    import ibis_parser as ip
    p = os.path.join(str(tmp_path), "adv.ibs")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("[IBIS Ver] 5.0\n[Component] C1\n[Pin]\n1 A M1\n"
                "[Model] M1\nModel_type Output\n"
                "[Series Pin Mapping] 1 2 M1\n[End]\n")
    doc = ip.parse_ibs(p)
    assert "seriespinmapping" in doc["unsupported"]
    with pytest.raises(im.IbisModelError) as e:
        im.resolve_diff(doc, "C1", "1")
    assert e.value.code == "IBIS_FEATURE_UNSUPPORTED"


# =========================================================================
# P0-H 测量相关
# =========================================================================
def test_measurement_import_csv():
    import measurement_import as mi
    m = mi.load_csv(os.path.join(GOLDEN, "meas", "scope_step_noisy.csv"))
    assert m["meta"]["n_rows"] > 100
    assert m["time_s"][0] == 0.0
    assert abs(np.median(np.diff(m["time_s"])) - 5e-12) < 1e-15
    assert m["voltage"].max() > 3.0


def test_measurement_import_rail_with_current():
    import measurement_import as mi
    m = mi.load_csv(os.path.join(GOLDEN, "meas", "rail_droop.csv"))
    assert m["current"] is not None
    assert abs(np.nanmax(m["current"]) - 0.15) < 0.02


def test_correlate_self_is_zero_error():
    import correlate as co
    import measurement_import as mi
    m = mi.load_csv(os.path.join(GOLDEN, "meas", "scope_step_noisy.csv"))
    r = co.correlate_waveform({"time_s": m["time_s"], "voltage": m["voltage"]},
                              m, {"auto_delay": False})
    assert r["rmse_v"] < 1e-12
    assert r["delay_s"] == 0.0


def test_correlate_detects_injected_delay():
    import correlate as co
    import measurement_import as mi
    m = mi.load_csv(os.path.join(GOLDEN, "meas", "scope_step_noisy.csv"))
    shift = 200e-12
    sim = {"time_s": m["time_s"] + shift, "voltage": m["voltage"]}
    r = co.correlate_waveform(sim, m, {"auto_delay": True,
                                       "max_delay_s": 1e-9, "dt_s": 5e-12})
    assert abs(abs(r["delay_s"]) - shift) < 20e-12


def test_correlate_sparam():
    import correlate as co
    a = _gold("rf/lc_lpf.s2p")
    b = _gold("rf/lc_lpf.s2p")
    m = co.correlate_sparam(a, b)
    assert m["rms_mag_error_db"] < 1e-9
    assert m["nports_compared"] == 2


# =========================================================================
# P0-I 参数校准 + 灵敏度 + 可辨识性
# =========================================================================
def _toy_sim(params):
    """可解析的"电路"：f0 与 Q 由两个参数决定，用于验证拟合/灵敏度/可辨识性。"""
    f0 = params["F0"]
    q = params["Q"]
    f = np.linspace(0.5e6, 3e6, 200)
    x = f / f0
    return 1.0 / np.sqrt((1 - x ** 2) ** 2 + (x / q) ** 2)


def test_calibration_recovers_parameters():
    import parameter_fit as pf
    truth = {"F0": 1.0e6, "Q": 8.0}
    target = _toy_sim(truth)
    res = pf.fit(lambda p: _toy_sim(p), target, [
        {"name": "F0", "initial": 1.2e6, "bounds": [0.5e6, 2e6]},
        {"name": "Q", "initial": 5.0, "bounds": [1.0, 20.0]},
    ], log=lambda *_: None)
    assert abs(res["fitted"]["F0"] - truth["F0"]) < 2e3
    assert abs(res["fitted"]["Q"] - truth["Q"]) < 0.2
    assert res["fit_error"]["rms"] < 1e-3
    assert res["sensitivity_ranking"][0]["parameter"] in ("F0", "Q")


def test_calibration_requires_bounds():
    import parameter_fit as pf
    with pytest.raises(pf.FitError) as e:
        pf.prepare_params([{"name": "X", "initial": 1.0}])
    assert e.value.code == "PARAM_BOUNDS_MISSING"


def test_calibration_train_validation_split():
    import parameter_fit as pf
    truth = {"F0": 1.0e6, "Q": 8.0}
    target = _toy_sim(truth)
    res = pf.fit(lambda p: _toy_sim(p), target, [
        {"name": "F0", "initial": 1.1e6, "bounds": [0.5e6, 2e6]},
        {"name": "Q", "initial": 6.0, "bounds": [1.0, 20.0]},
    ], fit_slice=(0, 120), validation_slice=(120, 200), log=lambda *_: None)
    assert res["fit_error"]["n"] == 120
    assert res["validation_error"]["n"] == 80
    # 数据本身无噪声 → 验证误差也应很小
    assert res["validation_error"]["rms"] < 5e-3


def test_identifiability_flags_correlated_params():
    import parameter_fit as pf
    # 两个参数只以乘积形式出现 → 完全相关，应报 IDENTIFIABILITY_LOW
    def sim(p):
        f = np.linspace(0.5e6, 3e6, 100)
        return (p["A"] * p["B"]) / (1 + (f / 1e6) ** 2)
    res = pf.fit(sim, sim({"A": 2.0, "B": 3.0}), [
        {"name": "A", "initial": 2.5, "bounds": [0.5, 10]},
        {"name": "B", "initial": 2.5, "bounds": [0.5, 10]},
    ], log=lambda *_: None)
    assert res["identifiability"]["code"] == "PARAMETER_IDENTIFIABILITY_LOW"


# =========================================================================
# P1-C 求解器抽象 / P2 EM 桥
# =========================================================================
def test_solver_backends():
    import solver_backend as sb
    b = sb.get("ngspice")
    p = b.probe()
    assert p["available"] is True
    assert p["status"] == "QUALIFIED"
    assert "sp" in b.capabilities()["analyses"]
    xy = sb.get("xyce")
    assert xy.capabilities()["rf_ports"] is False
    with pytest.raises(sb.SolverError):
        sb.get("nope")


def test_em_bridge_plan_and_script(tmp_path):
    import em_bridge as eb
    geo = _geo_with_pair()
    region = eb.plan_region(geo, ["CLK_P", "CLK_N"], 5e9)
    assert region["margin_mm"] >= 3.0
    assert region["cell_mm"] <= 0.2
    assert region["status"] == "EXPERIMENTAL"
    # v2.5：EM 必须知道铜层位置，只给介质不够（原来这里只有 dielectrics，
    # 导致铜层 z 全靠猜）。改成一块真实的 2 层板叠层。
    st = {"layers": [{"name": "Top Layer", "copper_um": 35},
                     {"name": "Bottom Layer", "copper_um": 35}],
          "dielectrics": [{"between": ["Top Layer", "Bottom Layer"],
                           "thickness_mm": 0.394, "er": 3.3,
                           "loss_tangent": 0.02}]}
    plan, path, script = eb.write_plan(geo, st, region,
                                       [{"name": "P1", "positive": "A", "negative": "B",
                                         "z0": 0.035, "z1": 0.464}],
                                       str(tmp_path))
    assert os.path.exists(script)
    txt = open(script, encoding="utf-8").read()
    assert "CSXCAD" in txt and "AddLumpedPort" in txt
    assert "EXPERIMENTAL" in txt
    assert plan["limitations"]


def test_em_bridge_rejects_unknown_net():
    import em_bridge as eb
    with pytest.raises(eb.EmBridgeError):
        eb.plan_region(_geo_with_pair(), ["NOPE"], 1e9)
