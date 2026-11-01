"""§21 兼容性：v2.1 已验证的行为不能被 v2.2 改坏。"""
import os
import subprocess
import sys

import pytest

import build_deck
from conftest import SKILL, element, lines_of, make_ir, run_gen


def test_basic_rc_netlist_unchanged(tmp_path, empty_model_map):
    ir = make_ir(
        [("V1", "V", [(1, "1"), (2, "2")], {"value": "12V"}),
         ("R1", "R", [(1, "1"), (2, "2")], {"value": "43kΩ"}),
         ("C1", "C", [(1, "1"), (2, "2")], {"value": "10uF"})],
        [("VIN", [("V1", "1"), ("R1", "1"), ("C1", "1")]),
         ("GND", [("V1", "2"), ("R1", "2"), ("C1", "2")])])
    text, report = run_gen(ir, tmp_path, models=empty_model_map)
    assert element(text, "V1")[1:] == ["VIN", "0", "DC", "12V", "AC", "0", "0"]
    assert element(text, "R1")[1:] == ["VIN", "0", "43kΩ"]
    assert element(text, "C1")[1:] == ["VIN", "0", "10uF"]
    assert text.rstrip().endswith(".end")
    assert report["errors"] == []


def test_bjt_generic_fallback(tmp_path, empty_model_map):
    ir = make_ir(
        [("Q1", "Q", [(1, "B"), (2, "E"), (3, "C")], {"title": "NPN", "mpn": "UNKNOWN"})],
        [("B_NET", [("Q1", "1")]), ("E_NET", [("Q1", "2")]), ("C_NET", [("Q1", "3")])])
    text, report = run_gen(ir, tmp_path, models=empty_model_map)
    toks = element(text, "Q1")
    assert toks[4] == "MODNPN_GENERIC"
    assert report["generic"][0]["designator"] == "Q1"


def test_diode_model_from_library(tmp_path, empty_model_map):
    ir = make_ir(
        [("D1", "D", [(1, "1"), (2, "2")], {"mpn": "1N4148"})],
        [("A_NET", [("D1", "1")]), ("K_NET", [("D1", "2")])])
    text, report = run_gen(ir, tmp_path, models=empty_model_map)
    assert element(text, "D1")[3] == "1N4148"
    assert ".MODEL D1N4148" in text


def test_keep_open_comment(tmp_path, empty_model_map):
    ir = make_ir([("J1", "J", [(1, "1"), (2, "2")], {"title": "HEADER"})],
                 [("A", [("J1", "1")]), ("B", [("J1", "2")])])
    text, report = run_gen(ir, tmp_path, models=empty_model_map, keep_open=True)
    assert any("* [skipped] J1" in ln for ln in text.splitlines())
    assert report["excluded"]


def test_build_deck_all_legacy_modes():
    raw = "* x\nR1 a 0 1k\n.end\n"
    for mode, expect in (("op", "op\n"), ("tran", "tran 5u 25m"),
                         ("ac", "ac dec 100 10 100meg"), ("dc", "dc V1 0 12 0.1")):
        deck = build_deck.build(raw, mode, "V1 0 12 0.1" if mode == "dc" else None)
        assert expect in deck
        assert deck.count(".control") == 1 and deck.rstrip().endswith(".end")


def test_build_deck_custom_mode(tmp_path):
    ctrl = os.path.join(str(tmp_path), "ctrl.txt")
    with open(ctrl, "w", encoding="utf-8") as f:
        f.write("op\nprint v(a)\n")
    deck = build_deck.build("R1 a 0 1k\n.end\n", "custom", ctrl)
    assert "print v(a)" in deck


def test_build_deck_rejects_unknown_mode():
    with pytest.raises(ValueError):
        build_deck.build("R1 a 0 1k\n.end\n", "sweep")


def test_build_deck_does_not_touch_ends():
    """插入控制块时不能误命中 .ENDS。"""
    raw = "* x\n.SUBCKT FOO A B\nR1 A B 1\n.ENDS FOO\nX1 a b FOO\n.end\n"
    deck = build_deck.build(raw, "op")
    assert ".ENDS FOO" in deck
    assert deck.index(".control") > deck.index(".ENDS FOO")


def test_legacy_cli_still_works(tmp_path, empty_model_map):
    """旧命令行 `netlist_gen.py <ir> <out> [--keep-open]` 必须继续可用。"""
    from conftest import write_json
    ir = make_ir([("R1", "R", [(1, "1"), (2, "2")], {"value": "1k"})],
                 [("A", [("R1", "1")]), ("GND", [("R1", "2")])])
    ir_path = os.path.join(str(tmp_path), "ir.json")
    out = os.path.join(str(tmp_path), "o.cir")
    write_json(ir_path, ir)
    proc = subprocess.run(
        [sys.executable, os.path.join(SKILL, "scripts", "netlist_gen.py"), ir_path, out],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert os.path.exists(out)
    body = open(out, encoding="utf-8").read()
    assert "R1 A 0 1k" in body


def test_strict_fails_on_unmapped_ic(tmp_path, empty_model_map):
    """§18：strict 下未映射 IC 位于电流路径 → 直接失败。"""
    ir = make_ir([("U1", "U", [(1, "A"), (2, "B")], {"title": "MYSTERY_IC"})],
                 [("A", [("U1", "1")]), ("B", [("U1", "2")])])
    text, report = run_gen(ir, tmp_path, models=empty_model_map, strict=True)
    assert any(e["code"] == "UNMAPPED_IC" for e in report["errors"])
    assert not [l for l in lines_of(text) if l.startswith("U1")]


def test_non_strict_reports_exclusion(tmp_path, empty_model_map):
    ir = make_ir([("U1", "U", [(1, "A"), (2, "B")], {"title": "MYSTERY_IC"})],
                 [("A", [("U1", "1")]), ("B", [("U1", "2")])])
    _, report = run_gen(ir, tmp_path, models=empty_model_map)
    assert not report["errors"]
    assert report["excluded"][0]["level"] == "EXCLUDED"
    assert "无仿真模型" in report["excluded"][0]["reason"]


def test_behavioral_placeholder_marked(tmp_path, empty_model_map):
    from conftest import write_json
    bhv = write_json(tmp_path / "behavioral.json", {"version": 2, "models": {
        "FPGA": {"match": {"device": ["EP4CE*"]}, "type": "dynamic_current_load",
                 "pins": {"supply": ["VCCINT"], "ground": ["GND"]},
                 "waveform": {"type": "pulse", "i_idle": "120m", "i_active": "300m",
                              "delay": "10u", "rise": "2n", "fall": "2n",
                              "on": "5u", "period": "10u"},
                 "not_simulated": ["logic core"]}}})
    ir = make_ir([("U3", "U", [(1, "VCCINT"), (2, "GND")], {"title": "EP4CE10"})],
                 [("VCCINT", [("U3", "1")]), ("GND", [("U3", "2")])])
    text, report = run_gen(ir, tmp_path, models=empty_model_map, behavioral=bhv)
    assert "BEHAVIORAL_APPROXIMATION" in text
    assert report["behavioral"][0]["level"] == "BEHAVIORAL_APPROXIMATION"
    assert "PULSE(120m 300m" in text
    assert report["included"][0]["level"] == "Level 1"


def test_fidelity_levels_are_reported(tmp_path, empty_model_map):
    ir = make_ir([("R1", "R", [(1, "1"), (2, "2")], {"value": "1k"})],
                 [("A", [("R1", "1")]), ("GND", [("R1", "2")])])
    _, report = run_gen(ir, tmp_path, models=empty_model_map)
    assert report["fidelity"]["overall_level"] == 0
