"""v2.6 依赖边界测试：定位、错误码、IR 适配、PCB 产物适配、离线端到端。

这些用例**不需要真实工程文件**：
  * 契约类用例用合成 JSON（schematic / pcb_geometry）；
  * 端到端用例用依赖 skill 的 tests/make_fixture.py 现场造一个 .epro2。
"""
import glob
import json
import os
import subprocess
import sys

import pytest

from conftest import SCRIPTS, write_json

sys.path.insert(0, SCRIPTS)
import analysis_dep  # noqa: E402
import ir_from_analysis  # noqa: E402
import pcb_geometry as pg  # noqa: E402

DEP_ROOT = analysis_dep.locate(required=False)
HAS_DEP = DEP_ROOT is not None


# -------------------------------------------------------------------------
# 1. 定位与错误
# -------------------------------------------------------------------------
def test_locate_finds_dependency_or_reports_missing():
    if HAS_DEP:
        assert os.path.isfile(os.path.join(DEP_ROOT, "scripts", "analyze.py"))
    else:
        with pytest.raises(analysis_dep.DependencyError) as ei:
            analysis_dep.locate()
        assert ei.value.code == "DEPENDENCY_MISSING"
        assert "已尝试" in ei.value.message


def test_env_var_takes_precedence(monkeypatch, tmp_path):
    """EASYEDA_PROJECT_ANALYSIS_DIR 指向哪就用哪（先建一个假的 analyze.py）。"""
    fake = tmp_path / "dep"
    (fake / "scripts").mkdir(parents=True)
    (fake / "scripts" / "analyze.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setenv("EASYEDA_PROJECT_ANALYSIS_DIR", str(fake))
    assert os.path.normpath(analysis_dep.locate()) == os.path.normpath(str(fake))


def test_missing_dependency_message_is_actionable(monkeypatch, tmp_path):
    monkeypatch.setenv("EASYEDA_PROJECT_ANALYSIS_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(analysis_dep, "_candidates", lambda: [str(tmp_path / "nope")])
    with pytest.raises(analysis_dep.DependencyError) as ei:
        analysis_dep.locate()
    assert ei.value.code == "DEPENDENCY_MISSING"
    # 必须告诉用户怎么装/怎么指定，不能只报"找不到"
    assert "EASYEDA_PROJECT_ANALYSIS_DIR" in ei.value.message


@pytest.mark.skipif(not HAS_DEP, reason="没有依赖 skill")
def test_missing_project_file_is_input_not_found(tmp_path):
    with pytest.raises(analysis_dep.DependencyError) as ei:
        analysis_dep.run_analyze(str(tmp_path / "nope.epro2"), str(tmp_path / "out"))
    assert ei.value.code == "INPUT_NOT_FOUND"


# -------------------------------------------------------------------------
# 2. IR 适配器
# -------------------------------------------------------------------------
def _schematic(page_name="P1", uuid="u1"):
    return {
        "project": "Proj",
        "page": {"uuid": uuid, "name": page_name, "docType": "SCH_PAGE"},
        "components": [
            {"id": "c1", "componentType": "part", "designator": "R1",
             "deviceTitle": "Resistor", "value": "10k",
             "x": 10.0, "y": 20.0, "rotation": 0, "mirror": False,
             "pins": [{"number": "1", "name": "A", "x": 5.0, "y": 20.0},
                      {"number": "2", "name": "B", "x": 15.0, "y": 20.0}],
             "attrs": {"Value": "10k"}},
            {"id": "c2", "componentType": "netflag", "designator": None,
             "deviceTitle": "GND", "attrs": {"Name": "GND"},
             "x": 5.0, "y": 25.0, "pins": [], "net": "GND"},
        ],
        "wires": [{"id": "w1", "line": [5.0, 20.0, 15.0, 20.0], "net": "VCC"}],
    }


def test_ir_adapter_fills_required_fields():
    ir = ir_from_analysis.convert(_schematic())
    assert ir["version"] == 1
    assert ir["source"] == "analysis"
    assert ir["lines"] == []
    r1 = ir["components"][0]
    assert r1["name"] == "Resistor"          # 依赖只有 deviceTitle
    assert r1["pinPrefix"] == "R"            # normalize 补的
    # netflag 没有位号，name 兜底供 connectivity 命名
    assert ir["components"][1]["name"] == "GND"
    assert ir["wires"][0]["net"] == "VCC"


def test_ir_adapter_keeps_pin_coordinates():
    ir = ir_from_analysis.convert(_schematic())
    pins = {(p["number"], p["x"], p["y"]) for p in ir["components"][0]["pins"]}
    assert pins == {("1", 5.0, 20.0), ("2", 15.0, 20.0)}


def test_pick_schematic_by_name_and_uuid(tmp_path):
    write_json(tmp_path / "schematic_P1.json", _schematic("P1", "u1"))
    write_json(tmp_path / "schematic_P2.json", _schematic("P2", "u2"))
    assert analysis_dep.pick_schematic(str(tmp_path), "P2").endswith("schematic_P2.json")
    assert analysis_dep.pick_schematic(str(tmp_path), "u1").endswith("schematic_P1.json")
    with pytest.raises(analysis_dep.DependencyError) as ei:
        analysis_dep.pick_schematic(str(tmp_path), "P9")
    assert ei.value.code == "PAGE_NOT_FOUND"
    assert "P1" in ei.value.message and "P2" in ei.value.message


# -------------------------------------------------------------------------
# 3. PCB 几何适配
# -------------------------------------------------------------------------
def _analysis_geo():
    return {
        "unit": "mil",
        "layers": [{"id": 1, "type": "TOP", "name": "Top Layer"},
                   {"id": 2, "type": "BOTTOM", "name": "Bottom Layer"},
                   {"id": 4, "type": "TOP_SILK", "name": "Top Silk"}],
        "segments": [
            {"net": "VCC", "layer_id": 1, "x1": 0, "y1": 0, "x2": 1000, "y2": 0, "width": 10},
            {"net": "VCC", "layer_id": 2, "x1": 1000, "y1": 0, "x2": 1000, "y2": 500, "width": 10},
            {"net": "", "layer_id": 4, "x1": 0, "y1": 0, "x2": 10, "y2": 10, "width": 5},
        ],
        "pads": [{"component": "R1", "pin": "1", "net": "VCC", "x": 0, "y": 0,
                  "layer_id": 1, "w": 0.6, "h": 0.6},
                 {"component": "", "pin": "1", "net": "GHOST", "x": 5, "y": 5,
                  "layer_id": 1}],
        "vias": [{"net": "VCC", "x": 1000, "y": 0, "hole": 12, "diameter": 24,
                  "type": "THROUGH"}],
        "stackup": [{"id": ["LAYER_PHYS"], "raw": {"thickness": 1.6, "permittivity": 4.4,
                                                   "lossTangent": 0.02}}],
        "outlines": [{"min_x": 0, "max_x": 1000, "min_y": 0, "max_y": 500,
                      "width_mm": 25.4, "height_mm": 12.7}],
        "warnings": [],
    }


def test_pcb_geometry_consumes_analysis_artifact(tmp_path):
    p = write_json(tmp_path / "pcb_geometry.json", _analysis_geo())
    raw = pg.load_pcb_input(p)
    # 丝印层的 LINE 不是铜，必须剔除
    assert len(raw["tracks"]) == 2
    assert all(t["layer_id"] in (1, 2) for t in raw["tracks"])
    # 无位号的焊盘是删除残留，必须剔除
    assert [x["component"] for x in raw["pads"]] == ["R1"]
    assert raw["vias"][0]["drill"] == 12.0 and raw["vias"][0]["pad"] == 24.0
    assert raw["stackup_raw"][0]["er"] == 4.4
    assert raw["extent"] == {"x": [0, 1000], "y": [0, 500]}


def test_pcb_geometry_builds_net_topology(tmp_path):
    p = write_json(tmp_path / "pcb_geometry.json", _analysis_geo())
    geo = pg.build_geometry(pg.load_pcb_input(p))
    assert "VCC" in geo["nets"]
    vcc = geo["nets"]["VCC"]
    assert vcc["total_length_mm"] == pytest.approx(25.4 + 12.7, rel=1e-3)
    # 过孔把顶层与底层串起来，不能合并节点
    assert vcc["vias"] and len(vcc["vias"][0]["nodes"]) >= 2


def test_pcb_geometry_rejects_epro2(tmp_path):
    fake = tmp_path / "board.epro2"
    fake.write_bytes(b"PK\x03\x04")
    with pytest.raises(pg.PcbError) as ei:
        pg.load_pcb_input(str(fake))
    assert ei.value.code == "PCB_INPUT_NOT_PARSED"
    assert "analysis_dep.py" in ei.value.message


# -------------------------------------------------------------------------
# 4. 离线端到端（依赖现场造工程）
# -------------------------------------------------------------------------
def _find_bash():
    if os.name != "nt":
        return "bash", {}
    base = os.path.expanduser("~/.workbuddy/binaries/PortableGit/versions")
    for exe in sorted(glob.glob(os.path.join(base, "*", "bin", "bash.exe"))):
        root = os.path.dirname(os.path.dirname(exe))
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([os.path.join(root, "usr", "bin"),
                                       os.path.join(root, "bin"),
                                       env.get("PATH", "")])
        return exe, env
    return "bash", {}


@pytest.mark.slow
@pytest.mark.skipif(not HAS_DEP, reason="没有依赖 skill")
def test_sim_file_end_to_end_with_generated_project(tmp_path):
    """依赖造一个合成 .epro2 → sim_file.sh op → 必须产出网表/manifest。

    这条用例同时验证：依赖定位、analyze 调用、IR 适配、离线主链路。
    """
    sys.path.insert(0, os.path.join(DEP_ROOT, "tests"))
    import make_fixture  # noqa: E402  依赖自带的工程生成器

    proj = tmp_path / "gen.epro2"
    make_fixture.write_project(str(proj))
    out = tmp_path / "out"
    exe, env = _find_bash()
    r = subprocess.run([exe, os.path.join(SCRIPTS, "sim_file.sh"),
                        str(proj), "op", "", "", str(out)],
                       capture_output=True, timeout=300, env=env or None)
    txt = (r.stdout or b"").decode("utf-8", "replace") + \
          (r.stderr or b"").decode("utf-8", "replace")
    assert r.returncode == 0, txt[-2000:]

    assert (out / "eda-out" / "schematic_P1.json").exists()
    assert (out / "eda-out" / "pcb_geometry.json").exists()
    cir = (out / "schematic.cir").read_text(encoding="utf-8", errors="replace")
    assert "R1" in cir
    manifest = json.loads((out / "simulation_manifest.json").read_text(encoding="utf-8"))
    assert manifest["skill_version"] == "2.6.0"
