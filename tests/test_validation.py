"""v2.5 P0-A：验证等级必须被证据支持（元数据不能自说自话）。

这些测试是"可信度元数据"的 lint：等级高估会让 CI 直接红。
"""
import json
import os

import pytest

from conftest import SKILL

REG = os.path.join(SKILL, "validation_registry.json")
DOC = os.path.join(SKILL, "VALIDATION_STATUS.md")


def test_registry_levels_are_supported_by_evidence():
    import validation
    res = validation.check_all()
    assert res["ok"], "\n".join(res["problems"])
    assert res["n_features"] >= 30


def test_doc_matches_registry():
    """VALIDATION_STATUS.md 必须由注册表生成 —— 手改表格会让这个测试红。"""
    import validation
    with open(DOC, encoding="utf-8") as f:
        on_disk = f.read()
    assert on_disk.strip() == validation.render_markdown().strip(), (
        "VALIDATION_STATUS.md 与注册表不一致，请运行 `python scripts/validation.py --write`")


def test_synthetic_measurement_cannot_be_correlated():
    """核心规则：合成数据永远不能升 CORRELATED。"""
    import validation
    bad = {"feature": "X", "status": "CORRELATED", "evidence": [
        {"type": "measurement", "source": "synthetic", "sample": "OUT_OF_SAMPLE",
         "instrument": "none", "file": "x.csv", "what": "合成波形"}]}
    ok, probs = validation.check_entry(bad)
    assert not ok
    assert any("instrument" in p for p in probs)


def test_in_sample_measurement_cannot_be_correlated():
    import validation
    bad = {"feature": "X", "status": "CORRELATED", "evidence": [
        {"type": "measurement", "source": "instrument", "sample": "IN_SAMPLE",
         "instrument": "DSOX3024T", "file": "x.csv", "what": "同一份数据也用于拟合"}]}
    ok, probs = validation.check_entry(bad)
    assert not ok
    assert any("OUT_OF_SAMPLE" in p for p in probs)


def test_qualified_needs_all_three_and_regression():
    import validation
    partial = {"feature": "X", "status": "QUALIFIED", "evidence": [
        {"type": "unit_test", "what": "t"},
        {"type": "analytic", "what": "a"}]}
    ok, probs = validation.check_entry(partial)
    assert not ok
    assert any("实测或全波" in p for p in probs)
    full = {"feature": "X", "status": "QUALIFIED", "regression": True, "evidence": [
        {"type": "unit_test", "what": "t"},
        {"type": "analytic", "what": "a"},
        {"type": "measurement", "source": "instrument", "sample": "OUT_OF_SAMPLE",
         "instrument": "DSOX3024T", "file": "x.csv", "what": "实测阶跃"}]}
    ok, probs = validation.check_entry(full)
    assert ok, probs


def test_cross_checked_needs_independent_source():
    import validation
    only_tests = {"feature": "X", "status": "CROSS_CHECKED",
                  "evidence": [{"type": "unit_test", "what": "t"}]}
    ok, probs = validation.check_entry(only_tests)
    assert not ok
    assert any("独立信源" in p for p in probs)


def test_experimental_is_never_promoted():
    """EXPERIMENTAL 有条目也不会被"证据"升格，反之不能被标成更高等级骗人。"""
    import validation
    reg = json.load(open(REG, encoding="utf-8"))
    em = [f for f in reg["features"] if "openEMS" in f["feature"]]
    assert em and em[0]["status"] == "EXPERIMENTAL"
    # 把它标成 CORRELATED 但没有实测 → 必须报错
    fake = dict(em[0], status="CORRELATED")
    ok, probs = validation.check_entry(fake)
    assert not ok


def test_no_feature_claims_qualified_without_measurement():
    """全表体检：任何 CORRELATED/QUALIFIED 都必须有仪器证据。"""
    import validation
    reg = json.load(open(REG, encoding="utf-8"))
    for f in reg["features"]:
        if f["status"] not in ("CORRELATED", "QUALIFIED"):
            continue
        meas = [e for e in f["evidence"] if e.get("type") in ("measurement", "field_solver")]
        assert meas, f"{f['feature']} 标了 {f['status']} 却没有实测/全波证据"
        for e in meas:
            if e["type"] == "measurement":
                assert str(e.get("source")).lower() != "synthetic"
                assert str(e.get("sample")).upper() == "OUT_OF_SAMPLE"
