#!/usr/bin/env python3
"""v2.3 P0-D(3)：IBIS → ngspice 行为级等效元件。

不是晶体管级模型：输入用 C_comp(+封装 R/L)，输出用"理想摆幅源 + 由 IV 曲线反推的
Rout + C_comp"，边沿来自 [Ramp]/[Rising|Falling Waveform]。所有产物标
`IBIS_DERIVED_BEHAVIORAL_MODEL`，manifest 里带 IBIS 文件 sha256。

ibis_map.json（--ibis）格式::

    {
      "parts": [
        {"component": "U3", "pin": "89", "ibis_file": "EP4CE10.ibs",
         "model": "3V3_LVCMOS_IN", "vcc": 3.3, "corner": "typ"},
        {"mpn": "EP4CE10", "pin": "45", "ibis_file": "EP4CE10.ibs"},   // 也可按型号匹配
        {"designator": "U7", "pins": ["89", "45"], "ibis_file": "x.ibs"}
      ]
    }
"""
import hashlib
import os

from ibis_model import (
    IBIS_FIDELITY, IbisModelError, classify, describe, edge_times, input_capacitance,
    output_drive, resolve, resolve_references,
)
from ibis_parser import IbisError, parse_ibs
from sim_config import spice_ident

LEVEL = IBIS_FIDELITY


class IbisSet:
    def __init__(self, map_path=None, vcc_default=3.3, corner="typ", strict=False):
        self.entries = []
        self.errors = []
        self.warnings = []
        self.docs = {}
        self.vcc_default = vcc_default
        self.corner = corner
        self.strict = strict
        self._preamble = []
        self._vcc_nodes = {}
        self._cur = (None, None, None, None)
        if not map_path:
            return
        if not os.path.exists(map_path):
            self.errors.append({"code": "IBIS_MAP_MISSING",
                                "message": f"IBIS 映射文件不存在: {map_path}"})
            return
        import json
        with open(map_path, encoding="utf-8") as f:
            raw = json.load(f)
        base = os.path.dirname(os.path.abspath(map_path))
        for e in raw.get("parts") or raw.get("entries") or []:
            e = dict(e)
            f_ = e.get("ibis_file")
            if f_ and not os.path.isabs(f_):
                cand = os.path.normpath(os.path.join(base, f_))
                if os.path.exists(cand):
                    e["ibis_file"] = cand
            self.entries.append(e)

    # ------------------------------------------------------------------ 匹配
    def match(self, comp):
        des = str(comp.get("designator") or "").upper()
        mpn = str(comp.get("manufacturerPart") or "").upper()
        dev = str(comp.get("deviceTitle") or "").upper()
        out = []
        for e in self.entries:
            if e.get("designator") and str(e["designator"]).upper() == des:
                out.append(e)
            elif e.get("component") and str(e["component"]).upper() in (des, mpn, dev):
                out.append(e)
            elif e.get("mpn") and str(e["mpn"]).upper() == mpn:
                out.append(e)
            elif e.get("device") and str(e["device"]).upper() == dev:
                out.append(e)
        return out

    def doc_for(self, path):
        key = os.path.abspath(path)
        if key not in self.docs:
            try:
                self.docs[key] = parse_ibs(path)
            except IbisError as e:
                self.errors.append({"code": e.code, "message": e.message})
                self.docs[key] = None
        return self.docs[key]

    # ------------------------------------------------------------------ 生成
    def build(self, ctx, comp, node_of):
        self._preamble = []
        self._vcc_nodes = {}
        entries = self.match(comp)
        if not entries:
            return [], []
        des = comp.get("designator") or "?"
        lines, infos = [], []
        pins = comp.get("pins") or []
        for e in entries:
            doc = self.doc_for(e["ibis_file"])
            if doc is None:
                continue
            pin_list = e.get("pins") or ([e["pin"]] if e.get("pin") else [])
            if not pin_list:
                self.warnings.append(f"{des}: IBIS 条目既没有 pin 也没有 pins，跳过")
                continue
            for pin in pin_list:
                # 映射里的 component 优先；没写且 IBIS 只有一个 [Component] 时自动用它
                comp_name = e.get("component")
                if not comp_name and len(doc.get("components") or {}) == 1:
                    comp_name = next(iter(doc["components"]))
                try:
                    spec = resolve(doc, comp_name or comp.get("designator"),
                                   pin, e.get("model"))
                except IbisModelError as ex:
                    # 按 component 名匹配失败时，退化成"只按 model 名"（用户显式给了 model）
                    if ex.code in ("IBIS_COMPONENT_NOT_FOUND",) and e.get("model"):
                        try:
                            spec = resolve(doc, None, None, e["model"])
                        except IbisModelError as ex2:
                            self.errors.append({"code": ex2.code,
                                                "message": f"{des}.{pin}: {ex2.message}"})
                            continue
                    else:
                        self.errors.append({"code": ex.code,
                                            "message": f"{des}.{pin}: {ex.message}"})
                        continue
                node = node_of(comp, str(pin))
                if not node:
                    self.errors.append({"code": "IBIS_PIN_FLOATING",
                                        "message": f"{des}.{pin}: 该引脚没有连接网络"})
                    continue
                vcc = e.get("vcc", self.vcc_default)
                corner = e.get("corner", self.corner)
                kind = classify(spec)
                self._cur = (doc, comp_name, comp, node_of)
                out, info = self._emit(ctx, des, pin, node, spec, kind, vcc, corner, e)
                lines.extend(out)
                if info:
                    infos.append(info)
        if self._preamble:
            lines = self._preamble + lines
        return lines, infos

    # ------------------------------------------------------------------ clamp
    def _emit_clamps(self, ctx, des, pin, node, spec, vcc, entry, tag):
        """把 [POWER Clamp] / [GND Clamp] 的 V-I 表变成分段线性电流源。

        **IBIS 语义（Ver 4.0，v2.5.1 修正）**：
          * `[POWER Clamp]` 的自变量是 `Vtable = Vcc − Voutput`，导通区在 Vtable < 0，
            且该区间的电流在规范示例里是**正值**（如 −5 V → +4450 mA）；
          * `[GND Clamp]` 的自变量是 ground-relative `Vtable = Voutput − Vgnd`，
            导通区同样在 Vtable < 0，但电流是**负值**（如 −5 V → −3900 mA）；
          * "currents are positive when their direction is into the component"，
            所以**不要**再统一取负：表里的符号本身就是方向。

        v2.5 的错误：POWER clamp 用 `V(pin)` 当自变量（应为 `V(vcc)−V(pin)`），
        并且对所有表统一乘 −1。测试替身恰好也用了非标准形状，两个错误互相抵消，
        DC 扫描看起来完全正确。v2.5.1 连同 fixture 一起重做。

        参数来源见 `ibis_model.resolve_references()`：
        `[POWER Clamp Reference]` → `[Pullup Reference]` → `[Voltage Range]`；
        `[GND Clamp Reference]` → `[Pulldown Reference]` → 0 V。
        """
        if not entry.get("clamps", True):
            return [], {"clamps": "disabled"}
        cl_p = spec.get("power_clamp") or []
        cl_n = spec.get("gnd_clamp") or []
        if not cl_p and not cl_n:
            return [], None
        refs = resolve_references(spec, vcc=vcc, strict=self.strict)
        lines, detail = [], {}
        if cl_p:
            vcc_node = self._vcc_node(ctx, des, entry, vcc, signal_node=node)
            expr = _pwl_current(cl_p, f"(V({vcc_node})-V({node}))")
            if expr:
                nm = ctx.name("B", f"{tag}_PCLAMP")
                lines.append(f"{nm} {node} {vcc_node} I = {expr}")
                detail["power_clamp"] = (
                    f"{len(cl_p)} 点 PWL，自变量 V({vcc_node})−V({node})，"
                    f"ref={refs['power']['value']:g}V（{refs['power']['source']}）")
        if cl_n:
            gnd_node = entry.get("ref_gnd") or "0"
            arg = f"V({node})" if gnd_node == "0" else f"(V({node})-V({gnd_node}))"
            expr = _pwl_current(cl_n, arg)
            if expr:
                nm = ctx.name("B", f"{tag}_NCLAMP")
                lines.append(f"{nm} {node} {gnd_node} I = {expr}")
                detail["gnd_clamp"] = (
                    f"{len(cl_n)} 点 PWL，自变量 {arg}，"
                    f"ref={refs['gnd']['value']:g}V（{refs['gnd']['source']}）")
        detail["references"] = {"power": refs["power"], "gnd": refs["gnd"]}
        if lines:
            lines.insert(0, f"* IBIS clamps {des}.{pin}（PWL 二极管夹位，"
                            f"配置 clamps:false 可关）")
        return lines, detail

    def _vcc_node(self, ctx, des, entry, vcc, signal_node=None):
        """POWER clamp 需要一根电源参考轨。优先级：

        1. 映射里显式的 `ref_vcc`（真实网络名，最可靠）；
        2. 自动：IBIS `[Pin]` 里标成 POWER 的引脚，它连的网络就是这颗器件的电源轨
           （夹位电流真正流回板上电源，而不是流进一个凭空造的理想源）；
        3. 兜底：为这颗器件单独造一个理想电压源节点。
        """
        ref = entry.get("ref_vcc")
        if ref:
            return ref
        key = (str(des).upper(), float(vcc))
        if key in self._vcc_nodes:
            return self._vcc_nodes[key]
        auto = None
        doc, comp_name, comp, node_of = self._cur
        if doc and comp_name and comp is not None and node_of is not None:
            for pnum, pinfo in ((doc.get("components") or {})
                                .get(comp_name, {}).get("pins") or {}).items():
                if (pinfo.get("keyword") or "").upper() == "POWER":
                    auto = node_of(comp, str(pnum))
                    # 防自环：电源脚和信号脚接到同一个网络说明映射本身有问题
                    if auto and auto != signal_node:
                        break
                    auto = None
        if auto:
            self._vcc_nodes[key] = auto
            self.warnings.append(
                f"{des}: POWER clamp 自动接到 IBIS POWER 引脚所在网络 `{auto}`")
            return auto
        node = ctx.node(f"IBIS_{spice_ident(des)}_VCC")
        nm = ctx.name("V", f"IBIS_{spice_ident(des)}_VCC")
        self._preamble.append(f"{nm} {node} 0 DC {float(vcc):g}")
        self._vcc_nodes[key] = node
        return node

    def _emit(self, ctx, des, pin, node, spec, kind, vcc, corner, entry):
        tag = f"IBIS_{spice_ident(des)}_{spice_ident(str(pin))}"
        cap = input_capacitance(spec, corner)
        r_pkg = spec.get("r_pkg") or 0.0
        l_pkg = spec.get("l_pkg") or 0.0
        lines = [f"* {LEVEL} {des}.{pin} model={spec['model']} ({spec.get('model_type')})"]
        info = {
            "designator": des, "pin": str(pin), "node": node,
            "model": spec["model"], "model_type": spec.get("model_type"),
            "kind": kind, "level": LEVEL,
            "ibis_file": os.path.basename(spec.get("ibis_path") or spec.get("ibis_file") or ""),
            "ibis_file_path": spec.get("ibis_path"),
            "ibis_sha256": _sha256(spec.get("ibis_path")),
            "corner": corner, "vcc": vcc,
            "c_total_pf": cap["c_total_pf"],
            "elements": [],
        }
        # 封装的串联 R/L + C_comp 到地（输入、I/O 都要）
        terminal = node
        if l_pkg and l_pkg > 0:
            mid = ctx.node(f"{tag}_PKG")
            ln = ctx.name("L", f"{tag}_PKG")
            lines.append(f"{ln} {node} {mid} {l_pkg:.6g}")
            info["elements"].append(ln)
            terminal = mid
        if r_pkg and r_pkg > 0:
            mid = ctx.node(f"{tag}_RSER")
            rn = ctx.name("R", f"{tag}_SER")
            lines.append(f"{rn} {terminal} {mid} {r_pkg:.6g}")
            info["elements"].append(rn)
            terminal = mid

        if kind in ("output", "io", "open"):
            d = output_drive(spec, vcc, corner)
            e_ = edge_times(spec, vcc, corner)
            rout = d.get("rout_ohm")
            tr = e_.get("tr") or 0.5e-9
            tf = e_.get("tf") or 0.5e-9
            if rout is None:
                self.warnings.append(
                    f"{des}.{pin}: IBIS 无 IV 曲线，输出电阻按 50Ω 兜底（{LEVEL}）")
                rout = 50.0
            drv = ctx.node(f"{tag}_DRV")
            vs = ctx.name("V", f"{tag}_DRV")
            # 1 ns 周期的 PULSE 只是"能跑的默认激励"，真实激励由 override sources 决定
            lines.append(f"{vs} {drv} 0 PULSE(0 {vcc:.6g} 1n {tr:.6g} {tf:.6g} "
                         f"8n 20n)")
            rs = ctx.name("R", f"{tag}_ROUT")
            lines.append(f"{rs} {drv} {terminal} {rout:.6g}")
            info["elements"] += [vs, rs]
            info["rout_ohm"] = rout
            info["tr_s"] = tr
            info["tf_s"] = tf
            info["edge_basis"] = e_.get("basis")
            info["drive"] = d
            info["note"] = ("V 源的 PULSE 参数只是占位默认激励；实际时钟/数据波形请用 "
                            "override 的 sources 覆盖，或把本元件作为负载")
        if cap["c_total_pf"] > 0:
            cn = ctx.name("C", f"{tag}_CCOMP")
            lines.append(f"{cn} {terminal} 0 {cap['c_total_pf']:.6g}p")
            info["elements"].append(cn)
        cl_lines, cl_detail = self._emit_clamps(ctx, des, pin, node, spec, vcc,
                                                entry, tag)
        if cl_lines:
            lines.extend(cl_lines)
        if cl_detail:
            info["clamps"] = cl_detail
        if spec.get("vinl") is not None or spec.get("vinh") is not None:
            info["vinl"] = spec.get("vinl")
            info["vinh"] = spec.get("vinh")
            info["vmeas"] = spec.get("vmeas")
        return lines, info


def _sha256(path):
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def summary_text(infos):
    out = []
    for i in infos:
        out.append(f"{i['designator']}.{i['pin']} {i['model']}")
        out.append(f"simulation level: {i['level']}")
        out.append(f"ibis: {i['ibis_file']}  sha256={str(i['ibis_sha256'])[:12]}…")
        out.append(f"C_total = {i['c_total_pf']} pF")
        if i.get("rout_ohm"):
            out.append(f"Rout = {i['rout_ohm']} Ω   tr/tf = "
                       f"{i['tr_s'] * 1e12:.1f}/{i['tf_s'] * 1e12:.1f} ps "
                       f"[{i.get('edge_basis')}]")
    return out


__all__ = ["IbisSet", "describe", "LEVEL", "IBIS_FIDELITY"]


def _pwl_current(points, vexpr, sign=1.0):
    """V-I 折线 → ngspice B 源表达式（用 uramp 拼分段线性）。

    两个必须记住的点（v2.5 / v2.5.1 各踩过一次）：

    1. **折点取线段起点**：`slope_k` 只在 `[v_k, v_{k+1})` 内有效；
    2. **系数是斜率增量** `slope_k − slope_{k−1}`：uramp 项是累加的，
       直接写 `slope_k` 会让旧斜率一直叠着。

    I(v) = I(v0) + Σ_k (slope_k − slope_{k−1})·uramp(v − v_start_k)

    返回值就是**表里的电流本身**（`sign=+1`）；IBIS 的 "positive = into the component"
    已经把方向编码在符号里，调用方不要再统一取负。
    """
    pts = [(float(p["v"]), float(p.get("i_typ") if p.get("i_typ") is not None
                                  else p.get("i", 0.0))) for p in points
           if p.get("v") is not None]
    pts.sort(key=lambda x: x[0])
    # 去掉重复电压点（IBIS 表里偶尔有）
    uniq = []
    for v, i in pts:
        if uniq and abs(v - uniq[-1][0]) < 1e-12:
            uniq[-1] = (v, i)
        else:
            uniq.append((v, i))
    if len(uniq) < 2:
        return None
    segs = []
    for k in range(1, len(uniq)):
        v0, i0 = uniq[k - 1]
        v1, i1 = uniq[k]
        if v1 - v0 <= 1e-12:
            continue
        segs.append((v0, (i1 - i0) / (v1 - v0)))
    if not segs:
        return None
    terms = []
    i0 = uniq[0][1] * sign
    if abs(i0) > 1e-15:
        terms.append(f"{i0:.9g}")
    prev = 0.0
    for v_start, slope in segs:
        delta = (slope - prev) * sign
        prev = slope
        if abs(delta) < 1e-15:
            continue
        terms.append(f"({delta:.9g})*uramp({vexpr}-({v_start:.9g}))")
    return " + ".join(terms) if terms else None

