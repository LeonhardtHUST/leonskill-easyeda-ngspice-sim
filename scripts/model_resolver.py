#!/usr/bin/env python3
"""v2.2 P0-B：外部 SPICE 模型映射（.MODEL / .SUBCKT / .LIB）。

v2.1 对 `U` / `IC` 一律"无仿真模型，已排除"，导致 buck/LDO/ADC/clock buffer
被整体挖掉、FPGA 负载消失 —— 拓扑保留但电路已经和真实系统无关。

v2.2 增加 models/model_map.json 映射层，匹配优先级（严格按此顺序）：

    designator override  >  精确 MPN  >  精确 DeviceName  >  alias  >  generic

IC 引脚映射必须是**可验证**的，禁止"按 1,2,3,4 塞进 .SUBCKT"或按几何猜。
任何 pin 缺失 / 重复 / subckt pin 数不符 / mapping 不完整，直接产出
`MODEL_MAPPING_ERROR`；只有显式 `--allow-partial-model` 才降级为警告。
"""
import fnmatch
import os
import re

from sim_config import load_json, quote_path, spice_ident, spice_node

ROLE_VOCAB = ("power_input", "power_output", "signal_input", "signal_output",
              "digital_input", "digital_output", "analog_input", "analog_output",
              "open_drain_output", "ground", "passive")


class MappingError(Exception):
    def __init__(self, code, message, designator=None, detail=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.designator = designator
        self.detail = detail or {}


def _up(x):
    return str(x or "").strip().upper()


def parse_subckt_pins(text, subckt):
    """从 lib 文本里解析 `.SUBCKT <name> pin...` 的引脚顺序。"""
    want = _up(subckt)
    for m in re.finditer(r"(?im)^[ \t]*\.subckt[ \t]+(\S+)([^\n]*)", text):
        if _up(m.group(1)) != want:
            continue
        tail = m.group(2)
        tail = tail.split("$")[0].split(";")[0]
        # 去掉可能出现的参数段（params: / PARAMS:）
        tail = re.split(r"(?i)\bparams?\b\s*[:=]?", tail)[0]
        pins = [p for p in tail.strip().split() if p]
        return pins
    return None


def parse_model_names(text):
    return {_up(m.group(1)) for m in re.finditer(r"(?im)^[ \t]*\.model[ \t]+(\S+)", text)}


class ModelResolver:
    def __init__(self, path=None, allow_partial=False, strict=False):
        self.path = os.path.abspath(path) if path else None
        self.base_dir = os.path.dirname(self.path) if self.path else os.getcwd()
        self.allow_partial = allow_partial
        self.strict = strict
        self.entries = {}
        self.generics = []
        self._by_des = {}
        self._by_mpn = {}
        self._by_device = {}
        self._by_alias = {}
        self._file_cache = {}
        self.errors = []
        self.warnings = []
        self._includes = []
        if path and os.path.exists(path):
            self._load(path)
        elif path:
            self.errors.append({"code": "MODEL_MAP_MISSING",
                                "message": f"model_map 文件不存在: {path}"})

    # ------------------------------------------------------------------ 加载
    def _load(self, path):
        cfg = load_json(path)
        parts = cfg.get("parts") or {}
        for key, ent in parts.items():
            ent = dict(ent or {})
            ent["key"] = key
            self.entries[key] = ent
            match = ent.get("match") or {}
            for des in match.get("designator") or []:
                self._by_des[_up(des)] = key
            keys = list(match.get("mpn") or []) + [key]
            for mpn in keys:
                self._by_mpn.setdefault(_up(mpn), key)
            for dev in match.get("device") or []:
                self._by_device.setdefault(_up(dev), key)
            for al in match.get("aliases") or []:
                self._by_alias.setdefault(_up(al), key)
        self.generics = cfg.get("generics") or []

    # ------------------------------------------------------------- 文件读取
    def _read(self, path):
        p = os.path.abspath(path)
        if p not in self._file_cache:
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    self._file_cache[p] = f.read()
            except OSError:
                self._file_cache[p] = None
        return self._file_cache[p]

    def _abs_file(self, rel):
        if not rel:
            return None
        if os.path.isabs(rel):
            return os.path.normpath(rel)
        return os.path.normpath(os.path.join(self.base_dir, rel))

    # ------------------------------------------------------------------ 匹配
    def match(self, comp):
        """返回 (entry, matched_by) 或 (None, None)。"""
        des = _up(comp.get("designator"))
        if des and des in self._by_des:
            return self.entries[self._by_des[des]], "designator"
        mpn = _up(comp.get("manufacturerPart"))
        if mpn and mpn in self._by_mpn:
            return self.entries[self._by_mpn[mpn]], "mpn"
        dev = _up(comp.get("deviceTitle"))
        if dev and dev in self._by_device:
            return self.entries[self._by_device[dev]], "device"
        for cand in (mpn, dev, _up(comp.get("name"))):
            if cand and cand in self._by_alias:
                return self.entries[self._by_alias[cand]], "alias"
        gen = self._match_generic(comp)
        if gen:
            return gen, "generic"
        return None, None

    def _match_generic(self, comp):
        prefix = _up(comp.get("pinPrefix"))
        title = _up(comp.get("deviceTitle"))
        mpn = _up(comp.get("manufacturerPart"))
        for g in self.generics:
            m = g.get("match") or {}
            if m.get("pin_prefix") and prefix not in [_up(x) for x in m["pin_prefix"]]:
                continue
            pats = m.get("title_contains") or []
            if pats and not any(p.upper() in (title + " " + mpn) for p in pats):
                continue
            if not m:
                continue
            return g
        return None

    # ------------------------------------------------------------------ 解析
    def resolve(self, comp, node_lookup):
        """把元件解析成一条可落地的实例描述；失败抛 MappingError。

        node_lookup(comp, pin_number) -> 最终 SPICE 节点名 or None
        """
        des = comp.get("designator") or "?"
        entry, matched_by = self.match(comp)
        if not entry:
            return None
        mtype = str(entry.get("type") or "subckt").lower()
        res = {
            "designator": des,
            "key": entry.get("key"),
            "matched_by": matched_by,
            "type": mtype.upper(),
            "roles": {},
            "pin_mapping": {},
            "nodes": [],
            "status": "VERIFIED",
            "fallback": "no",
            "file": None,
            "file_display": None,
            "subckt": entry.get("subckt") or entry.get("key"),
            "derivation": [],
        }
        if entry.get("mpn"):
            res["mpn"] = entry.get("mpn")
        if mtype in ("subckt", "x"):
            self._resolve_subckt(comp, entry, res, node_lookup)
        elif mtype in ("model", "card"):
            self._resolve_card(comp, entry, res, node_lookup)
        elif mtype == "include_only":
            self._register_include(entry, res)
        else:
            raise MappingError("MODEL_MAPPING_ERROR",
                               f"{des}: 未知 model type {mtype!r}", des)
        return res

    # ---- .SUBCKT ----
    def _resolve_subckt(self, comp, entry, res, node_lookup):
        des = res["designator"]
        subckt = entry.get("subckt") or entry.get("key")
        rel = entry.get("file")
        if not rel:
            raise MappingError("MODEL_MAPPING_ERROR", f"{des}: subckt 类型缺少 file 字段", des)
        path = self._abs_file(rel)
        text = self._read(path)
        if text is None:
            self.errors.append({"code": "MODEL_FILE_MISSING",
                                "message": f"{des}: 模型文件不存在 {rel} ({path})",
                                "designator": des})
            raise MappingError("MODEL_FILE_MISSING", f"{des}: 模型文件不存在 {rel}", des)
        res["file"] = path
        res["file_display"] = rel.replace("\\", "/")

        file_pins = parse_subckt_pins(text, subckt)
        if file_pins is None:
            raise MappingError(
                "MODEL_MAPPING_ERROR",
                f"{des}: 文件 {rel} 中找不到 .SUBCKT {subckt}", des)
        order = entry.get("subckt_order")
        if order:
            if [_up(p) for p in order] != [_up(p) for p in file_pins]:
                if sorted(_up(p) for p in order) != sorted(_up(p) for p in file_pins):
                    raise MappingError(
                        "MODEL_MAPPING_ERROR",
                        f"{des}: subckt_order 与文件里的 .SUBCKT {subckt} 引脚集合不一致"
                        f"（配置 {order} / 文件 {file_pins}）", des)
                self.warnings.append(
                    f"{des}: subckt_order 与文件引脚顺序不同，按文件顺序 "
                    f"{file_pins} 生成 X 行（位置映射以文件为准）")
            order = file_pins
        else:
            order = file_pins
        res["subckt_order"] = order

        pins = entry.get("pins")
        auto_by_name = False
        if not pins:
            pins, auto_by_name = self._autopin_by_name(comp, order)
            if pins is None:
                raise MappingError(
                    "MODEL_MAPPING_ERROR",
                    f"{des}: 未提供 pins 映射，且原理图引脚名与 .SUBCKT {subckt} 引脚名"
                    f"无法一一对应（原理图脚名 {[p.get('name') for p in comp.get('pins') or []]}）",
                    des)
        pins = {str(k): str(v) for k, v in pins.items()}

        # ---- 校验 ----
        comp_pins = {str(p.get("number")): p for p in comp.get("pins") or []}
        covered = set(pins.values())
        unknown = [p for p in covered if p not in order]
        if unknown:
            raise MappingError(
                "MODEL_MAPPING_ERROR",
                f"{des}: pins 映射到 .SUBCKT {subckt} 不存在的引脚 {unknown}"
                f"（subckt 引脚 {order}）", des)
        # 同一 subckt 引脚被多个原理图引脚映射 → 允许，但必须落在同一个网络
        by_sub = {}
        for num, sp in pins.items():
            by_sub.setdefault(sp, []).append(num)
        for sp, nums in by_sub.items():
            if len(nums) > 1:
                nodes = {node_lookup(comp, n) for n in nums}
                nodes.discard(None)
                if len(nodes) > 1:
                    raise MappingError(
                        "MODEL_MAPPING_ERROR",
                        f"{des}: .SUBCKT {subckt} 引脚 {sp} 被多个原理图引脚 "
                        f"{nums} 映射，但它们不在同一网络（{sorted(nodes)}）", des)

        connected = {n for n, p in comp_pins.items()
                     if node_lookup(comp, n) is not None}
        missing = sorted(connected - set(pins), key=_numkey)
        if missing:
            self._incomplete(des, f"原理图引脚 {missing} 没有 pin mapping"
                                  f"（.SUBCKT {subckt} 引脚 {order}）")

        nc_pins = {_up(x) for x in (entry.get("nc_pins") or [])}
        unmapped_sub = [p for p in order if p not in covered]
        unmapped_required = [p for p in unmapped_sub if _up(p) not in nc_pins]
        if unmapped_required:
            self._incomplete(
                des,
                f".SUBCKT {subckt} 引脚 {unmapped_required} 没有对应原理图引脚"
                f"（如确为开路请加入 nc_pins）")

        # ---- 组装节点序列 ----
        dummy_count = 0
        for sp in order:
            nums = by_sub.get(sp) or []
            nodes = [node_lookup(comp, n) for n in nums]
            nodes = [n for n in nodes if n]
            if nodes:
                res["nodes"].append(nodes[0])
            else:
                dummy = f"__NC_{spice_ident(des)}_{dummy_count}"
                dummy_count += 1
                res["nodes"].append(dummy)
                res.setdefault("nc_nodes", []).append({"pin": sp, "node": dummy})
        res["pin_mapping"] = {n: s for n, s in sorted(pins.items(), key=lambda kv: _numkey(kv[0]))}
        res["pin_mapping_auto"] = auto_by_name
        res["status"] = "PARTIAL" if (missing or unmapped_required) else "VERIFIED"
        if res["status"] == "PARTIAL":
            res["fallback"] = "yes"
        res["roles"] = self._build_roles(pins, entry, order)
        self._register_include(entry, res)

    def _autopin_by_name(self, comp, order):
        names = {}
        for p in comp.get("pins") or []:
            nm = _up(p.get("name"))
            if nm:
                names.setdefault(nm, []).append(str(p.get("number")))
        mapping = {}
        for sp in order:
            hit = names.get(_up(sp))
            if not hit:
                return None, False
            for n in hit:
                mapping[n] = sp
        if not mapping:
            return None, False
        return mapping, True

    def _build_roles(self, pins, entry, order):
        roles_cfg = {_up(k): v for k, v in (entry.get("roles") or {}).items()}
        roles = {}
        # 先按 subckt 引脚名给出角色（与具体器件引脚号解耦）
        for sp in order:
            r = roles_cfg.get(_up(sp))
            if r:
                if r not in ROLE_VOCAB:
                    self.warnings.append(f"未知 pin role {r!r}（pin {sp}），按 signal_input 处理")
                roles[sp] = r
        # 再补按原理图引脚号声明的角色
        for num, sp in pins.items():
            r = roles_cfg.get(_up(num))
            if r and sp not in roles:
                roles[sp] = r
        return roles

    # ---- .MODEL 卡片 ----
    def _resolve_card(self, comp, entry, res, node_lookup):
        des = res["designator"]
        model = entry.get("model") or entry.get("subckt") or entry.get("key")
        card = entry.get("card")
        rel = entry.get("file")
        if not card and rel:
            path = self._abs_file(rel)
            text = self._read(path)
            if text is None:
                raise MappingError("MODEL_FILE_MISSING", f"{des}: 模型文件不存在 {rel}", des)
            res["file"], res["file_display"] = path, rel.replace("\\", "/")
            if _up(model) not in parse_model_names(text):
                raise MappingError("MODEL_MAPPING_ERROR",
                                   f"{des}: 文件 {rel} 中找不到 .MODEL {model}", des)
            self._register_include(entry, res)
        elif card:
            res["card"] = card
        else:
            raise MappingError("MODEL_MAPPING_ERROR",
                               f"{des}: model 类型需要 card 或 file", des)
        res["model"] = model
        res["device_letter"] = entry.get("device_letter")
        pins = entry.get("pins") or {}
        nodes = []
        order = entry.get("node_order") or []
        if order:
            for role in order:
                nums = [n for n, r in pins.items() if _up(r) == _up(role)]
                if not nums:
                    raise MappingError("MODEL_MAPPING_ERROR",
                                       f"{des}: node_order 里的 {role} 没有 pin mapping", des)
                nodes.append(node_lookup(comp, nums[0]))
        else:
            three = sorted(pins.items(), key=lambda kv: _numkey(kv[0]))
            nodes = [node_lookup(comp, n) for n, _ in three]
        if not nodes or any(n is None for n in nodes):
            self._incomplete(des, "model 类型器件存在悬空引脚，无法实例化")
            res["status"] = "PARTIAL"
        res["nodes"] = [n or f"__NC_{spice_ident(des)}" for n in nodes]
        res["pin_mapping"] = {str(k): str(v) for k, v in pins.items()}

    def _incomplete(self, des, msg):
        if self.allow_partial:
            self.warnings.append(f"{des}: {msg}（--allow-partial-model 已降级为警告）")
            return
        raise MappingError("MODEL_MAPPING_ERROR", f"{des}: {msg}", des)

    # ---- include 去重 ----
    def _register_include(self, entry, res):
        rel = entry.get("file")
        if not rel:
            return
        path = self._abs_file(rel)
        section = entry.get("lib_section")
        key = (os.path.normcase(os.path.abspath(path)), _up(section))
        if key in [k for k, _ in self._includes]:
            res["include"] = self._includes[[k for k, _ in self._includes].index(key)][1]
            return
        if section:
            line = f".lib {quote_path(path, self.deck_dir)} {section}"
        else:
            line = f".include {quote_path(path, self.deck_dir)}"
        self._includes.append((key, line))
        res["include"] = line

    deck_dir = None  # 由调用方设置（deck 输出目录），决定 include 用相对还是绝对路径

    def include_lines(self):
        return [ln for _, ln in self._includes]


def _numkey(x):
    s = str(x)
    return (0, int(s)) if s.isdigit() else (1, s)


def provenance_text(res):
    """报告里的模型 provenance 段（需求文档第四节给的样子）。"""
    if not res:
        return []
    out = [f"{res['designator']} {res.get('mpn') or res.get('key')}"]
    if res.get("file_display"):
        out.append(f"model source: {res['file_display']}")
    elif res.get("card"):
        out.append("model source: inline card")
    out.append(f"model type: {res['type']}")
    if res.get("subckt"):
        out.append(f"subckt: {res['subckt']}")
    if res.get("pin_mapping"):
        how = " (by pin name)" if res.get("pin_mapping_auto") else ""
        out.append(f"pin mapping: {res['status']}{how}")
    out.append(f"fallback: {res['fallback']}")
    return out
