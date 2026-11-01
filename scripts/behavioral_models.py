#!/usr/bin/env python3
"""v2.2 P0-D：Behavioral placeholder models。

FPGA / MCU / ADC 数字输出 / 专用 RF IC 往往没有公开可用的晶体管级模型，
v2.1 只能把它们排除（等于从电路里挖掉）。v2.2 支持三类"行为级占位"：

  1. dynamic_current_load  — 电源脚挂 PULSE/PWL 电流汇（FPGA VCCINT 等）
  2. capacitive_input      — 输入脚挂 Cin（CLK / GPIO / 比较器输入）
  3. switching_output      — 输出脚用 PULSE 源 + Rout + Cload 造边沿

**这些一律标记为 BEHAVIORAL_APPROXIMATION**，报告里不得与厂商模型混为一谈；
"logic core: not simulated" 之类的声明由配置里的 not_simulated 带出。
"""
import fnmatch
import os

from sim_config import load_json, spice_ident
from parasitics import ParasiticError, fmt, parse_eng, series_chain, waveform_text

LEVEL = "BEHAVIORAL_APPROXIMATION"

TYPE_DESC = {
    "dynamic_current_load": "dynamic current load",
    "capacitive_input": "input capacitance",
    "switching_output": "switching output (PULSE + Rout + Cload)",
}


class BehavioralError(Exception):
    def __init__(self, code, message, designator=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.designator = designator


class BehavioralSet:
    def __init__(self, path=None, enabled=True):
        self.enabled = enabled
        self.path = path
        self.models = {}
        self.errors = []
        self._index = []
        if path and os.path.exists(path):
            cfg = load_json(path)
            self.models = cfg.get("models") or {}
            for key, ent in self.models.items():
                ent = dict(ent or {})
                ent.setdefault("key", key)
                self._index.append(ent)
        elif path:
            self.errors.append({"code": "BEHAVIORAL_FILE_MISSING",
                                "message": f"behavioral 配置不存在: {path}"})

    # ------------------------------------------------------------------ 匹配
    def match(self, comp):
        if not self.enabled:
            return None
        des = str(comp.get("designator") or "").upper()
        mpn = str(comp.get("manufacturerPart") or "").upper()
        dev = str(comp.get("deviceTitle") or "").upper()
        title = f"{dev} {mpn}"
        for ent in self._index:
            m = ent.get("match") or {}
            if not m:
                continue
            if any(str(d).upper() == des for d in m.get("designator") or []):
                return ent
            pats = m.get("aliases") or []
            if any(fnmatch.fnmatchcase(title, p.upper()) for p in pats):
                return ent
            for field, val in (("mpn", mpn), ("device", dev)):
                pats = m.get(field) or []
                if not pats:
                    continue
                if any(fnmatch.fnmatchcase(val, p.upper()) for p in pats):
                    return ent
        return None

    # ------------------------------------------------------------------ 生成
    def build(self, ctx, comp, node_of):
        """返回 (lines, info)。node_of(comp, pin_number) → 节点名 or None。"""
        ent = self.match(comp)
        if not ent:
            return [], None
        des = comp.get("designator") or "?"
        kind = str(ent.get("type") or "").lower()
        if kind not in TYPE_DESC:
            raise BehavioralError("BEHAVIORAL_TYPE_UNKNOWN",
                                  f"{des}: 未知 behavioral 类型 {kind!r}", des)
        if kind == "dynamic_current_load":
            lines, detail = self._current_load(ctx, comp, ent, node_of)
        elif kind == "capacitive_input":
            lines, detail = self._cap_input(ctx, comp, ent, node_of)
        else:
            lines, detail = self._switching_output(ctx, comp, ent, node_of)
        info = {
            "designator": des,
            "part": comp.get("manufacturerPart") or comp.get("deviceTitle"),
            "key": ent.get("key"),
            "type": kind,
            "level": LEVEL,
            "models": detail,
            "not_simulated": ent.get("not_simulated") or [],
            "note": ent.get("note"),
            "elements": [ln.split()[0] for ln in lines],
        }
        return lines, info

    # ---- 引脚定位 ----
    def _pins(self, comp, ent, role):
        """按引脚名（glob，忽略大小写）或引脚号找引脚。"""
        spec = (ent.get("pins") or {}).get(role)
        if spec is None:
            return []
        if isinstance(spec, str):
            spec = [spec]
        by_num = bool((ent.get("pins_by") or {}).get(role) == "number")
        out = []
        for p in comp.get("pins") or []:
            num = str(p.get("number"))
            nm = str(p.get("name") or "")
            for pat in spec:
                pat = str(pat)
                if by_num:
                    if num.upper() == pat.upper():
                        out.append(p)
                elif nm and (fnmatch.fnmatchcase(nm.upper(), pat.upper())):
                    out.append(p)
                elif num.upper() == pat.upper():  # 允许直接写引脚号
                    out.append(p)
        seen, uniq = set(), []
        for p in out:
            if str(p.get("number")) in seen:
                continue
            seen.add(str(p.get("number")))
            uniq.append(p)
        return uniq

    def _nodes(self, comp, ent, role, node_of):
        pins = self._pins(comp, ent, role)
        if not pins:
            raise BehavioralError(
                "BEHAVIORAL_PIN_MISSING",
                f"{comp.get('designator')}: behavioral 配置的 {role} 引脚"
                f" {(ent.get('pins') or {}).get(role)} 在原理图中找不到"
                f"（现有引脚名 {[p.get('name') for p in comp.get('pins') or []]}）",
                comp.get("designator"))
        nodes = []
        for p in pins:
            n = node_of(comp, str(p.get("number")))
            if n:
                nodes.append(n)
        if not nodes:
            raise BehavioralError(
                "BEHAVIORAL_PIN_FLOATING",
                f"{comp.get('designator')}: {role} 引脚没有连接任何网络",
                comp.get("designator"))
        return nodes

    # ---- 类型 1 ----
    def _current_load(self, ctx, comp, ent, node_of):
        des = comp.get("designator") or "?"
        supplies = self._nodes(comp, ent, "supply", node_of)
        ref = ent.get("ref")
        if ref is None:
            grounds = self._nodes(comp, ent, "ground", node_of) if (ent.get("pins") or {}).get("ground") else []
            ref = grounds[0] if grounds else "0"
        wf = waveform_text(ent.get("waveform"))
        if not wf:
            raise BehavioralError("BEHAVIORAL_WAVE_MISSING",
                                  f"{des}: dynamic_current_load 缺少 waveform", des)
        uniq = []
        for n in supplies:
            if n not in uniq:
                uniq.append(n)
        lines, detail = [], {}
        scale = 1.0 / len(uniq)
        for idx, node in enumerate(uniq):
            hint = f"BHV_{spice_ident(des)}_I{idx + 1}"
            nm = ctx.name("I", hint)
            expr = wf
            if scale != 1.0:
                expr = _scale_waveform(wf, scale)
            lines.append(f"{nm} {node} {ref} {expr}")
            detail[f"{node}"] = f"{TYPE_DESC['dynamic_current_load']} -> {ref}"
        lines.insert(0, f"* BEHAVIORAL_APPROXIMATION {des}: dynamic current load "
                        f"({', '.join(uniq)} -> {ref})")
        return lines, detail

    # ---- 类型 2 ----
    def _cap_input(self, ctx, comp, ent, node_of):
        des = comp.get("designator") or "?"
        nodes = self._nodes(comp, ent, "input", node_of) if (ent.get("pins") or {}).get("input") \
            else self._nodes(comp, ent, "supply", node_of)
        ref = ent.get("ref", "0")
        c = ent.get("c")
        if c is None:
            raise BehavioralError("BEHAVIORAL_PARAM_MISSING",
                                  f"{des}: capacitive_input 缺少 c", des)
        r = ent.get("r")
        lines, detail = [], {}
        for idx, node in enumerate(dict.fromkeys(nodes)):
            hint = f"BHV_{spice_ident(des)}_CIN{idx + 1}"
            if r is not None:
                out = series_chain(ctx, hint, node, ref, [("R", r), ("C", c)])
                lines.extend(out)
            else:
                nm = ctx.name("C", hint)
                lines.append(f"{nm} {node} {ref} {c}")
            detail[node] = f"{TYPE_DESC['capacitive_input']} {c}"
        lines.insert(0, f"* BEHAVIORAL_APPROXIMATION {des}: input capacitance "
                        f"{c}" + (f" + {r} series" if r is not None else ""))
        return lines, detail

    # ---- 类型 3 ----
    def _switching_output(self, ctx, comp, ent, node_of):
        des = comp.get("designator") or "?"
        nodes = self._nodes(comp, ent, "output", node_of)
        ref = ent.get("ref", "0")
        wf = ent.get("waveform") or {"type": "pulse", "v_low": "0", "v_high": "3.3",
                                     "delay": "0", "rise": "1n", "fall": "1n",
                                     "on": "5n", "period": "20n"}
        expr = waveform_text(wf)
        rout = ent.get("rout")
        cload = ent.get("cload")
        lines, detail = [], {}
        for idx, node in enumerate(dict.fromkeys(nodes)):
            hint = f"BHV_{spice_ident(des)}_OUT{idx + 1}"
            drv = ctx.node(f"{hint}_DRV")
            vs = ctx.name("V", f"{hint}_SRC")
            lines.append(f"{vs} {drv} {ref} {expr}")
            rs = ctx.name("R", f"{hint}_ROUT")
            if rout is not None:
                lines.append(f"{rs} {drv} {node} {rout}")
                detail[node] = f"{TYPE_DESC['switching_output']} Rout={rout}"
            else:
                lines.append(f"{rs} {drv} {node} 1e-3")  # 未给 Rout 时近乎理想短接
                detail[node] = f"{TYPE_DESC['switching_output']} (no Rout)"
            if cload is not None:
                cs = ctx.name("C", f"{hint}_CLOAD")
                lines.append(f"{cs} {node} {ref} {cload}")
                detail[node] += f" Cload={cload}"
        lines.insert(0, f"* BEHAVIORAL_APPROXIMATION {des}: switching output "
                        f"{expr}")
        return lines, detail


def _scale_waveform(expr, scale):
    """多个 supply 网络并存时按比例分摊电流（PULSE 的前两个参数）。"""
    if not expr.upper().startswith("PULSE("):
        return expr
    body = expr[6:-1].split()
    if len(body) < 2:
        return expr
    try:
        i1 = parse_eng(body[0]) * scale
        i2 = parse_eng(body[1]) * scale
    except ParasiticError:
        return expr
    body[0], body[1] = fmt(i1), fmt(i2)
    return "PULSE(" + " ".join(body) + ")"


def summary_text(infos):
    """报告里的 behavioral 段落（需求文档第七节的样式）。"""
    if not infos:
        return []
    out = []
    for i in infos:
        out.append(f"{i['designator']} {i.get('part') or ''}".rstrip())
        out.append(f"simulation level: {i['level']}")
        out.append("models:")
        for node, desc in i["models"].items():
            out.append(f"  {node} = {desc}")
        for ns in i.get("not_simulated") or []:
            out.append(f"{ns}: not simulated")
    return out
