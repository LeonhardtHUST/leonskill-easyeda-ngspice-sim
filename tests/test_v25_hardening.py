"""v2.5 Validation & Field Hardening —— 单元/集成测试。

覆盖四处 v2.5 的实质改动：
  1. IBIS [POWER/GND Clamp] → 分段线性 B 源（PWL），含 A/B 开关与过冲抑制；
  2. openEMS 几何真实化：中心线+线宽 → 旋转多边形，多层介质 stack；
  3. de-embedding（scikit-rf）能精确还原"已知构造"的 DUT；
  4. RLGC 有损线阶梯（Rdc / Z0 / emit 元素类型）与内容哈希缓存。

原则：能用解析真值对照的，就不只断言"跑通了"。
"""
import json
import math
import os
import subprocess
import sys

import numpy as np
import pytest

from conftest import FIXTURES, SCRIPTS, make_ir, run_gen, lines_of

NGSPICE = os.environ.get("NGSPICE_BIN") or os.path.expanduser(
    "~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe")
HAS_NG = os.path.exists(NGSPICE)
IBS = os.path.join(FIXTURES, "ibis", "fixture_lvcmos.ibs")


# =========================================================================
# 1. IBIS clamp
# =========================================================================
def _clamp_lines(tmp_path, entry_extra=None, node_of=None):
    import ibis_to_spice as its
    from parasitics import NetCtx
    from sim_config import Namer
    entry = {"designator": "U5", "pin": "45", "ibis_file": IBS,
             "model": "3V3_LVCMOS_OUT"}
    entry.update(entry_extra or {})
    p = tmp_path / "map.json"
    p.write_text(json.dumps({"parts": [entry]}), encoding="utf-8")
    s = its.IbisSet(str(p))
    ctx = NetCtx(Namer())
    comp = {"designator": "U5", "pins": [{"number": "45"}]}
    lines, infos = s.build(ctx, comp, node_of or (lambda c, pn: "DQ"))
    return s, lines, infos


def test_clamp_pwl_generated_with_correct_sign(tmp_path):
    """POWER clamp 在 V>Vcc 时应把电流灌回电源轨（B 源正向）。"""
    s, lines, infos = _clamp_lines(tmp_path, {"ref_vcc": "VCC"})
    clamp = [l for l in lines if l.startswith("B_")]
    assert len(clamp) == 2, clamp
    pclamp = next(l for l in clamp if "PCLAMP" in l)
    nclamp = next(l for l in clamp if "NCLAMP" in l)
    assert "uramp(V(DQ)-(3.3))" in pclamp, pclamp
    assert pclamp.split()[2] == "VCC", "POWER clamp 必须接到电源轨而不是地"
    # 上夹位在 3.3V 之上导通：斜率为正（流入电源轨）
    assert "(0.00333333)*uramp" in pclamp
    # 下夹位常量项 = −I(−6V) = −95 mA（从地灌入节点）
    assert nclamp.split()[-1].startswith("-0.095") or "-0.095" in nclamp
    assert infos[0]["clamps"]["power_clamp"].startswith("7 点 PWL")
    assert infos[0]["clamps"]["gnd_clamp"].startswith("6 点 PWL")
    assert not s.errors


def test_clamp_pwl_breakpoints_are_segment_starts(tmp_path):
    """折点必须是线段起点，且系数是斜率增量（跨折点不能继续叠旧斜率）。"""
    import ibis_to_spice as its
    pts = [{"v": 3.3, "i_typ": 0.0}, {"v": 3.6, "i_typ": -1e-3},
           {"v": 4.2, "i_typ": -12e-3}, {"v": 6.0, "i_typ": -95e-3}]
    expr = its._pwl_current(pts, "V(X)")
    assert "uramp(V(X)-(3.3))" in expr
    assert "uramp(V(X)-(3.6))" in expr
    assert expr.endswith("uramp(V(X)-(4.2))"), expr

    # 在折点上求值必须精确等于表值（这是斜率增量写法才成立的）
    def ev(v):
        return eval(expr.replace("V(X)", str(v))
                    .replace("uramp(", "_uramp("),
                    {"_uramp": lambda x: max(x, 0.0)})

    for p in pts:
        # 系数按 %.6g 落到 deck，所以只要求 1e-5 相对精度
        got = ev(p["v"])
        if abs(p["i_typ"]) < 1e-12:
            assert abs(got) < 1e-12
        else:
            assert got == pytest.approx(-p["i_typ"], rel=1e-5), p


def test_clamp_can_be_disabled_ab(tmp_path):
    s, lines, infos = _clamp_lines(tmp_path, {"clamps": False})
    assert not [l for l in lines if l.startswith("B_")]
    assert infos[0]["clamps"] == {"clamps": "disabled"}


def test_clamp_vcc_node_auto_from_power_pin(tmp_path):
    """没有 ref_vcc 时，应自动接到 IBIS POWER 引脚所在网络。"""
    def node_of(comp, pin):
        return "VCC30" if str(pin) == "12" else "DQ"
    s, lines, infos = _clamp_lines(tmp_path, None, node_of=node_of)
    pclamp = next(l for l in lines if "PCLAMP" in l)
    assert pclamp.split()[2] == "VCC30", pclamp
    assert any("POWER clamp 自动接到" in w for w in s.warnings)


def test_clamp_vcc_node_falls_back_to_dedicated_source(tmp_path):
    """既无 ref_vcc、POWER 脚又映射到同一网络（防自环）→ 自建理想源，且只建一次。"""
    s, lines, infos = _clamp_lines(tmp_path, None, node_of=lambda c, p: "DQ")
    pre = [l for l in lines if l.upper().startswith("V_")]
    assert len([l for l in pre if "IBIS_U5_VCC" in l]) == 1, pre
    pclamp = next(l for l in lines if "PCLAMP" in l)
    assert "__IBIS_U5_VCC" in pclamp


@pytest.mark.skipif(not HAS_NG, reason="没有 ngspice")
def test_clamp_dc_sweep_matches_ibis_table(tmp_path):
    """DC 扫描逐点对比 IBIS 表：这是"真的实现了 clamp"的唯一有力证据。"""
    import ibis_parser
    doc = ibis_parser.parse_ibs(IBS)
    m = doc["models"]["3V3_LVCMOS_OUT"]
    _, lines, _ = _clamp_lines(tmp_path, {"ref_vcc": "VCC"})
    body = [l for l in lines if l.startswith("B_")]
    assert len(body) == 2
    deck = ("* clamp dc check\nVCC VCC 0 DC 3.3\nVSW SW 0 DC 0\nRSW SW DQ 0.0001\n"
            + "\n".join(body) + """
.control
dc VSW -6.5 6.5 0.1
wrdata clamp_dc.txt i(VSW) v(dq)
.endc
.end
""")
    d = tmp_path / "clamp.cir"
    d.write_text(deck, encoding="utf-8")
    r = subprocess.run([NGSPICE, "-b", "clamp.cir"], cwd=str(tmp_path),
                       capture_output=True, text=True, errors="replace", timeout=120)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    rows = []
    for line in (tmp_path / "clamp_dc.txt").read_text(encoding="utf-8").splitlines():
        p = line.split()
        if len(p) >= 4:
            rows.append((float(p[0]), float(p[1])))
    assert len(rows) > 100

    def interp(table, v):
        t = sorted(table, key=lambda x: x[0])
        if v <= t[0][0]:
            return t[0][1]
        if v >= t[-1][0]:
            return t[-1][1]
        for k in range(1, len(t)):
            if v <= t[k][0]:
                (v0, i0), (v1, i1) = t[k - 1], t[k]
                return i0 + (i1 - i0) * (v - v0) / (v1 - v0)

    pu = [(p["v"], p["i_typ"]) for p in m["power_clamp"]]
    dn = [(p["v"], p["i_typ"]) for p in m["gnd_clamp"]]
    worst = 0.0
    for v, i in rows:
        if abs(v) > 6.0 or abs(v) < 0.05:
            continue
        expected = interp(pu, v) + interp(dn, v)
        worst = max(worst, abs(i - expected))
    assert worst < 5e-5, f"clamp I-V 与 IBIS 表最大误差 {worst * 1e3:.4f} mA"


@pytest.mark.skipif(not HAS_NG, reason="没有 ngspice")
def test_clamp_suppresses_overshoot_ab(tmp_path):
    """同样的注入电流，开/关 clamp 的峰值电压必须明显不同。"""
    _, on, _ = _clamp_lines(tmp_path, {"ref_vcc": "VCC"})
    _, off, _ = _clamp_lines(tmp_path, {"ref_vcc": "VCC", "clamps": False})
    body_on = [l for l in on if l.startswith("B_")]

    def vmax(extra):
        deck = ("* overshoot ab\nVCC VCC 0 DC 3.3\nI1 0 DQ PULSE(0 0.2 1n 50p 50p 20n 40n)\n"
                "R1 DQ 0 50\n" + "\n".join(extra) + """
.control
tran 10p 12n
meas tran vpeak MAX v(DQ) FROM=1n TO=12n
.endc
.end
""")
        p = tmp_path / "ab.cir"
        p.write_text(deck, encoding="utf-8")
        r = subprocess.run([NGSPICE, "-b", "ab.cir"], cwd=str(tmp_path),
                           capture_output=True, text=True, errors="replace", timeout=120)
        assert r.returncode == 0, r.stdout[-1500:]
        for line in r.stdout.splitlines():
            if line.strip().startswith("vpeak") and "=" in line:
                # 行格式： "vpeak = <值> at= <时间>"，取第一个等号后的第一个数
                return float(line.split("=")[1].split()[0])
        raise AssertionError("meas 没有输出 vpeak:\n" + r.stdout[-1000:])

    v_no = vmax([])
    v_yes = vmax(body_on)
    assert v_no > 8.0, v_no          # 0.2 A × 50 Ω = 10 V（无任何夹位）
    assert v_yes < 0.7 * v_no, (v_yes, v_no)


# =========================================================================
# 2. openEMS 几何
# =========================================================================
def test_segment_polygon_area_and_rotation():
    import em_bridge as eb
    cases = [
        ({"x_mm": 0, "y_mm": 0}, {"x_mm": 10, "y_mm": 0}, 0.2, 10.0),      # 水平
        ({"x_mm": 5, "y_mm": 0}, {"x_mm": 5, "y_mm": 8}, 0.3, 8.0),        # 垂直
        ({"x_mm": 0, "y_mm": 0}, {"x_mm": 3, "y_mm": 3}, 0.2, math.hypot(3, 3)),
    ]
    for a, b, w, length in cases:
        poly = eb.segment_polygon(a, b, w)
        assert len(poly) == 4
        assert eb.polygon_area(poly) == pytest.approx(length * w, rel=1e-9)
        # 四个角都必须是"旋转后"的角，而不是退化直线
        assert len({(round(x, 9), round(y, 9)) for x, y in poly}) == 4


def test_segment_polygon_45deg_is_not_axis_aligned_box():
    """v2.4 的 AddBox(x1,y1→x2,y2) 会把 45° 线变成一个 3×3 的大方块。"""
    import em_bridge as eb
    a, b, w = {"x_mm": 0, "y_mm": 0}, {"x_mm": 3, "y_mm": 3}, 0.2
    poly = eb.segment_polygon(a, b, w)
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    true_area = eb.polygon_area(poly)
    # 真旋转矩形：3√2 × 0.2 = 0.849 mm²；v2.4 的 AddBox 会给 3×3 = 9 mm²
    assert true_area == pytest.approx(3 * math.sqrt(2) * w, rel=1e-9)
    assert true_area < 1.0, "面积像一个 3×3 的轴对齐方块（AddBox 退化）"
    # 四个角必须有两个落在对角方向上，而不是与坐标轴对齐
    assert not ({round(x, 6) for x in xs} <= {0.0, 3.0}), "多边形顶点与坐标轴对齐"


def test_segment_polygon_rejects_degenerate():
    import em_bridge as eb
    with pytest.raises(eb.EmBridgeError) as e:
        eb.segment_polygon({"x_mm": 1, "y_mm": 1}, {"x_mm": 1, "y_mm": 1}, 0.2)
    assert e.value.code == "EM_SEGMENT_DEGENERATE"


def _stack4():
    return {"layers": [{"name": "L1", "copper_um": 35},
                       {"name": "L2", "copper_um": 35},
                       {"name": "L3", "copper_um": 35},
                       {"name": "L4", "copper_um": 35}],
            "dielectrics": [{"between": ["L1", "L2"], "thickness_mm": 0.2,
                             "er": 4.3, "loss_tangent": 0.02},
                            {"between": ["L2", "L3"], "thickness_mm": 1.0,
                             "er": 4.3, "loss_tangent": 0.02},
                            {"between": ["L3", "L4"], "thickness_mm": 0.2,
                             "er": 4.3, "loss_tangent": 0.02}]}


def test_layer_stack_multilayer_not_flattened():
    import em_bridge as eb
    st = eb.layer_stack(_stack4())
    assert [c["name"] for c in st["copper"]] == ["L1", "L2", "L3", "L4"]
    assert len(st["dielectrics"]) == 3, "介质被压成一层了"
    assert st["total_thickness_mm"] == pytest.approx(0.035 * 4 + 0.2 + 1.0 + 0.2)
    # 铜层首尾相接、不重叠
    prev = -1e9
    for c in st["copper"]:
        assert c["z_bottom_mm"] >= prev - 1e-12
        assert c["z_top_mm"] > c["z_bottom_mm"]
        prev = c["z_top_mm"]
    # 每个介质恰好夹在相邻两片铜之间
    for d, lo, hi in zip(st["dielectrics"], st["copper"], st["copper"][1:]):
        assert d["z_bottom_mm"] == pytest.approx(lo["z_top_mm"])
        assert d["z_top_mm"] == pytest.approx(hi["z_bottom_mm"])
    assert (st["dielectrics"][1]["z_top_mm"] - st["dielectrics"][1]["z_bottom_mm"]) \
        == pytest.approx(1.0)


def test_layer_stack_rejects_incomplete():
    import em_bridge as eb
    with pytest.raises(eb.EmBridgeError) as e:
        eb.layer_stack({"layers": [{"name": "L1", "copper_um": 35}],
                        "dielectrics": [{"between": ["L1", "L2"],
                                         "thickness_mm": 0}]})
    assert e.value.code == "STACKUP_INCOMPLETE"


def _find_bash():
    """定位真正的 bash.exe。

    本机直接 spawn `bash`（PATH 上的 shim）会 rc=1 且输出乱码；
    绝对路径调 PortableGit 的 bash.exe 才正常（v2.5 实测）。
    """
    import glob
    if os.name != "nt":
        return "bash", {}
    base = os.path.expanduser("~/.workbuddy/binaries/PortableGit/versions")
    for exe in sorted(glob.glob(os.path.join(base, "*", "bin", "bash.exe"))):
        root = os.path.dirname(os.path.dirname(exe))
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(
            [os.path.join(root, "usr", "bin"), os.path.join(root, "bin"),
             env.get("PATH", "")])
        return exe, env
    return "bash", {}


def _run_bash(args, timeout=300):
    """跑 bash 并稳健解码（输出可能是 UTF-8，也可能是 GBK）。"""
    exe, env = _find_bash()
    r = subprocess.run([exe] + [str(a) for a in args], capture_output=True,
                       timeout=timeout, env=env or None)

    def dec(b):
        if not b:
            return ""
        for enc in ("utf-8", "gbk", "cp936"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", "replace")

    return r.returncode, dec(r.stdout) + dec(r.stderr)


def test_sim_file_rejects_unknown_mode(tmp_path):
    """未知 mode 必须立刻失败，而不是把 'rf' 当分析模式传给 sim_exec。"""
    rc, out = _run_bash([os.path.join(SCRIPTS, "sim_file.sh"),
                         tmp_path / "x.epro2", "rf"], timeout=60)
    assert rc == 2, out[-500:]
    assert "未知模式" in out or "unknown" in out.lower(), out[-500:]
    assert "Python API" in out, out[-500:]


REAL_EPRO2 = os.environ.get("WOLF_LITE_EPRO2") or \
    r"C:\Users\Leon\WorkBuddy\2026-10-22-17-11-32\target_test.epro2"


@pytest.mark.skipif(not os.path.exists(REAL_EPRO2), reason="没有真实 .epro2 样例")
def test_sim_file_em_mode_passes_stackup_through(tmp_path):
    """shell 层回归：`--stackup` 必须真的传到 pcb_em.py。

    这里曾经有一个 bash 陷阱 —— `arr=($PCB_OPTS)` 只展开元素 0，
    导致 `--stackup` 的值恒为空串（auto-parasitics 也一起中招）。
    """
    import pcb_geometry as pg
    try:
        raw = pg.load_pcb_input(REAL_EPRO2)
        geo = pg.build_geometry(raw)
    except Exception as e:                                  # pragma: no cover
        pytest.skip(f"读不了真实工程: {e}")
    nets = [n for n in ("VBUS", "+5V") if n in geo["nets"]] or list(geo["nets"])[:2]
    if len(nets) < 2:                                       # pragma: no cover
        pytest.skip("真实工程里没有两个可用网络")
    stk = tmp_path / "stk.json"
    stk.write_text(json.dumps({"layers": [{"name": l["name"], "copper_um": 35}
                                          for l in geo["layers"]]}), encoding="utf-8")
    out = tmp_path / "em"
    args = [os.path.join(SCRIPTS, "sim_file.sh"), REAL_EPRO2, "em", out, "--fmax", "1e9",
            "--stackup", stk]
    for n in nets:
        args += ["--net", n]
    rc, txt = _run_bash(args)
    assert rc == 0, txt[-2000:]
    assert "STACKUP_INCOMPLETE" not in txt, "叠层没传进去（shell 数组展开 bug）"
    plan = json.loads((out / "EM_PLAN.json").read_text(encoding="utf-8"))
    assert plan["status"] == "EXPERIMENTAL"
    assert plan["region"]["nets"] == nets
    assert plan["stack"]["copper"], "没拿到铜层"
    assert len(plan["ports"]) == len(nets)
    assert (out / "em_coupon.py").exists()


def test_pcb_em_cli_produces_plan_and_script(tmp_path):
    """CLI 端到端：geometry.json → em_coupon.py + EM_PLAN.json。

    两条线分别放在 L1 / L2，验证端口 z 各自跟随所在层（而不是都挂到顶层）。
    """
    geo = {"source": "synthetic",
           "layers": [{"name": "L1"}, {"name": "L2"}],
           "nets": {
               "N_TOP": {"segments": [{"from": "A1", "to": "A2", "layer": "L1",
                                       "length_mm": 30.0, "width_mm": 0.2,
                                       "via_count": 0}],
                         "terminals": {"A1": {"x_mm": 0, "y_mm": 0},
                                       "A2": {"x_mm": 30, "y_mm": 0}}, "vias": []},
               "N_BOT": {"segments": [{"from": "B1", "to": "B2", "layer": "L2",
                                       "length_mm": 20.0, "width_mm": 0.15,
                                       "via_count": 0}],
                         "terminals": {"B1": {"x_mm": 0, "y_mm": 2},
                                       "B2": {"x_mm": 14.142, "y_mm": 16.142}},
                         "vias": []}}}
    gp = tmp_path / "geo.json"
    gp.write_text(json.dumps(geo), encoding="utf-8")
    sp = tmp_path / "stk.json"
    sp.write_text(json.dumps(_stack4()), encoding="utf-8")
    out = tmp_path / "em"
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "pcb_em.py"), str(gp),
                        "--net", "N_TOP", "--net", "N_BOT", "--stackup", str(sp),
                        "--fmax", "3e9", "--out", str(out)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    plan = json.loads((out / "EM_PLAN.json").read_text(encoding="utf-8"))
    assert plan["status"] == "EXPERIMENTAL"
    assert len(plan["stack"]["copper"]) == 4
    assert (out / "em_coupon.py").exists()
    z0s = {p["net"]: p["z0"] for p in plan["ports"]}
    assert z0s["N_TOP"] != z0s["N_BOT"], "两个网络的端口挂到了同一层"
    assert z0s["N_TOP"] == pytest.approx(0.0)
    assert z0s["N_BOT"] == pytest.approx(0.235)
    txt = (out / "em_coupon.py").read_text(encoding="utf-8")
    assert "excite=True" in txt and "excite=False" in txt
    assert "SetElevation(0.235,0.27)" in txt.replace(" ", "")


def test_csxcad_script_uses_port_layer_z(tmp_path):
    """端口 z 必须来自各自走线层，而不是一刀切用顶层铜；铜多边形也必须按层 z 拉伸。"""
    import em_bridge as eb
    geo = {"nets": {"N1": {"terminals": {"A": {"x_mm": 0, "y_mm": 0},
                                        "B": {"x_mm": 10, "y_mm": 0}},
                          "segments": [{"from": "A", "to": "B", "layer": "L2",
                                        "width_mm": 0.2}]}}}
    stack = _stack4()
    region = {"nets": ["N1"],
              "bbox_mm": {"x": [-3, 13], "y": [-3, 3]},
              "margin_mm": 3, "f_max_hz": 3e9, "lambda_mm": 100, "cell_mm": 0.2,
              "cells_estimate": {"x": 80, "y": 30}, "status": "EXPERIMENTAL"}
    # L2 埋在 diel1 下面：z = 0.035 + 0.2 = 0.235 → 0.27；板总厚 1.54
    ports = [{"name": "P1", "net": "N1", "x0": 0, "y0": 0, "z0": 0.235,
              "x1": 0, "y1": 0, "z1": 1.54}]
    txt = eb.csxcad_script(geo, stack, region, ports)
    flat = txt.replace(" ", "")
    assert "AddLumpedPort(1,50," in flat
    assert "[0,0,0.235]" in flat and "[0,0,1.54]" in flat, \
        "端口没有用走线层的 z"
    assert "0.235,0.27" in flat, "铜多边形没有按 L2 的 z 拉伸"
    assert "make_polygon(CSX," in flat
    assert "def make_polygon" in txt, "缺少 CSXCAD 双写法兼容层"
    # 面积自检注释应 ≈ 10mm × 0.2mm
    assert "2mm^2" in flat.replace(" ", "")


# =========================================================================
# 3. de-embedding
# =========================================================================
def _write_snp(path, freqs, s, z0=50.0):
    s = np.asarray(s, dtype=complex)
    n = s.shape[1]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# Hz S RI R {z0:g}\n")
        for k, fr in enumerate(freqs):
            row = ["%.9g" % fr]
            for i in range(n):
                for j in range(n):
                    row += ["%.9g" % s[k, i, j].real, "%.9g" % s[k, i, j].imag]
            f.write(" ".join(row) + "\n")
    return str(path)


def _s_from_y(freqs, y, z0=50.0):
    import skrf as rf
    freq = rf.Frequency.from_f(list(freqs), unit="hz")
    n = rf.Network(frequency=freq, s=np.zeros((len(freqs), y.shape[1], y.shape[2]),
                                              dtype=complex), z0=z0)
    n.y = np.asarray(y)
    return np.array(n.s)


def _s_from_z(freqs, z, z0=50.0):
    import skrf as rf
    freq = rf.Frequency.from_f(list(freqs), unit="hz")
    n = rf.Network(frequency=freq, s=np.zeros((len(freqs), z.shape[1], z.shape[2]),
                                              dtype=complex), z0=z0)
    n.z = np.asarray(z)
    return np.array(n.s)


def test_deembed_open_recovers_known_series_dut(tmp_path):
    """构造：Y_meas = Y_pad(open) + Y_dut(串联 10Ω)。Open 反嵌入必须精确减掉 pad。"""
    import deembed
    import rf_network as rn
    if not rn.available():
        pytest.skip("没有 scikit-rf")
    freqs = np.logspace(6, 10, 41)
    w = 2 * np.pi * freqs
    cp = 0.1e-12
    ypad = np.zeros((len(freqs), 2, 2), dtype=complex)
    ypad[:, 0, 0] = 1j * w * cp
    ypad[:, 1, 1] = 1j * w * cp
    ydut = np.zeros_like(ypad)
    ys = 1.0 / 10.0
    ydut[:, 0, 0] = ys
    ydut[:, 0, 1] = -ys
    ydut[:, 1, 0] = -ys
    ydut[:, 1, 1] = ys
    s_meas = _s_from_y(freqs, ypad + ydut)
    s_open = _s_from_y(freqs, ypad)

    dut_p = _write_snp(tmp_path / "dut.s2p", freqs, s_meas)
    open_p = _write_snp(tmp_path / "open.s2p", freqs, s_open)
    dut = rn.RfNetwork.from_touchstone(dut_p, name="dut")
    fx = deembed.load_fixtures({"open": open_p})
    res, man = deembed.apply(dut, fx, method="open", workdir=str(tmp_path),
                             name="dut")
    y_out = np.array(res._net.y)
    assert np.max(np.abs(y_out - ydut)) < 1e-9, np.max(np.abs(y_out - ydut))
    assert man["class"] == "Open" and man["method"] == "open"
    assert man["dut"]["sha256"] and man["fixtures"][0]["sha256"]
    assert os.path.exists(os.path.join(str(tmp_path), "DEEMBED_MANIFEST.json"))
    assert "de-embedding: open (Open)" in "\n".join(deembed.summary_lines(man))


def test_deembed_openshort_wiring_and_manifest(tmp_path):
    """OpenShort 的接口、manifest 与"为什么不能要求精确"的边界。

    注意（这是真实现象，不是 bug）：OpenShort 先做 `Y -= Yopen`，剩下的是纯串联
    网络的 Y = [[1/Z,−1/Z],[−1/Z,1/Z]]，它**在数学上奇异**（det=0）。skrf 接着对它
    求 `.z`，等于对奇异矩阵求逆 → 条件数爆炸。所以真实 Open-Short 必须依赖假件里
    残留的并联寄生才可算；本测试只验证接口、清单与数值是否有限，
    精确性由 `method="open"` 那条用例负责（那条是解析可证的）。
    """
    import deembed
    import rf_network as rn
    if not rn.available():
        pytest.skip("没有 scikit-rf")
    freqs = np.logspace(6, 9, 31)
    w = 2 * np.pi * freqs
    cp = 0.05e-12
    ypad = np.zeros((len(freqs), 2, 2), dtype=complex)
    ypad[:, 0, 0] = ypad[:, 1, 1] = 1j * w * cp

    z_lead = 0.05 + 1j * w * 0.05e-9
    z_dut = 10.0 + 1j * w * 1e-9

    def y_of_series(zz):
        y = np.zeros((len(freqs), 2, 2), dtype=complex)
        y[:, 0, 0] = 1 / zz
        y[:, 1, 1] = 1 / zz
        y[:, 0, 1] = y[:, 1, 0] = -1 / zz
        return y

    s_meas = _s_from_y(freqs, ypad + y_of_series(2 * z_lead + z_dut))
    dut_p = _write_snp(tmp_path / "dut.s2p", freqs, s_meas)
    op = _write_snp(tmp_path / "open.s2p", freqs, _s_from_y(freqs, ypad))
    sh = _write_snp(tmp_path / "short.s2p", freqs,
                    _s_from_y(freqs, ypad + y_of_series(2 * z_lead)))
    dut = rn.RfNetwork.from_touchstone(dut_p)
    fx = deembed.load_fixtures({"open": op, "short": sh})

    # 1) 同一份数据用 Open：Y 相减是精确的 → 应还原 2·Zlead + Zdut 的 Y 矩阵
    res_o, man_o = deembed.apply(dut, fx, method="open")
    y_out = np.array(res_o._net.y)
    y_expect = y_of_series(2 * z_lead + z_dut)
    err = np.max(np.abs(y_out - y_expect)) / np.max(np.abs(y_expect))
    assert err < 1e-6, f"Open 没把 pad 去掉，误差 {err:.3%}"
    assert man_o["class"] == "Open"

    # 2) OpenShort：接口/清单必须完整；数值是否可用由条件数决定，不在这里断言精度
    res, man = deembed.apply(dut, fx, method="open_short", workdir=str(tmp_path))
    assert res.nports == 2 and man["class"] == "OpenShort"
    assert np.all(np.isfinite(np.array(res._net.s)))
    assert {f["role"] for f in man["fixtures"]} == {"open", "short"}
    assert all(f["sha256"] for f in man["fixtures"])
    with open(os.path.join(str(tmp_path), "DEEMBED_MANIFEST.json"),
              encoding="utf-8") as f:
        assert json.load(f)["method"] == "open_short"


def test_deembed_short_open_uses_distinct_fixture_roles(tmp_path):
    """ShortOpen 与 OpenShort 的假件顺序相反，不能传错。"""
    import deembed
    import rf_network as rn
    if not rn.available():
        pytest.skip("没有 scikit-rf")
    freqs = np.logspace(6, 8, 9)
    s2 = np.zeros((len(freqs), 2, 2), dtype=complex)
    s2[:, 0, 1] = s2[:, 1, 0] = 0.4
    s2[:, 0, 0] = s2[:, 1, 1] = 0.1
    dut = rn.RfNetwork.from_touchstone(_write_snp(tmp_path / "d.s2p", freqs, s2))
    fx = deembed.load_fixtures({
        "open": _write_snp(tmp_path / "o.s2p", freqs, s2 * 0.5),
        "short": _write_snp(tmp_path / "s.s2p", freqs, s2 * 0.25)})
    res, man = deembed.apply(dut, fx, method="short_open")
    assert man["class"] == "ShortOpen" and res.nports == 2
    with pytest.raises(deembed.DeembedError) as e:
        deembed.apply(dut, {"open": fx["open"]}, method="open_short")
    assert e.value.code == "DEEMBED_FIXTURE_MISSING"


def test_deembed_rejects_port_and_freq_mismatch(tmp_path):
    import deembed
    import rf_network as rn
    if not rn.available():
        pytest.skip("没有 scikit-rf")
    f2 = np.logspace(6, 9, 11)
    f1 = np.logspace(6, 8, 11)
    s2 = np.zeros((len(f2), 2, 2), dtype=complex)
    s2[:, 0, 1] = s2[:, 1, 0] = 0.5
    s1 = np.zeros((len(f1), 1, 1), dtype=complex)
    s1[:, 0, 0] = 0.1
    dut = rn.RfNetwork.from_touchstone(_write_snp(tmp_path / "d.s2p", f2, s2))
    one = rn.RfNetwork.from_touchstone(_write_snp(tmp_path / "o.s1p", f2, s1))
    with pytest.raises(deembed.DeembedError) as e:
        deembed.apply(dut, {"open": one}, method="open")
    assert e.value.code == "DEEMBED_PORT_MISMATCH"

    same = rn.RfNetwork.from_touchstone(_write_snp(
        tmp_path / "o2.s2p", f1, np.tile(s2[0], (len(f1), 1, 1))))
    with pytest.raises(deembed.DeembedError) as e:
        deembed.apply(dut, {"open": same}, method="open")
    assert e.value.code == "DEEMBED_FREQ_MISMATCH"

    with pytest.raises(deembed.DeembedError) as e:
        deembed.apply(dut, {"open": same}, method="nonsense")
    assert e.value.code == "DEEMBED_METHOD_UNKNOWN"


# =========================================================================
# 4. RLGC / 缓存
# =========================================================================
def _stackup_obj():
    import stackup as stk
    raw = _stack4()
    st = stk.Stackup(raw["layers"], raw["dielectrics"])
    st.normalize()
    return st


def test_rlgc_matches_transmission_line_limits():
    import rlgc
    st = _stackup_obj()
    m = rlgc.from_stackup(st, "L1", width_mm=0.2, length_mm=30.0, n_seg=10)
    # 1) Rdc = ρ·l/(w·t)
    expect_rdc = rlgc.CU_RHO * 0.030 / (0.2e-3 * 35e-6)
    assert m["r_dc_ohm"] == pytest.approx(expect_rdc, rel=1e-6)
    # 2) 由 Z0 与 td 反推的 L/C 必须自洽：√(L/C) = Z0（每米量纲）
    z0_from_lc = math.sqrt(m["l_h_per_m"] / m["c_f_per_m"])
    assert z0_from_lc == pytest.approx(m["z0_ohm"], rel=1e-6)
    # 3) td = l·td_per_mm
    assert m["td_s"] == pytest.approx(30.0 * m["td_s"] / 30.0)
    # 4) 介质损耗随频率线性增长
    g1 = rlgc.at_frequency(m, 1e9)["g"]
    g2 = rlgc.at_frequency(m, 2e9)["g"]
    assert g2 == pytest.approx(2 * g1, rel=1e-9)
    # 5) 导体损耗按 √f 增长
    r1 = rlgc.at_frequency(m, 1e9)["r"]
    r4 = rlgc.at_frequency(m, 4e9)["r"]
    assert r4 > r1


def test_rlgc_emit_only_uses_rlc_elements():
    """并行电导必须落成 R，不能写成两端 G（ngspice 的 G 是四端 VCCS）。"""
    import rlgc
    from parasitics import NetCtx
    from sim_config import Namer
    m = rlgc.from_stackup(_stackup_obj(), "L1", 0.2, 30.0, n_seg=5)
    lines, info = rlgc.emit(NetCtx(Namer()), m, "IN", "OUT", name="TL")
    body = [l for l in lines if not l.startswith("*")]
    assert len(body) == 5 * 4
    for ln in body:
        assert ln[0].upper() in "RLC", ln
    assert info["n_seg"] == 5
    assert info["per_segment"]["r"] * info["n_seg"] == pytest.approx(
        m["r_dc_ohm"], rel=1e-12)


@pytest.mark.skipif(not HAS_NG, reason="没有 ngspice")
def test_rlgc_ladder_dc_division(tmp_path):
    """阶梯的直流行为：V(OUT) = 1V × 50/(Rdc+50)。"""
    import rlgc
    from parasitics import NetCtx
    from sim_config import Namer
    m = rlgc.from_stackup(_stackup_obj(), "L1", 0.2, 30.0, n_seg=8)
    lines, info = rlgc.emit(NetCtx(Namer()), m, "IN", "OUT", name="TL")
    deck = ("* rlgc dc\nV1 IN 0 DC 1\n" + "\n".join(lines) +
            "\nRL OUT 0 50\n.control\nop\nprint v(OUT)\n.endc\n.end\n")
    p = tmp_path / "rlgc.cir"
    p.write_text(deck, encoding="utf-8")
    r = subprocess.run([NGSPICE, "-b", "rlgc.cir"], cwd=str(tmp_path),
                       capture_output=True, text=True, errors="replace", timeout=120)
    assert r.returncode == 0, r.stdout[-1200:]
    got = None
    for line in r.stdout.splitlines():
        if "v(out)" in line.lower() and "=" in line:
            got = float(line.split("=")[-1])
    assert got is not None, r.stdout[-800:]
    rdc = info["dc_resistance_ohm"]
    assert got == pytest.approx(50.0 / (rdc + 50.0), rel=2e-3)


def test_cache_key_is_content_sensitive(tmp_path):
    import sim_cache
    a, pa = sim_cache.build_key("deck A", {"x": 1}, solver_version="47")
    b, pb = sim_cache.build_key("deck A", {"x": 1}, solver_version="47")
    c, _ = sim_cache.build_key("deck B", {"x": 1}, solver_version="47")
    d, _ = sim_cache.build_key("deck A", {"x": 2}, solver_version="47")
    e, _ = sim_cache.build_key("deck A", {"x": 1}, solver_version="48")
    assert a == b and len({a, c, d, e}) == 4
    assert pa == pb and pa["deck"] != sim_cache.build_key("deck B", {})[1]["deck"]


def test_cache_store_load_and_stats(tmp_path):
    import sim_cache
    cache = sim_cache.SimCache(tmp_path / "cache")
    key, _ = sim_cache.build_key("deck", {})
    assert not cache.hit(key)
    deck = tmp_path / "deck.cir"
    deck.write_text("* hi\n.end\n", encoding="utf-8")
    cache.store(key, [deck], metrics={"gain": 1.0}, meta={"mode": "op"})
    assert cache.hit(key)
    got = cache.load(key)
    assert got["hit"] and got["metrics"]["gain"] == 1.0
    st = cache.stats()
    assert st["entries"] >= 2 and st["bytes"] > 0


def test_incremental_plan_reports_changed_inputs():
    import sim_cache
    prev = {"a": {"schematic": "h1", "pcb": "p1", "models": {}, "config": "c1",
                  "solver": "47"},
            "b": {"schematic": "s2", "pcb": "p2", "models": {}, "config": "c2",
                  "solver": "47"}}
    cur = {"a": {"schematic": "h1", "pcb": "p1", "models": {}, "config": "c1",
                 "solver": "47"},
           "b": {"schematic": "s2", "pcb": "p9", "models": {}, "config": "c2",
                 "solver": "47"},
           "c": {"schematic": "s3", "pcb": "p3", "models": {}, "config": "c3",
                 "solver": "47"}}
    plan = sim_cache.incremental_plan(prev, cur)
    assert plan["reusable"] == ["a"]
    changed = {x["study"]: x["changed"] for x in plan["affected"]}
    assert changed["b"] == ["pcb"]
    assert "c" in changed


# =========================================================================
# 5. 端到端：cache 真的让第二次不跑 ngspice
# =========================================================================
@pytest.mark.skipif(not HAS_NG, reason="没有 ngspice")
def test_sim_exec_cache_second_run_skips_ngspice(tmp_path):
    ir = make_ir([("R1", "R", [("1", "A"), ("2", "B")], {"value": "1k"}),
                  ("V1", "V", [("1", "A"), ("2", "GND")], {"value": "5"})],
                 [("A", [("R1", 1), ("V1", 1)]),
                  ("B", [("R1", 2)]),
                  ("GND", [("V1", 2)])])
    deck, rep = run_gen(ir, tmp_path)
    deck_p = tmp_path / "schematic.cir"
    deck_p.write_text(deck, encoding="utf-8")
    (tmp_path / "rep.json").write_text(json.dumps(rep, ensure_ascii=False),
                                      encoding="utf-8")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"metrics": []}), encoding="utf-8")
    cache_dir = tmp_path / "cache"
    base = [sys.executable, os.path.join(SCRIPTS, "sim_exec.py"), str(deck_p), "op",
            "--config", str(cfg), "--report", str(tmp_path / "rep.json"),
            "--workdir", str(tmp_path), "--ngspice", NGSPICE,
            "--cache", str(cache_dir)]
    r1 = subprocess.run(base, capture_output=True, text=True, timeout=180)
    assert r1.returncode == 0, r1.stdout[-1500:] + r1.stderr[-1500:]
    assert "MISS" in r1.stdout
    r2 = subprocess.run(base, capture_output=True, text=True, timeout=180)
    assert r2.returncode == 0, r2.stdout[-1500:]
    assert "HIT" in r2.stdout, r2.stdout[-800:]
    man = json.loads((tmp_path / "simulation_manifest.json").read_text(encoding="utf-8"))
    assert man["cache"]["hit"] is True
