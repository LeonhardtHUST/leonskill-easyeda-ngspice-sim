"""P0-E / P0-F：override 层、端点切分、传输线与寄生元件。"""
import pytest

from conftest import element, lines_of, make_ir, run_gen


def _clock_ir():
    comps = [("C1", "C", [(1, "1"), (2, "2")], {"value": "100p"}),
             ("DA1", "DA", [(7, "CLK")], {"title": "AD9226", "mpn": "AD9226"}),
             ("U5", "U", [(89, "CLKIN")], {"title": "EP4CE10", "mpn": "EP4CE10"})]
    nets = [("CLK_OUT", [("C1", "1")]),
            ("NET-CLOCK", [("C1", "2"), ("DA1", "7"), ("U5", "89")])]
    return make_ir(comps, nets)


def _ovr(tmp_path, obj):
    import json
    import os
    path = os.path.join(str(tmp_path), "sim_override.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f)
    return path


def test_virtual_node_segmentation(tmp_path, empty_model_map):
    ovr = _ovr(tmp_path, {
        "segments": [{"net": "NET-CLOCK",
                      "endpoints": {"C1.2": "$CLK_SRC", "DA1.7": "$CLK_ADC",
                                    "U5.89": "$CLK_FPGA"}}],
        "parasitics": [
            {"name": "TL1", "type": "transmission_line", "from": "$CLK_SRC",
             "to": "$CLK_BR", "z0": 52, "td": "270p"},
            {"name": "TL2", "type": "transmission_line", "from": "$CLK_BR",
             "to": "$CLK_ADC", "z0": 52, "td": "30p"},
            {"name": "TL3", "type": "transmission_line", "from": "$CLK_BR",
             "to": "$CLK_FPGA", "z0": 52, "td": "30p"},
        ]})
    text, report = run_gen(_clock_ir(), tmp_path, models=empty_model_map, override=ovr)
    cap = element(text, "C1")
    assert cap[2] == "__CLK_SRC", cap          # 引脚已从原网络摘下
    segs = {s["endpoint"]: s["to"] for s in report["net_segments"]}
    assert segs == {"C1.2": "__CLK_SRC", "DA1.7": "__CLK_ADC", "U5.89": "__CLK_FPGA"}
    ts = [l for l in lines_of(text) if l.startswith("T_TL")]
    assert len(ts) == 3
    assert ts[0].split()[1:5] == ["__CLK_SRC", "0", "__CLK_BR", "0"]


def test_auto_rehome_without_segments(tmp_path, empty_model_map):
    ovr = _ovr(tmp_path, {"parasitics": [
        {"name": "SER", "type": "series_rl", "from": "C1.2", "to": "NET-CLOCK",
         "r": "10m", "l": "1n"}]})
    text, report = run_gen(_clock_ir(), tmp_path, models=empty_model_map, override=ovr)
    auto = [s for s in report["net_segments"] if s["kind"] == "auto"]
    assert auto and auto[0]["endpoint"] == "C1.2"
    assert auto[0]["to"].startswith("__NET-CLOCK_C1_2")


def test_shunt_to_ground_does_not_rehome(tmp_path, empty_model_map):
    """对地并联（去耦电容）不能把引脚摘下来，否则寄生就挂空了。"""
    ovr = _ovr(tmp_path, {"parasitics": [
        {"name": "DECAP", "type": "capacitor", "from": "C1.1", "to": "0",
         "c": "100n"}]})
    _, report = run_gen(_clock_ir(), tmp_path, models=empty_model_map, override=ovr)
    assert not report["net_segments"]


def test_td_from_length(tmp_path, empty_model_map):
    ovr = _ovr(tmp_path, {"parasitics": [
        {"name": "TL", "type": "transmission_line", "from": "CLK_OUT",
         "to": "NET-CLOCK", "z0": 52, "length_mm": 45.08, "velocity_ps_per_mm": 6.0}]})
    text, report = run_gen(_clock_ir(), tmp_path, models=empty_model_map, override=ovr)
    line = [l for l in lines_of(text) if l.startswith("T_TL")][0]
    td = float(line.split("TD=")[1].split()[0])
    assert abs(td - 270.48e-12) < 1e-15
    deriv = report["overrides"][0]["derivation"]
    assert "TD = 270.5 ps" in deriv


def test_bad_velocity_is_rejected(tmp_path, empty_model_map):
    ovr = _ovr(tmp_path, {"parasitics": [
        {"name": "TL", "type": "transmission_line", "from": "CLK_OUT",
         "to": "NET-CLOCK", "z0": 52, "length_mm": 45, "velocity_ps_per_mm": 6000}]})
    _, report = run_gen(_clock_ir(), tmp_path, models=empty_model_map, override=ovr)
    assert any(e["code"] == "TL_PARAM_INVALID" for e in report["errors"])


def test_capacitor_esr_esl_chain(tmp_path, empty_model_map):
    ovr = _ovr(tmp_path, {"parasitics": [
        {"name": "CAP", "element_names": ["L_ESL", "R_ESR", "C_BODY"],
         "type": "capacitor", "from": "CLK_OUT", "to": "0",
         "c": "100n", "esr": "20m", "esl": "0.7n", "source": "datasheet"}]})
    text, report = run_gen(_clock_ir(), tmp_path, models=empty_model_map, override=ovr)
    esl, esr, body = element(text, "L_ESL"), element(text, "R_ESR"), element(text, "C_BODY")
    assert esl[1] == "CLK_OUT" and esr[1] == esl[2] and body[1] == esr[2]
    assert body[2] == "0"
    assert float(esl[3]) == pytest.approx(0.7e-9)
    assert report["overrides"][0]["source"] == "datasheet"


def test_current_source_injection(tmp_path, empty_model_map):
    ovr = _ovr(tmp_path, {"sources": [
        {"type": "current_source", "name": "I_FPGA", "from": "NET-CLOCK", "to": "0",
         "waveform": "PULSE(120m 300m 10u 2n 2n 5u 10u)"}]})
    text, _ = run_gen(_clock_ir(), tmp_path, models=empty_model_map, override=ovr)
    toks = element(text, "I_I_FPGA") or element(text, "I_FPGA")
    assert toks and toks[1:3] == ["NET-CLOCK", "0"]
    assert "PULSE(" in " ".join(toks)


def test_missing_endpoint_is_an_error(tmp_path, empty_model_map):
    ovr = _ovr(tmp_path, {"parasitics": [
        {"name": "X", "type": "resistor", "from": "NOPE.1", "to": "0", "r": "1k"}]})
    _, report = run_gen(_clock_ir(), tmp_path, models=empty_model_map, override=ovr)
    assert any(e["code"] == "OVERRIDE_ENDPOINT_MISSING" for e in report["errors"])


def test_disable_component(tmp_path, empty_model_map):
    ovr = _ovr(tmp_path, {"disable": ["C1"]})
    text, report = run_gen(_clock_ir(), tmp_path, models=empty_model_map, override=ovr)
    assert not element(text, "C1")
    assert any(e["designator"] == "C1" and e["level"] == "EXCLUDED"
               for e in report["excluded"])


def test_short_merges_grounds(tmp_path, empty_model_map):
    ir = make_ir([("R1", "R", [(1, "1"), (2, "2")], {"value": "1k"})],
                 [("AGND", [("R1", "1")]), ("GND", [("R1", "2")])])
    ovr = _ovr(tmp_path, {"short": [{"from": "AGND", "to": "0"}]})
    text, report = run_gen(ir, tmp_path, models=empty_model_map, override=ovr)
    toks = element(text, "R1")
    assert toks[1] == "0" and toks[2] == "0"
    assert report["overrides"][0]["op"] == "short"


def test_replace_with_ideal_regulator(tmp_path, empty_model_map):
    ir = make_ir([("U9", "U", [(1, "VIN"), (3, "GND"), (5, "VOUT")],
                   {"title": "LDO", "mpn": "XYZ"})],
                 [("VIN_NET", [("U9", "1")]), ("GND", [("U9", "3")]),
                  ("VOUT_NET", [("U9", "5")])])
    ovr = _ovr(tmp_path, {"replace": {"U9": {"type": "ideal_regulator", "vout": 2.5}}})
    text, report = run_gen(ir, tmp_path, models=empty_model_map, override=ovr,
                           allow_partial_model=True)
    src = [l for l in lines_of(text) if l.startswith("V_REPL_U9")]
    assert src and src[0].split()[1] == "VOUT_NET" and "2.5" in src[0]
    assert report["overrides"][0]["op"] == "replace"


def test_element_name_prefix_must_match(tmp_path, empty_model_map):
    ovr = _ovr(tmp_path, {"parasitics": [
        {"name": "R", "element_name": "L_WRONG", "type": "resistor",
         "from": "CLK_OUT", "to": "0", "r": "1k"}]})
    _, report = run_gen(_clock_ir(), tmp_path, models=empty_model_map, override=ovr)
    assert any(e["code"] == "ELEMENT_NAME_MISMATCH" for e in report["errors"])


def test_params_are_emitted(tmp_path, empty_model_map):
    ovr = _ovr(tmp_path, {"params": {"DUTY": 0.5}})
    text, _ = run_gen(_clock_ir(), tmp_path, models=empty_model_map, override=ovr)
    assert ".param DUTY=0.5" in text
