"""P0-A：地映射策略（AGND/DGND 默认不静默合并）。"""
import pytest

from conftest import empty_model_map, element, make_ir, run_gen
from ground import GroundMap, is_ground_like


def test_default_only_gnd_ground_zero():
    gm = GroundMap()
    assert gm.map("GND")[0] == "0"
    assert gm.map("GROUND")[0] == "0"
    assert gm.map("0")[0] == "0"
    assert gm.is_zero("gnd")
    # 关键回归：v2.1 会把它们全部压成 0
    for name in ("AGND", "DGND", "PGND", "CHASSIS", "EARTH", "AGND1", "VSS"):
        assert gm.map(name)[0] is None, name
        assert not gm.is_zero(name), name


def test_gnd_digit_is_not_auto_merged():
    gm = GroundMap()
    assert gm.map("GND1")[0] is None
    assert is_ground_like("GND1")


def test_user_can_merge_explicitly():
    gm = GroundMap({"0": ["GND", "AGND", "DGND"]})
    assert gm.is_zero("AGND")
    assert gm.is_zero("DGND")
    assert gm.is_explicit_merge_to_zero("AGND")
    # 显式合并要在报告里带标记
    lines, mapping = gm.report(["GND", "AGND", "DGND"])
    assert mapping["AGND"] == "0"
    assert any("explicit merge" in ln for ln in lines[1:])


def test_conflicting_alias_is_an_error():
    gm = GroundMap({"0": ["AGND"], "AGND_REF": ["AGND"]})
    assert gm.errors and gm.errors[0]["code"] == "GROUND_MAPPING_CONFLICT"


def test_report_always_printed():
    gm = GroundMap()
    lines, mapping = gm.report(["GND", "AGND", "DGND", "VCC"])
    assert lines[0] == "Ground mapping:"
    assert mapping == {"GND": "0", "AGND": "AGND", "DGND": "DGND"}


def test_netlist_keeps_agnd_separate(tmp_path, empty_model_map):
    ir = make_ir(
        [("V1", "V", [(1, "1"), (2, "2")], {"value": "5V"}),
         ("C1", "C", [(1, "1"), (2, "2")], {"value": "100n"})],
        [("AGND", [("C1", "2")]),
         ("GND", [("V1", "2")]),
         ("5V", [("V1", "1"), ("C1", "1")])],
    )
    text, report = run_gen(ir, tmp_path, models=empty_model_map)
    assert report["ground_mapping"]["AGND"] == "AGND"
    assert report["ground_mapping"]["GND"] == "0"
    caps = [ln for ln in text.splitlines() if ln.startswith("C1 ")]
    assert caps and caps[0].split()[2] == "AGND"
    # 未合并的地要给出提示，不能静默
    assert any("未合并到节点 0" in w for w in report["warnings"])


def test_explicit_merge_through_config(tmp_path, empty_model_map):
    ir = make_ir(
        [("C1", "C", [(1, "1"), (2, "2")], {"value": "100n"})],
        [("AGND", [("C1", "2")]), ("GND", [("C1", "1")])],
    )
    text, report = run_gen(ir, tmp_path, models=empty_model_map,
                           config={"ground_aliases": {"0": ["GND", "AGND"]}})
    assert report["ground_mapping"]["AGND"] == "0"
    toks = element(text, "C1")
    assert toks[1] == "0" and toks[2] == "0"
