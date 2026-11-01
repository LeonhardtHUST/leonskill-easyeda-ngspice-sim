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


def test_clamp_pwl_uses_ibis_vcc_relative_voltage(tmp_path):
    """v2.5.1：POWER clamp 的自变量必须是 `V(VCC) − V(DQ)`（IBIS Vtable = Vcc−Vout）。

    v2.5 用的是 `V(DQ)`，加上 fixture 本身也非标准，两个错误互相抵消。
    """
    s, lines, infos = _clamp_lines(tmp_path, {"ref_vcc": "VCC"})
    clamp = [l for l in lines if l.startswith("B_")]
    assert len(clamp) == 2, clamp
    pclamp = next(l for l in clamp if "PCLAMP" in l)
    nclamp = next(l for l in clamp if "NCLAMP" in l)
    assert "uramp((V(VCC)-V(DQ))-(-6.6))" in pclamp, pclamp
    assert "(V(VCC)-V(DQ))" in pclamp, "POWER clamp 仍在用 V(DQ) 当自变量"
    assert "uramp(V(DQ)-" not in pclamp, "POWER clamp 的自变量不该是 V(DQ)"
    assert pclamp.split()[2] == "VCC", "POWER clamp 必须接到电源轨而不是地"
    # GND clamp 是 ground-relative：自变量就是 V(DQ)
    assert "uramp(V(DQ)-(-6.6))" in nclamp, nclamp
    assert nclamp.split()[2] == "0"
    # 不再统一取负：表里的符号就是方向（POWER 导通为正、GND 导通为负）
    assert pclamp.split("=", 1)[1].strip().startswith("0.095"), pclamp
    assert nclamp.split("=", 1)[1].strip().startswith("-0.095"), nclamp
    assert infos[0]["clamps"]["power_clamp"].startswith("7 点 PWL")
    assert infos[0]["clamps"]["gnd_clamp"].startswith("7 点 PWL")
    assert infos[0]["clamps"]["references"]["power"]["source"] == "[Voltage Range]"
    assert not s.errors


def test_ibis_fixture_is_standards_compliant():
    """fixture 本身必须是标准 IBIS 形状，否则测试只是在自我印证。

    IBIS 4.0 的例子：
        [GND Clamp]   -5.0V -3900m / -0.7V -80m / -0.4V 0 / 5.0V 0
        [POWER Clamp] -5.0V  4450m / -0.7V  95m / -0.4V 0 / 0.0V 0
    """
    import ibis_parser
    m = ibis_parser.parse_ibs(IBS)["models"]["3V3_LVCMOS_OUT"]
    pu = m["power_clamp"]
    dn = m["gnd_clamp"]
    # POWER：只在 Vtable < 0 导通，且电流**为正**
    assert [p["v"] for p in pu][0] < 0, "POWER clamp 表必须从负电压开始"
    assert all(p["i_typ"] >= 0 for p in pu), "POWER clamp 电流必须非负（导通为正）"
    assert max(p["i_typ"] for p in pu if p["v"] > 0) == 0.0
    assert max(pu, key=lambda p: p["i_typ"])["v"] < 0
    # GND：同样只在 Vtable < 0 导通，但电流**为负**
    assert [p["v"] for p in dn][0] < 0
    assert all(p["i_typ"] <= 0 for p in dn), "GND clamp 电流必须非正（导通为负）"
    assert min(dn, key=lambda p: p["i_typ"])["v"] < 0
    assert min(p["i_typ"] for p in dn if p["v"] > 0) == 0.0
    # 两张表都要覆盖 −Vcc..+2Vcc（规范要求），否则仿真只能外推
    for tbl in (pu, dn):
        assert min(p["v"] for p in tbl) <= -3.3
        assert max(p["v"] for p in tbl) >= 6.6
    # [Voltage Range] 必须存在（POWER clamp 的参考）
    assert m.get("voltage_range", {}).get("typ") == pytest.approx(3.3)


def test_ibis_reference_precedence(tmp_path):
    """[POWER Clamp Reference] > [Pullup Reference] > [Voltage Range]。"""
    import ibis_model as im
    base = {"model": "M", "voltage_range": {"typ": 3.3},
            "pullup_ref": {"typ": 2.5}, "power_clamp_ref": {"typ": 1.8}}
    r = im.resolve_references(base, vcc=9.9)
    assert r["power"]["value"] == pytest.approx(1.8)
    assert r["power"]["source"] == "[POWER Clamp Reference]"
    r = im.resolve_references({**base, "power_clamp_ref": None}, vcc=9.9)
    assert r["power"]["value"] == pytest.approx(2.5)
    assert r["power"]["source"] == "[Pullup Reference]"
    r = im.resolve_references({"model": "M", "voltage_range": {"typ": 3.3}}, vcc=9.9)
    assert r["power"]["value"] == pytest.approx(3.3)
    assert r["power"]["source"] == "[Voltage Range]"
    r = im.resolve_references({"model": "M",
                               "gnd_clamp_ref": {"typ": -0.1}}, vcc=3.3)
    assert r["gnd"]["value"] == pytest.approx(-0.1)
    assert r["gnd"]["source"] == "[GND Clamp Reference]"
    # 什么都没有 → 用映射 vcc 兜底并如实标注
    r = im.resolve_references({"model": "M"}, vcc=5.0)
    assert r["power"]["value"] == pytest.approx(5.0)
    assert "兜底" in r["power"]["source"]
    # strict 下拒绝猜
    with pytest.raises(im.IbisModelError) as e:
        im.resolve_references({"model": "M"}, vcc=5.0, strict=True)
    assert e.value.code == "IBIS_REFERENCE_UNRESOLVED"


def test_clamp_pwl_breakpoints_are_segment_starts(tmp_path):
    """折点必须是线段起点，系数是斜率增量，且**符号与表一致**（不再取负）。"""
    import ibis_to_spice as its
    pts = [{"v": -6.0, "i_typ": 95e-3}, {"v": -4.2, "i_typ": 12e-3},
           {"v": -1.0, "i_typ": 1e-3}, {"v": -0.3, "i_typ": 0.0}]
    expr = its._pwl_current(pts, "V(X)")
    assert "uramp(V(X)-(-6))" in expr, expr
    assert "uramp(V(X)-(-4.2))" in expr
    assert expr.endswith("uramp(V(X)-(-1))"), expr

    def ev(v):
        return eval(expr.replace("V(X)", str(v))
                    .replace("uramp(", "_uramp("),
                    {"_uramp": lambda x: max(x, 0.0)})

    for p in pts:
        # 系数按 %.6g 落到 deck，所以只要求 1e-4 相对精度
        got = ev(p["v"])
        if abs(p["i_typ"]) < 1e-12:
            assert abs(got) < 1e-9        # 折线本身是 0，只剩浮点残差
        else:
            # 正号：表值原样给出（v2.5 这里是 −i_typ，属于多做了一次取负）
            assert got == pytest.approx(p["i_typ"], rel=1e-4), p
    # 折点之间也必须是折线插值，而不是旧斜率继续累加
    # 在 −2.6 V 处应落在 (−4.2, 12mA) → (−1.0, 1mA) 这一段上：6.5 mA
    assert ev(-2.6) == pytest.approx(6.5e-3, rel=1e-5)


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
    """DC 扫描逐点对比 IBIS 表，**同时验证方向**。

    判据来自 SPICE 约定：`i(V)` 为负 = 该电压源在"输出功率"。
      * Vpin > Vcc（POWER clamp 导通）→ 引脚电流被灌回 VCC → VSW 必须**输出**电流 → i(VSW) < 0
      * Vpin < 0  （GND clamp 导通）  → 电流从地灌进引脚 → VSW 必须**吸收**电流 → i(VSW) > 0
    只对比幅值不够：v2.5 的 `−I_ibis` 在幅值上是"对的"，方向却是反的。
    """
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
            rows.append((float(p[3]), float(p[1])))     # (v(dq), i(VSW))
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
    n_cmp = 0
    for v, i in rows:
        if abs(v) > 6.0 or abs(v) < 0.05:
            continue
        # 两个夹位各自的自变量不同：POWER 用 Vcc−Vpin，GND 用 Vpin
        i_power = interp(pu, 3.3 - v)      # 从引脚灌回 VCC 的电流（导通时为 +）
        i_gnd = interp(dn, v)              # GND 支路电流（导通时为 −，即从地流入引脚）
        # 这两份电流都从 VSW 支路「取走/注入」节点，所以 i(VSW) = −(两者之和)
        expected = -(i_power + i_gnd)
        worst = max(worst, abs(i - expected))
        n_cmp += 1
    assert n_cmp > 100
    assert worst < 5e-5, f"clamp I-V 与 IBIS 表最大误差 {worst * 1e3:.4f} mA"

    # 方向断言（不依赖符号推导，直接看源在输出还是吸收）
    above = [i for v, i in rows if v > 4.0]
    below = [i for v, i in rows if v < -1.0]
    assert above and all(i < -1e-6 for i in above), "V>Vcc 时电流没有灌回电源轨"
    assert below and all(i > 1e-6 for i in below), "V<0 时电流没有从地灌入引脚"


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
    # 这份 fixture 是 1mA 级 ESD 夹位（不是 power clamp），对 0.2 A 注入只能压掉近 20%；
    # 判据取"显著下降"，不假装它能钳到 Vcc。
    assert v_yes < 0.85 * v_no, (v_yes, v_no)


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

    两条线分别放在 L1 / L2，验证端口 z 各自跟随所在层；外加 GND 网络做参考面。
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
                         "vias": []},
               "GND": {"segments": [{"from": "G1", "to": "G2", "layer": "L2",
                                     "length_mm": 30.0, "width_mm": 1.0,
                                     "via_count": 0}],
                       "terminals": {"G1": {"x_mm": 0, "y_mm": 10},
                                     "G2": {"x_mm": 30, "y_mm": 10}}, "vias": []}}}
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
    # 参考面必须是 GND 所在的 L2，并标注为近似
    assert plan["reference"]["net"] == "GND"
    assert plan["reference"]["layer"] == "L2"
    assert plan["reference_plane"] == "REFERENCE_PLANE_APPROXIMATION"
    txt = (out / "em_coupon.py").read_text(encoding="utf-8")
    assert "excite=True" in txt and "excite=False" in txt
    assert "metal0.AddLinPoly(" in txt
    assert "REF_GND" in txt


def test_csxcad_script_uses_current_api_and_real_plane(tmp_path):
    """v2.5.1：生成代码必须是 `CSXCAD.ContinuousStructure` + `AddLinPoly`，
    且参考平面要真的建进几何。"""
    import em_bridge as eb
    geo = {"nets": {"N1": {"terminals": {"A": {"x_mm": 0, "y_mm": 0},
                                        "B": {"x_mm": 10, "y_mm": 0}},
                          "segments": [{"from": "A", "to": "B", "layer": "L2",
                                        "width_mm": 0.2}]},
                    "GND": {"terminals": {"G1": {"x_mm": 0, "y_mm": 3},
                                          "G2": {"x_mm": 10, "y_mm": 3}},
                            "segments": [{"from": "G1", "to": "G2", "layer": "L3",
                                          "width_mm": 1.0}]}}}
    stack = _stack4()
    stackd = eb.layer_stack(stack)
    ref = eb.select_reference(geo, ["N1"], stackd)
    assert ref["net"] == "GND" and ref["layer"] == "L3"
    region = {"nets": ["N1"],
              "bbox_mm": {"x": [-3, 13], "y": [-3, 3]},
              "margin_mm": 3, "f_max_hz": 3e9, "lambda_mm": 100, "cell_mm": 0.2,
              "cells_estimate": {"x": 80, "y": 30}, "status": "EXPERIMENTAL"}
    # L2 埋在 diel1 下面：z = 0.035 + 0.2 = 0.235 → 0.27；L3 的 z_bottom = 1.235
    ports = [{"name": "P1", "net": "N1", "x0": 0, "y0": 0, "z0": 0.235,
              "x1": 0, "y1": 0, "z1": ref["z_bottom_mm"]}]
    txt = eb.csxcad_script(geo, stack, region, ports, reference=ref)
    flat = txt.replace(" ", "")
    assert "AddLumpedPort(1,50," in flat
    assert "[0,0,0.235]" in flat and f"[0,0,{ref['z_bottom_mm']:.4g}]" in flat, \
        "端口没有用走线层 → 参考平面层的 z 跨度"
    # 铜：AddLinPoly(points, 'z', elevation, thickness)
    assert "CSX.AddMetal('N1_1')" in txt
    assert "AddLinPoly([[0,0.1],[10,0.1],[10,-0.1],[0,-0.1]],'z',0.235,0.035)" in flat
    # 参考平面必须真的建出来
    assert "ref=CSX.AddMetal('REF_GND')" in flat
    assert "ref.AddBox(" in txt
    assert "REFERENCE_PLANE_APPROXIMATION" in txt
    # 不能再用 v2.5 的错误 API
    assert "CSX.AddPolygon" not in txt
    assert "CSXCAD.Polygon(" not in txt
    assert ".SetElevation(" not in txt
    assert "from CSXCAD import ContinuousStructure" not in txt
    assert "CSX = CSXCAD.ContinuousStructure()" in txt
    # 生成的脚本本身必须是可解析的 Python
    import ast
    ast.parse(txt)


def test_reference_plane_unresolved_fails_loudly():
    import em_bridge as eb
    st = eb.layer_stack(_stack4())
    geo = {"nets": {"N1": {"terminals": {"A": {"x_mm": 0, "y_mm": 0},
                                        "B": {"x_mm": 10, "y_mm": 0}},
                          "segments": [{"from": "A", "to": "B", "layer": "L1",
                                        "width_mm": 0.2}]}}}
    with pytest.raises(eb.EmBridgeError) as e:
        eb.select_reference(geo, ["N1"], st)
    assert e.value.code == "EM_REFERENCE_PLANE_UNRESOLVED"
    assert "--reference-net" in e.value.message


def test_reference_plane_explicit_net():
    """--reference-net 可以指定非 GND 名字的参考网络。"""
    import em_bridge as eb
    st = eb.layer_stack(_stack4())
    geo = {"nets": {
        "N1": {"terminals": {"A": {"x_mm": 0, "y_mm": 0},
                             "B": {"x_mm": 10, "y_mm": 0}},
               "segments": [{"from": "A", "to": "B", "layer": "L1", "width_mm": 0.2}]},
        "VSS_ISO": {"terminals": {"C": {"x_mm": 0, "y_mm": 5},
                                  "D": {"x_mm": 10, "y_mm": 5}},
                    "segments": [{"from": "C", "to": "D", "layer": "L2",
                                  "width_mm": 1.0}]}}}
    ref = eb.select_reference(geo, ["N1"], st, ref_net="VSS_ISO")
    assert ref["net"] == "VSS_ISO" and ref["source"] == "--reference-net"
    assert eb.is_groundish("GND_CAN") and eb.is_groundish("AGND")
    assert not eb.is_groundish("VBUS")


def _has_csxcad():
    import importlib.util
    return importlib.util.find_spec("CSXCAD") is not None


def test_em_script_has_no_dead_api():
    """v2.5 生成的 `CSX.AddPolygon` / `CSXCAD.Polygon` / `SetElevation` 在当前
    openEMS/CSXCAD 里不存在；这里做全文静止检查（不需要装 openEMS）。"""
    import em_bridge as eb
    st = eb.layer_stack(_stack4())
    geo = {"nets": {
        "N1": {"terminals": {"A": {"x_mm": 0, "y_mm": 0},
                             "B": {"x_mm": 5, "y_mm": 0}},
               "segments": [{"from": "A", "to": "B", "layer": "L1", "width_mm": 0.2}]},
        "GND": {"terminals": {"C": {"x_mm": 0, "y_mm": 4},
                              "D": {"x_mm": 5, "y_mm": 4}},
                "segments": [{"from": "C", "to": "D", "layer": "L2",
                              "width_mm": 1.0}]}}}
    ref = eb.select_reference(geo, ["N1"], st)
    region = eb.plan_region(geo, ["N1"], 2e9)
    txt = eb.csxcad_script(geo, _stack4(), region,
                           [{"name": "P1", "net": "N1", "x0": 0, "y0": 0,
                             "z0": 0.0, "x1": 0, "y1": 0, "z1": 0.235}],
                           reference=ref)
    for bad in ("AddPolygon", "CSXCAD.Polygon(", "SetElevation(",
                "from CSXCAD import ContinuousStructure, CSXCAD"):
        assert bad not in txt, f"仍然生成了不存在的 API: {bad}"
    assert "CSX = CSXCAD.ContinuousStructure()" in txt
    assert txt.count("AddLinPoly(") >= 1
    assert "'z', " in txt
    import ast
    ast.parse(txt)


@pytest.mark.skipif(not _has_csxcad(), reason="本机没有 openEMS/CSXCAD")
def test_em_script_executes_against_real_csxcad(tmp_path):
    """有 openEMS 的机器上真正 exec 生成的脚本（不跑 FDTD）。

    这是把 openEMS 桥从"纯 EXPERIMENTAL"往上抬的唯一硬证据；
    本机没有 openEMS，所以默认 skip。
    """
    import em_bridge as eb
    st = eb.layer_stack(_stack4())
    geo = {"nets": {
        "N1": {"terminals": {"A": {"x_mm": 0, "y_mm": 0},
                             "B": {"x_mm": 5, "y_mm": 0}},
               "segments": [{"from": "A", "to": "B", "layer": "L1", "width_mm": 0.2}]},
        "GND": {"terminals": {"C": {"x_mm": 0, "y_mm": 4},
                              "D": {"x_mm": 5, "y_mm": 4}},
                "segments": [{"from": "C", "to": "D", "layer": "L2",
                              "width_mm": 1.0}]}}}
    ref = eb.select_reference(geo, ["N1"], st)
    region = eb.plan_region(geo, ["N1"], 2e9)
    txt = eb.csxcad_script(geo, _stack4(), region,
                           [{"name": "P1", "net": "N1", "x0": 0, "y0": 0,
                             "z0": 0.0, "x1": 0, "y1": 0, "z1": 0.235}],
                           reference=ref)
    ns = {}
    exec(compile(txt, "em_coupon.py", "exec"), ns)     # noqa: S102 - 生成物自检
    assert "CSX" in ns and "P1" in ns


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
    assert e.value.code == "DEEMBED_FREQ_GRID_MISMATCH"

    with pytest.raises(deembed.DeembedError) as e:
        deembed.apply(dut, {"open": same}, method="nonsense")
    assert e.value.code == "DEEMBED_METHOD_UNKNOWN"


def test_deembed_rejects_same_endpoints_different_grid(tmp_path):
    """v2.5.1：首尾相同但**中间点不同**，必须报错（v2.5 会放过去）。"""
    import deembed
    import rf_network as rn
    if not rn.available():
        pytest.skip("没有 scikit-rf")
    f_dut = np.linspace(1e9, 5e9, 5)               # 1,2,3,4,5 GHz
    f_fx = np.array([1e9, 1.2e9, 1.8e9, 3.7e9, 5e9])
    s = np.zeros((5, 2, 2), dtype=complex)
    s[:, 0, 1] = s[:, 1, 0] = 0.5
    dut = rn.RfNetwork.from_touchstone(_write_snp(tmp_path / "d.s2p", f_dut, s))
    fx = rn.RfNetwork.from_touchstone(_write_snp(tmp_path / "o.s2p", f_fx, s * 0.5))
    with pytest.raises(deembed.DeembedError) as e:
        deembed.apply(dut, {"open": fx}, method="open")
    assert e.value.code == "DEEMBED_FREQ_GRID_MISMATCH"
    assert "interpolate" in e.value.message

    # 显式允许插值 → 重采到 DUT 网格并在 manifest 里交代
    fx2 = rn.RfNetwork.from_touchstone(_write_snp(tmp_path / "o2.s2p", f_fx, s * 0.5))
    res, man = deembed.apply(dut, {"open": fx2}, method="open",
                             workdir=str(tmp_path), interpolate=True)
    assert res.npoints == len(f_dut)
    grid = man["frequency_grid"]
    assert grid["interpolated"] is True
    assert grid["resampled_roles"] == ["open"]
    assert grid["fixtures_before"]["open"]["n_points"] == 5
    assert grid["fixtures_after"]["open"]["n_points"] == 5
    assert grid["dut"]["n_points"] == 5


# =========================================================================
# 4. RLGC / 缓存
# =========================================================================
def _stackup_obj():
    import stackup as stk
    raw = _stack4()
    st = stk.Stackup(raw["layers"], raw["dielectrics"])
    st.normalize()
    return st


def test_rlgc_absolute_values_and_delay():
    """v2.5.1：L/C 的量级必须对（v2.5 少了 10³），且 √(L·C) 必须等于传播延迟。

    只测 √(L'/C') == Z0 发现不了共同的比例错误 —— 那条在 v2.5 也是通过的。
    """
    import rlgc
    st = _stackup_obj()
    m = rlgc.from_stackup(st, "L1", width_mm=0.2, length_mm=30.0, n_seg=10)
    td = m["td_s"]
    # 1) 绝对量（按当前 4 层叠层的实际数值，容差 2%）
    assert td == pytest.approx(178.158e-12, rel=0.02)
    assert m["l_total_h"] == pytest.approx(11.12e-9, rel=0.02)
    assert m["c_total_f"] == pytest.approx(2.854e-12, rel=0.02)
    # 2) 硬约束：整段 L·C 的平方根必须等于整段传播延迟（量纲自洽的判据）
    assert math.sqrt(m["l_total_h"] * m["c_total_f"]) == pytest.approx(td, rel=1e-9)
    # 3) 每米量除以长度才是整段量，两者不能差 1000 倍
    length_m = 30.0e-3
    assert m["l_h_per_m"] * length_m == pytest.approx(m["l_total_h"], rel=1e-12)
    assert m["c_f_per_m"] * length_m == pytest.approx(m["c_total_f"], rel=1e-12)
    # 4) 单位长度特征阻抗仍然是 Z0
    assert math.sqrt(m["l_h_per_m"] / m["c_f_per_m"]) == pytest.approx(
        m["z0_ohm"], rel=1e-6)
    # 5) Rdc 与每米电阻一致
    expect_rdc = rlgc.CU_RHO * length_m / (0.2e-3 * 35e-6)
    assert m["r_dc_ohm"] == pytest.approx(expect_rdc, rel=1e-6)


def test_rlgc_r_is_in_series_with_l():
    """v2.5.1：每段必须是 cur─R─mid─L─next，不能把 R 与 L 并在同两点。"""
    import rlgc
    from parasitics import NetCtx
    from sim_config import Namer
    m = rlgc.from_stackup(_stackup_obj(), "L1", 0.2, 30.0, n_seg=4)
    lines, info = rlgc.emit(NetCtx(Namer()), m, "IN", "OUT", name="TL")
    body = [l.split() for l in lines if l and not l.startswith("*")]
    assert len(body) == 4 * 4
    for ln in body:
        assert ln[0][0].upper() in "RLC", ln
    # 按段还原拓扑
    for seg in range(4):
        rrow, lrow = body[seg * 4], body[seg * 4 + 1]
        assert rrow[0][0].upper() == "R" and lrow[0][0].upper() == "L"
        mid = rrow[2]
        assert rrow[1] != rrow[2]
        # 电感从同一个 mid 出发（串联），而不是与电阻共用两个端点
        assert lrow[1] == mid, (rrow, lrow)
        assert lrow[1:3] != rrow[1:3], "R 与 L 又并联在同样两个节点上了"
    # 第一段起点必须是 IN，最后一段终点必须是 OUT
    assert body[0][1] == "IN"
    assert body[3 * 4 + 1][2] == "OUT"
    # 并联支路都是对地（0）的 R/C，且没有两端 G
    shunt = [ln for ln in body if ln[2] == "0"]
    assert len(shunt) == 8
    assert all(ln[0][0].upper() in "RC" for ln in shunt)


def test_rlgc_is_honestly_labelled_constant_at_fref():
    """R/G 是 f_ref 处的常数，不能说成频率相关网络。"""
    import rlgc
    from parasitics import NetCtx
    from sim_config import Namer
    m = rlgc.from_stackup(_stackup_obj(), "L1", 0.2, 30.0, n_seg=5)
    lines, info = rlgc.emit(NetCtx(Namer()), m, "IN", "OUT", name="TL")
    assert info["model_fidelity"] == "RLGC_CONSTANT_AT_FREF"
    assert "RLGC_CONSTANT_AT_FREF" in lines[0]
    assert info["f_ref_hz"] == pytest.approx(1e9)
    assert info["r_used_ohm"] == pytest.approx(m["r_dc_ohm"])
    assert "Rdc" in info["r_basis"]
    assert "不是 G(f)" in info["g_basis"]
    # at_frequency 仍给解析 R(f)，但那是报告用的，不进网表
    assert rlgc.at_frequency(m, 4e9)["r"] > rlgc.at_frequency(m, 1e9)["r"]


@pytest.mark.skipif(not HAS_NG, reason="没有 ngspice")
def test_rlgc_ladder_dc_division(tmp_path):
    """直流：电感短路后电流仍必须穿过 R。

    R||L 的错误实现会让 V(OUT) = 1.0 V（电感旁路掉铜阻）；
    正确实现是 50/(Rdc+50) = 0.99852。因此容差必须比 0.15% 更紧。
    为隔离 R 的效果，loss_tangent 取 0（G=0，并联支路近似开路）。
    """
    import rlgc
    from parasitics import NetCtx
    from sim_config import Namer
    m = rlgc.from_stackup(_stackup_obj(), "L1", 0.2, 30.0, n_seg=8, loss_tangent=0.0)
    assert m["g_at_f_ref_s"] == 0
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
    assert got < 1.0 - 1e-4, f"V(OUT)={got} 说明电感把串联电阻旁路了"
    assert got == pytest.approx(50.0 / (rdc + 50.0), rel=1e-5)


def test_cache_key_is_content_sensitive(tmp_path):
    import sim_cache
    a, pa = sim_cache.build_key("deck A", {"x": 1}, solver_version="47")
    b, pb = sim_cache.build_key("deck A", {"x": 1}, solver_version="47")
    c, _ = sim_cache.build_key("deck B", {"x": 1}, solver_version="47")
    d, _ = sim_cache.build_key("deck A", {"x": 2}, solver_version="47")
    e, _ = sim_cache.build_key("deck A", {"x": 1}, solver_version="48")
    assert a == b and len({a, c, d, e}) == 4
    assert pa == pb and pa["deck"] != sim_cache.build_key("deck B", {})[1]["deck"]


def test_cache_key_includes_included_model_files(tmp_path):
    """v2.5.1：`vendor.lib` 内容变了、deck 没变 → key 必须变（否则返回 stale 结果）。"""
    import sim_cache
    lib = tmp_path / "vendor.lib"
    lib.write_text(".MODEL DX D(IS=1e-14)\n", encoding="utf-8")
    deck = '* test\n.include "vendor.lib"\nD1 A 0 DX\n.end\n'
    deps1 = sim_cache.collect_deps(deck, {}, base_dir=str(tmp_path))
    assert any(d.endswith("vendor.lib") for d in deps1), deps1
    k1, _ = sim_cache.build_key(deck, {"m": 1}, model_files=deps1)
    # 只改文件内容，deck / config / solver 都不变
    lib.write_text(".MODEL DX D(IS=2e-14)\n", encoding="utf-8")
    deps2 = sim_cache.collect_deps(deck, {}, base_dir=str(tmp_path))
    k2, _ = sim_cache.build_key(deck, {"m": 1}, model_files=deps2)
    assert k1 != k2, "改了 .include 的模型文件，cache key 没变"


def test_cache_collects_deps_from_config_and_override(tmp_path):
    """override 里的 .sNp、model_map 里的 .lib 也要纳入依赖。"""
    import sim_cache
    snp = tmp_path / "filter.s2p"
    snp.write_text("# Hz S RI R 50\n1e9 0 0 1 0 1 0 0 0\n", encoding="utf-8")
    ovr = tmp_path / "ovr.json"
    ovr.write_text(json.dumps({"parasitics": [
        {"type": "touchstone", "network": "filter.s2p"}]}), encoding="utf-8")
    lib = tmp_path / "vend.lib"
    lib.write_text(".SUBCKT BUCK VIN VOUT\n.ENDS\n", encoding="utf-8")
    mm = tmp_path / "model_map.json"
    mm.write_text(json.dumps({"parts": [{"designator": "U1", "type": "subckt",
                                         "file": "vend.lib",
                                         "subckt": "BUCK"}]}),
                  encoding="utf-8")
    deps = sim_cache.collect_deps("* d\n.end\n",
                                  {"override": str(ovr), "models": str(mm)},
                                  base_dir=str(tmp_path))
    names = {os.path.basename(d) for d in deps}
    assert {"ovr.json", "filter.s2p", "model_map.json", "vend.lib"} <= names, names


def test_cache_hit_materializes_files(tmp_path):
    """命中时 raw/csv 要还原回 run 目录，不能只写一个 metrics.json。"""
    import sim_cache
    cache = sim_cache.SimCache(tmp_path / "cache")
    key, _ = sim_cache.build_key("deck", {})
    src = tmp_path / "src"
    src.mkdir()
    (src / "op.raw").write_text("Title: x\n", encoding="utf-8")
    (src / "op.csv").write_text("time,v\n0,1\n", encoding="utf-8")
    cache.store(key, [str(src / "op.raw"), str(src / "op.csv")],
                metrics={"gain": 2.0}, meta={"mode": "op"})
    dest = tmp_path / "run2"
    got = cache.materialize(key, str(dest))
    assert set(got) == {"op.raw", "op.csv", "metrics.json"}, got
    assert (dest / "op.raw").exists() and (dest / "metrics.json").exists()
    assert not (dest / "cache_meta.json").exists()


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
