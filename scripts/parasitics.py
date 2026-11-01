#!/usr/bin/env python3
"""v2.2 P0-F：寄生元件构造器（RLC / transmission line / ferrite / via ...）。

所有构造器只做两件事：校验参数、吐出 SPICE 行。节点命名与去重由调用方
（override_apply.py）通过 ctx 提供。

关键实测约束：
  * ngspice-47 的无损传输线语法 `Txxx n1 ref1 n2 ref2 Z0=xx TD=yy`（`T` 行
    必须有 4 个节点，参考节点通常是 0）；
  * 参数单位走 SPICE 风格后缀（p/n/u/m/k/meg/g/t），但 **必须** 校验，
    否则 45 mm × 6 ps/mm 这种长度推导一旦单位写错就会差三个数量级。
"""
import math
import re

_UNIT = [
    ("meg", 1e6), ("t", 1e12), ("g", 1e9), ("k", 1e3), ("m", 1e-3),
    ("u", 1e-6), ("n", 1e-9), ("p", 1e-12), ("f", 1e-15),
]
# 允许但忽略的尾缀（单位名，非倍率）
_UNIT_WORDS = ("ohms", "ohm", "f", "h", "s", "v", "a", "r", "w", "hz")


class ParasiticError(ValueError):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def parse_eng(value, field="value"):
    """SPICE 风格数值解析："180p"→1.8e-10，"4k7"→不支持，"2.2meg"→2.2e6。"""
    if value is None:
        raise ParasiticError("PARAM_INVALID", f"{field} 缺失")
    if isinstance(value, bool):
        raise ParasiticError("PARAM_INVALID", f"{field} 不是数值: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(" ", "").replace("µ", "u").replace("μ", "u")
    s = s.replace("Ω", "").replace("ω", "").replace("欧", "")
    m = re.fullmatch(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([A-Za-z]*)", s)
    if not m:
        raise ParasiticError("PARAM_INVALID", f"{field} 无法解析为数值: {value!r}")
    num, suffix = float(m.group(1)), m.group(2).lower()
    mult = 1.0
    if suffix:
        for name, f in _UNIT:
            if suffix.startswith(name):
                mult = f
                suffix = suffix[len(name):]
                break
        # 纯单位名（"1kohm"、"10pf"）不改变倍率，忽略即可
        if suffix and not any(suffix.startswith(w) for w in _UNIT_WORDS):
            raise ParasiticError("PARAM_INVALID", f"{field} 尾缀无法识别: {value!r}")
    return num * mult


def fmt(x, digits=6):
    """浮点 → SPICE 数值文本（避免 1e-10 变成 1.0000000000000001e-10）。"""
    if x == 0:
        return "0"
    return f"{x:.{digits}g}"


def fmt_time(sec):
    return fmt(sec)


def td_from_length(length_mm, velocity_ps_per_mm):
    """长度 × 皮秒/毫米 → 秒，同时返回推导文本（报告必须同时显示）。

    TD = l × t_pd   [mm] × [ps/mm] = [ps]
    """
    l = parse_eng(length_mm, "length_mm")
    v = parse_eng(velocity_ps_per_mm, "velocity_ps_per_mm")
    if l <= 0:
        raise ParasiticError("TL_PARAM_INVALID", f"length_mm 必须 > 0，收到 {length_mm}")
    if v <= 0:
        raise ParasiticError("TL_PARAM_INVALID",
                             f"velocity_ps_per_mm 必须 > 0，收到 {velocity_ps_per_mm}"
                             f"（注意单位是 ps/mm，不是 mm/ps 或 m/s）")
    td_ps = l * v
    # 物理合理性护栏：1 mm 线路不可能达到 1 ns 以上（≈0.3c 也只是 3.3 ps/mm）
    if v > 20:
        raise ParasiticError(
            "TL_PARAM_INVALID",
            f"velocity_ps_per_mm={v:g} 不合理（PCB 上典型 5~7 ps/mm，上限 ~10）；"
            f"如写的是 m/s 或 in/ps 请换算")
    if td_ps > 1e6:
        raise ParasiticError("TL_PARAM_INVALID",
                             f"推导出的 TD={td_ps:.1f} ps 明显越界，请检查 length/velocity 单位")
    derivation = [
        f"length = {l:g} mm",
        f"velocity = {v:g} ps/mm",
        f"TD = {td_ps:.1f} ps",
    ]
    return td_ps * 1e-12, derivation


class NetCtx:
    """节点/元件名分配上下文。"""

    def __init__(self, namer, used_nodes=None):
        self.namer = namer
        self.used_nodes = used_nodes if used_nodes is not None else set()

    def name(self, prefix, hint):
        return self.namer(prefix, hint)

    def node(self, hint):
        base = "__" + re.sub(r"[^0-9A-Za-z_]", "_", str(hint or "MID"))
        s, i = base, 2
        while s.upper() in self.used_nodes:
            s = f"{base}_{i}"
            i += 1
        self.used_nodes.add(s.upper())
        return s


def _val(value, field, minimum=None):
    x = parse_eng(value, field)
    if minimum is not None and x <= minimum:
        raise ParasiticError("PARAM_INVALID", f"{field} 必须 > {minimum}，收到 {value!r}")
    return x


# --------------------------------------------------------------------------
# 基本元件
# --------------------------------------------------------------------------
def resistor(ctx, name_hint, n1, n2, r, source=None, note=None):
    x = _val(r, "r")
    if x < 0:
        raise ParasiticError("PARAM_INVALID", f"电阻不能为负: {r}")
    if x == 0:
        # ngspice 对 0Ω 只会 clamp 到 1e-12 并警告，且 1µΩ 量级会让 tran 收敛失败
        # （实测 timestep too small 挂死）；1 mΩ 在 52Ω 系统里已是可忽略的短接
        x = 1e-3
        note = (note or "") + " [0Ω → 1mΩ 等效短接：ngspice 不接受零阻值，1µΩ 会不收敛]"
    nm = ctx.name("R", name_hint)
    return [f"{nm} {n1} {n2} {fmt(x)}" + (f"  $ {note}" if note else "")], nm


def inductor(ctx, name_hint, n1, n2, l, source=None, note=None):
    x = _val(l, "l", minimum=0)
    nm = ctx.name("L", name_hint)
    return [f"{nm} {n1} {n2} {fmt(x)}" + (f"  $ {note}" if note else "")], nm


def capacitor(ctx, name_hint, n1, n2, c, source=None, note=None):
    x = _val(c, "c", minimum=0)
    nm = ctx.name("C", name_hint)
    return [f"{nm} {n1} {n2} {fmt(x)}" + (f"  $ {note}" if note else "")], nm


def transmission_line(ctx, name_hint, n1, n2, spec, ref="0", ref2=None):
    """无损传输线：`T n1 ref n2 ref2 Z0=.. TD=..`。

    接受 z0/td 直接给定，或 length_mm + velocity_ps_per_mm 推导。
    返回 (lines, info)，info 含 td(秒)、derivation（报告用）。
    """
    ref = str(ref or "0")
    ref2 = str(ref2 if ref2 is not None else ref)
    z0 = _val(spec.get("z0"), "z0", minimum=0)
    derivation = []
    if spec.get("td") not in (None, ""):
        td = _val(spec.get("td"), "td", minimum=0)
        derivation = [f"TD = {td * 1e12:.1f} ps  (explicit)"]
    elif spec.get("length_mm") not in (None, "") and spec.get("velocity_ps_per_mm") not in (None, ""):
        td, derivation = td_from_length(spec["length_mm"], spec["velocity_ps_per_mm"])
    else:
        raise ParasiticError(
            "TL_PARAM_INVALID",
            "传输线需要 z0 + (td) 或 (length_mm + velocity_ps_per_mm)"
            "；缺少参数时无法保证传播延迟量级正确")
    nm = ctx.name("T", name_hint)
    line = f"{nm} {n1} {ref} {n2} {ref2} Z0={fmt(z0)} TD={fmt_time(td)}"
    return [line], {"element": nm, "z0": z0, "td": td, "derivation": derivation}


# --------------------------------------------------------------------------
# 组合模型
# --------------------------------------------------------------------------
def series_chain(ctx, name_hint, n1, n2, parts, note=None):
    """把多个两端元件串起来插在 n1..n2 之间。

    parts: [("L", "0.7n"), ("R", "20m"), ("C", "100n")]
    """
    lines = []
    nodes = [n1]
    for i in range(len(parts) - 1):
        nodes.append(ctx.node(f"{name_hint}_{i + 1}"))
    nodes.append(n2)
    for idx, (kind, val) in enumerate(parts):
        hint = f"{name_hint}_{'LRC'[('L', 'R', 'C').index(kind)] if kind in 'LRC' else kind}"
        if kind == "L":
            out, _ = inductor(ctx, hint, nodes[idx], nodes[idx + 1], val, note=note if idx == 0 else None)
        elif kind == "R":
            out, _ = resistor(ctx, hint, nodes[idx], nodes[idx + 1], val, note=note if idx == 0 else None)
        elif kind == "C":
            out, _ = capacitor(ctx, hint, nodes[idx], nodes[idx + 1], val, note=note if idx == 0 else None)
        else:
            raise ParasiticError("PARAM_INVALID", f"未知串联元件类型 {kind}")
        lines.extend(out)
    return lines


def capacitor_model(ctx, name_hint, n1, n2, c, esr=None, esl=None, source=None, note=None):
    """带 ESR/ESL 的电容：n1 - L_ESL - R_ESR - C - n2（v2.2 需求文档指定顺序）。"""
    parts = []
    if esl is not None:
        parts.append(("L", esl))
    if esr is not None:
        parts.append(("R", esr))
    parts.append(("C", c))
    if len(parts) == 1:
        nm = ctx.name("C", name_hint)
        return [f"{nm} {n1} {n2} {fmt(_val(c, 'c', minimum=0))}"
                + (f"  $ {note}" if note else "")], nm
    tag = name_hint + (f"_{source}" if source else "")
    return series_chain(ctx, tag, n1, n2, parts, note=note), None


def ferrite_model(ctx, name_hint, n1, n2, rdc=None, l=None, rp=None, source=None):
    """Ferrite bead 简化模型：R_dc 串联 + (L ∥ R_p) 并联块。"""
    lines = []
    cur = n1
    if rdc is not None:
        mid = ctx.node(f"{name_hint}_A")
        out, _ = resistor(ctx, f"{name_hint}_RDC", cur, mid, rdc)
        lines.extend(out)
        cur = mid
    if l is None and rp is None:
        if cur == n1:
            out, _ = resistor(ctx, f"{name_hint}_R", n1, n2, "10m")
            lines.extend(out)
        else:
            lines.append(f"R{ctx.name('Z', name_hint)} {cur} {n2} 1e-3")
        return lines
    b = n2
    if rp is not None:
        out, _ = resistor(ctx, f"{name_hint}_RP", cur, b, rp)
        lines.extend(out)
    if l is not None:
        out, _ = inductor(ctx, f"{name_hint}_L", cur, b, l)
        lines.extend(out)
    return lines


def connector_model(ctx, name_hint, n1, n2, r=None, l=None):
    lines = []
    if r is None and l is None:
        return lines
    parts = []
    if l is not None:
        parts.append(("L", l))
    if r is not None:
        parts.append(("R", r))
    return series_chain(ctx, f"{name_hint}_CONN", n1, n2, parts)


def via_model(ctx, name_hint, n1, n2, r=None, l=None):
    return connector_model(ctx, f"{name_hint}_VIA", n1, n2, r, l)


# --------------------------------------------------------------------------
# 波形（current / voltage source）
# --------------------------------------------------------------------------
def waveform_text(wf):
    """waveform 描述 → SPICE 波形表达式。

    支持：
      {"type":"pulse","i_idle":"120m","i_active":"300m","delay":"10u",
       "rise":"2n","fall":"2n","on":"5u","period":"10u"}
      {"type":"pwl","points":[[0,"0"],[1u,"120m"],...]}
      直接字符串 "PULSE(0 3.3 0 1n 1n 5n 10n)"
    """
    if wf is None:
        return None
    if isinstance(wf, str):
        return wf
    t = str(wf.get("type", "pulse")).lower()
    if t == "pulse":
        i1 = wf.get("i_idle", wf.get("v_low", wf.get("v1", "0")))
        i2 = wf.get("i_active", wf.get("v_high", wf.get("v2", "1")))
        td = wf.get("delay", "0")
        tr = wf.get("rise", "1n")
        tf = wf.get("fall", wf.get("rise", "1n"))
        pw = wf.get("on", wf.get("width", wf.get("pw")))
        per = wf.get("period", wf.get("pw"))
        if pw is None or per is None:
            raise ParasiticError("PARAM_INVALID", "pulse 波形需要 on/period（或 width/period）")
        return (f"PULSE({_num_or_str(i1)} {_num_or_str(i2)} {_num_or_str(td)} "
                f"{_num_or_str(tr)} {_num_or_str(tf)} {_num_or_str(pw)} {_num_or_str(per)})")
    if t == "pwl":
        pts = wf.get("points") or []
        if not pts:
            raise ParasiticError("PARAM_INVALID", "pwl 波形需要 points")
        flat = " ".join(f"{_num_or_str(p[0])} {_num_or_str(p[1])}" for p in pts)
        return f"PWL({flat})"
    if t in ("sin", "sine"):
        args = [wf.get(k, d) for k, d in (("offset", "0"), ("ampl", "1"),
                                         ("freq", "1k"), ("delay", "0"),
                                         ("theta", "0"), ("phase", "0"))]
        return "SIN(" + " ".join(_num_or_str(a) for a in args) + ")"
    if t in ("dc", "const"):
        return f"DC {_num_or_str(wf.get('value', wf.get('v', wf.get('i', '0'))))}"
    raise ParasiticError("PARAM_INVALID", f"未知波形类型 {t}")


def _num_or_str(v):
    """保留用户写法（便于回读），但校验可解析。"""
    if isinstance(v, (int, float)):
        return fmt(float(v))
    s = str(v).strip()
    parse_eng(s, "waveform")
    return s
