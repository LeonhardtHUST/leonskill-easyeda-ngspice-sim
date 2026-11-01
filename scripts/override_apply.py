#!/usr/bin/env python3
"""v2.2 P0-E：PCB 寄生 override 层 + 端点级网段切分（virtual node）。

问题：原理图里 `NET-CLOCK` 只有一个网络，PCB 上却是
`XG1 → TL1 → 分支点 → TL2 → ADC / TL3 → FPGA`。网表里没有"物理分段"这个概念，
在同一个 net 上并联几个 R/L 表达不了串联寄生。

解法：override 层允许用 `component.pin` 作为端点，生成网表时自动把该引脚从原网络
上"摘下来"，挂到独立虚拟节点（`$CLK_ADC` 之类）。摘下来之后，串联元件才真正
落在信号路径上。

节点命名：`$FOO` 在 deck 中写作 `__FOO`（ngspice 里 `$` 是行内注释符，实测
`V1 $SRC 0 1` 直接报 "Not enough parameters for v source"）。报告与 manifest
仍保留 `$FOO` 写法。
"""
import os
import re

from ground import is_ground_like
from parasitics import (
    ParasiticError, capacitor, capacitor_model, connector_model, ferrite_model,
    fmt, inductor, parse_eng, resistor, series_chain, transmission_line, via_model,
    waveform_text,
)
from sim_config import is_virtual, spice_ident, spice_node

SERIES_TYPES = {
    "transmission_line", "tline", "series_rl", "series_rlc", "series_lc",
    "via", "via_rl", "connector", "ferrite",
}


class Term:
    __slots__ = ("kind", "node", "des", "pin", "ref")

    def __init__(self, kind, node, ref, des=None, pin=None):
        self.kind = kind      # endpoint | node
        self.node = node
        self.ref = ref
        self.des = des
        self.pin = pin

    def __repr__(self):
        return f"<{self.kind} {self.node}>"


class OverridePlan:
    def __init__(self, cfg, ir, pin_net, ground, namer, deck_dir=None):
        self.cfg = cfg or {}
        self.ir = ir or {}
        self.pin_net = pin_net or {}
        self.ground = ground
        self.namer = namer
        self.deck_dir = deck_dir
        self.errors = []
        self.warnings = []
        self.elements = []
        self.component_lines = {}     # designator -> [lines]（替换默认元件）
        self.disabled = set()
        self.node_map = {}            # (des, pin) -> node or None
        self.virtual_nodes = []
        self.segments_info = []
        self.overrides_info = []
        self.merged = {}              # 被 short 合并的节点
        self.params = dict(self.cfg.get("params") or {})   # .param 定义（供表达式/扫参用）
        self._counts = {}
        self._des_index = {}
        self._net_names = set()
        self._built = False
        self._ctx = _Ctx(self)

    # ---------------------------------------------------------------- 工具
    def node_of(self, comp_or_des, pin):
        des = comp_or_des.get("designator") if isinstance(comp_or_des, dict) else comp_or_des
        if des is None or pin is None:
            return None
        return self.node_map.get((str(des), str(pin)))

    def node_for_net(self, netname):
        return self._canon(self._canonical_net(netname))

    def _canonical_net(self, name):
        if name is None:
            return None
        tgt = self.ground.node(name)
        return spice_node(tgt)

    def _canon(self, node):
        if node is None:
            return None
        seen = 0
        cur = node
        while cur in self.merged and seen < 64:
            cur = self.merged[cur]
            seen += 1
        return cur

    def _count_on(self, node):
        return self._counts.get(node, 0)

    def _drop(self, node):
        if node in self._counts:
            self._counts[node] -= 1

    def _add(self, node):
        self._counts[node] = self._counts.get(node, 0) + 1

    def _err(self, code, message, **extra):
        d = {"code": code, "message": message, "source": "sim_override.json"}
        d.update(extra)
        self.errors.append(d)

    def _virtual(self, name):
        node = spice_node(name)
        if node not in self.virtual_nodes:
            self.virtual_nodes.append(node)
        return node

    # ---------------------------------------------------------------- 构建
    def build(self):
        if self._built:
            return self
        self._built = True
        ir = self.ir
        for c in ir.get("components", []):
            if c.get("componentType") != "part":
                continue
            des = str(c.get("designator") or "")
            if des:
                self._des_index[des.upper()] = c
        for n in ir.get("nets", []):
            if n.get("name"):
                self._net_names.add(str(n.get("name")))

        # 1) 初始节点：引脚 → 规范化节点
        for des, pins in self.pin_net.items():
            for pin, net in (pins or {}).items():
                node = self._canonical_net(net)
                self.node_map[(str(des), str(pin))] = node
                self._add(node)
        # 补齐无网络引脚 = None
        for c in ir.get("components", []):
            if c.get("componentType") != "part":
                continue
            des = str(c.get("designator") or "")
            for p in c.get("pins") or []:
                key = (des, str(p.get("number")))
                self.node_map.setdefault(key, None)

        # 2) disable / replace
        for des in self.cfg.get("disable") or []:
            self.disabled.add(str(des).upper())
        # 3) disconnect
        for item in self.cfg.get("disconnect") or []:
            ref = item if isinstance(item, str) else item.get("endpoint") or item.get("from")
            t = self._parse_endpoint(ref)
            if t and t.des and t.pin:
                key = (t.des, t.pin)
                if self.node_map.get(key) is not None:
                    self._drop(self.node_map[key])
                self.node_map[key] = None
                self.overrides_info.append({"op": "disconnect", "endpoint": ref})
            else:
                self._err("OVERRIDE_ENDPOINT_MISSING",
                          f"disconnect 端点不存在: {ref!r}")
        # 4) short
        for item in self.cfg.get("short") or []:
            a, b = self._term_node(item.get("from")), self._term_node(item.get("to"))
            if a is None or b is None:
                self._err("OVERRIDE_ENDPOINT_MISSING",
                          f"short 端点不存在: {item}")
                continue
            if a != b:
                self.merged[a] = b
                self.node_map = {k: (self._canon(v) if v else v) for k, v in self.node_map.items()}
                self._recount()
                self.overrides_info.append({"op": "short", "from": a, "to": b})
        # 5) 显式网段切分
        self._apply_segments()
        # 6) component_parasitics（元件自身寄生：ESR/ESL/DCR/ferrite/connector）
        self._apply_component_parasitics()
        # 7) replace
        self._apply_replace()
        # 8) 寄生 / 元件 / 源
        self._apply_parasitics()
        self._apply_sparams()
        self._apply_sources()
        return self

    def _recount(self):
        self._counts = {}
        for v in self.node_map.values():
            if v:
                self._add(v)

    # ---- 端点解析 ----
    def _parse_endpoint(self, ref):
        """'U20.5' → (des, pin)；非端点返回 None。"""
        if not isinstance(ref, str) or "." not in ref:
            return None
        des, pin = ref.rsplit(".", 1)
        des = des.strip()
        pin = pin.strip()
        if not des or not pin:
            return None
        if des.upper() in self._des_index:
            return Term("endpoint", self.node_map.get((des, pin)), ref, des, pin)
        return None

    def _term_node(self, ref):
        """解析任意终点引用 → 节点名；失败返回 None 并记错。"""
        if not isinstance(ref, str) or not ref.strip():
            self._err("OVERRIDE_ENDPOINT_MISSING", f"终点引用为空: {ref!r}")
            return None
        s = ref.strip()
        if is_virtual(s):
            return self._canon(self._virtual(s))
        ep = self._parse_endpoint(s)
        if ep:
            if ep.node is None:
                self._err("OVERRIDE_ENDPOINT_MISSING",
                          f"端点 {s} 没有连接网络（未连线或已被 disconnect）")
            return self._canon(ep.node)
        if s in self._net_names:
            return self._canon(self._canonical_net(s))
        for n in self._net_names:          # 大小写不敏感兜底
            if n.upper() == s.upper():
                return self._canon(self._canonical_net(n))
        if s == "0" or is_ground_like(s) or self.ground.map(s)[0] is not None:
            # 原理图里没出现的地名（0 / EARTH / CHASSIS ...）也允许直接引用
            return spice_node(self.ground.node(s))
        self._err("OVERRIDE_ENDPOINT_MISSING",
                  f"终点 {s!r} 既不是已存在的元件引脚（如 U20.5），也不是网络名或 $虚拟节点")
        return None

    def _is_ground_term(self, ref):
        if not isinstance(ref, str):
            return False
        s = ref.strip()
        if s == "0":
            return True
        if is_virtual(s):
            return False
        if self._parse_endpoint(s):
            return False
        return is_ground_like(s) or self.ground.is_zero(s) or self.ground.map(s)[0] is not None

    def _rehome(self, ep, mode=None, other=None):
        """把端点引脚从共享网络上摘到私有虚拟节点。返回节点名。"""
        node = ep.node
        if node is None or ep.des is None:
            return node
        if mode == "shunt":
            return node
        if other is not None and self._is_ground_term(other):
            return node                      # 对地并联 → 不是串联插入
        if self._count_on(node) <= 1:
            return node                      # 已经是独点，保留可读的网络名
        pin_tag = re.sub(r"[^0-9A-Za-z]", "_", str(ep.pin))
        new = f"__{node}_{spice_ident(ep.des)}_{pin_tag}"
        new = self._unique_node(new)
        self._drop(node)
        self.node_map[(ep.des, ep.pin)] = new
        self._add(new)
        self.virtual_nodes.append(new)
        self.segments_info.append({"kind": "auto", "endpoint": f"{ep.des}.{ep.pin}",
                                   "from": node, "to": new})
        return new

    def _unique_node(self, node):
        existing = set(self.node_map.values()) | set(self.virtual_nodes)
        s, i = node, 2
        while s in existing:
            s = f"{node}_{i}"
            i += 1
        return s

    # ---- 显式网段 ----
    def _apply_segments(self):
        for seg in self.cfg.get("segments") or []:
            net = seg.get("net")
            for ref, target in (seg.get("endpoints") or {}).items():
                ep = self._parse_endpoint(ref)
                if not ep:
                    self._err("OVERRIDE_ENDPOINT_MISSING",
                              f"segments: 端点 {ref!r} 不存在于原理图")
                    continue
                if net and ep.node is not None and ep.node != self._canonical_net(net):
                    self.warnings.append(
                        f"segments: 端点 {ref} 当前不在网络 {net} 上（实际 {ep.node}），仍按配置切分")
                if ep.node is not None:
                    self._drop(ep.node)
                node = self._virtual(target) if is_virtual(str(target)) else spice_node(str(target))
                self.node_map[(ep.des, ep.pin)] = node
                self._add(node)
                self.segments_info.append({"kind": "declared", "endpoint": ref,
                                           "from": ep.node, "to": node, "net": net})

    # ---- 元件自身寄生 ----
    def _apply_component_parasitics(self):
        for item in self.cfg.get("component_parasitics") or []:
            des = str(item.get("component") or item.get("designator") or "")
            model = item.get("model") or {}
            comp = self._des_index.get(des.upper())
            if not comp:
                self._err("OVERRIDE_ENDPOINT_MISSING", f"component_parasitics: 元件 {des} 不存在")
                continue
            pins = comp.get("pins") or []
            if len(pins) != 2:
                self._err("PARASITIC_COMPONENT_UNSUPPORTED",
                          f"{des}: 元件级寄生仅支持 2 脚元件（实际 {len(pins)} 脚）")
                continue
            n1 = self.node_map.get((des, str(pins[0].get("number"))))
            n2 = self.node_map.get((des, str(pins[1].get("number"))))
            if not n1 or not n2:
                self._err("PARASITIC_COMPONENT_UNSUPPORTED", f"{des}: 存在悬空引脚，无法加寄生")
                continue
            source = item.get("source")
            if not source:
                self.warnings.append(
                    f"{des}: 寄生参数缺少 source 字段（datasheet / engineering_estimate），已按未声明处理")
            hint = spice_ident(des)
            kind = str(model.get("type") or "").lower()
            prefix = (comp.get("pinPrefix") or "").upper()
            try:
                if kind == "ferrite" or "RDC" in model:
                    lines = ferrite_model(self._ctx, hint, n1, n2,
                                          rdc=model.get("RDC") or model.get("DCR"),
                                          l=model.get("L"), rp=model.get("RP"))
                elif kind == "connector":
                    lines = connector_model(self._ctx, hint, n1, n2,
                                            r=model.get("R"), l=model.get("L"))
                elif prefix == "C" or "C" in model:
                    lines, _ = capacitor_model(
                        self._ctx, hint, n1, n2, model.get("C") or comp.get("value") or "100n",
                        esr=model.get("ESR"), esl=model.get("ESL"), source=source)
                elif prefix == "L" or "L" in model:
                    lval = model.get("L") or comp.get("value") or "1m"
                    if model.get("DCR") is None and model.get("ESR") is None:
                        out, _ = inductor(self._ctx, hint, n1, n2, lval)
                        lines = out
                    else:
                        mid = self._ctx.node(f"{hint}_DCR")
                        lines, _ = inductor(self._ctx, hint, n1, mid, lval)
                        r_val = model.get("DCR", model.get("ESR"))
                        out, _ = resistor(self._ctx, f"{hint}_DCR", mid, n2, r_val)
                        lines += out
                else:
                    self._err("PARASITIC_COMPONENT_UNSUPPORTED",
                              f"{des}: 无法识别的元件寄生模型 {model}")
                    continue
            except ParasiticError as e:
                self._err(e.code, f"{des}: {e.message}")
                continue
            if lines:
                self.component_lines[des] = lines
                self.overrides_info.append({
                    "op": "component_parasitic", "component": des,
                    "source": source, "model": model,
                    "elements": [ln.split()[0] for ln in lines]})

    # ---- replace ----
    def _apply_replace(self):
        for des, spec in (self.cfg.get("replace") or {}).items():
            comp = self._des_index.get(str(des).upper())
            if not comp:
                self._err("OVERRIDE_ENDPOINT_MISSING", f"replace: 元件 {des} 不存在")
                continue
            kind = str((spec or {}).get("type") or "ideal_source").lower()
            self.disabled.add(str(des).upper())
            if kind in ("open", "none"):
                self.overrides_info.append({"op": "replace", "component": des,
                                            "type": "open (removed)"})
                continue
            node = self._replace_target_node(comp, spec)
            if node is None:
                continue
            ref = spice_node(str(spec.get("ref", "0")))
            try:
                if kind in ("ideal_regulator", "ideal_source", "voltage_source"):
                    v = fmt(parse_eng(spec.get("vout", spec.get("v", "0")), "vout"))
                    nm = self.namer("V", f"REPL_{spice_ident(des)}")
                    lines = [f"{nm} {node} {ref} DC {v}"]
                    desc = f"ideal source {v} V"
                elif kind == "resistor":
                    lines = []
                    out, _ = resistor(self._ctx, f"REPL_{spice_ident(des)}", node, ref,
                                      spec.get("r", "1k"))
                    lines += out
                    desc = f"resistor {spec.get('r', '1k')}"
                elif kind in ("short", "wire"):
                    nm = self.namer("R", f"REPL_{spice_ident(des)}")
                    lines = [f"{nm} {node} {ref} 1e-3"]
                    desc = "0Ω short"
                else:
                    self._err("REPLACE_UNSUPPORTED", f"{des}: 不支持的 replace 类型 {kind}")
                    continue
            except ParasiticError as e:
                self._err(e.code, f"{des}: {e.message}")
                continue
            self.component_lines[str(des)] = lines
            self.overrides_info.append({"op": "replace", "component": des,
                                        "type": kind, "node": node, "desc": desc,
                                        "elements": [ln.split()[0] for ln in lines]})

    def _replace_target_node(self, comp, spec):
        des = comp.get("designator")
        if spec.get("node"):
            return self._term_node(str(spec["node"]))
        if spec.get("pin"):
            n = self.node_map.get((des, str(spec["pin"])))
            if n is None:
                self._err("OVERRIDE_ENDPOINT_MISSING", f"{des}: replace 指定的 pin {spec['pin']} 无连接")
            return n
        cands = []
        for p in comp.get("pins") or []:
            nm = str(p.get("name") or "").upper()
            if any(k in nm for k in ("VOUT", "OUT", "VO")):
                n = self.node_map.get((des, str(p.get("number"))))
                if n:
                    cands.append(n)
        uniq = list(dict.fromkeys(cands))
        if len(uniq) == 1:
            return uniq[0]
        if not uniq:
            self._err("REPLACE_TARGET_UNKNOWN",
                      f"{des}: 无法确定 replace 的输出节点，请显式给 pin 或 node")
            return None
        self.warnings.append(f"{des}: 有多个输出候选 {uniq}，取第一个")
        return uniq[0]

    # ---- 寄生元件 ----
    def _apply_parasitics(self):
        for idx, item in enumerate(self.cfg.get("parasitics") or []):
            name = str(item.get("name") or f"P{idx + 1}")
            kind = str(item.get("type") or "").lower()
            f_ref, t_ref = item.get("from"), item.get("to")
            mode = item.get("mode")
            if mode is None:
                mode = "series" if kind in SERIES_TYPES else None
            f_ep = self._parse_endpoint(f_ref) if isinstance(f_ref, str) else None
            t_ep = self._parse_endpoint(t_ref) if isinstance(t_ref, str) else None
            # 同一个 2 脚元件的两脚之间插元件 = "替换这颗器件"（如理想磁珠/0Ω），
            # 此时不能把引脚从各自网络上摘下来，否则两端都悬空。
            replace_semantics = (f_ep and t_ep and f_ep.des == t_ep.des
                                 and f_ep.node is not None and t_ep.node is not None
                                 and f_ep.node != t_ep.node)
            if f_ep:
                f_node = self._term_node(f_ref) if replace_semantics \
                    else self._rehome(f_ep, mode, t_ref)
            else:
                f_node = self._term_node(f_ref)
            if t_ep:
                t_node = self._term_node(t_ref) if replace_semantics \
                    else self._rehome(t_ep, mode, f_ref)
            else:
                t_node = self._term_node(t_ref)
            if f_node is None or t_node is None:
                continue
            hint = spice_ident(name)
            try:
                lines, info = self._build_parasitic(kind, hint, f_node, t_node, item)
            except ParasiticError as e:
                self._err(e.code, f"{name}: {e.message}", name=name, type=kind)
                continue
            if not lines:
                self.warnings.append(f"{name}: 未产生任何元件（参数为空？）")
                continue
            en = item.get("element_name")
            ens = item.get("element_names")
            if en or ens:
                # 允许显式指定元件名，供 sweep 按名字改值（如 R_CLK_DAMP / C_HF）
                names = ens if isinstance(ens, list) else [en]
                it = iter(names)
                for i, ln in enumerate(lines):
                    if ln.startswith("*"):
                        continue
                    try:
                        new = self.namer.take(next(it))
                    except StopIteration:
                        break
                    kind = ln.split()[0][:1].upper()
                    if new[:1].upper() != kind:
                        # 首字母决定 ngspice 的器件类型：把 R 命名成 L_xxx 会被当成电感
                        self._err("ELEMENT_NAME_MISMATCH",
                                  f"{name}: element_name {new!r} 的首字母必须是 {kind}"
                                  f"（该元件是 {kind} 类型）")
                        continue
                    parts = ln.split()
                    parts[0] = new
                    lines[i] = " ".join(parts)
                if info:
                    info["element"] = lines[[i for i, l in enumerate(lines)
                                             if not l.startswith("*")][0]].split()[0]
            self.elements.extend(lines)
            rec = {"name": name, "type": kind, "from": f_node, "to": t_node,
                   "elements": [ln.split()[0] for ln in lines],
                   "source": item.get("source")}
            if info:
                rec.update(info)
            self.overrides_info.append(rec)

    def _build_parasitic(self, kind, hint, n1, n2, item):
        ref = item.get("ref", "0")
        if kind in ("transmission_line", "tline"):
            lines, info = transmission_line(self._ctx, hint, n1, n2, item, ref=ref,
                                           ref2=item.get("ref2"))
            return lines, info
        if kind in ("series_rl", "via", "via_rl"):
            # 顺序固定为 R → L（与 element_names 的直觉一致；串联顺序不影响结果）
            parts = []
            if item.get("r") is not None:
                parts.append(("R", item["r"]))
            if item.get("l") is not None:
                parts.append(("L", item["l"]))
            if not parts:
                raise ParasiticError("PARAM_INVALID", "series_rl 需要 r 和/或 l")
            return series_chain(self._ctx, hint, n1, n2, parts), None
        if kind in ("series_rlc", "series_lc"):
            parts = [(k.upper(), item[k]) for k in ("l", "r", "c") if item.get(k) is not None]
            if not parts:
                raise ParasiticError("PARAM_INVALID", "series_rlc 需要 l/r/c 至少一项")
            return series_chain(self._ctx, hint, n1, n2, parts), None
        if kind in ("capacitor", "cap", "shunt_cap"):
            lines, _ = capacitor_model(self._ctx, hint, n1, n2, item.get("c"),
                                       esr=item.get("esr") or item.get("esr_ohm"),
                                       esl=item.get("esl"), source=item.get("source"))
            return lines, {"c": item.get("c"), "esr": item.get("esr"), "esl": item.get("esl")}
        if kind == "resistor":
            return resistor(self._ctx, hint, n1, n2, item.get("r"))[0], None
        if kind == "inductor":
            return inductor(self._ctx, hint, n1, n2, item.get("l"))[0], None
        if kind == "ferrite":
            return ferrite_model(self._ctx, hint, n1, n2,
                                 rdc=item.get("rdc") or item.get("r"),
                                 l=item.get("l"), rp=item.get("rp")), None
        if kind == "connector":
            return connector_model(self._ctx, hint, n1, n2,
                                   r=item.get("r"), l=item.get("l")), None
        raise ParasiticError("PARASITIC_TYPE_UNKNOWN", f"未知寄生类型 {kind!r}")

    # ---- Touchstone / S 参数元件 ----
    def _apply_sparams(self):
        """override 里的 "sparams": [{name, from, to, file, backend, band}]。

        ngspice-47 的 `S` 元件不可用（实测 model type mismatch），所以默认走
        sparam_element 的集总拟合 adapter；拟合不合格会直接报错，不接一个形状不对的模型。
        """
        items = self.cfg.get("sparams") or []
        if not items:
            return
        import sparam_element
        import touchstone
        for item in items:
            name = str(item.get("name") or "SPARAM")
            path = item.get("file")
            if not path:
                self._err("SPARAM_FILE_MISSING", f"{name}: 缺少 file")
                continue
            if not os.path.isabs(path):
                base = self.cfg.get("_dir") or os.getcwd()
                cand = os.path.normpath(os.path.join(base, path))
                if os.path.exists(cand):
                    path = cand
            n1 = self._term_node(item.get("from"))
            n2 = self._term_node(item.get("to"))
            if n1 is None or n2 is None:
                continue
            backend = str(item.get("backend") or "lumped").lower()
            try:
                if backend in ("vectorfit", "vf"):
                    lines, info = self._emit_vectorfit(name, path, n1, n2, item)
                else:
                    ts = touchstone.parse(path)
                    band = item.get("band")
                    lines, info = sparam_element.emit(
                        ts, self._ctx, name, n1, n2,
                        backend=backend, max_err_db=item.get("max_err_db"),
                        band=band)
            except Exception as e:
                self._err(getattr(e, "code", "SPARAM_ERROR"),
                          f"{name}: {getattr(e, 'message', e)}")
                continue
            self.elements.extend(lines)
            info.update({"from": n1, "to": n2, "file": os.path.basename(path)})
            self.overrides_info.append({"op": "sparam", "name": name,
                                        "type": info.get("fidelity"),
                                        "from": n1, "to": n2,
                                        "elements": [l.split()[0] for l in lines
                                                     if l and not l.startswith("*")],
                                        "sparam": info})
            for w in info.get("warnings") or []:
                self.warnings.append(f"{name}: {w}")

    # ---- vector-fit 宏模型 ----
    def _emit_vectorfit(self, name, path, n1, n2, item):
        """宽带 N-port：scikit-rf VectorFitting → SPICE 子电路 → round-trip 验证。"""
        import network_validate as nv
        import rf_network as rn
        import vector_fit as vfmod
        if not rn.available():
            raise RuntimeError(
                "RF_BACKEND_UNAVAILABLE: vectorfit 需要 scikit-rf"
                "（pip install -r requirements-rf.txt）；或改用 backend=lumped")
        net = rn.RfNetwork.from_touchstone(path, name=name)
        target = float(item.get("target_z0") or net.z0_vector()[0].real)
        net, zinfo = rn.ensure_z0(net, target, strict=True)
        res = vfmod.fit(net, order=item.get("order", "auto"),
                        max_order=int(item.get("max_order") or 20),
                        target_rms_db=float(item.get("target_rms_db") or 0.2),
                        enforce_passivity=bool(item.get("enforce_passivity")),
                        log=lambda *_: None)
        if item.get("require_passive", True) and not res["passivity"]["passive"]:
            raise RuntimeError(
                "PASSIVITY_VIOLATION: 拟合结果非无源且未开启 enforce_passivity；"
                "请设 \"enforce_passivity\": true 或用 backend=lumped")
        sub = f"S_AUTO_{name}"
        outdir = item.get("workdir") or self.cfg.get("_dir") or os.getcwd()
        subpath = os.path.join(outdir, f"{sub}.sp")
        vfmod.write_spice(res, subpath, sub, header_extra={
            "source": os.path.basename(path), "z0": zinfo})
        report = None
        if item.get("validate", True):
            report, _ = nv.validate(subpath, sub, net, outdir,
                                    tol_db=float(item.get("tolerance_db") or 0.5),
                                    passivity=res["passivity"])
        lines = [f"* vector-fit macromodel for {name} "
                 f"({res['order']} poles, rms {res['error']['rms_mag_error_db']:.4f} dB)",
                 f'X_{name} {n1} {n2} {sub}']
        info = {"name": name, "file": os.path.basename(path), "num_ports": net.nports,
                "backend": "vectorfit", "fidelity": vfmod.FIDELITY,
                "model": vfmod.to_manifest(res, subpath),
                "z0": zinfo, "subckt": sub, "subckt_file": subpath,
                "roundtrip": report}
        return lines, info

    # ---- 独立源 ----
    def _apply_sources(self):
        for idx, item in enumerate(self.cfg.get("sources") or []):
            kind = str(item.get("type") or "current_source").lower()
            name = str(item.get("name") or f"SRC{idx + 1}")
            n1 = self._term_node(item.get("from"))
            n2 = self._term_node(item.get("to") or "0")
            if n1 is None or n2 is None:
                continue
            prefix = "I" if kind.startswith("i") or "current" in kind else "V"
            nm = self.namer.take(name if name.upper().startswith(prefix) else f"{prefix}_{name}")
            spec = item.get("waveform")
            if isinstance(spec, dict) and "type" not in spec:
                spec = dict(spec, type=item.get("waveform_type", "pulse"))
            expr = waveform_text(spec)
            if expr is None:
                self._err("PARAM_INVALID", f"{name}: 缺少 waveform")
                continue
            line = f"{nm} {n1} {n2} {expr}"
            if item.get("ac") is not None:
                line += f" AC {item['ac']}"
            self.elements.append(f"* override source {name} ({kind})")
            self.elements.append(line)
            self.overrides_info.append({"name": name, "type": kind, "from": n1, "to": n2,
                                        "elements": [nm], "waveform": expr,
                                        "source": item.get("source")})

    # ---------------------------------------------------------------- 报告
    def report(self):
        lines = []
        if self.segments_info:
            lines.append("Net segmentation (virtual nodes):")
            for s in self.segments_info:
                tag = "declared" if s["kind"] == "declared" else "auto"
                lines.append(f"  {s['endpoint']}: {s['from']} -> {s['to']}  [{tag}]")
        if self.merged:
            lines.append("Shorted nets: " + ", ".join(f"{a} -> {b}" for a, b in self.merged.items()))
        if self.overrides_info:
            lines.append("Overrides applied: " + str(len(self.overrides_info)))
        return lines


class _Ctx:
    """把 plan 的命名/建节点能力包成 parasitics.NetCtx 接口。"""

    def __init__(self, plan):
        from parasitics import NetCtx
        self._net = NetCtx(plan.namer)
        self._plan = plan

    @property
    def namer(self):
        return self._net.namer

    def name(self, prefix, hint):
        return self._net.name(prefix, hint)

    def node(self, hint):
        return self._net.node(hint)
