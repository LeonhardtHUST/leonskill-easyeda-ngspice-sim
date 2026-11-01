"""P0-B：外部 SPICE 模型映射 + 严格 pin mapping。"""
import os

import pytest

from conftest import element, make_ir, run_gen, write_json

LIB = """* test lib
.SUBCKT REG VIN EN GND VOUT FB
R1 VIN VOUT 1
E1 VOUT GND VIN GND 0.5
R2 FB GND 1k
.ENDS REG
"""


def _ic_ir(value_net="VOUT_NET", extra_pins=5):
    pins = [(1, "VIN"), (2, "EN"), (3, "GND"), (4, "VOUT"), (5, "FB")][:extra_pins]
    comps = [("U1", "U", pins, {"title": "LDO_X", "mpn": "REGPART"}),
             ("V1", "V", [(1, "1"), (2, "2")], {"value": "5V"})]
    nets = [("5V", [("V1", "1"), ("U1", "1")]),
            ("GND", [("V1", "2"), ("U1", "3")]),
            ("EN_NET", [("U1", "2")]),
            ("VOUT_NET", [("U1", "4")]),
            ("FB_NET", [("U1", "5")])]
    return make_ir(comps, nets)


@pytest.fixture
def map_file(tmp_path):
    lib = tmp_path / "ext" / "reg.lib"
    os.makedirs(lib.parent, exist_ok=True)
    lib.write_text(LIB, encoding="utf-8")

    def _mk(pins=None, subckt="REG", order=None, roles=None, file="ext/reg.lib"):
        entry = {"match": {"mpn": ["REGPART"]}, "type": "subckt",
                 "file": file, "subckt": subckt}
        if pins is not None:
            entry["pins"] = pins
        if order is not None:
            entry["subckt_order"] = order
        if roles is not None:
            entry["roles"] = roles
        return write_json(tmp_path / "model_map.json",
                          {"version": 2, "parts": {"REGPART": entry}})
    return _mk


def test_subckt_instance_is_x_prefixed_in_order(tmp_path, map_file):
    mp = map_file(pins={"1": "VIN", "2": "EN", "3": "GND", "4": "VOUT", "5": "FB"},
                  order=["VIN", "EN", "GND", "VOUT", "FB"],
                  roles={"VOUT": "power_output"})
    text, report = run_gen(_ic_ir(), tmp_path, models=mp)
    toks = element(text, "XU1")
    assert toks, [l for l in text.splitlines() if l.startswith("X")]
    # X + des，然后按 subckt_order 的引脚顺序给节点
    assert toks[1:6] == ["N_5V", "EN_NET", "0", "VOUT_NET", "FB_NET"]
    assert toks[6] == "REG"
    assert report["models"][0]["status"] == "VERIFIED"
    assert report["models"][0]["pin_mapping"] == {
        "1": "VIN", "2": "EN", "3": "GND", "4": "VOUT", "5": "FB"}


def test_include_dedup_and_provenance(tmp_path, map_file):
    mp = map_file(pins={"1": "VIN", "2": "EN", "3": "GND", "4": "VOUT", "5": "FB"})
    text, report = run_gen(_ic_ir(), tmp_path, models=mp)
    assert text.count(".include") == 1
    assert '".lib"' in text or ".lib" in text.split(".include")[1]
    m = report["models"][0]
    assert m["file_display"] == "ext/reg.lib"
    assert m["type"] == "SUBCKT"
    assert m["fallback"] == "no"


def test_missing_pin_mapping_fails(tmp_path, map_file):
    # 只映射 4 个脚，FB 没映射 → 必须报 MODEL_MAPPING_ERROR
    mp = map_file(pins={"1": "VIN", "2": "EN", "3": "GND", "4": "VOUT"})
    text, report = run_gen(_ic_ir(), tmp_path, models=mp)
    codes = [e["code"] for e in report["errors"]]
    assert "MODEL_MAPPING_ERROR" in codes
    assert not [l for l in text.splitlines() if l.startswith("XU1")]


def test_allow_partial_model_downgrades(tmp_path, map_file):
    mp = map_file(pins={"1": "VIN", "2": "EN", "3": "GND", "4": "VOUT"})
    text, report = run_gen(_ic_ir(), tmp_path, models=mp, allow_partial_model=True)
    assert not [e for e in report["errors"] if e["code"] == "MODEL_MAPPING_ERROR"]
    toks = element(text, "XU1")
    assert toks and len(toks) == 7          # 5 个引脚全给（缺的用 __NC_ 占位）
    assert any("__NC_" in t for t in toks)


def test_unknown_subckt_pin_fails(tmp_path, map_file):
    mp = map_file(pins={"1": "VIN", "2": "EN", "3": "GND", "4": "VOUT", "5": "NOPE"})
    _, report = run_gen(_ic_ir(), tmp_path, models=mp)
    assert any("不存在的引脚" in e["message"] for e in report["errors"])


def test_missing_lib_file_fails(tmp_path, map_file):
    mp = map_file(pins={"1": "VIN", "2": "EN", "3": "GND", "4": "VOUT", "5": "FB"},
                  file="ext/does_not_exist.lib")
    _, report = run_gen(_ic_ir(), tmp_path, models=mp)
    assert "MODEL_FILE_MISSING" in [e["code"] for e in report["errors"]]


def test_missing_subckt_name_fails(tmp_path, map_file):
    mp = map_file(pins={"1": "VIN", "2": "EN", "3": "GND", "4": "VOUT", "5": "FB"},
                  subckt="NOT_THERE")
    _, report = run_gen(_ic_ir(), tmp_path, models=mp)
    assert any("找不到 .SUBCKT" in e["message"] for e in report["errors"])


def test_auto_pin_by_name(tmp_path, map_file):
    """不给 pins 时按引脚名自动匹配（名字对得上才算，不做几何/顺序猜测）。"""
    mp = map_file(pins=None)
    text, report = run_gen(_ic_ir(), tmp_path, models=mp)
    assert element(text, "XU1")
    assert report["models"][0]["pin_mapping_auto"] is True
    assert report["models"][0]["status"] == "VERIFIED"


def test_pin_order_follows_file_not_config(tmp_path, map_file):
    """subckt_order 与文件一致时以文件为准；不一致要在报告里说清楚。"""
    mp = map_file(pins={"1": "VIN", "2": "EN", "3": "GND", "4": "VOUT", "5": "FB"},
                  order=["FB", "VOUT", "GND", "EN", "VIN"])
    text, report = run_gen(_ic_ir(), tmp_path, models=mp)
    toks = element(text, "XU1")
    assert toks[1:6] == ["N_5V", "EN_NET", "0", "VOUT_NET", "FB_NET"]
    assert any("按文件顺序" in w for w in report["warnings"])


def test_include_path_with_spaces_is_quoted(tmp_path):
    libdir = tmp_path / "dir with space"
    os.makedirs(libdir, exist_ok=True)
    (libdir / "my lib.lib").write_text(LIB, encoding="utf-8")
    mp = write_json(tmp_path / "model_map.json", {
        "version": 2,
        "parts": {"REGPART": {"match": {"mpn": ["REGPART"]}, "type": "subckt",
                              "file": "dir with space/my lib.lib", "subckt": "REG",
                              "pins": {"1": "VIN", "2": "EN", "3": "GND",
                                       "4": "VOUT", "5": "FB"}}}})
    text, report = run_gen(_ic_ir(), tmp_path, models=mp)
    inc = [l for l in text.splitlines() if l.strip().startswith(".include")]
    assert inc and 'dir with space/my lib.lib"' in inc[0]
    assert inc[0].count('"') == 2


def test_lib_section_form(tmp_path):
    libdir = tmp_path / "sect"
    os.makedirs(libdir, exist_ok=True)
    (libdir / "tt.lib").write_text(".LIB TT\n" + LIB + ".ENDL TT\n", encoding="utf-8")
    mp = write_json(tmp_path / "model_map.json", {
        "version": 2,
        "parts": {"REGPART": {"match": {"mpn": ["REGPART"]}, "type": "subckt",
                              "file": "sect/tt.lib", "lib_section": "TT",
                              "subckt": "REG",
                              "pins": {"1": "VIN", "2": "EN", "3": "GND",
                                       "4": "VOUT", "5": "FB"}}}})
    text, _ = run_gen(_ic_ir(), tmp_path, models=mp)
    lib_lines = [l for l in text.splitlines() if l.strip().startswith(".lib ")]
    assert lib_lines and lib_lines[0].strip().endswith(" TT")
    assert not [l for l in text.splitlines() if l.strip().startswith(".include")]


def test_designator_override_priority(tmp_path):
    lib = tmp_path / "a.lib"
    lib.write_text(".SUBCKT AAA A B\nR1 A B 1\n.ENDS\n"
                   ".SUBCKT BBB A B\nR1 A B 2\n.ENDS\n", encoding="utf-8")
    mp = write_json(tmp_path / "model_map.json", {"version": 2, "parts": {
        "BY_MPN": {"match": {"mpn": ["SAMEPART"]}, "type": "subckt", "file": "a.lib",
                   "subckt": "AAA", "pins": {"1": "A", "2": "B"}},
        "BY_DES": {"match": {"designator": ["U7"]}, "type": "subckt", "file": "a.lib",
                   "subckt": "BBB", "pins": {"1": "A", "2": "B"}}}})
    ir = make_ir([("U7", "U", [(1, "A"), (2, "B")], {"mpn": "SAMEPART"})],
                 [("NA", [("U7", "1")]), ("NB", [("U7", "2")])])
    text, report = run_gen(ir, tmp_path, models=mp)
    toks = element(text, "XU7")
    assert toks[-1] == "BBB"
    assert report["models"][0]["matched_by"] == "designator"


def test_power_output_blocks_auto_source(tmp_path, map_file):
    """§4 P0-C：regulator 的 power_output 已经驱动 +5V，就不能再补理想源。"""
    mp = map_file(pins={"1": "VIN", "2": "EN", "3": "GND", "4": "VOUT", "5": "FB"},
                  roles={"VOUT": "power_output"})
    ir = make_ir(
        [("U1", "U", [(1, "VIN"), (2, "EN"), (3, "GND"), (4, "VOUT"), (5, "FB")],
          {"mpn": "REGPART"})],
        [("+5V", [("U1", "4")]), ("GND", [("U1", "3")]),
         ("VIN_NET", [("U1", "1")]), ("EN_NET", [("U1", "2")]),
         ("FB_NET", [("U1", "5")])])
    text, report = run_gen(ir, tmp_path, models=mp, power_policy="auto")
    assert not [l for l in text.splitlines() if l.startswith("VPWR")]
    assert any("power_output 驱动" in w for w in report["warnings"])


def test_legacy_auto_power_still_works(tmp_path, empty_model_map):
    """§21：v2.1 的按网名自动补源要继续可用（带显著 warning）。"""
    ir = make_ir(
        [("R1", "R", [(1, "1"), (2, "2")], {"value": "1k"})],
        [("+12V", [("R1", "1")]), ("GND", [("R1", "2")])])
    text, report = run_gen(ir, tmp_path, models=empty_model_map)
    toks = element(text, "VPWR_12V")
    assert toks and toks[2:] == ["0", "12", "DC", "AC", "0", "0"]
    assert any("POWER-AUTO" in w for w in report["warnings"])


def test_explicit_policy_never_guesses(tmp_path, empty_model_map):
    ir = make_ir(
        [("R1", "R", [(1, "1"), (2, "2")], {"value": "1k"})],
        [("+12V", [("R1", "1")]), ("GND", [("R1", "2")])])
    text, report = run_gen(ir, tmp_path, models=empty_model_map, power_policy="explicit")
    assert not [l for l in text.splitlines() if l.startswith("VPWR")]
    assert any("explicit" in w for w in report["warnings"])


def test_external_supply_declaration(tmp_path, empty_model_map):
    ir = make_ir(
        [("R1", "R", [(1, "1"), (2, "2")], {"value": "1k"})],
        [("+12V", [("R1", "1")]), ("GND", [("R1", "2")])])
    text, report = run_gen(ir, tmp_path, models=empty_model_map, power_policy="explicit",
                           config={"external_supplies": [{"net": "+12V", "voltage": 12}]})
    assert [l for l in text.splitlines() if l.startswith("V_EXT")]
    assert any(p["source"] == "config" for p in report["powers"])
