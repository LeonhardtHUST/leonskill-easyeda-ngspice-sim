"""Wolf-Lite 回归 fixture（端到端，需要本机 ngspice-47）。

覆盖需求文档 §23/§24/§25/§29：
  Case A  clock 分支（45 mm T-line + T branch + damping sweep）
  Case B  ADC PDN（AGND 独立 + 去耦 ESR/ESL + via）
  Case C  Front PWM（线束 + 滤波 + duty 扫描 + 谐波）
  Acceptance 13.8V → buck → LDO → trace/via → ADC 去耦网络
"""
import json
import os
import shutil

import pytest

from conftest import FIXTURES, SCRIPTS

NGSPICE = os.environ.get("NGSPICE_BIN") or os.path.expanduser(
    "~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not os.path.exists(NGSPICE), reason="本机没有 ngspice-47"),
]


def _gen(fixture, workdir, config_name, extra=None):
    """跑 netlist_gen，返回 (deck_text, report)。"""
    import netlist_gen
    fdir = os.path.join(FIXTURES, fixture)
    ir = json.load(open(os.path.join(fdir, "ir.json"), encoding="utf-8"))
    pin_net = {}
    for n in ir["nets"]:
        for m in n["members"]:
            pin_net.setdefault(m["component"], {})[str(m["pin"])] = n["name"]
    cfg = {}
    path = os.path.join(fdir, config_name)
    if os.path.exists(path):
        from sim_config import load_config
        cfg = load_config(path)
    if extra and extra.get("override", 1) is None:      # 显式关掉 override（Case A0）
        cfg = dict(cfg, override=None)
    opts = {"config": cfg, "deck_dir": workdir,
            "models": cfg.get("models"), "behavioral": cfg.get("behavioral_models"),
            "override": cfg.get("override"), "power_policy": cfg.get("power_policy"),
            "strict": cfg.get("strict")}
    if extra:
        opts.update({k: v for k, v in extra.items() if v is not None})
    text, report = netlist_gen.generate(ir, pin_net, opts)
    return text, report, cfg


def _run(workdir, deck_text, mode, args, metrics_specs):
    """写 deck → ngspice → 解析 raw → 计算指标。返回 (rc, metrics, log)。"""
    import build_deck
    import metrics as metrics_mod
    import parse_raw
    import sweep_runner
    deck = build_deck.build(deck_text, mode, args)
    deck_path = os.path.join(workdir, "deck.cir")
    with open(deck_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(deck)
    rc, out, err = sweep_runner.run_ngspice(NGSPICE, deck_path, workdir, timeout=600)
    plots = []
    for fn in sorted(os.listdir(workdir)):
        if fn.endswith(".raw"):
            plots.extend(parse_raw.parse_all(os.path.join(workdir, fn)))
    res, errs = metrics_mod.compute(plots, metrics_specs, mode=mode)
    return rc, (res.get("metrics") or {}), (out + "\n" + err), errs


@pytest.fixture
def work(tmp_path):
    d = str(tmp_path)
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- Case A
def test_case_a_netlist_has_tline_with_correct_td(work):
    text, report, _ = _gen("wolf_lite_clock", work, "sim_config.json")
    lines = [l for l in text.splitlines() if l.startswith("T_CLK_MAIN")]
    assert lines, text
    td = float(lines[0].split("TD=")[1].split()[0])
    # 45.08 mm × 6.0 ps/mm = 270.5 ps —— 不能出现 ns 量级错误
    assert 265e-12 < td < 275e-12
    segs = {s["endpoint"]: s["to"] for s in report["net_segments"]}
    assert set(segs) == {"C1.2", "DA1.7", "U5.89"}


def test_case_a0_ideal_short_has_no_tline(work):
    """A0：理想短导线（保留接收端端接，去掉传输线与源端阻尼）。"""
    a0 = os.path.join(FIXTURES, "wolf_lite_clock", "sim_override_a0.json")
    text, report, _ = _gen("wolf_lite_clock", work, "sim_config.json", {"override": a0})
    assert not [l for l in text.splitlines() if l.startswith("T_")]
    rc, m, log, _ = _run(work, text, "tran", "10p 40n", [
        {"name": "adc", "metric": "tran_stats", "node": "$CLK_ADC", "t0": 2e-8},
        {"name": "fpga", "metric": "tran_stats", "node": "$CLK_FPGA", "t0": 2e-8},
        {"name": "skew", "metric": "time_delta", "a": "$CLK_ADC@1.65",
         "b": "$CLK_FPGA@1.65", "t0": 2e-8}])
    assert rc == 0, log[-800:]
    assert m["adc"]["peak_to_peak"] > 1.0
    # 理想短导线 + 完全对称端接 → skew 应该远小于 100 ps
    assert abs(m["skew"]["skew"]) < 100e-12


def test_case_a2_damping_reduces_overshoot(work):
    """A2：0/22/68 Ω 三种阻尼，过冲必须单调下降。"""
    import sweep_runner
    text, _, _ = _gen("wolf_lite_clock", work, "sim_config.json")
    metrics = [
        {"name": "adc", "metric": "tran_stats", "node": "$CLK_ADC", "t0": 6e-8},
        {"name": "adc_cross", "metric": "threshold_crossings", "node": "$CLK_ADC",
         "threshold": 1.65, "t0": 6e-8},
        {"name": "skew", "metric": "time_delta", "a": "$CLK_ADC@1.65",
         "b": "$CLK_FPGA@1.65", "t0": 6e-8},
    ]
    import build_deck
    deck = build_deck.build(text, "tran", "10p 120n")
    cases = sweep_runner.expand_cases([{"name": "R_CLK_DAMP", "values": [0, 22, 68]}])
    overs, peaks, pp = [], [], []
    for case in cases:
        rec, _ = sweep_runner.run_case(NGSPICE, deck, case, work, "tran", metrics)
        assert rec["returncode"] == 0, rec.get("log_tail")
        m = rec["metrics"]["adc"]
        overs.append(m["overshoot_pct"])
        peaks.append(m["max"])
        pp.append(m["peak_to_peak"])
        assert "skew" in rec["metrics"]
        assert 0 <= rec["metrics"]["adc_cross"]["total"] <= 40
    # 无阻尼时过冲最大；峰值与峰峰值随阻尼单调下降（衰减/展宽，物理上必然）
    assert overs[0] > overs[1] and overs[0] > overs[2], overs
    assert peaks[0] > peaks[1] > peaks[2], peaks
    assert pp[0] > pp[1] > pp[2], pp


# ---------------------------------------------------------------- Case B
def test_case_b_agnd_stays_separate_and_ac_runs(work):
    text, report, _ = _gen("wolf_lite_pdn", work, "sim_config_ac.json")
    assert report["ground_mapping"] == {"AGND": "AGND", "GND": "0"}
    assert report["errors"] == []
    # 去耦电容的 ESR/ESL 必须真的串进去
    assert any(l.startswith("L_HF ") for l in text.splitlines())
    rc, m, log, _ = _run(work, text, "ac", "dec 20 1k 100meg", [
        {"name": "z", "metric": "impedance", "node": "AVDD", "amplitude": 1,
         "at": [1e8]}])
    assert rc == 0, log[-800:]
    assert m["z"]["max_impedance"] > 0
    assert m["z"]["z_at_1e+08"] > 0.1


def test_case_b_via_inductance_raises_hf_impedance(work):
    import sweep_runner
    text, _, _ = _gen("wolf_lite_pdn", work, "sim_config_ac.json")
    import build_deck
    deck = build_deck.build(text, "ac", "dec 20 1k 100meg")
    metrics = [{"name": "z", "metric": "impedance", "node": "AVDD", "at": [1e8]}]
    cases = sweep_runner.expand_cases([{"name": "L_VIA", "values": ["0.2n", "2n"]}])
    zs = []
    for case in cases:
        rec, _ = sweep_runner.run_case(NGSPICE, deck, case, work, "ac", metrics)
        assert rec["returncode"] == 0
        zs.append(rec["metrics"]["z"]["z_at_1e+08"])
    assert zs[1] > zs[0]


# ---------------------------------------------------------------- Case C
def test_case_c_pwm_harmonics(work):
    text, report, _ = _gen("wolf_lite_pwm", work, "sim_config.json")
    assert ".param DUTY=0.5" in text
    rc, m, log, _ = _run(work, text, "tran", "20n 60u", [
        {"name": "main_i", "metric": "tran_stats", "node": "I(V1)", "t0": 3e-5},
        {"name": "fft", "metric": "fft_harmonics", "node": "I(V1)", "f0": 100000,
         "n": 3, "t0": 3e-5, "t1": 6e-5}])
    assert rc == 0, log[-800:]
    assert m["main_i"]["peak_to_peak"] > 0
    assert m["fft"]["harmonics"][0]["magnitude"] > 0


def test_case_c_duty_changes_average_current(work):
    import sweep_runner
    text, _, _ = _gen("wolf_lite_pwm", work, "sim_config.json")
    import build_deck
    deck = build_deck.build(text, "tran", "20n 60u")
    metrics = [{"name": "main_i", "metric": "tran_stats", "node": "I(V1)", "t0": 3e-5}]
    cases = sweep_runner.expand_cases([{"name": "DUTY", "values": [0.1, 1.0]}])
    means = []
    for case in cases:
        rec, _ = sweep_runner.run_case(NGSPICE, deck, case, work, "tran", metrics)
        assert rec["returncode"] == 0
        means.append(abs(rec["metrics"]["main_i"]["mean"]))
    assert means[1] > means[0] * 2


# ---------------------------------------------------------------- Acceptance
def test_acceptance_power_tree(work):
    """§29：13.8V → buck → LDO → trace/via → ADC 去耦，关键部分不能被挖掉。"""
    text, report, _ = _gen("wolf_lite_acceptance", work, "sim_config.json")
    assert any(l.startswith("XU1 ") for l in text.splitlines())      # buck
    assert any(l.startswith("XU2 ") for l in text.splitlines())      # LDO
    assert ".include" in text and "fixture_power.lib" in text
    assert report["errors"] == []
    assert not report["excluded"], report["excluded"]
    # 电容 ESR/ESL 与被 override 替换
    assert any(l.startswith("C_C1_") for l in text.splitlines())
    # 走线 / via
    assert any(l.startswith("R_ADC_TRACE") for l in text.splitlines())
    assert any(l.startswith("L_VIA_") for l in text.splitlines())
    # ADC 动态负载
    assert any("BEHAVIORAL_APPROXIMATION DA1" in l for l in text.splitlines())
    rc, m, log, _ = _run(work, text, "tran", "10n 40u", [
        {"name": "v3v3", "metric": "tran_stats", "node": "3V3", "t0": 2e-5},
        {"name": "avdd", "metric": "tran_stats", "node": "AVDD", "t0": 2e-5}])
    assert rc == 0, log[-1200:]
    assert abs(m["v3v3"]["mean"] - 3.3) < 0.1
    assert abs(m["avdd"]["mean"] - 2.5) < 0.1
    assert m["avdd"]["peak_to_peak"] > 0


def test_acceptance_auto_policy_does_not_double_drive_3v3(work):
    """§4 P0-C：3V3 由 buck 的 power_output 驱动，auto 模式不得再补理想源。"""
    text, report, _ = _gen("wolf_lite_acceptance", work, "sim_config.json",
                           {"power_policy": "auto", "strict": False})
    assert not [l for l in text.splitlines() if l.startswith("VPWR")]
    assert any("power_output 驱动" in w for w in report["warnings"])
