#!/usr/bin/env python3
"""PCB 几何：依赖 skill 的 `pcb_geometry.json` → 本 skill 的 PCB_GEOMETRY.json。

**本模块不解析 .epro2。** 容器解包、记录流 ticket 归约、焊盘坐标变换都由
`leonskill-easyeda-project-analysis` 负责，产物是它的 `pcb_geometry.json`：

    segments[]  {net, layer_id, x1, y1, x2, y2, width}   逐段走线（mil）
    pads[]      {component, pin, net, x, y, layer_id, w, h}  焊盘绝对坐标（mil）
    vias[]      {net, x, y, hole, diameter, type}
    layers[]    {id, type, name}
    stackup[]   {id, raw}                                 LAYER_PHYS 原始记录
    outlines[]  {min_x, max_x, min_y, max_y, width_mil, height_mil, doc}  逐板框
    outline     {min_x, max_x, min_y, max_y, width_*, height_*, boards}   所有板框并集

字段承诺与变更流程见依赖方的 `references/output-contract.md`。

`raw_from_analysis()` 把上面这套改名成本模块的 raw 图元
（tracks/vias/pads/layers/stackup_raw/extent），而不是重新解释记录。

输入单位是 **mil**（依赖实测口径，与 CANVAS.unit 写的 mm 无关）；
`stackup.py` 负责判断叠层缺项（STACKUP_INCOMPLETE），绝不默认 FR4=4.5。

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
# 1. 输入：依赖 skill 的 PCB 几何产物
# --------------------------------------------------------------------------
def raw_from_analysis(geo):
    """依赖 skill 的 pcb_geometry.json → 本模块的 raw 图元。

    依赖给的是逐图元 `segments` / `pads`（单位 mil），这里只做字段改名，
    不重新做坐标变换：焊盘绝对坐标已在依赖里算好。
    丝印/机械层的 LINE 会被剔除——它们不是铜，混进来会造出假网络。
    """
    layers = [{"id": int(l["id"]), "type": (l.get("type") or "").upper(),
               "name": l.get("name") or f"Layer{l['id']}", "use": True}
              for l in geo.get("layers") or [] if _as_int(l.get("id")) is not None]
    layer_type = {l["id"]: l["type"] for l in layers}
    warnings = list(geo.get("warnings") or [])

    tracks = []
    for seg in geo.get("segments") or []:
        lid = _as_int(seg.get("layer_id"))
        if lid is None or layer_type.get(lid) in NON_COPPER_LAYER_TYPES:
            continue
        tracks.append({
            "net": seg.get("net") or "", "layer_id": lid,
            "x1": float(seg.get("x1") or 0.0), "y1": float(seg.get("y1") or 0.0),
            "x2": float(seg.get("x2") or 0.0), "y2": float(seg.get("y2") or 0.0),
            "width": float(seg.get("width") or 0.0),
        })

    vias = [{
        "net": v.get("net") or "",
        "x": float(v.get("x") or 0.0), "y": float(v.get("y") or 0.0),
        "drill": float(v.get("hole") or 0.0), "pad": float(v.get("diameter") or 0.0),
        "type": (v.get("type") or "NORMAL").upper(),
        "unused": [],
    } for v in geo.get("vias") or []]

    pads = []
    for p in geo.get("pads") or []:
        lid = _as_int(p.get("layer_id"))
        if lid is None or not p.get("component"):
            continue
        pads.append({
            "component": p["component"], "pin": str(p.get("pin")),
            "net": p.get("net"), "x": float(p.get("x") or 0.0),
            "y": float(p.get("y") or 0.0), "layer_id": lid,
            "w": p.get("w"), "h": p.get("h"), "drill": None,
        })

    # 板框：依赖给 outlines（逐板 min/max）与 outline（并集）；只有都拿不到时才
    # 退回走线范围——那是铜皮范围，不是板子范围，必须留 warning 让结论可追溯。
    def _box_range(item):
        if not isinstance(item, dict):
            return None
        if all(k in item for k in ("min_x", "max_x", "min_y", "max_y")):
            return item
        return None

    boxes = [b for b in (_box_range(x) for x in (geo.get("outlines") or [])) if b]
    union = _box_range(geo.get("outline"))
    if boxes:
        extent = {"x": [min(b["min_x"] for b in boxes), max(b["max_x"] for b in boxes)],
                  "y": [min(b["min_y"] for b in boxes), max(b["max_y"] for b in boxes)]}
    elif union:
        extent = {"x": [union["min_x"], union["max_x"]],
                  "y": [union["min_y"], union["max_y"]]}
    else:
        xs = [t["x1"] for t in tracks] + [t["x2"] for t in tracks]
        ys = [t["y1"] for t in tracks] + [t["y2"] for t in tracks]
        extent = {"x": [min(xs), max(xs)] if xs else [0, 0],
                  "y": [min(ys), max(ys)] if ys else [0, 0]}
        if xs or ys:
            warnings.append("依赖未提供板框范围，extent 由走线范围推算（不是板子尺寸）")

    # 叠层：依赖给 LAYER_PHYS 原始记录，字段名与 stackup.py 的约定不同，必须归一。
    # 透传原始记录会让 stackup 判定"没有介质"（er/loss_tangent 全是 None）。
    stackup_raw = []
    for i, item in enumerate(geo.get("stackup") or []):
        rec = item.get("raw") if isinstance(item, dict) else None
        if not isinstance(rec, dict):
            continue
        stackup_raw.append({
            "z": rec.get("zIndex") if rec.get("zIndex") is not None else i,
            "material": rec.get("material"),
            "thickness": rec.get("thickness"),
            "er": rec.get("permittivity"),
            "loss_tangent": rec.get("lossTangent"),
        })
    stackup_raw.sort(key=lambda x: x["z"])

    return {
        "source": geo.get("source") or "pcb_geometry.json",
        "project": geo.get("project") or "",
        "pcb_title": geo.get("pcb_title") or "",
        "unit": geo.get("unit") or DEFAULT_UNIT,
        "canvas_unit": None,
        "layers": layers,
        "stackup_raw": stackup_raw,
        "tracks": tracks,
        "vias": vias,
        "pads": pads,
        "extent": extent,
        "warnings": warnings,
    }


def load_pcb_input(path, unit=None):
    """读 PCB 几何输入（只接受 JSON）。

    `.epro2` 不再在本 skill 里解析——先用依赖 skill 生成 pcb_geometry.json：
        python scripts/analysis_dep.py <工程.epro2> <产物目录>
    两种 JSON 都认：
      * 依赖的 pcb_geometry.json（有 segments/pads）→ raw_from_analysis
      * 本模块的 raw 格式（tracks/vias/pads）→ 原样使用
    """
    low = path.lower()
    if low.endswith((".epro2", ".epro")):
        raise PcbError(
            "PCB_INPUT_NOT_PARSED",
            "本 skill 不再直接解析 .epro2；请先运行 "
            "`python scripts/analysis_dep.py <工程.epro2> <产物目录>`，"
            "再把 --pcb 指向产出的 pcb_geometry.json")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw.get("nets"), dict):
        raise PcbError("PCB_INPUT_BUILT",
                       f"{os.path.basename(path)} 已是建好的拓扑（含 nets），"
                       "不能再当作 raw 图元读入")
    if raw.get("tracks") is None and (raw.get("segments") or raw.get("traces")):
        return raw_from_analysis(raw)
    for k in ("tracks", "vias", "pads", "layers"):
        raw.setdefault(k, [])
    raw.setdefault("unit", unit or DEFAULT_UNIT)
    raw.setdefault("stackup_raw", [])
    raw.setdefault("warnings", [])
    raw.setdefault("source", os.path.basename(path))
    return raw


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
