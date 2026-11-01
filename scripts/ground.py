#!/usr/bin/env python3
"""v2.2 P0-A：网络 → SPICE 地节点映射策略。

v2.1 的 `sanitize_net` 用一条 `gnd\\d*|ground|agnd|dgnd|0` 正则把所有
“像地”的网络统统压成节点 0。对 PDN / ground impedance / ferrite bead /
0Ω ground link / star point / analog-digital return 这类分析，这个隐式合并
会直接制造错误结果（AGND 的返回电流被强行并进 DGND），所以 v2.2 改为：

  默认只把 `GND` / `GROUND` / `0` 映射到 SPICE 节点 0；
  `AGND` / `DGND` / `PGND` / `CHASSIS` / `EARTH` / `VSS` / 其他 ground-like
  网络一律保持独立节点，名字原样保留。

合并必须由用户在 ground_aliases 中显式声明，且报告里必须逐条打印
"Ground mapping: XXX -> YYY"，禁止静默合并。
"""
import re

ZERO_NODE = "0"

# 默认自动映射（不视为“隐式合并”，因为这是 SPICE 的公共约定）
DEFAULT_ZERO_NETS = ("GND", "GROUND", "0")

# 仅用于“这条网络看起来是地”的识别（决定是否要在报告里列出/是否要提示）
_GROUND_LIKE = re.compile(
    r"(?i)^(0|gnd\d*|ground\d*|agnd\d*|dgnd\d*|pgnd\d*|sgnd\d*|"
    r"chassis\d*|earth\d*|vss\d*|gnd_[a-z0-9_]+|.*_(gnd|agnd|dgnd)|"
    r".*gnd.*|.*ground.*|.*earth.*)$"
)


def is_ground_like(name):
    if name is None:
        return False
    s = str(name).strip()
    if not s:
        return False
    return bool(_GROUND_LIKE.match(s))


class GroundMapError(ValueError):
    pass


class GroundMap:
    """ground_aliases 配置 → 可查询的映射表。

    配置形态::

        {"ground_aliases": {"0": ["GND", "GROUND"], "AGND": ["AGND"]}}

    key   = 目标 SPICE 节点名（"0" 表示 SPICE 全局地）
    value = 归并到该节点的网络名列表

    默认映射始终生效；若需完全接管（连 GND→0 也自己指定），在
    ground_aliases 里加 `"_replace_defaults": true`。
    """

    def __init__(self, cfg=None):
        self._map = {}          # UPPER(net) -> (target, explicit)
        self.explicit_merge = []  # 被显式合并的 ground-like 网络（报告用）
        self.errors = []
        cfg = cfg or {}
        replace = bool(cfg.get("_replace_defaults")) if isinstance(cfg, dict) else False
        if not replace:
            for n in DEFAULT_ZERO_NETS:
                self._map[n.upper()] = (ZERO_NODE, False)
        if isinstance(cfg, dict):
            for target, nets in cfg.items():
                if str(target).startswith("_"):
                    continue
                if isinstance(nets, str):
                    nets = [nets]
                for n in nets or []:
                    key = str(n).strip().upper()
                    if not key:
                        continue
                    prev = self._map.get(key)
                    if prev and prev[0] != str(target):
                        self.errors.append({
                            "code": "GROUND_MAPPING_CONFLICT",
                            "message": f"网络 {n} 同时被映射到 {prev[0]} 与 {target}",
                        })
                        continue
                    self._map[key] = (str(target), True)
                    if is_ground_like(n) and str(target) == ZERO_NODE and key not in (
                            x.upper() for x in DEFAULT_ZERO_NETS):
                        self.explicit_merge.append(str(n))

    # ---- 查询 ----
    def map(self, name):
        """返回 (目标节点 or None, explicit)。None 表示保持自己的网络名。"""
        if name is None:
            return None, False
        hit = self._map.get(str(name).strip().upper())
        if hit:
            return hit[0], hit[1]
        return None, False

    def node(self, name):
        """直接给出最终节点名（未映射则返回原网络名）。"""
        tgt, _ = self.map(name)
        return tgt if tgt is not None else name

    def is_zero(self, name):
        tgt, _ = self.map(name)
        if tgt is not None:
            return str(tgt) == ZERO_NODE
        return str(name).strip() == ZERO_NODE

    def is_explicit_merge_to_zero(self, name):
        tgt, explicit = self.map(name)
        return explicit and str(tgt) == ZERO_NODE and str(name).strip().upper() not in (
            x.upper() for x in DEFAULT_ZERO_NETS)

    # ---- 报告 ----
    def report(self, net_names):
        """对设计里实际出现的网络生成 ground mapping 段。

        返回 (lines, mapping_dict)。只列出 ground-like 或被显式映射的网络。
        """
        seen = []
        for n in net_names or []:
            if n is None:
                continue
            s = str(n).strip()
            if not s or s in seen:
                continue
            if is_ground_like(s) or self.map(s)[0] is not None:
                seen.append(s)
        lines = ["Ground mapping:"]
        mapping = {}
        if not seen:
            lines.append("  (no ground-like net found)")
        for s in seen:
            tgt = self.node(s)
            mapping[s] = tgt
            tag = ""
            if self.is_explicit_merge_to_zero(s):
                tag = "   [explicit merge, from ground_aliases]"
            elif str(tgt) != ZERO_NODE and is_ground_like(s) and s.upper() not in (
                    x.upper() for x in DEFAULT_ZERO_NETS):
                tag = "   [kept isolated]"
            lines.append(f"  {s}  ->  {tgt}{tag}")
        return lines, mapping

    def warn_unmerged(self, net_names):
        """提示 ground-like 但未自动合并的网络（用户可能期望合并）。"""
        out = []
        for n in net_names or []:
            if n is None:
                continue
            s = str(n).strip()
            if not s or not is_ground_like(s):
                continue
            if s.upper() in (x.upper() for x in DEFAULT_ZERO_NETS):
                continue
            if self.map(s)[0] is not None:
                continue
            out.append(s)
        return out
