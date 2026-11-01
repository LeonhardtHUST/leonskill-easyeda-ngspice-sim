#!/usr/bin/env python3
"""v2.2 共用配置与命名工具。

职责：
  1. 统一加载 sim_config.json（复杂仿真的主入口）并把 CLI 覆盖项合并进来；
  2. 相对路径解析规则（config 文件目录 → skill 目录兜底）；
  3. SPICE 标识符 / 节点名清洗（含虚拟节点 `$NAME` → `__NAME` 映射）；
  4. 唯一元件名分配器 Namer。

节点命名约定（实测结论，勿改）：
  ngspice-47 把 `$` 当作行内注释起始符，`V1 $SRC 0 1` 会报
  "Not enough parameters for v source"。因此用户侧书写 `$CLK_ADC`（可读、可写在
  json 里），落到 deck 时统一转成 `__CLK_ADC`。报告与 manifest 保留用户写法。
"""
import json
import os
import re

SKILL_VERSION = "2.3.0"

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
MODELS_DIR = os.path.join(HERE, "models")

DEFAULTS = {
    "power_policy": "auto",          # auto | explicit
    "strict": False,
    "allow_partial_model": False,
    "behavioral": True,              # False 时关闭 behavioral placeholder
    "max_cases": 200,
    "ground_aliases": None,
    "models": None,                  # model_map.json 路径
    "behavioral_models": None,       # behavioral.json 路径
    "override": None,                # sim_override.json 路径
    "external_supplies": [],         # explicit 模式下允许的外部供电声明
    "analysis": {},
    "metrics": [],
    "sweep": None,
}

_RESERVED_NET_CHARS = re.compile(r"[^0-9A-Za-z_.+\-]")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _deep_merge(base, extra):
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def resolve_path(path, bases):
    """按 bases 顺序解析相对路径；绝对路径直接返回（不存在也返回原值）。"""
    if not path:
        return None
    if os.path.isabs(path):
        return path
    for b in bases:
        if not b:
            continue
        cand = os.path.normpath(os.path.join(b, path))
        if os.path.exists(cand):
            return cand
    return os.path.normpath(os.path.join(bases[0] or os.getcwd(), path))


def load_config(path=None, overrides=None):
    """加载 sim_config.json，返回带 DEFAULTS 的完整配置。

    配置里的相对路径统一转成绝对路径，另外保存 `_dir`（config 所在目录）
    与 `_path`，供 model_map 内的 `external/xxx.lib` 解析。
    """
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["_dir"] = os.getcwd()
    cfg["_path"] = None
    if path:
        raw = load_json(path)
        cfg["_dir"] = os.path.dirname(os.path.abspath(path))
        cfg["_path"] = os.path.abspath(path)
        _deep_merge(cfg, raw)
    for k, v in (overrides or {}).items():
        if v is not None:
            cfg[k] = v
    bases = [cfg["_dir"], MODELS_DIR, HERE]
    for key in ("models", "behavioral_models", "override", "ibis"):
        if cfg.get(key):
            cfg[key] = resolve_path(cfg[key], bases)
    return cfg


def spice_ident(name, fallback="X"):
    """元件位号 / 元件名 → SPICE 安全标识符（撇号会破坏 ngspice 解析）。"""
    s = re.sub(r"[^0-9A-Za-z_]", "_", str(name or fallback))
    if not s:
        s = fallback
    if not re.match(r"^[A-Za-z]", s):
        s = fallback[0].upper() + s
    return s


VIRTUAL_PREFIXES = ("$", "__AUTO_")


def is_virtual(name):
    """虚拟节点写法：`$FOO`（手写）或 `__AUTO_FOO`（PCB 自动生成，v2.3）。"""
    return isinstance(name, str) and name.startswith(VIRTUAL_PREFIXES)


def spice_node(name):
    """网络名/节点名 → deck 里的节点标识符。

    - `$FOO`  → `__FOO`（虚拟节点；`$` 在 ngspice 里是注释符）
    - 其他    → 非法字符换 `_`，数字开头补 `N_`
    """
    if name is None:
        return None
    s = str(name).strip()
    if not s:
        return "N_unnamed"
    if s == "0":
        return "0"
    if s.startswith("__AUTO_"):
        return _RESERVED_NET_CHARS.sub("_", s)          # 已是 spice 安全名
    if s.startswith("$"):
        body = _RESERVED_NET_CHARS.sub("_", s[1:])
        return "__" + (body or "V")
    s = _RESERVED_NET_CHARS.sub("_", s)
    if re.match(r"^[0-9]", s):
        s = "N_" + s
    return s or "N_unnamed"


def display_node(node):
    """deck 节点名 → 报告里的可读写法（`__FOO` → `$FOO`）。"""
    if isinstance(node, str) and node.startswith("__"):
        return "$" + node[2:]
    return node


def quote_path(path, base_dir=None):
    """生成 .include 用路径：Windows 安全、尽量相对 deck 目录、含空格加引号。"""
    if not path:
        return '""'
    p = os.path.abspath(path).replace("\\", "/")
    if base_dir:
        try:
            rel = os.path.relpath(p, os.path.abspath(base_dir)).replace("\\", "/")
            if not rel.startswith(".."):
                p = rel
        except ValueError:
            pass
    return '"' + p + '"'


class Namer:
    """元件名分配器：保证唯一，并按类型前缀生成可读名字。"""

    def __init__(self):
        self.used = set()

    def take(self, name):
        base = spice_ident(name)
        s, i = base, 2
        while s.upper() in self.used:
            s = f"{base}_{i}"
            i += 1
        self.used.add(s.upper())
        return s

    def __call__(self, prefix, hint):
        return self.take(f"{prefix}_{hint}" if hint else prefix)
