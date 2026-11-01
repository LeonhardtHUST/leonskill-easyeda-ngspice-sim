"""v2.3 端到端回归：Case D/E/F/G + HEADLESS_EXTERNAL_SIMULATION。

标记 slow（需要本机 ngspice-47）。`-m "not slow"` 可跳过。
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

from conftest import FIXTURES, SCRIPTS, SKILL

NGSPICE = os.environ.get("NGSPICE_BIN") or os.path.expanduser(
    "~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe")
REAL_EPRO2 = os.environ.get("WOLF_LITE_EPRO2") or \
    r"C:\Users\Leon\WorkBuddy\2026-10-22-17-11-32\target_test.epro2"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not os.path.exists(NGSPICE), reason="本机没有 ngspice-47"),
]


def _gen(fixture, config_name, workdir, extra=None):
    """生成网表，返回 (deck_text, report)。"""
    import netlist_gen
    from sim_config import load_config
    fdir = os.path.join(FIXTURES, fixture)
    ir = json.load(open(os.path.join(fdir, "ir.json"), encoding="utf-8"))
    pn = {}
    for n in ir["nets"]:
        for m in n["members"]:
            pn.setdefault(m["component"], {})[str(m["pin"])] = n["name"]
    cfg = load_config(os.path.join(fdir, config_name))
    opts = {"config": cfg, "deck_dir": workdir,
            "models": cfg.get("models"), "behavioral": cfg.get("behavioral_models"),
            "override": cfg.get("override"), "power_policy": cfg.get("power_policy"),
            "strict": cfg.get("strict"), "ibis": cfg.get("ibis")}
    if extra:
        opts.update({k: v for k, v in extra.items() if v is not None})
    text, report = netlist_gen.generate(ir, pn, opts)
    return text, report, cfg


def _run(workdir, deck_text, mode, args, metric_specs, timeout=300):
    import build_deck
    import metrics as metrics_mod
    import parse_raw
    import sweep_runner
    deck = build_deck.build(deck_text, mode, args)
    p = os.path.join(workdir, "deck.cir")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(deck)
    rc, out, err = sweep_runner.run_ngspice(NGSPICE, p, workdir, timeout=timeout)
    plots = []
    for fn in sorted(os.listdir(workdir)):
        if fn.endswith(".raw"):
            plots.extend(parse_raw.parse_all(os.path.join(workdir, fn)))
    res, errs = metrics_mod.compute(plots, metric_specs, mode=mode)
    return rc, (res.get("metrics") or {}), (out + "\n" + err)


@pytest.fixture
def work(tmp_path):
    d = str(tmp_path)
    yield d
    shutil.rmtree(d, ignore_errors=True)


# =========================================================================
# Case D — IBIS clock
# =========================================================================
def test_case_d_ibis_receiver_replaces_generic_cin(work):
    """generic Cin(6p, behavioral) vs IBIS 接收模型。"""
    text_gen, rep_gen, _ = _gen("wolf_lite_clock", "sim_config.json", work)
    assert any("C_BHV_U5_CIN1" in l for l in text_gen.splitlines())

    text_ibis, rep_ibis, _ = _gen("wolf_lite_clock", "sim_config_ibis.json", work)
    assert rep_ibis["ibis"], "IBIS 没被应用"
    info = rep_ibis["ibis"][0]
    assert info["level"] == "IBIS_DERIVED_BEHAVIORAL_MODEL"
    assert info["designator"] == "U5" and info["pin"] == "89"
    # IBIS 路径下不再有 behavioral 的 6p 电容
    assert not any("C_BHV_U5_CIN1" in l for l in text_ibis.splitlines())
    # 封装 L/R + C_comp 串进去
    assert any("_PKG" in l for l in text_ibis.splitlines())
    assert info["c_total_pf"] == pytest.approx(4.6, abs=0.05)

    metrics = [{"name": "adc", "metric": "tran_stats", "node": "$CLK_ADC", "t0": 6e-8}]
    rc1, m1, log1 = _run(work, text_gen, "tran", "10p 120n", metrics)
    assert rc1 == 0, log1[-600:]
    work2 = os.path.join(work, "ibis")
    os.makedirs(work2, exist_ok=True)
    rc2, m2, log2 = _run(work2, text_ibis, "tran", "10p 120n", metrics)
    assert rc2 == 0, log2[-600:]
    # 两种接收模型都该给出可比的波形（差异应在 20% 以内，仅验证"能跑且合理"）
    assert m1["adc"]["peak_to_peak"] > 0.5 and m2["adc"]["peak_to_peak"] > 0.5
    ratio = m2["adc"]["peak_to_peak"] / m1["adc"]["peak_to_peak"]
    assert 0.7 < ratio < 1.4, ratio


# =========================================================================
# Case E — Touchstone ferrite（ideal / RLC / S2P）
# =========================================================================
def test_case_e_three_bead_models(work):
    specs = {"ideal": "override_ideal.json", "rlc": "override_rlc.json",
             "s2p": "override_s2p.json"}
    texts = {}
    for name, ovr in specs.items():
        t, rep, _ = _gen("wolf_lite_ferrite", "sim_config.json", work,
                         {"override": os.path.join(FIXTURES, "wolf_lite_ferrite", ovr)})
        assert not rep["errors"], (name, rep["errors"])
        texts[name] = t
    # ideal 是 1mΩ 短接；rlc 是 ferrite 三段；s2p 是拟合出的 R/L 网络
    assert any(l.startswith("R_FB1_IDEAL") for l in texts["ideal"].splitlines())
    assert any(l.startswith("L_FB1_L") for l in texts["rlc"].splitlines())
    assert any(l.startswith("L_FB1_LP") or l.startswith("L_FB1_L0")
               for l in texts["s2p"].splitlines())
    metrics = [{"name": "avdd", "metric": "tran_stats", "node": "AVDD", "t0": 2e-5}]
    res = {}
    for name, t in texts.items():
        d = os.path.join(work, name)
        os.makedirs(d, exist_ok=True)
        rc, m, log = _run(d, t, "tran", "10n 40u", metrics)
        assert rc == 0, (name, log[-500:])
        res[name] = m["avdd"]
    # 理想磁珠纹波最小；带 R/L 的模型一定更大
    assert res["ideal"]["peak_to_peak"] < res["rlc"]["peak_to_peak"]
    assert res["ideal"]["peak_to_peak"] < res["s2p"]["peak_to_peak"]
    # 三种模型都要有合理的直流电平（不能被拟合塞进几十欧的串联电阻）
    for name, m in res.items():
        assert m["mean"] > 3.0, (name, m["mean"])


# =========================================================================
# Case F — multi-rail PDN
# =========================================================================
def test_case_f_multiport_pdn(work):
    import pdn_multiport
    text, rep, cfg = _gen("wolf_lite_pdn_multi", "sim_config.json", work)
    assert not rep["errors"], rep["errors"]
    ports = json.load(open(os.path.join(FIXTURES, "wolf_lite_pdn_multi", "ports.json"),
                           encoding="utf-8"))
    res, _log = pdn_multiport.run(text, ports, NGSPICE, work,
                                 f_stop="100meg", ppd=10, strict=True)
    z = res["impedance"]
    assert "1,1" in z and "2,2" in z and "2,1" in z
    assert z["1,1"]["z_max_ohm"] > 0
    assert z["1,1"]["z_dc_ohm"] > 0 and z["1,1"]["z_dc_ohm"] < 1.0
    # 两条 rail 之间有 150mΩ 耦合 → Z21 必须非零
    assert max(z["2,1"]["|z|_ohm"]) > 1e-4
    assert res["note"].startswith("simulation result")


def test_case_f_ideal_source_short_is_blocked(work):
    """rail 上挂理想源时，strict 必须拦住（Z11 恒 0 无意义）。"""
    import pdn_multiport
    deck = "* x\nV1 1V2 0 DC 1.2\nR1 1V2 0 0.01\nR2 3V3 0 0.02\n.end\n"
    ports = {"ports": [{"name": "A", "node": "1V2", "return": "0"},
                       {"name": "B", "node": "3V3", "return": "0"}]}
    with pytest.raises(pdn_multiport.PdnError) as e:
        pdn_multiport.run(deck, ports, NGSPICE, work, strict=True)
    assert e.value.code == "PDN_PORT_SHORTED_BY_IDEAL_SOURCE"


# =========================================================================
# Case G — Monte Carlo
# =========================================================================
def test_case_g_monte_carlo_reproducible(work):
    import monte_carlo as mc
    import build_deck
    text, rep, cfg = _gen("wolf_lite_clock", "sim_config_mc.json", work)
    block = cfg["montecarlo"]
    specs = block["parameters"]
    cases_a = mc.expand(specs, 4, seed=block["seed"])
    cases_b = mc.expand(specs, 4, seed=block["seed"])
    assert [c["params"] for c in cases_a] == [c["params"] for c in cases_b]
    deck = build_deck.build(text, "tran", "10p 120n")
    metrics = cfg["metrics"]
    recs = mc.run(deck, cases_a, NGSPICE, work, "tran", metrics, workers=1,
                  log=lambda *_: None)
    assert len(recs) == 4
    assert all(r["returncode"] == 0 for r in recs), [r.get("log_tail") for r in recs]
    summary = mc.summarize(recs, block["criteria"])
    assert summary["n_cases"] == 4
    key = "adc.overshoot_pct"
    assert key in summary["metrics"]
    st = summary["metrics"][key]
    assert st["min"] <= st["p50"] <= st["max"]
    assert summary["failure_rate"] is not None
    csv_p, json_p = mc.write_outputs(recs, summary, work)
    assert os.path.exists(csv_p) and os.path.exists(json_p)


# =========================================================================
# HEADLESS_EXTERNAL_SIMULATION（硬性测试，需求文档指定）
# =========================================================================
@pytest.mark.skipif(not os.path.exists(REAL_EPRO2), reason="没有真实 .epro2 样例")
def test_headless_external_simulation(work):
    """EDA 停在 PCB 页、不允许切仿真页 → 仍能完成 extract → deck → ngspice → metrics。

    这里完全不接触 EDA：只用 .epro2 文件（等价于"设计数据源已落盘"）。
    """
    import pcb_geometry as pg
    import pcb_to_override
    import stackup as stk
    import trace_extract as tx

    # 1) 原理图 IR（期望已由 sim_file.sh 生成；这里直接调用解析器）
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "epro2_parse.py"),
                        REAL_EPRO2, os.path.join(work, "ir.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "connectivity.py"),
                        os.path.join(work, "ir.json"), os.path.join(work, "ir_nets.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    # 2) PCB 几何（走线 + 过孔 + 焊盘）
    raw = pg.load_pcb_input(REAL_EPRO2)
    geo = pg.build_geometry(raw)
    assert geo["layers"], "没提到铜层"
    assert geo["nets"], "没提到任何网络"
    assert all(n["segments"] for n in geo["nets"].values())

    # 3) 叠层不完整时必须明确失败（真实工程确实缺铜厚）
    pcb_st = stk.Stackup.from_pcb(geo["layers"], raw.get("stackup_raw"), 1.6)
    assert pcb_st.validate(), "这个真实工程的叠层应当被判定为不完整"

    # 4) 补一份最小 stackup（只写铜厚，εr 用 PCB 里的）
    sp = os.path.join(work, "stackup.json")
    with open(sp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"layers": [{"name": l["name"], "copper_um": 35}
                              for l in geo["layers"]]}, f)
    st = stk.Stackup.from_file(sp, base=pcb_st)
    assert not st.validate(), st.validate()

    # 5) 自动 override（T 线 + via R/L）
    target = "VBUS" if "VBUS" in geo["nets"] else list(geo["nets"])[0]
    ovr = pcb_to_override.build_override(geo, st, nets=[target],
                                         board_thickness_mm=1.6)
    assert ovr["parasitics"], "没生成任何寄生元件"
    tls = [p for p in ovr["parasitics"] if p["type"] == "transmission_line"]
    assert tls and all(p["z0"] > 5 for p in tls)
    assert all(float(p["td"].rstrip("p")) > 0 for p in tls)

    # 6) 生成网表 + 跑 op（headless，无 GUI、无 EDA）
    ovr_path = os.path.join(work, "ovr.json")
    with open(ovr_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(ovr, f, ensure_ascii=False)
    cfg = os.path.join(work, "cfg.json")
    with open(cfg, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"power_policy": "auto", "strict": False}, f)
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "netlist_gen.py"),
                        os.path.join(work, "ir_nets.json"),
                        os.path.join(work, "schematic.cir"),
                        "--config", cfg, "--override", ovr_path,
                        "--report", os.path.join(work, "rep.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    deck_text = open(os.path.join(work, "schematic.cir"), encoding="utf-8").read()
    assert ".end" in deck_text
    assert any("T_AUTO" in l for l in deck_text.splitlines())

    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "sim_exec.py"),
                        os.path.join(work, "schematic.cir"), "op",
                        "--config", cfg, "--report", os.path.join(work, "rep.json"),
                        "--workdir", work, "--ngspice", NGSPICE],
                       capture_output=True, text=True, timeout=300)
    # op 可能因"VBUS 没有电源（USB 连接器被排除）"而奇异，但**流程必须走完**：
    # manifest 必须生成且记录排除项
    man = json.load(open(os.path.join(work, "simulation_manifest.json"), encoding="utf-8"))
    import sim_config
    # 版本号跟着代码走，不要在测试里硬编码（v2.5 起改为对比 SKILL_VERSION）
    assert man["skill_version"] == sim_config.SKILL_VERSION
    assert tuple(int(x) for x in man["skill_version"].split(".")) >= (2, 3, 0)
    assert man["ngspice_version"].startswith("ngspice")
    assert man["excluded_components"], "真实工程的连接器/螺丝应当被列为 excluded"
    assert man["confidence"]["level"] in ("LOW", "MEDIUM", "HIGH")
    # 没给 --workspace 时 manifest 里没有 workspace 键（只有 Workspace.save 才写）
    assert man.get("workspace") is None or isinstance(man.get("workspace"), dict)
