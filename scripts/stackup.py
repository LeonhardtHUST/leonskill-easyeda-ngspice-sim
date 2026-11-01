#!/usr/bin/env python3
"""v2.3 P0-B：叠层模型 + 传输线参数解析估计。

设计原则（需求文档 §3/§4）:
  * PCB 文件缺叠层时**禁止**默认为 FR4 εr=4.5 后静默继续 → 必须报
    `STACKUP_INCOMPLETE`，并允许用户用 stackup.json 补齐/覆盖。
  * 估计结果一律标 `ANALYTIC_ESTIMATE`，不得宣称 field-solver 精度。

公式来源：Hammerstad-Jensen 微带线近似 + IPC-2141 带状线近似，都是工程通用的
解析式，误差量级 ±10%（在 w/h 与 εr 常规范围内）。用它们做"量级正确 + 趋势正确"
的寄生估值是合适的，做阻抗控制验收则不行——文档里必须说清楚。
"""
import json
import math
import os

C_PS_PER_MM = 3.33564     # 1/c = 3.33564 ps/mm（真空）


class StackupError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Stackup:
    """layers: [{name, type: signal|plane, copper_um}]
    dielectrics: [{between: [a, b], thickness_mm, er, loss_tangent}]
    """

    def __init__(self, layers=None, dielectrics=None, source=None, meta=None):
        self.layers = [dict(l) for l in (layers or [])]
        self.dielectrics = [dict(d) for d in (dielectrics or [])]
        self.source = source
        self.meta = meta or {}
        self.warnings = []
        self.errors = []

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_file(cls, path, base=None):
        """读取用户 stackup.json；给了 base（从 PCB 抽的）就只补缺、不覆盖已有值。

        这就是需求文档 §3 的"允许用户补充"：PCB 缺铜厚时，用户只写铜厚即可，
        不用把 PCB 里已有的 εr/介质厚度再抄一遍。
        """
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        st = cls(raw.get("layers"), raw.get("dielectrics"), source=os.path.basename(path),
                 meta={"file": os.path.abspath(path)})
        st.normalize()
        if base is not None:
            st.fill_from(base)
        return st

    def fill_from(self, base):
        """用 base 补齐 self 里缺失的字段（同名列 / 同介质对 / 顺序位置）。"""
        for l in self.layers:
            if l.get("copper_um"):
                continue
            bl = base.layer(l.get("name"))
            if bl and bl.get("copper_um"):
                l["copper_um"] = bl["copper_um"]
                l["_filled_from"] = "pcb"
        # 介质：优先按 between 精确匹配，其次按顺序位置
        for i, d in enumerate(self.dielectrics):
            if d.get("thickness_mm") and d.get("er"):
                continue
            src = base._find_dielectric(d.get("between") or [])
            if not src and i < len(base.dielectrics):
                src = base.dielectrics[i]
            if not src:
                continue
            if not d.get("thickness_mm") and src.get("thickness_mm"):
                d["thickness_mm"] = src["thickness_mm"]
                d["_filled_from"] = "pcb"
            if not d.get("er") and src.get("er"):
                d["er"] = src["er"]
                d["_filled_from"] = "pcb"
        # 用户只写了铜厚 + 一层介质时：把 PCB 的介质映射到相邻层对
        if not self.dielectrics and base.dielectrics:
            # PCB 抽出来的 between 是占位名（layer2/layer3），必须按层顺序重映射，
            # 否则 _find_dielectric 找不到 ("Top Layer","Bottom Layer") 这一对
            n_pairs = max(len(self.layers) - 1, 1)
            for i in range(n_pairs):
                src = base.dielectrics[min(i, len(base.dielectrics) - 1)]
                nd = dict(src)
                if i + 1 < len(self.layers):
                    nd["between"] = [self.layers[i]["name"], self.layers[i + 1]["name"]]
                nd["_filled_from"] = "pcb"
                self.dielectrics.append(nd)
        elif len(self.dielectrics) < len(self.layers) - 1 and base.dielectrics:
            # 层数比介质多：按顺序把 PCB 的介质追加到缺失的层对
            for i in range(len(self.layers) - 1):
                a, b = self.layers[i]["name"], self.layers[i + 1]["name"]
                if self._find_dielectric((a, b)):
                    continue
                src = base.dielectrics[min(i, len(base.dielectrics) - 1)]
                nd = dict(src)
                nd["between"] = [a, b]
                nd["_filled_from"] = "pcb"
                self.dielectrics.append(nd)
        return self

    @classmethod
    def from_pcb(cls, geo_layers, stackup_raw, board_thickness_mm=None):
        """从 PCB 抽出来的部分叠层信息尽力还原；缺什么就记什么。"""
        copper = [l for l in (geo_layers or [])
                  if (l.get("type") or "").upper() in ("TOP", "BOTTOM", "INNER", "SIGNAL",
                                                       "PLANE")
                  or (l.get("type") or "").upper().startswith("INNER")]
        layers = []
        for l in copper:
            layers.append({"name": l["name"], "type": "signal"
                           if (l.get("type") or "").upper() in ("TOP", "BOTTOM", "SIGNAL")
                           else "plane", "copper_um": None})
        dielectrics = []
        phys = stackup_raw or []
        for i in range(len(phys)):
            pl = phys[i]
            er = _f(pl.get("er"))
            th = _f(pl.get("thickness"))
            if er and th and th > 0:
                prev = phys[i - 1] if i > 0 else None
                nxt = phys[i + 1] if i + 1 < len(phys) else None
                dielectrics.append({
                    "between": [f"layer{i}", f"layer{i + 1}"],
                    "thickness_mm": th,
                    "er": er,
                    "loss_tangent": _f(pl.get("loss_tangent")),
                    "material": pl.get("material") or None,
                    "from_pcb": True,
                    "_z": pl.get("z"),
                })
        st = cls(layers, dielectrics, source="pcb",
                 meta={"board_thickness_mm": board_thickness_mm})
        st.normalize()
        return st

    def normalize(self):
        """把 layers 的 copper 厚度与 dielectrics 的 between 对齐；缺项记 None。"""
        for l in self.layers:
            cu = _f(l.get("copper_um"))
            l["copper_um"] = cu
            l["type"] = (l.get("type") or "signal").lower()
            l.setdefault("name", "layer")
        for d in self.dielectrics:
            d["thickness_mm"] = _f(d.get("thickness_mm"))
            d["er"] = _f(d.get("er"))
        return self

    # ---------------------------------------------------------------- 校验
    def validate(self, need_layers=None):
        """返回缺失项列表；空列表表示可用于 auto-Z0。"""
        missing = []
        if not self.layers:
            missing.append("no copper layers")
        if len(self.layers) < 2:
            missing.append("need >= 2 copper layers to know layer count")
        if not self.dielectrics:
            missing.append("no dielectric entry (thickness/er)")
        for i, l in enumerate(self.layers):
            if not l.get("copper_um"):
                missing.append(f"copper thickness missing on {l.get('name')}")
        for d in self.dielectrics:
            if not d.get("thickness_mm"):
                missing.append(f"dielectric thickness missing between {d.get('between')}")
            if not d.get("er"):
                missing.append(f"dielectric er missing between {d.get('between')}")
        if need_layers:
            for nm in need_layers:
                if not any(l.get("name") == nm for l in self.layers):
                    missing.append(f"layer '{nm}' not in stackup")
        return missing

    def require_complete(self, need_layers=None):
        missing = self.validate(need_layers)
        if missing:
            raise StackupError(
                "STACKUP_INCOMPLETE",
                "叠层信息不完整，无法做 auto-Z0；请提供 stackup.json 补齐（不会默认 FR4）："
                + "; ".join(missing[:6]))
        return True

    # ---------------------------------------------------------------- 查询
    def layer(self, name):
        for l in self.layers:
            if l.get("name") == name:
                return l
        return None

    def layer_index(self, name):
        for i, l in enumerate(self.layers):
            if l.get("name") == name:
                return i
        return None

    def dielectric_for(self, layer_name):
        """返回 (h_mm, er, loss, model) —— 该层走线到最近参考平面的距离。"""
        idx = self.layer_index(layer_name)
        if idx is None:
            raise StackupError("STACKUP_LAYER_NOT_FOUND", f"叠层里没有层 {layer_name!r}")
        lay = self.layers[idx]
        n = len(self.layers)
        outer = idx == 0 or idx == n - 1
        if outer:
            # 微带：单侧参考平面。
            # 顶层 → 下方介质；底层 → 上方介质
            pair = (self.layers[idx]["name"], self.layers[idx + 1]["name"]) if idx == 0 else \
                   (self.layers[idx - 1]["name"], self.layers[idx]["name"])
            d = self._find_dielectric(pair)
            if not d:
                raise StackupError("STACKUP_INCOMPLETE",
                                   f"{layer_name} 的介质信息缺失（{pair}）")
            return d["thickness_mm"], d["er"], d.get("loss_tangent"), "microstrip"
        # 内层：带状线，两侧都是介质 → 总厚度 = 上下介质之和
        up = self._find_dielectric((self.layers[idx - 1]["name"], lay["name"]))
        dn = self._find_dielectric((lay["name"], self.layers[idx + 1]["name"]))
        if not up or not dn:
            raise StackupError("STACKUP_INCOMPLETE",
                               f"{layer_name} 上下介质信息不全，无法按带状线建模")
        if abs((up["er"] or 0) - (dn["er"] or 0)) > 0.2:
            self.warnings.append(
                f"{layer_name} 上下介质 εr 不同（{up['er']} vs {dn['er']}），带状线取平均")
        return (up["thickness_mm"] + dn["thickness_mm"]), (up["er"] + dn["er"]) / 2, \
               up.get("loss_tangent"), "stripline"

    def _find_dielectric(self, pair):
        a, b = pair
        for d in self.dielectrics:
            bt = d.get("between") or []
            if len(bt) == 2 and {bt[0], bt[1]} == {a, b}:
                return d
        # from_pcb 的 between 是占位名，按 z 顺序兜底
        if self.dielectrics and self.source == "pcb":
            idx = self.layer_index(a)
            cands = [d for d in self.dielectrics if d.get("_z")]
            if cands and idx is not None:
                return cands[min(idx, len(cands) - 1)]
        return None

    def to_dict(self):
        return {
            "layers": self.layers,
            "dielectrics": [{k: v for k, v in d.items() if not k.startswith("_")}
                            for d in self.dielectrics],
            "source": self.source,
            "meta": self.meta,
        }


# --------------------------------------------------------------------------
# 传输线参数（解析估计）
# --------------------------------------------------------------------------
def microstrip_z0(w_mm, h_mm, t_mm, er):
    """Hammerstad-Jensen 微带线：返回 (z0, eps_eff)。"""
    if w_mm <= 0 or h_mm <= 0:
        raise StackupError("TRACE_GEOMETRY_INVALID", f"线宽/介质厚度必须 > 0（w={w_mm}, h={h_mm}）")
    if er < 1:
        raise StackupError("STACKUP_INCOMPLETE", f"εr={er} 不合理")
    # 有限铜厚修正
    if t_mm and t_mm > 0:
        du = t_mm / h_mm
        w_eff = w_mm + (t_mm / math.pi) * (
            1 + math.log(4 * math.e / (du ** 2 + (1 / math.pi) ** 2 * (1 / (du + 1.1)) ** 2)))
    else:
        w_eff = w_mm
    u = w_eff / h_mm
    eps_eff = (er + 1) / 2 + (er - 1) / 2 * (1 + 12 / u) ** -0.5
    if u <= 1:
        z0 = 60 / math.sqrt(eps_eff) * math.log(8 / u + u / 4)
    else:
        z0 = 120 * math.pi / (math.sqrt(eps_eff) *
                              (u + 1.393 + 0.667 * math.log(u + 1.444)))
    return z0, eps_eff


def stripline_z0(w_mm, b_mm, t_mm, er):
    """IPC-2141 带状线（b = 两参考平面间距）。返回 (z0, eps_eff=er)。"""
    if w_mm <= 0 or b_mm <= 0:
        raise StackupError("TRACE_GEOMETRY_INVALID", f"线宽/板厚必须 > 0（w={w_mm}, b={b_mm}）")
    t = t_mm or 0.0
    if t <= 0:
        z0 = 60 / math.sqrt(er) * math.log(4 * b_mm / (0.67 * math.pi * w_mm))
    else:
        z0 = 60 / math.sqrt(er) * math.log(
            4 * b_mm / (0.67 * math.pi * w_mm * (0.8 + t / w_mm)))
    return z0, er


def trace_params(stackup, layer_name, width_mm, length_mm=None):
    """给出 (z0, td_ps_per_mm, eps_eff, model)；length 给了就顺带算 td_ps。"""
    h, er, loss, model = stackup.dielectric_for(layer_name)
    lay = stackup.layer(layer_name) or {}
    t_mm = (lay.get("copper_um") or 0) / 1000.0
    if model == "microstrip":
        z0, eps_eff = microstrip_z0(width_mm, h, t_mm, er)
    else:
        z0, eps_eff = stripline_z0(width_mm, h, t_mm, er)
    td_ps_per_mm = C_PS_PER_MM * math.sqrt(eps_eff)
    out = {
        "layer": layer_name,
        "width_mm": round(width_mm, 4),
        "h_mm": round(h, 4),
        "er": er,
        "copper_um": (lay.get("copper_um") or None),
        "model": model,
        "z0_ohm": round(z0, 2),
        "eps_eff": round(eps_eff, 4),
        "td_ps_per_mm": round(td_ps_per_mm, 4),
        "model_fidelity": "ANALYTIC_ESTIMATE",
    }
    if loss:
        out["loss_tangent"] = loss
    if length_mm:
        out["length_mm"] = round(length_mm, 4)
        out["td_ps"] = round(length_mm * td_ps_per_mm, 3)
    return out


def summary_lines(trace):
    return [
        f"{trace['layer']}  w={trace['width_mm']:g} mm  h={trace['h_mm']:g} mm  "
        f"εr={trace['er']:g}  ({trace['model']})",
        f"Z0 = {trace['z0_ohm']:g} Ω   εeff = {trace['eps_eff']:g}   "
        f"td = {trace['td_ps_per_mm']:g} ps/mm  [ANALYTIC_ESTIMATE]",
    ]


def main():
    import sys
    if len(sys.argv) < 3:
        print("usage: stackup.py <stackup.json> <layer> <width_mm> [length_mm]")
        sys.exit(2)
    st = Stackup.from_file(sys.argv[1])
    tr = trace_params(st, sys.argv[2], float(sys.argv[3]),
                      float(sys.argv[4]) if len(sys.argv) > 4 else None)
    for ln in summary_lines(tr):
        print(ln)


if __name__ == "__main__":
    main()
