"""P1-A：参数扫描（一维/二维、case 上限、按名改值）。"""
import pytest

import sweep_runner
from sweep_runner import SweepError


def test_normalize_one_dimensional():
    dims = sweep_runner.normalize_sweep({"parameter": "R", "values": [0, 10, 22]})
    assert dims == [{"name": "R", "values": [0, 10, 22]}]
    assert len(sweep_runner.expand_cases(dims)) == 3


def test_normalize_two_dimensional_product():
    dims = sweep_runner.normalize_sweep([
        {"name": "R_DAMP", "values": [0, 22, 33, 47]},
        {"name": "C_LOAD", "values": ["2p", "4p", "8p"]}])
    cases = sweep_runner.expand_cases(dims)
    assert len(cases) == 12
    assert cases[0]["params"] == {"R_DAMP": 0, "C_LOAD": "2p"}
    assert cases[-1]["params"] == {"R_DAMP": 47, "C_LOAD": "8p"}
    assert cases[0]["id"] != cases[1]["id"]


def test_max_cases_guard():
    with pytest.raises(SweepError) as e:
        sweep_runner.normalize_sweep(
            [{"name": "A", "values": list(range(20))},
             {"name": "B", "values": list(range(20))}], max_cases=200)
    assert e.value.code == "SWEEP_TOO_MANY_CASES"


def test_apply_params_element_value():
    deck = "R_CLK_DAMP n1 n2 22\nC1 a b 100n\n.end\n"
    out, counts = sweep_runner.apply_params(deck, {"R_CLK_DAMP": 47})
    assert "R_CLK_DAMP n1 n2 47" in out
    assert counts == {"R_CLK_DAMP": 1}


def test_apply_params_zero_ohm_is_clamped():
    deck = "R_DAMP a b 22\n.end\n"
    out, _ = sweep_runner.apply_params(deck, {"R_DAMP": 0})
    assert out.splitlines()[0].endswith(" 1m")


def test_apply_params_dot_param():
    deck = ".param DUTY=0.5\nI1 a 0 PULSE(0 1 0 1n 1n {DUTY*10u} 10u)\n.end\n"
    out, counts = sweep_runner.apply_params(deck, {"DUTY": 0.1})
    assert ".param DUTY=0.1" in out
    assert counts == {"DUTY": 1}


def test_apply_params_missing_is_an_error():
    with pytest.raises(SweepError) as e:
        sweep_runner.apply_params("R1 a b 1k\n.end\n", {"NOPE": 1})
    assert e.value.code == "SWEEP_PARAM_NOT_FOUND"


def test_retarget_raw_moves_into_case_dir():
    deck = "tran 1n 10n\nwrite tran.raw all\n.end\n"
    out = sweep_runner.retarget_raw(deck, "runs/001_x")
    assert "write runs/001_x/tran.raw all" in out


def test_flatten_metrics():
    m = {"a": {"metric": "tran_stats", "max": 1.0, "min": 0.0, "peak": [1, 2]},
         "b": {"metric": "x", "error": "E"}}
    flat = sweep_runner.flatten_metrics(m)
    assert flat["a.max"] == 1.0
    assert flat["b"] == "ERROR:E"
    assert "a.peak" not in flat
