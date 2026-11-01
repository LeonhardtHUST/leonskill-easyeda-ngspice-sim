#!/usr/bin/env python3
"""v2.3 P0-A：PCB 几何提取（EasyEDA Pro PCB → PCB_GEOMETRY.json）。

实测格式事实（EasyEDA Pro 4.x，基于 3 个真实 .epro2 验证）:
  * .epro2 的 .epru 日志里 docType=PCB 的文档包含:
      LINE          走线：{netName, layerId, startX, startY, endX, endY, width}
      VIA           过孔：{netName, centerX, centerY, holeDiameter, viaDiameter,
                          viaType, unusedInnerLayers}
      LAYER         层定义：id = ["LAYER", n]，payload.layerType/layerName
      LAYER_PHYS    叠层物理参数（可能全为 null → STACKUP_INCOMPLETE）
      COMPONENT     器件实例：{x, y, angle, layerId}
      ATTR          parentId=COMPONENT.id，key=Designator / Footprint / Device
      PAD_NET       焊盘网络：id = ["PAD_NET", compId, padNum, padId]，payload.padNet
      POUR/POURED   铺铜（本轮只用它判断 plane 层，不做网格提取）
  * 坐标与线宽单位是 **mil**，与 CANVAS.unit（显示单位，可能写 mm）无关。
    焊盘绝对坐标 = COMPONENT.x/y + rot(FOOTPRINT.PAD.centerX/centerY, angle)。
    实测 C1 pad2 与某条 LINE 端点对齐到 0.02 mil。
  * `LAYER_PHYS` 里只有介质层有真实数据（thickness/er/lossTangent），铜厚常为 0/null
    → 交给 stackup.py 判 STACKUP_INCOMPLETE，绝不默认 FR4=4.5 静默继续。

输出（PCB_GEOMETRY.json）把"图元"变成"拓扑"：
  net → terminals（DES.PIN 或 __AUTO_<NET>_B/E<n>）+ segments（layer/length/width/via_count）
节点命名遵循需求文档 §2：自动节点一律 `__AUTO_...`，不依赖 `$`。
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MIL_TO_MM = 0.0254
SNAP_MM = 0.0025          # 节点吸附容差（0.1 mil ≈ 2.5 µm）
DEFAULT_UNIT = "mil"      # EasyEDA Pro PCB 内部坐标单位（实测）

COPPER_LAYER_TYPES = {"TOP", "BOTTOM", "INNER", "INNER1", "INNER2", "PLANE"}
NON_COPPER_LAYER_TYPES = {
    "TOP_SILK", "BOT_SILK", "TOP_SOLDER_MASK", "BOT_SOLDER_MASK", "TOP_PASTE_MASK",
    "BOT_PASTE_MASK", "TOP_ASSEMBLY", "BOT_ASSEMBLY", "OUTLINE", "MULTI", "DOCUMENT",
    "MECHANICAL", "HOLE", "COMPONENT_SHAPE", "COMPONENT_MARKING", "PIN_SOLDERING",
    "PIN_FLOATING", "3D_SHELL_OUTLINE", "3D_SHELL_TOP", "3D_SHELL_BOTTOM",
    "DRILL_DRAWING", "OTHER",
}


class PcbError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _unit_scale(unit):
    u = (unit or DEFAULT_UNIT).lower()
    if u in ("mil", "thou"):
        return MIL_TO_MM
    if u in ("mm", "millimeter", "millimetre"):
        return 1.0
    if u in ("um", "µm", "micron"):
        return 0.001
    if u in ("in", "inch"):
        return 25.4
    raise PcbError("PCB_UNIT_UNKNOWN", f"无法识别的 PCB 单位 {unit!r}（支持 mil/mm/um/in）")


# --------------------------------------------------------------------------
# 1. 从 .epro2 抽取原始图元
# --------------------------------------------------------------------------
def _rid_parts(rid):
    """元素 id 可能是 list，也可能是 JSON 编码后的字符串（实测两种都出现）。"""
    if isinstance(rid, list):
        return rid
    if isinstance(rid, str) and rid.startswith("["):
        try:
            v = json.loads(rid)
            return v if isinstance(v, list) else [rid]
        except json.JSONDecodeError:
            return [rid]
    return [rid]


def parse_pcb(epro2_path, unit=None, index=0):
    """解析 .epro2 里的 PCB 文档 → 原始图元（mil）。"""
    import epro2_parse as ep

    text, project_meta = ep.open_epro2(epro2_path)
    docs = ep.parse_records(text)
    pcbs = [d for d in docs if d["docType"] == "PCB"]
    if not pcbs:
        raise PcbError("PCB_DOC_NOT_FOUND",
                       f"{os.path.basename(epro2_path)} 里没有 docType=PCB 的文档"
                       f"（只有原理图的项目无法做几何提参）")
    if index >= len(pcbs):
        raise PcbError("PCB_DOC_NOT_FOUND", f"PCB 文档索引 {index} 越界（共 {len(pcbs)} 个）")
    pcb = pcbs[index]
    bt, meta = ep.doc_elements(pcb)

    # 层
    layers = {}
    for rid, pl in bt.get("LAYER", []):
        lid = _rid_parts(rid)[-1]
        layers[int(lid)] = {
            "id": int(lid),
            "type": (pl.get("layerType") or "").upper(),
            "name": pl.get("layerName") or f"Layer{lid}",
            "use": bool(pl.get("use", True)),
        }
    # 叠层物理参数（zIndex 顺序）
    phys = sorted(
        [(pl.get("zIndex") or i, pl) for i, (rid, pl) in enumerate(bt.get("LAYER_PHYS", []))],
        key=lambda x: x[0])
    stackup_raw = []
    for z, pl in phys:
        stackup_raw.append({
            "z": z,
            "material": pl.get("material"),
            "thickness": pl.get("thickness"),
            "er": pl.get("permittivity"),
            "loss_tangent": pl.get("lossTangent"),
        })

    # 器件位号 / 封装
    designator, footprint, layer_of_comp = {}, {}, {}
    for rid, a in bt.get("ATTR", []):
        pid, key, val = a.get("parentId"), a.get("key"), a.get("value")
        if not pid:
            continue
        if key == "Designator":
            designator[pid] = val
        elif key == "Footprint":
            footprint[pid] = val
    for rid, c in bt.get("COMPONENT", []):
        cid = _rid_parts(rid)[-1]
        layer_of_comp[cid] = int(c.get("layerId") or 1)

    # 封装焊盘
    fp_pads = {}
    for dd in docs:
        if dd["docType"] != "FOOTPRINT":
            continue
        b2, _ = ep.doc_elements(dd)
        pads = []
        for prid, p in b2.get("PAD", []):
            pads.append({
                "num": str(p.get("num") or ""),
                "x": float(p.get("centerX") or 0.0),
                "y": float(p.get("centerY") or 0.0),
                "angle": float(p.get("padAngle") or 0.0),
                "layer_id": int(p.get("layerId") or 1),
                "width": float(((p.get("defaultPad") or {}).get("width")) or 0.0),
                "height": float(((p.get("defaultPad") or {}).get("height")) or 0.0),
                "hole": (p.get("hole") or {}).get("width"),
            })
        if pads:
            fp_pads[dd["uuid"]] = pads

    # 焊盘 → 网络（PAD_NET id = [PAD_NET, compId, padNum, padId]）
    pad_net = {}
    for rid, pl in bt.get("PAD_NET", []):
        parts = _rid_parts(rid)
        if len(parts) >= 3:
            pad_net[(parts[1], str(parts[2]))] = pl.get("padNet")

    # 器件 → 绝对焊盘
    pads = []
    for rid, c in bt.get("COMPONENT", []):
        cid = _rid_parts(rid)[-1]
        des = designator.get(cid) or cid
        fpuuid = footprint.get(cid)
        fx, fy = float(c.get("x") or 0.0), float(c.get("y") or 0.0)
        ang = float(c.get("angle") or 0.0)
        for p in fp_pads.get(fpuuid, []):
            ax, ay = _rotate(p["x"], p["y"], ang)
            pads.append({
                "component": des,
                "pin": p["num"],
                "net": pad_net.get((cid, p["num"])),
                "x": fx + ax,
                "y": fy + ay,
                "layer_id": p["layer_id"] if p["layer_id"] != 12 else layer_of_comp.get(cid, 1),
                "w": p["width"],
                "h": p["height"],
                "drill": p["hole"],
            })

    tracks, vias = [], []
    canvas_unit = None
    for rid, pl in bt.get("CANVAS", []):
        canvas_unit = pl.get("unit")
    for rid, pl in bt.get("LINE", []):
        lid = int(pl.get("layerId") or 0)
        if lid not in layers or layers[lid]["type"] in NON_COPPER_LAYER_TYPES:
            continue
        tracks.append({
            "net": pl.get("netName") or "",
            "layer_id": lid,
            "x1": float(pl.get("startX") or 0.0), "y1": float(pl.get("startY") or 0.0),
            "x2": float(pl.get("endX") or 0.0), "y2": float(pl.get("endY") or 0.0),
            "width": float(pl.get("width") or 0.0),
        })
    for rid, pl in bt.get("VIA", []):
        vias.append({
            "net": pl.get("netName") or "",
            "x": float(pl.get("centerX") or 0.0),
            "y": float(pl.get("centerY") or 0.0),
            "drill": float(pl.get("holeDiameter") or 0.0),
            "pad": float(pl.get("viaDiameter") or 0.0),
            "type": (pl.get("viaType") or "NORMAL").upper(),
            "unused": pl.get("unusedInnerLayers") or [],
        })
    # 板框尺寸（OUTLINE 层走线 / POUR 外包）
    xs, ys = [], []
    for rid, pl in bt.get("LINE", []):
        xs += [float(pl.get("startX") or 0), float(pl.get("endX") or 0)]
        ys += [float(pl.get("startY") or 0), float(pl.get("endY") or 0)]

    return {
        "source": os.path.basename(epro2_path),
        "project": (project_meta or {}).get("name") or os.path.splitext(
            os.path.basename(epro2_path))[0],
        "pcb_title": (meta or {}).get("title"),
        "unit": unit or DEFAULT_UNIT,
        "canvas_unit": canvas_unit,
        "layers": [layers[k] for k in sorted(layers)],
        "stackup_raw": stackup_raw,
        "tracks": tracks,
        "vias": vias,
        "pads": pads,
        "extent": {"x": [min(xs), max(xs)] if xs else [0, 0],
                   "y": [min(ys), max(ys)] if ys else [0, 0]},
        "warnings": [],
    }


def _rotate(x, y, deg):
    if not deg:
        return x, y
    th = math.radians(deg)
    return x * math.cos(th) - y * math.sin(th), x * math.sin(th) + y * math.cos(th)


def load_pcb_input(path, unit=None):
    """接受两种输入：.epro2 工程，或已解析的 PCB IR（{tracks,vias,pads,layers}）。"""
    if path.lower().endswith((".epro2", ".epro")):
        return parse_pcb(path, unit=unit)
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    for k in ("tracks", "vias", "pads", "layers"):
        raw.setdefault(k, [])
    raw.setdefault("unit", unit or DEFAULT_UNIT)
    raw.setdefault("stackup_raw", [])
    raw.setdefault("warnings", [])
    raw.setdefault("source", os.path.basename(path))
    return raw


# --------------------------------------------------------------------------
# 2. 图 → 拓扑
# --------------------------------------------------------------------------
class _Union:
    def __init__(self):
        self.p = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _key(x, y, layer, scale):
    """吸附到 SNAP_MM 网格的节点 key（含层）。"""
    qx = round(x * scale / SNAP_MM) * SNAP_MM
    qy = round(y * scale / SNAP_MM) * SNAP_MM
    return (round(qx, 4), round(qy, 4), layer)


def _xy(a):
    """同时接受 (x, y) 元组、节点 key、节点 dict。"""
    if isinstance(a, dict):
        return a["x"], a["y"]
    return a[0], a[1]


def _dist(a, b):
    ax, ay = _xy(a)
    bx, by = _xy(b)
    return math.hypot(ax - bx, ay - by)


def _point_on_segment(p, a, b, tol):
    """p 是否落在线段 a-b 上（含容差），返回参数 t 或 None。"""
    ax, ay = a[0], a[1]
    bx, by = b[0], b[1]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 1e-12:
        return None
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2
    if t <= 1e-9 or t >= 1 - 1e-9:
        return None
    px, py = ax + t * dx, ay + t * dy
    if math.hypot(p[0] - px, p[1] - py) <= tol:
        return t
    return None


def _used_copper_layers(layers, tracks, vias, pads):
    """只保留真正用到的铜层。

    EasyEDA Pro 的 LAYER 表里恒有 60+ 条（INNER1..INNER30 之类，type=SIGNAL），
    两层板也会全列出来。用"有走线/焊盘引用"来判定真实层数，否则叠层模型直接算错。
    """
    used = {int(t["layer_id"]) for t in tracks if t.get("layer_id")}
    used |= {int(p["layer_id"]) for p in pads if p.get("layer_id")}
    out = []
    for lid in sorted(layers):
        l = layers[lid]
        t = (l.get("type") or "").upper()
        if t in NON_COPPER_LAYER_TYPES:
            continue
        if lid not in used and t not in ("TOP", "BOTTOM"):
            continue
        out.append({"id": lid, "name": l["name"], "type": t})
    return out


def build_geometry(raw, net=None, scale=None, stackup=None):
    """原始图元 → 拓扑（terminals + segments）。"""
    unit = raw.get("unit") or DEFAULT_UNIT
    scale = scale or _unit_scale(unit)
    warnings = list(raw.get("warnings") or [])
    nets = raw.get("tracks") or []
    vias = raw.get("vias") or []
    pads = raw.get("pads") or []
    layers = {int(l["id"]): l for l in raw.get("layers") or [] if l.get("id") is not None}

    def layer_name(lid):
        l = layers.get(int(lid))
        return l["name"] if l else f"Layer{lid}"

    targets = [net] if isinstance(net, str) else (net or [])
    net_names = sorted({t["net"] for t in nets if t.get("net")} |
                       {v["net"] for v in vias if v.get("net")} |
                       {p["net"] for p in pads if p.get("net")})
    if targets:
        missing = [t for t in targets if t not in net_names]
        if missing:
            raise PcbError("PCB_NET_NOT_FOUND",
                           f"net {missing} 在 PCB 上不存在；可用网络: {net_names[:20]}")
        net_names = targets

    result = {
        "version": 1,
        "source": raw.get("source"),
        "project": raw.get("project"),
        "pcb_title": raw.get("pcb_title"),
        "units": {"internal": unit, "emitted": "mm", "scale_mm_per_unit": scale},
        "board": {"extent_mm": {
            "x": [round(v * scale, 3) for v in (raw.get("extent") or {}).get("x", [0, 0])],
            "y": [round(v * scale, 3) for v in (raw.get("extent") or {}).get("y", [0, 0])],
        }},
        "layers": _used_copper_layers(layers, nets, vias, pads),
        "nets": {},
        "warnings": warnings,
        "errors": [],
    }

    for name in net_names:
        nt = _build_net(name, nets, vias, pads, scale, layer_name, warnings)
        if nt["segments"] or nt["terminals"]:
            result["nets"][name] = nt

    if stackup is not None:
        result["stackup"] = stackup
    return result


def _build_net(name, tracks, vias, pads, scale, layer_name, warnings):
    tr = [t for t in tracks if t.get("net") == name]
    vi = [v for v in vias if v.get("net") == name]
    pd = [p for p in pads if p.get("net") == name]

    tol = SNAP_MM
    uf = _Union()
    nodes = {}                 # key -> {"x","y","layer"}
    edges = []                 # {"a","b","layer","width","length","kind"}

    def node_of(x, y, layer):
        k = _key(x, y, layer, scale)
        if k not in nodes:
            nodes[k] = {"x": k[0], "y": k[1], "layer": int(layer)}
        uf.find(k)
        return k

    for t in tr:
        a = node_of(t["x1"], t["y1"], t["layer_id"])
        b = node_of(t["x2"], t["y2"], t["layer_id"])
        if a == b:
            continue
        # nodes 里存的是 mm（_key 已乘 scale），长度不能再乘一次
        edges.append({"a": a, "b": b, "layer": int(t["layer_id"]),
                      "width": t["width"], "length": _dist(nodes[a], nodes[b])})

    # T 型接头：端点落在另一条走线中间 → 打断
    split_edges = []
    for e in edges:
        a, b = nodes[e["a"]], nodes[e["b"]]
        cuts = []
        for other in edges:
            if other is e or other["layer"] != e["layer"]:
                continue
            for endk in (other["a"], other["b"]):
                p = nodes[endk]
                if p is e["a"] or p is e["b"]:
                    continue
                t = _point_on_segment((p["x"], p["y"]), (a["x"], a["y"]), (b["x"], b["y"]), tol)
                if t is not None:
                    cuts.append((t, endk))
        if not cuts:
            split_edges.append(e)
            continue
        cuts.sort()
        chain = [e["a"]] + [k for _, k in cuts] + [e["b"]]
        for i in range(len(chain) - 1):
            k1, k2 = chain[i], chain[i + 1]
            split_edges.append({"a": k1, "b": k2, "layer": e["layer"], "width": e["width"],
                                "length": _dist(nodes[k1], nodes[k2])})
    edges = split_edges

    # 过孔：**不做节点合并**。过孔是串联寄生（R+L），必须保留成"两个层各自的节点 + 一个过孔元件"，
    # 否则 PCB 走线换层后 R/L 无处安放（v2.2 的 override 层正是按节点分段建模的）。
    via_recs = []
    via_names = {}          # 层内节点 key -> 过孔节点名
    for vi_idx, v in enumerate(vi, start=1):
        layers_of_net = sorted({e["layer"] for e in edges})
        if not layers_of_net:
            continue
        if v["type"] == "NORMAL" or not v.get("unused"):
            span = layers_of_net
        else:
            span = [l for l in layers_of_net if l not in set(v.get("unused") or [])]
        if len(span) < 2:
            continue
        keys = [node_of(v["x"], v["y"], l) for l in span]
        names = []
        for li, k in enumerate(keys):
            tag = "" if li == 0 else "_" + _spice_token(layer_name(span[li])).upper()
            nm = f"__AUTO_{_spice_token(name)}_V{vi_idx}{tag}"
            via_names[k] = nm
            names.append(nm)
        via_recs.append({
            "id": f"__AUTO_{_spice_token(name)}_V{vi_idx}",
            "x_mm": round(v["x"] * scale, 4), "y_mm": round(v["y"] * scale, 4),
            "drill_mm": round(v["drill"] * scale, 4), "pad_mm": round(v["pad"] * scale, 4),
            "type": v["type"], "layers": [layer_name(l) for l in span],
            "layer_ids": span, "keys": keys, "nodes": names,
        })

    if not edges:
        return {"segments": [], "terminals": {}, "vias": []}

    # 邻接（含过孔合并）
    adj = {}
    for i, e in enumerate(edges):
        ra, rb = uf.find(e["a"]), uf.find(e["b"])
        if ra == rb and e["a"] != e["b"]:
            # 同一超级节点内部（过孔短接）→ 忽略零长边
            continue
        adj.setdefault(ra, []).append((rb, i))
        adj.setdefault(rb, []).append((ra, i))

    # 端子识别：焊盘 or 悬空端点
    pad_at = {}
    for p in pd:
        k = uf.find(node_of(p["x"], p["y"], p["layer_id"])) if p.get("layer_id") else None
        cand = None
        for e in edges:
            for endk in (e["a"], e["b"]):
                n = nodes[endk]
                if _dist((p["x"] * scale, p["y"] * scale), (n["x"], n["y"])) <= 0.15:
                    cand = uf.find(endk)
                    break
            if cand:
                break
        if cand:
            pad_at.setdefault(cand, []).append(p)
        elif k is not None:
            pad_at.setdefault(k, []).append(p)

    super_deg = {k: len(v) for k, v in adj.items()}

    terms = {}          # super-node -> name
    orphan_ends = []    # 没落在焊盘上的走线端点（汇总后统一告警）
    counters = {"B": 0, "E": 0}
    base = _spice_token(name)
    for k, deg in super_deg.items():
        if k in via_names:
            continue        # 过孔节点已由 via_names 命名，不能再被当成 E/B 端子
        if k in pad_at:
            p = sorted(pad_at[k], key=lambda x: (x["component"], x["pin"]))[0]
            terms[k] = f"{p['component']}.{p['pin']}"
        elif deg == 1:
            counters["E"] += 1
            terms[k] = f"__AUTO_{base}_E{counters['E']}"
            orphan_ends.append((nodes[k]["x"], nodes[k]["y"], terms[k]))
        elif deg >= 3:
            counters["B"] += 1
            terms[k] = f"__AUTO_{base}_B{counters['B']}"

    # 走路径：在"显著节点"（焊盘/端点/分支/过孔）之间走，其余 degree-2 节点合并。
    # 关键点：端点必须取**遍历的真实首尾**，不能用边的存储方向（曾因此把 from/to 弄反）。
    visited = set()
    sig = set(terms) | set(via_names)
    name_cache = {}

    def name_for(k):
        if k in name_cache:
            return name_cache[k]
        if k in terms:
            nm = terms[k]
        elif k in via_names:
            nm = via_names[k]
        else:
            counters["N"] = counters.get("N", 0) + 1
            nm = f"__AUTO_{base}_N{counters['N']}"
        name_cache[k] = nm
        return nm

    def walk_out(from_key, first_edge):
        """从 from_key 出发沿 first_edge 一直走到某个显著节点（或走不动为止）。"""
        path, prev, cur, ei = [], from_key, from_key, first_edge
        while True:
            path.append(ei)
            visited.add(ei)
            e = edges[ei]
            ra, rb = uf.find(e["a"]), uf.find(e["b"])
            nxt = rb if ra == cur else ra
            prev, cur = cur, nxt
            if cur in sig or cur not in adj:
                break
            cand = [(n2, e2) for n2, e2 in adj.get(cur, [])
                    if e2 not in visited and n2 != prev]
            if not cand:
                break
            ei = cand[0][1]
        return path, cur

    segments = []
    # 从每个显著节点"顺流而下"，保证每条 segment 的两端都是显著节点
    for s_key in sorted(sig):
        for _nb, ei in adj.get(s_key, []):
            if ei in visited:
                continue
            path, end_key = walk_out(s_key, ei)
            segments.extend(_path_to_segments(path, edges, s_key, end_key,
                                              name_for, layer_name, scale))
    # 剩下的是完全没有显著节点的孤岛（画了一半的走线），从任意端开始走完
    for i in range(len(edges)):
        if i in visited:
            continue
        e = edges[i]
        st = uf.find(e["a"])
        path, end_key = walk_out(st, i)
        segments.extend(_path_to_segments(path, edges, st, end_key,
                                          name_for, layer_name, scale))

    if orphan_ends:
        head = ", ".join(f"{n}@({x:.1f},{y:.1f})" for x, y, n in orphan_ends[:4])
        warnings.append(
            f"net {name}: {len(orphan_ends)} 个走线端点没落在焊盘上（可能是测试点/铺铜连接/"
            f"未完成布线）: {head}{' ...' if len(orphan_ends) > 4 else ''}")
    terminals = {}
    for k, nm in terms.items():
        terminals[nm] = {
            "x_mm": round(nodes[k]["x"], 4),
            "y_mm": round(nodes[k]["y"], 4),
            "layer": layer_name(nodes[k]["layer"]),
            "kind": "pad" if any(p["component"] in nm for p in pad_at.get(k, [])) else "auto",
        }
    return {
        "segments": segments,
        "terminals": terminals,
        "vias": [{k: v for k, v in r.items() if k != "keys"} for r in via_recs],
        "total_length_mm": round(sum(s["length_mm"] for s in segments), 4),
    }


def _path_to_segments(path_edges, edges, start_key, end_key, name_for, layer_name, scale):
    """把一条路径按层变化切成若干 segment（过孔处断开），端点用显式首尾。"""
    if not path_edges:
        return []
    groups, cur, last_layer = [], [], None
    for ei in path_edges:
        e = edges[ei]
        if last_layer is not None and e["layer"] != last_layer:
            groups.append(cur)
            cur = []
        cur.append(ei)
        last_layer = e["layer"]
    if cur:
        groups.append(cur)

    out = []
    for gi, grp in enumerate(groups):
        first, last = edges[grp[0]], edges[grp[-1]]
        length = sum(edges[i]["length"] for i in grp)
        widths = sorted({round(edges[i]["width"] * scale, 4) for i in grp})
        if gi == 0:
            a_key = start_key
        else:                                  # 层变化处 = 过孔节点
            a_key = _other_end(first, groups[gi - 1][-1], edges)
        if gi == len(groups) - 1:
            b_key = end_key
        else:
            b_key = _other_end(last, grp[-2] if len(grp) > 1 else grp[0], edges)
        fa = name_for(a_key) if a_key is not None else "?"
        fb = name_for(b_key) if b_key is not None else "?"
        out.append({
            "from": fa, "to": fb,
            "layer": layer_name(first["layer"]),
            "length_mm": round(length, 4),
            "width_mm": widths[0] if len(widths) == 1 else widths,
            "via_count": sum(1 for nm in (fa, fb) if "_V" in nm and nm.startswith("__AUTO_")),
        })
    return out


def _other_end(edge, neighbour_edge_idx, edges):
    """层 변화断点：取与上一条边不共享的那个端点（即过孔位置）。"""
    n = edges[neighbour_edge_idx]
    shared = {edge["a"], edge["b"]} & {n["a"], n["b"]}
    if not shared:
        return edge["a"]
    for k in (edge["a"], edge["b"]):
        if k not in shared:
            return k
    return edge["a"]


def _via_node_name(node, via_recs, layer):
    for v in via_recs:
        if abs(v["x_mm"] - node["x"]) < 0.01 and abs(v["y_mm"] - node["y"]) < 0.01:
            return v.get("node_name") or f"__AUTO_VIA_{int(v['x_mm'] * 100)}_{int(v['y_mm'] * 100)}"
    return f"__AUTO_NODE_{int(node['x'] * 100)}_{int(node['y'] * 100)}"


def _spice_token(name):
    import re
    return re.sub(r"[^0-9A-Za-z_]", "_", str(name)).strip("_") or "NET"


# --------------------------------------------------------------------------
# 3. CLI
# --------------------------------------------------------------------------
def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        sys.exit(2)
    src = argv[0]
    out = None
    net = []
    unit = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--out":
            out = argv[i + 1]; i += 2
        elif a == "--net":
            net.append(argv[i + 1]); i += 2
        elif a == "--unit":
            unit = argv[i + 1]; i += 2
        else:
            if out is None:
                out = a
            i += 1
    out = out or "PCB_GEOMETRY.json"
    raw = load_pcb_input(src, unit=unit)
    geo = build_geometry(raw, net=net or None)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(geo, f, ensure_ascii=False, indent=1)
    print(f"PCB geometry written: {out}")
    print(f"  layers: {len(geo['layers'])}  nets: {len(geo['nets'])}  "
          f"unit: {geo['units']['internal']} → mm")
    for nm, n in geo["nets"].items():
        print(f"  [{nm}] segments={len(n['segments'])} "
              f"length={n.get('total_length_mm')} mm vias={len(n['vias'])} "
              f"terminals={len(n['terminals'])}")
    for w in geo["warnings"][:10]:
        print(f"  [warn] {w}")


if __name__ == "__main__":
    main()
