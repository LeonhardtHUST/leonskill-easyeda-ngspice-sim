"""v2.4 round-trip 验证（需要 ngspice）：.sNp → vector fit → SPICE → ngspice sp → 比较。

这是"模型真的能在 ngspice 里跑出原网络"的唯一证据，也是发布门槛之一。
"""
import json
import os
import shutil

import numpy as np
import pytest

from conftest import SKILL

NGSPICE = os.environ.get("NGSPICE_BIN") or os.path.expanduser(
    "~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe")
GOLDEN = os.path.join(SKILL, "tests", "golden")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not os.path.exists(NGSPICE), reason="本机没有 ngspice-47"),
]


@pytest.fixture
def work(tmp_path):
    d = str(tmp_path)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _gold(rel):
    import rf_network as rn
    return rn.RfNetwork.from_touchstone(os.path.join(GOLDEN, rel))


@pytest.mark.parametrize("rel,tol_db", [
    ("rf/ideal_thru.s2p", 0.1),
    ("rf/attenuator_6db.s2p", 0.1),
    ("rf/lc_lpf.s2p", 0.5),
    ("rf/bpf_100mhz.s2p", 1.0),
    ("si/via_mismatch.s2p", 0.5),
    ("si/source_termination.s2p", 0.5),
])
def test_roundtrip_golden_2port(work, rel, tol_db):
    import network_validate as nv
    import vector_fit as vf
    net = _gold(rel)
    res = vf.fit(net, order="auto", max_order=20, target_rms_db=tol_db / 2,
                 log=lambda *_: None)
    sub = "M_" + os.path.basename(rel).replace(".", "_")
    path = vf.write_spice(res, os.path.join(work, sub + ".sp"), sub)
    rep, jpath = nv.validate(path, sub, net, work, tol_db=tol_db,
                             passivity=res["passivity"])
    assert os.path.exists(jpath)
    assert rep["verdict"] == "PASS", json.dumps(
        {k: rep[k] for k in ("rms_mag_error_db", "max_mag_error_db",
                             "max_phase_error_deg")}, ensure_ascii=False)
    assert rep["max_mag_error_db"] <= tol_db
    assert rep["note"].startswith("round-trip")


def test_roundtrip_records_phase_and_group_delay(work):
    import network_validate as nv
    import vector_fit as vf
    net = _gold("rf/lc_lpf.s2p")
    res = vf.fit(net, order="auto", max_order=20, target_rms_db=0.2,
                 log=lambda *_: None)
    path = vf.write_spice(res, os.path.join(work, "M.sp"), "M")
    rep, _ = nv.validate(path, "M", net, work, tol_db=0.5)
    assert "rms_phase_error_deg" in rep and rep["rms_phase_error_deg"] < 5.0
    assert "rms_group_delay_error_s" in rep


def test_roundtrip_detects_broken_model(work):
    """故意把子电路端口接反/名字写错时，round-trip 必须失败而不是"看起来正常"。"""
    import network_validate as nv
    import vector_fit as vf
    net = _gold("rf/attenuator_6db.s2p")
    res = vf.fit(net, order="auto", max_order=12, target_rms_db=0.1,
                 log=lambda *_: None)
    path = vf.write_spice(res, os.path.join(work, "M.sp"), "M")
    with pytest.raises(nv.ValidationError):
        nv.run_roundtrip(path, "WRONG_SUBCKT_NAME", net, work)


def test_4port_roundtrip_diff_pair(work):
    """4 端口也能 round-trip（差分对用例）。"""
    import network_validate as nv
    import vector_fit as vf
    net = _gold("si/diff_pair_mismatch_5pct.s4p")
    res = vf.fit(net, order="auto", max_order=20, target_rms_db=0.5,
                 log=lambda *_: None)
    path = vf.write_spice(res, os.path.join(work, "DP.sp"), "DP")
    rep, _ = nv.validate(path, "DP", net, work, tol_db=1.0,
                         passivity=res["passivity"])
    assert rep["nports"] == 4
    assert rep["verdict"] == "PASS", rep["max_mag_error_db"]
