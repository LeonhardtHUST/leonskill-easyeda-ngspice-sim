#!/usr/bin/env python3
"""网表生成器 v2.2：IR + 网络归组 → NGspice 网表 + 结构化报告。

用法:
  python netlist_gen.py <ir_with_nets.json> <out.cir> [选项]

选项:
  --keep-open                    无模型器件保留为注释行（v2.1 兼容）
  --config sim_config.json       复杂仿真主入口（models / behavioral / override /
                                 ground_aliases / power_policy / strict 都可写在里面）
  --models model_map.json        外部 SPICE 模型映射（覆盖 config）
  --behavioral behavioral.json   行为级模型库；`--behavioral off` 关闭
  --override sim_override.json   PCB 寄生 / 网段切分 override
  --power-policy auto|explicit   auto=按网名补理想源（v2.1 行为，带显著告警）；
                                 explicit=只认真实源与 config 声明
  --strict                       任何拓扑可疑都直接失败（不产出"看着正常"的波形）
  --allow-partial-model          pin mapping 不完整时降级为警告
  --report out.json              结构化报告（manifest / sweep 用）

分类规则（优先级序，v2.2）：
  1. R/C/L（2脚）            直接按 Value 出实例（有元件级寄生则改出 ESR/ESL 链）
  2. Q/D/M                   查模型库 → 泛化兜底
  3. 电压源器件              按属性直出，并登记为已驱动网络
  4. U/IC/X                  → 外部 .SUBCKT/.MODEL（model_map）
                             → behavioral placeholder（behavioral.json）
                             → 排除（strict 下直接失败）
  5. Power 网标自动补源      受 --power-policy 约束
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from behavioral_models import LEVEL as BHV_LEVEL          # noqa: E402
from behavioral_models import BehavioralSet, summary_text  # noqa: E402
from ground import GroundMap                              # noqa: E402
from model_resolver import MappingError, ModelResolver, provenance_text  # noqa: E402
from override_apply import OverridePlan                   # noqa: E402
from sim_config import (                                  # noqa: E402
    MODELS_DIR, SKILL_VERSION, Namer, load_config, load_json, spice_ident,
)

VOLT_RE = re.compile(r"^[+\-]?(\d+)(?:[Vv](\d+))?[Vv]?$")
IC_PREFIXES = ("U", "IC", "X")
# 明确"不是 IC"的前缀（连接器 / 排针 / 保险丝 / 开关 ...）
NON_IC_PREFIXES = {
    "R", "C", "L", "D", "LED", "Q", "M", "Q_NMOS", "Q_PMOS", "V", "I",
    "H", "J", "P", "K", "CN", "SW", "TP", "F", "FB", "BT", "KEY", "S",
    "JP", "CON", "XH", "PH", "PZ", "ANT", "MH", "TP",
}


def load_models():
    disc = load_json(os.path.join(MODELS_DIR, "discrete.json"))
    gen = load_json(os.path.join(MODELS_DIR, "generic.json"))
    alias_map = {}
    for name, part in disc.get("parts", {}).items():
        for a in part.get("aliases", []) + [name]:
            alias_map[str(a).upper()] = name
    return disc, gen, alias_map


def sanitize_des(des, used=None):
    """器件位号 → SPICE 安全名（保留 v2.1 行为，供旧调用方使用）。"""
    s = spice_ident(des, "X")
    if used is not None:
        base, i = s, 2
        while s in used:
            s = f"{base}_{i}"
            i += 1
        used.add(s)
    return s


def parse_voltage(netname):
    """从网络名解析电压值：+12V→12，-15V→-15，5V→5，3V3→3.3。"""
    if not netname:
        return None
    s = str(netname).strip()
    neg = s.startswith("-")
    m = VOLT_RE.match(s)
    if not m:
        return None
    whole, dec = m.group(1), m.group(2)
    v = float(f"{whole}.{dec}") if dec else float(whole)
    if v <= 0 or v > 1000:
        return None
    return -v if neg else v


class NetlistBuilder:
    def __init__(self, ir, pin_net, opts=None):
        self.ir = ir or {}
        self.ir.setdefault("components", [])
        self.pin_net = pin_net or {}
        self.opts = opts or {}
        self.cfg = self.opts.get("config") or {}
        self.strict = bool(self.opts.get("strict") or self.cfg.get("strict"))
        self.keep_open = bool(self.opts.get("keep_open"))
        self.power_policy = (self.opts.get("power_policy")
                             or self.cfg.get("power_policy") or "auto").lower()
        if self.power_policy not in ("auto", "explicit"):
            raise SystemExit(f"--power-policy 只支持 auto|explicit，收到 {self.power_policy}")
        self.ground = GroundMap(self.cfg.get("ground_aliases"))
        self.namer = Namer()
        self.disc, self.gen, self.alias_map = load_models()
        self.resolver = ModelResolver(
            self.opts.get("models") or self.cfg.get("models"),
            allow_partial=bool(self.opts.get("allow_partial_model")
                               or self.cfg.get("allow_partial_model")),
            strict=self.strict)
        if self.opts.get("deck_dir"):
            self.resolver.deck_dir = self.opts["deck_dir"]
        bhv_path = self.opts.get("behavioral")
        if bhv_path is None:
            bhv_path = self.cfg.get("behavioral_models")
        enabled = str(bhv_path).lower() not in ("off", "none", "false")
        self.behavioral = BehavioralSet(bhv_path if enabled else None, enabled=enabled)
        self.plan = OverridePlan(
            self.opts.get("override_cfg") if isinstance(self.opts.get("override_cfg"), dict)
            else self._load_override(),
            self.ir, self.pin_net, self.ground, self.namer,
            deck_dir=self.opts.get("deck_dir"))
        self.report = {
            "version": SKILL_VERSION,
            "included": [], "excluded": [], "generic": [], "powers": [],
            "dangling_nets": [], "warnings": [], "errors": [],
            "models": [], "behavioral": [], "overrides": [],
            "ground_mapping": {}, "net_segments": [], "virtual_nodes": [],
            "power_policy": self.power_policy, "strict": self.strict,
            "fidelity": {}, "includes": [],
        }
        self.lines = []
        self.params = {}
        self.model_cards = {}
        self.sourced_nodes = {}          # node -> [描述]
        self.power_output_nodes = {}     # node -> designator
        self._net_owner = {}
        self._fidelity = []

    # ------------------------------------------------------------------ 初始化
    def _load_override(self):
        path = self.opts.get("override") or self.cfg.get("override")
        if not path:
            return {}
        if not os.path.exists(path):
            self.report["errors"].append({
                "code": "OVERRIDE_FILE_MISSING",
                "message": f"override 文件不存在: {path}"})
            return {}
        return load_json(path)

    # ------------------------------------------------------------------ 工具
    def net(self, comp, pin):
        return self.plan.node_of(comp, pin)

    def _err(self, code, message, **extra):
        d = {"code": code, "message": message}
        d.update(extra)
        self.report["errors"].append(d)

    def warn(self, msg):
        self.report["warnings"].append(msg)

    def _two_pin_nets(self, comp):
        pins = comp.get("pins") or []
        if len(pins) != 2:
            return None
        return (self.net(comp, pins[0].get("number")),
                self.net(comp, pins[1].get("number")),
                pins[0].get("number"), pins[1].get("number"))

    def _note_net(self, raw, node):
        if raw is None or node is None:
            return
        self._net_owner.setdefault(node, set()).add(str(raw))

    def _finish_net_check(self):
        for node, owners in self._net_owner.items():
            if node == "0":
                continue
            if len(owners) > 1:
                self.warn(f"不同网络名被压到同一 SPICE 节点 {node}: {sorted(owners)}"
                          f"（请检查 ground_aliases 或网络命名）")

    # ------------------------------------------------------------------ 主流程
    def build(self):
        self.plan.build()
        self.params = dict(self.plan.params)
        for e in self.plan.errors:
            self.report["errors"].append(e)
        self.report["warnings"].extend(self.plan.warnings)

        for comp in self.ir.get("components", []):
            if comp.get("componentType") != "part":
                continue
            self._emit_component(comp)

        self._emit_override_elements()
        self._auto_power()
        self._dangling_nets()
        self._finish_net_check()
        self._finalize_report()
        return self._assemble(), self.report

    def _emit_component(self, comp):
        des = comp.get("designator") or ""
        prefix = (comp.get("pinPrefix") or "").upper()
        title = comp.get("deviceTitle") or ""
        mpn = comp.get("manufacturerPart") or ""

        # 先看 override 是否接管了这个元件（replace / component_parasitics），
        # 再判断 disable —— replace 会把原元件放进 disabled，但替换元件要照常出。
        if des in self.plan.component_lines:
            self.lines.extend(self.plan.component_lines[des])
            self.report["included"].append({"designator": des, "as": "OVERRIDE_SUBSTITUTE",
                                            "level": "Level 3"})
            self._fidelity.append((des, 3, "override substitute / component parasitic"))
            return
        if des.upper() in self.plan.disabled:
            self.report["excluded"].append({
                "designator": des, "title": title, "mpn": mpn,
                "level": "EXCLUDED",
                "reason": "被 override 配置 disable/replace 移除"})
            return

        # 电压源
        if prefix == "V" or "电压源" in title or "VOLTAGE SOURCE" in title.upper():
            self._emit_source(comp)
            return
        # IC / 模块：显式 U/IC/X，或"未知前缀 + 3 脚以上"（DA1/MCU1 这类
        # 中文工程常见位号，v2.1 会把它们当无模型器件直接排除）
        if prefix in IC_PREFIXES or prefix.startswith("IC") or (
                prefix not in NON_IC_PREFIXES and len(comp.get("pins") or []) >= 2):
            self._emit_ic(comp)
            return

        kind = prefix if prefix in ("R", "C", "L") else (
            "D" if prefix in ("D", "LED") else (
                "Q" if prefix == "Q" else (
                    "M" if prefix in ("M", "Q_NMOS", "Q_PMOS") else None)))
        if kind in ("R", "C", "L"):
            self._emit_passive(comp, kind)
        elif kind == "Q":
            self._emit_bjt(comp)
        elif kind == "D":
            self._emit_diode(comp)
        elif kind == "M":
            self._emit_mosfet(comp)
        else:
            self._exclude(comp, f"无仿真模型（{mpn or title or '未知型号'}），已排除")

    # ---- 无源 ----
    def _emit_passive(self, comp, kind):
        des = comp.get("designator") or ""
        nets = self._two_pin_nets(comp)
        if not nets:
            self.warn(f"{des}: {kind} 引脚数不是 2，跳过")
            return
        na, nb = nets[0], nets[1]
        self._note_net(self.pin_net.get(des, {}).get(str(nets[2])), na)
        self._note_net(self.pin_net.get(des, {}).get(str(nets[3])), nb)
        if na is None or nb is None:
            self.warn(f"{des}: 存在悬空引脚（无网络），已跳过该器件")
            return
        val = comp.get("value")
        if not val:
            dflt = self.gen.get("valueDefaults", {}).get(kind, {})
            val = dflt.get("value", {"R": "1k", "C": "100n", "L": "1m"}.get(kind))
            self.warn(f"{des}: 无 Value，按 {val} 兜底")
        self.lines.append(f"{self.namer.take(des)} {na} {nb} {val}")
        self.report["included"].append({"designator": des, "as": kind, "level": "Level 0"})
        self._fidelity.append((des, 0, kind))

    # ---- 电压源 ----
    def _emit_source(self, comp):
        des = comp.get("designator") or ""
        nets = self._two_pin_nets(comp)
        if not nets:
            self.warn(f"{des}: 电压源引脚数异常，跳过")
            return
        na, nb = nets[0], nets[1]
        if na is None or nb is None:
            self.warn(f"{des}: 电压源存在悬空引脚，跳过")
            return
        vval = comp.get("value")
        for k, v in (comp.get("attrs") or {}).items():
            if "voltage" in str(k).lower() and v:
                vval = v
        title = (comp.get("deviceTitle") or "").upper()
        nm = self.namer.take(des)
        if "AC" in title or "正弦" in comp.get("deviceTitle", ""):
            self.lines.append(f"{nm} {na} {nb} SIN(0 10mV 1k) AC 1 0")
            desc = "AC_SOURCE"
        else:
            self.lines.append(f"{nm} {na} {nb} DC {vval or '5V'} AC 0 0")
            desc = "V"
        for node in (na, nb):
            self.sourced_nodes.setdefault(node, []).append(f"{des}(schematic)")
        self.report["included"].append({"designator": des, "as": desc, "level": "Level 0"})
        self.report["powers"].append({"net": na, "node": na, "voltage": vval,
                                      "source": "schematic", "element": nm})

    # ---- BJT ----
    def _emit_bjt(self, comp):
        des = comp.get("designator") or ""
        title, mpn = comp.get("deviceTitle") or "", comp.get("manufacturerPart") or ""
        model_name = model_pins = card = None
        for key in (mpn, title):
            if not key:
                continue
            hit = self.alias_map.get(str(key).upper())
            if hit:
                part = self.disc["parts"][hit]
                model_name, model_pins, card = hit, part.get("pins"), part.get("card")
                break
        if not model_name:
            is_pnp = ("PNP" in (title + mpn).upper() or "9012" in mpn
                      or "9015" in mpn or "8550" in mpn)
            g = self.gen["generic"]["PNP" if is_pnp else "NPN"]
            model_name, card, model_pins = g["modelName"], g["card"], None
            self.report["generic"].append({"designator": des, "model": model_name,
                                           "why": f"未在库中匹配 {mpn or title}"})
        role_pins = self._resolve_bjt_pins(comp, model_pins)
        if not role_pins:
            return
        nc = self.net(comp, role_pins["C"])
        nb = self.net(comp, role_pins["B"])
        ne = self.net(comp, role_pins["E"])
        if None in (nc, nb, ne):
            self.warn(f"{des}: 存在悬空引脚，已跳过该器件")
            return
        self.lines.append(f"{self.namer.take(des)} {nc} {nb} {ne} {model_name}")
        self.model_cards[model_name] = card
        self.report["included"].append({"designator": des, "as": "Q", "model": model_name,
                                        "level": "Level 0"})
        self._fidelity.append((des, 0, "BJT"))

    def _resolve_bjt_pins(self, comp, model_pins):
        des = comp.get("designator") or ""
        pins = comp.get("pins") or []
        if len(pins) < 3:
            return None

        def by_name(roles):
            for p in pins:
                nm = (p.get("name") or "").upper()
                for r in roles:
                    if r in nm:
                        return p
            return None

        got = {"C": by_name(["C", "COLLECTOR", "集电极"]),
               "B": by_name(["B", "BASE", "基极"]),
               "E": by_name(["E", "EMITTER", "发射极"])}
        if all(got.values()):
            return {r: p.get("number") for r, p in got.items()}
        if model_pins and all(k in model_pins for k in ("C", "B", "E")):
            return dict(model_pins)
        nums = [str(p.get("number")) for p in pins if p.get("number") is not None]
        if sorted(nums) == ["1", "2", "3"]:
            self.warn(f"{des}: 无引脚名/模型映射，按惯序 1=B 2=E 3=C 处理（请核对）")
            return {"B": "1", "E": "2", "C": "3"}
        self.warn(f"{des}: 无法确定 C/B/E 引脚，已跳过")
        return None

    # ---- 二极管 ----
    def _emit_diode(self, comp):
        des = comp.get("designator") or ""
        nets = self._two_pin_nets(comp)
        if not nets:
            self.warn(f"{des}: 二极管引脚数异常，跳过")
            return
        na, nb = nets[0], nets[1]
        if na is None or nb is None:
            self.warn(f"{des}: 存在悬空引脚，已跳过该器件")
            return
        title = comp.get("deviceTitle") or ""
        mpn = comp.get("manufacturerPart") or ""
        hit = self.alias_map.get((mpn or "").upper()) or self.alias_map.get((title or "").upper())
        if hit:
            part = self.disc["parts"][hit]
            model_name, card = hit, part["card"]
        else:
            g = self.gen["generic"]["LED" if "LED" in title.upper() else "D"]
            model_name, card = g["modelName"], g["card"]
            self.report["generic"].append({"designator": des, "model": model_name,
                                           "why": f"未在库中匹配 {mpn or title}"})
        self.lines.append(f"{self.namer.take(des)} {na} {nb} {model_name}")
        self.model_cards[model_name] = card
        self.report["included"].append({"designator": des, "as": "D", "model": model_name,
                                        "level": "Level 0"})
        self._fidelity.append((des, 0, "diode"))

    # ---- MOSFET ----
    def _emit_mosfet(self, comp):
        des = comp.get("designator") or ""
        title = comp.get("deviceTitle") or ""
        mpn = comp.get("manufacturerPart") or ""
        pins = comp.get("pins") or []
        role = {}
        for p in pins:
            nm = (p.get("name") or "").upper()
            for r in ("G", "D", "S"):
                if r in nm and r not in role:
                    role[r] = p.get("number")
        if len(role) < 3 and len(pins) == 3 and all(
                str(p.get("number")) in ("1", "2", "3") for p in pins):
            role = {"G": "1", "S": "2", "D": "3"}
            self.warn(f"{des}: MOSFET 按惯序 1=G 2=S 3=D 处理（请核对）")
        if len(role) < 3:
            self._exclude(comp, "MOSFET 引脚无法确定")
            return
        ng, nd, ns = self.net(comp, role["G"]), self.net(comp, role["D"]), self.net(comp, role["S"])
        if None in (ng, nd, ns):
            self.warn(f"{des}: 存在悬空引脚，已跳过该器件")
            return
        is_pmos = "PMOS" in (title + mpn).upper() or "P沟道" in title
        g = self.gen["generic"]["PMOS" if is_pmos else "NMOS"]
        self.lines.append(f"{self.namer.take(des)} {nd} {ng} {ns} 0 {g['modelName']}")
        self.model_cards[g["modelName"]] = g["card"]
        self.report["generic"].append({"designator": des, "model": g["modelName"],
                                       "why": "MOSFET 泛化模型"})
        self.report["included"].append({"designator": des, "as": "M", "model": g["modelName"],
                                        "level": "Level 0"})
        self._fidelity.append((des, 0, "MOSFET"))

    # ---- IC / 模块 ----
    def _emit_ic(self, comp):
        des = comp.get("designator") or ""
        title = comp.get("deviceTitle") or ""
        mpn = comp.get("manufacturerPart") or ""
        try:
            res = self.resolver.resolve(comp, self.net)
        except MappingError as e:
            self._err(e.code, e.message, designator=des)
            self._exclude(comp, f"模型映射失败（{e.code}）")
            return
        if res:
            nodes = [n or "0" for n in res["nodes"]]
            if res["type"] == "SUBCKT":
                # 子电路实例必须以 X 开头；直接叫 U1 会被 ngspice 当成 URC 传输线
                inst = self.namer.take("X" + spice_ident(des))
                self.lines.append(f"{inst} " + " ".join(nodes) + f" {res['subckt']}")
                self._register_power_outputs(res, nodes)
            elif res["type"] in ("MODEL", "CARD"):
                letter = (res.get("device_letter") or "").strip()
                if not letter:
                    self._err("MODEL_MAPPING_ERROR",
                              f"{des}: model 类型器件必须在 model_map 里给出 device_letter"
                              f"（如 Q/M/D/J），否则没法把 .MODEL 实例化成正确器件类型",
                              designator=des)
                    self._exclude(comp, "model 类型缺少 device_letter")
                    return
                inst = self.namer.take(letter[0].upper() + spice_ident(des))
                self.lines.append(f"{inst} " + " ".join(nodes) + f" {res['model']}")
                if res.get("card"):
                    self.model_cards[res["model"]] = res["card"]
            res["instance"] = inst
            self.report["models"].append(res)
            self.report["included"].append({
                "designator": des, "as": res["type"], "model": res.get("subckt") or res.get("model"),
                "level": "Level 2", "pin_mapping": res["status"]})
            self._fidelity.append((des, 2, res.get("subckt") or res.get("model") or "vendor model"))
            return

        # 没有厂商模型 → behavioral placeholder
        try:
            lines, info = self.behavioral.build(self.plan._ctx, comp, self.net)
        except Exception as e:  # BehavioralError 等
            self._err(getattr(e, "code", "BEHAVIORAL_ERROR"), str(e), designator=des)
            lines, info = [], None
        if info:
            self.lines.extend(lines)
            self.report["behavioral"].append(info)
            self.report["included"].append({
                "designator": des, "as": "BEHAVIORAL", "model": info.get("key"),
                "level": "Level 1"})
            self._fidelity.append((des, 1, "behavioral placeholder"))
            return

        reason = f"无仿真模型（{mpn or title or '未知型号'}），已排除"
        connected = sum(1 for p in comp.get("pins") or [] if self.net(comp, p.get("number")))
        if self.strict and connected >= 2:
            self._err("UNMAPPED_IC",
                      f"{des}（{mpn or title}）位于有效电流路径（{connected} 个已连接引脚）"
                      f"但没有可用模型：请在 model_map 里配置外部 .SUBCKT，"
                      f"或在 behavioral.json 里声明行为级占位",
                      designator=des)
        self._exclude(comp, reason)

    def _register_power_outputs(self, res, nodes):
        roles = res.get("roles") or {}
        order = res.get("subckt_order") or []
        for idx, sp in enumerate(order):
            if roles.get(sp) == "power_output" and idx < len(nodes):
                node = nodes[idx]
                self.power_output_nodes.setdefault(node, []).append(res["designator"])
                self.sourced_nodes.setdefault(node, []).append(
                    f"{res['designator']}(power_output)")

    def _exclude(self, comp, reason):
        des = comp.get("designator") or ""
        if self.keep_open:
            self.lines.append(f"* [skipped] {spice_ident(des)} {comp.get('deviceTitle') or ''}")
        self.report["excluded"].append({
            "designator": des, "title": comp.get("deviceTitle"),
            "mpn": comp.get("manufacturerPart"), "reason": reason, "level": "EXCLUDED"})

    # ---- override 元素 ----
    def _emit_override_elements(self):
        if self.plan.elements:
            self.lines.append("* ---- PCB parasitic / override elements ----")
            self.lines.extend(self.plan.elements)
        self.report["overrides"] = [o for o in self.plan.overrides_info]
        self.report["net_segments"] = self.plan.segments_info
        self.report["virtual_nodes"] = self.plan.virtual_nodes

    # ---- 电源 ----
    def _auto_power(self):
        explicit_supplies = self.cfg.get("external_supplies") or []
        for sup in explicit_supplies:
            net = sup.get("net") or sup.get("node")
            v = sup.get("voltage", sup.get("v"))
            if net is None or v is None:
                self._err("POWER_DECL_INVALID", f"external_supplies 条目缺 net/voltage: {sup}")
                continue
            node = self.plan.node_for_net(net)
            nm = self.namer("V", f"EXT_{spice_ident(str(net))}")
            self.lines.append(f"* external supply declared in config: {net} = {v} V")
            self.lines.append(f"{nm} {node} 0 DC {v} AC 0 0")
            self.sourced_nodes.setdefault(node, []).append(f"{net}(config)")
            self.report["powers"].append({"net": node, "voltage": v,
                                          "source": "config", "element": nm})

        # 重复理想源冲突检测
        for node, srcs in self.sourced_nodes.items():
            ideal = [s for s in srcs if "(schematic)" in s or "(config)" in s]
            if len(ideal) > 1:
                msg = f"节点 {node} 上有多个独立电压源: {ideal}"
                if self.strict:
                    self._err("DUPLICATE_VOLTAGE_SOURCE", msg)
                else:
                    self.warn(msg)
            if node in self.power_output_nodes and ideal:
                msg = (f"节点 {node} 由 {self.power_output_nodes[node]} 的 power_output 引脚驱动，"
                       f"同时又被理想电压源 {ideal} 驱动")
                if self.strict:
                    self._err("POWER_OUTPUT_CONFLICT", msg)
                else:
                    self.warn(msg)

        if self.power_policy == "explicit":
            for net in self.ir.get("nets", []):
                if net.get("origin") != "netflag":
                    continue
                nm = net.get("name")
                v = parse_voltage(nm)
                if v is None:
                    continue
                node = self.plan.node_for_net(nm)
                if node in self.sourced_nodes or node == "0":
                    continue
                msg = (f"{nm} 是电源网标但没有任何真实供电（--power-policy=explicit）："
                       f"请在原理图里加真实电压源，或在 config 的 external_supplies 中声明")
                if self.strict:
                    self._err("ISOLATED_RAIL", msg, net=nm)
                else:
                    self.warn(msg)
            return

        # ---- auto（v2.1 兼容行为）----
        added = 0
        for net in self.ir.get("nets", []):
            if net.get("origin") != "netflag":
                continue
            nm = net.get("name")
            v = parse_voltage(nm)
            if v is None:
                continue
            node = self.plan.node_for_net(nm)
            if node == "0":
                continue
            if node in self.power_output_nodes:
                self.warn(f"{nm} 已由 {self.power_output_nodes[node]} 的 power_output 驱动，"
                          f"跳过自动补源（这一点在 v2.1 会被错误地再并一个理想源）")
                continue
            if node in self.sourced_nodes:
                continue
            # 保持 v2.1 的命名习惯：+12V → VPWR_12V，-15V → VPWR_NEG_15V
            base = re.sub(r"[^0-9A-Za-z_]", "_", str(nm).lstrip("+-")) or "RAIL"
            dev = self.namer.take(("VPWR_NEG_" if str(nm).startswith("-") else "VPWR_") + base)
            self.lines.append(f"{dev} {node} 0 {v:g} DC AC 0 0")
            self.report["powers"].append({"net": nm, "node": node, "voltage": v,
                                          "source": "auto", "element": dev})
            self.sourced_nodes.setdefault(node, []).append(f"{nm}(auto)")
            added += 1
        if added:
            self.warn(f"[POWER-AUTO] {added} 个电源网标按网名自动补了理想电压源。"
                      f"这对 buck/LDO/PDN 分析会掩盖真实电源动态，复杂板卡请改用 "
                      f"--power-policy explicit（或 config 里 power_policy: explicit）")

    # ---- 悬空网络 ----
    def _dangling_nets(self):
        for net in self.ir.get("nets", []):
            if net.get("pinCount") == 1 and net.get("origin") == "auto":
                m = net["members"][0]
                self.report["dangling_nets"].append(
                    f"{net['name']} 仅连接 {m['component']}.{m['pin']}")
        header_like = [
            e for e in self.report["excluded"]
            if str(e.get("designator", ""))[:2].upper().startswith(("H", "SW", "JP"))
            or "PZ2.54" in str(e.get("mpn") or "")
            or "HEADER" in str(e.get("mpn") or "").upper()]
        if header_like:
            self.warn(f"检测到 {len(header_like)} 个跳线/排针"
                      f"（{', '.join(e['designator'] for e in header_like)}）位于信号路径中，"
                      f"网表按『未插跳线』状态生成；需要闭合请用 override 的 short/insert")

    # ---- 报告收尾 ----
    def _finalize_report(self):
        net_names = [n.get("name") for n in self.ir.get("nets", [])]
        glines, gmap = self.ground.report(net_names)
        self.report["ground_mapping"] = gmap
        self.report["ground_lines"] = glines
        for n in self.ground.warn_unmerged(net_names):
            self.warn(f"ground-like 网络 {n} 未合并到节点 0（v2.2 默认不静默合并）；"
                      f"如需合并请在 config 的 ground_aliases 里显式声明")
        if self.ground.errors:
            self.report["errors"].extend(self.ground.errors)
        self.report["ground_link_found"] = self._ground_linked(gmap)
        self.report["includes"] = self.resolver.include_lines()
        self.report["errors"].extend(self.resolver.errors)
        self.report["warnings"].extend(self.resolver.warnings)
        self.report["errors"].extend(self.behavioral.errors)
        self.report["fidelity"] = self._fidelity_table()
        self.report["counts"] = {
            "included": len(self.report["included"]),
            "excluded": len(self.report["excluded"]),
            "models": len(self.report["models"]),
            "behavioral": len(self.report["behavioral"]),
            "overrides": len(self.report["overrides"]),
            "power_sources": len(self.report["powers"]),
        }

    def _ground_linked(self, gmap):
        """ground-like 之间是否已有真实连接（override 的 0Ω / ferrite / short）。"""
        if len(gmap) <= 1:
            return True
        grounds = {v for v in gmap.values() if v != "0"}
        if not grounds:
            return True
        for o in self.plan.overrides_info:
            a, b = o.get("from"), o.get("to")
            if not a or not b:
                continue
            if (a in grounds and b == "0") or (b in grounds and a == "0"):
                return True
        for a, b in (self.plan.merged or {}).items():
            if (a in grounds and b == "0") or (b in grounds and a == "0"):
                return True
        return False

    def _fidelity_table(self):
        labels = self.cfg.get("fidelity_labels") or {}
        rows = {}
        for des, level, what in self._fidelity:
            key = labels.get(des) or f"{des} ({what})"
            rows[key] = {"level": level, "what": what, "designator": des}
        routed = any(o.get("type") not in (None, "current_source", "voltage_source")
                     for o in self.plan.overrides_info)
        if self.plan.segments_info or routed:
            rows[labels.get("_routing", "routing / PCB parasitics")] = {
                "level": 3, "what": "transmission line / RLC overrides", "designator": None}
        overall = max([r["level"] for r in rows.values()], default=0)
        return {"blocks": rows, "overall_level": overall}

    # ---- 组装 ----
    def _assemble(self):
        project = self.ir.get("project") or "-"
        page = (self.ir.get("page") or {}).get("name") or "-"
        head = [
            f"* NGspice netlist generated by easyeda-ngspice-sim v{SKILL_VERSION}",
            f"* project: {project}  page: {page}  source: {self.ir.get('source')}",
            f"* power-policy: {self.power_policy}  strict: {int(self.strict)}  "
            f"fidelity: Level {self.report['fidelity'].get('overall_level', 0)}",
        ]
        for ln in self.report.get("ground_lines") or []:
            head.append("* " + ln)
        gm = self.report.get("ground_mapping") or {}
        if len(gm) > 1 and not self.report.get("ground_link_found", True):
            head.append("* 注意：多个 ground-like 网络保持独立节点，"
                        "它们之间没有任何连接时 PDN 结果不可信")
        if self.report["excluded"]:
            head.append(f"* WARNING: {len(self.report['excluded'])} 个器件未进入仿真: "
                        + ", ".join(e["designator"] for e in self.report["excluded"]))
        if self.report["generic"]:
            head.append("* WARNING: 使用泛化模型: " + ", ".join(
                f"{g['designator']}→{g['model']}" for g in self.report["generic"]))
        if self.report["behavioral"]:
            head.append("* WARNING: BEHAVIORAL_APPROXIMATION（近似，不是厂商模型）: "
                        + ", ".join(b["designator"] for b in self.report["behavioral"]))
        for seg in self.report["virtual_nodes"]:
            head.append(f"* virtual node: {seg.replace('__', '$', 1)}  (deck 写法 {seg})")

        parts = ["\n".join(head)]
        if self.params:
            parts.append("* ---- .param (override config) ----")
            parts.extend(f".param {k}={v}" for k, v in self.params.items())
        if self.report["includes"]:
            parts.append("* ---- model includes ----")
            parts.extend(self.report["includes"])
        parts.append("\n".join(self.lines))
        if self.model_cards:
            parts.append("* ---- models ----")
            parts.extend([c for c in self.model_cards.values() if c])
        return "\n".join(p for p in parts if p) + "\n.end\n"


def generate(ir, pin_net, opts=None):
    return NetlistBuilder(ir, pin_net, opts).build()


# ---------------------------------------------------------------- CLI
def _pop_flag(args, name):
    if name in args:
        i = args.index(name)
        val = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2 if val is not None else i + 1]
        return val
    return None


def main():
    argv = sys.argv[1:]
    if len(argv) < 2:
        print(__doc__)
        sys.exit(2)
    ir_path, out_path = argv[0], argv[1]
    rest = argv[2:]
    flags = {
        "config": _pop_flag(rest, "--config"),
        "models": _pop_flag(rest, "--models"),
        "behavioral": _pop_flag(rest, "--behavioral"),
        "override": _pop_flag(rest, "--override"),
        "power_policy": _pop_flag(rest, "--power-policy"),
        "report": _pop_flag(rest, "--report"),
    }
    keep_open = "--keep-open" in rest
    strict = "--strict" in rest
    allow_partial = "--allow-partial-model" in rest

    ir = load_json(ir_path)
    pin_net = {}
    for n in ir.get("nets", []):
        for m in n.get("members", []):
            pin_net.setdefault(m["component"], {})[str(m["pin"])] = n["name"]

    cfg = load_config(flags["config"]) if flags["config"] else load_config(None)
    opts = {
        "config": cfg,
        "keep_open": keep_open,
        "strict": strict,
        "allow_partial_model": allow_partial,
        "models": flags["models"] or cfg.get("models") or os.path.join(MODELS_DIR, "model_map.json"),
        "behavioral": flags["behavioral"] if flags["behavioral"] else cfg.get("behavioral_models"),
        "override": flags["override"] or cfg.get("override"),
        "power_policy": flags["power_policy"] or cfg.get("power_policy"),
        "deck_dir": os.path.dirname(os.path.abspath(out_path)),
    }
    if opts["behavioral"] is None:
        opts["behavioral"] = os.path.join(MODELS_DIR, "behavioral.json")

    text, report = generate(ir, pin_net, opts)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if flags["report"]:
        with open(flags["report"], "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)

    print(f"netlist written: {out_path}")
    c = report["counts"]
    print(f"  included: {c['included']}, excluded: {c['excluded']}, "
          f"models: {c['models']}, behavioral: {c['behavioral']}, "
          f"overrides: {c['overrides']}, power: {c['power_sources']}")
    for ln in report.get("ground_lines") or []:
        print("  " + ln)
    for m in report["models"]:
        for ln in provenance_text(m):
            print("  [model   ] " + ln)
    btxt = summary_text(report["behavioral"])
    for ln in btxt:
        print("  [bhv     ] " + ln)
    for seg in report.get("net_segments") or []:
        print(f"  [segment ] {seg['endpoint']}: {seg['from']} -> {seg['to']} [{seg['kind']}]")
    for o in report.get("overrides") or []:
        if o.get("elements"):
            print(f"  [override] {o.get('name') or o.get('component')} "
                  f"({o.get('type') or o.get('op')}) -> {', '.join(o['elements'])}")
    for e in report["excluded"]:
        print(f"  [excluded] {e['designator']}: {e['reason']}")
    for g in report["generic"]:
        print(f"  [generic ] {g['designator']} -> {g['model']} ({g['why']})")
    for p in report["powers"]:
        print(f"  [power   ] {p.get('node') or p['net']} = {p['voltage']} ({p['source']})")
    for w in report["warnings"]:
        print(f"  [warn    ] {w}")
    for e in report["errors"]:
        print(f"  [ERROR   ] {e['code']}: {e['message']}")
    if report["dangling_nets"]:
        print("  [dangling] " + "; ".join(report["dangling_nets"]))
    if report["errors"]:
        print(f"\n{len(report['errors'])} errors"
              + ("（strict 模式：拒绝产出拓扑可疑的波形）" if strict else ""))
        if strict:
            sys.exit(3)


if __name__ == "__main__":
    main()
