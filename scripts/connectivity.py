#!/usr/bin/env python3
"""连通性引擎：坐标精确匹配 + 线段判定 + 并查集，把 IR 的元件引脚/导线/网络标志归组为网络。

用法（作为模块）:
  from connectivity import build_nets
  pin_net = build_nets(ir)   # 填充 ir["nets"]，返回 {designator: {pinNumber: netName}}

算法要点（easyeda-schematic-audit §3.5 实战经验）：
  - 坐标 key 用 round(2)，消除浮点噪声（324.99999999 -> 325.0）
  - 只认几何上真正接触的两种连接：点与点重合（±0.01）、点在段上（含导线顶点落在另一段上，即 EDA junction 语义）
  - 不做 ±1 以上容差的朴素匹配（会误短接平行引线）
  - 同名网络（网标/网名）全局合并
"""
import math


def _key(x, y):
    return f"{round(float(x), 2)},{round(float(y), 2)}"


def _dist(p, q):
    return math.hypot(p[0] - q[0], p[1] - q[1])


def _on_segment(p, a, b, eps=0.02):
    """点 p 是否严格落在线段 a-b 上（不含端点）。"""
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > eps:
        return False
    dot = (p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])
    sq = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2
    if sq == 0:
        return False
    t = dot / sq
    return 0.001 < t < 0.999


class UF:
    def __init__(self):
        self.parent = {}

    def add(self, k):
        if k not in self.parent:
            self.parent[k] = k

    def find(self, k):
        self.add(k)
        root = k
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[k] != root:
            self.parent[k], k = root, self.parent[k]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_nets(ir):
    uf = UF()
    segs = []          # (akey, bkey, wire_id)
    wire_net = {}      # wire_id -> net name
    points = {}        # key -> (x, y)

    def add_point(x, y):
        k = _key(x, y)
        points.setdefault(k, (round(float(x), 2), round(float(y), 2)))
        uf.add(k)
        return k

    # 1) 导线 → 线段 + 顶点并集
    for w in ir.get("wires", []):
        wid = w.get("id") or f"w{len(segs)}"
        if w.get("net"):
            wire_net[wid] = w["net"]
        line = w.get("line") or []
        pts = [(line[i], line[i + 1]) for i in range(0, len(line) - 1, 2)]
        keys = [add_point(x, y) for x, y in pts]
        for i in range(len(keys) - 1):
            uf.union(keys[i], keys[i + 1])
            segs.append((keys[i], keys[i + 1], wid))
    # epro2 侧：独立 LINE 图元挂 lineGroup
    for ln in ir.get("lines", []) or []:
        wid = ln.get("lineGroup") or ln.get("id") or "line"
        p, q = ln.get("p"), ln.get("q")
        if not p or not q:
            continue
        ka, kb = add_point(*p), add_point(*q)
        uf.union(ka, kb)
        segs.append((ka, kb, wid))

    # 2) 导线顶点落在另一线段上 → junction 语义连通
    for k, (x, y) in points.items():
        for (a, b, wid) in segs:
            pa, pb = points[a], points[b]
            if _on_segment((x, y), pa, pb):
                uf.union(k, a)

    # 3) 引脚 / 网标坐标接入
    pin_index = []   # (key, designator, pinNumber, pinName, compType, netName)
    for c in ir.get("components", []):
        ct = c.get("componentType")
        des = c.get("designator") or c.get("name") or "(anon)"
        if ct in ("netflag", "netport"):
            k = add_point(c.get("x"), c.get("y"))
            pin_index.append((k, des, None, None, ct, c.get("net") or c.get("name")))
            continue
        if ct != "part":
            continue
        for p in c.get("pins") or []:
            k = add_point(p.get("x"), p.get("y"))
            pin_index.append((k, des, p.get("number"), p.get("name"), ct, None))

    # 4) 引脚精确落点 → 与已有几何连接（含落在线段中段的情况）
    for (k, des, num, pname, ct, netname) in pin_index:
        x, y = points[k]
        for (a, b, wid) in segs:
            pa, pb = points[a], points[b]
            if _dist((x, y), pa) < 0.02 or _dist((x, y), pb) < 0.02:
                uf.union(k, a)
            elif _on_segment((x, y), pa, pb):
                uf.union(k, a)

    # 5) 归组：每组收集成员与网名
    groups = {}
    for (k, des, num, pname, ct, netname) in pin_index:
        root = uf.find(k)
        g = groups.setdefault(root, {"pins": [], "names": [], "types": set()})
        if ct == "part":
            g["pins"].append({"component": des, "pin": num, "pinName": pname})
        else:
            g["types"].add(ct)
        if netname:
            g["names"].append(netname)
    for wid, net in wire_net.items():
        for (a, b, w2) in segs:
            if w2 == wid:
                root = uf.find(a)
                if root in groups:
                    groups[root]["names"].append(net)
                break

    # 6) 组内同名去重 + 冲突告警
    warnings = ir.setdefault("warnings", [])
    named_groups = []
    for root, g in groups.items():
        uniq = []
        for n in g["names"]:
            if n and n not in uniq:
                uniq.append(n)
        g["uniq_names"] = uniq
        if len(uniq) > 1:
            warnings.append(f"网络命名冲突：{uniq}（取首个）")
        named_groups.append((root, g))

    # 7) 同名网络全局合并（如两处 +12V 标志）
    name_root = {}
    for root, g in named_groups:
        if g["uniq_names"]:
            nm = g["uniq_names"][0]
            if nm in name_root:
                uf.union(name_root[nm], root)
            else:
                name_root[nm] = root

    # 8) 重新归并（同名合并后）
    merged = {}
    for root, g in named_groups:
        r2 = uf.find(root)
        m = merged.setdefault(r2, {"pins": [], "names": [], "types": set()})
        m["pins"].extend(g["pins"])
        m["types"] |= g["types"]
        for n in g["uniq_names"]:
            if n not in m["names"]:
                m["names"].append(n)

    # 9) 输出 nets
    nets = []
    auto_i = 0
    for root, m in merged.items():
        has_name = bool(m["names"])
        if not m["pins"] and not has_name:
            continue
        if has_name:
            name, origin = m["names"][0], ("netflag" if ("netflag" in m["types"]) else "wire")
        else:
            auto_i += 1
            name, origin = f"N{auto_i}", "auto"
        nets.append({
            "name": name,
            "origin": origin,
            "members": sorted(m["pins"], key=lambda p: (p["component"], str(p["pin"]))),
            "pinCount": len(m["pins"]),
        })

    # 排序：有名网络在前，其后按引脚数降序
    nets.sort(key=lambda n: (n["origin"] == "auto", -n["pinCount"], n["name"]))
    ir["nets"] = nets

    # 10) 引脚 → 网名 索引
    pin_net = {}
    for n in nets:
        for m in n["members"]:
            pin_net.setdefault(m["component"], {})[str(m["pin"])] = n["name"]
    return pin_net


if __name__ == "__main__":
    import json
    import sys
    sys.path.insert(0, __file__.rsplit("\\", 1)[0])
    from ir_common import load_ir, save_ir

    ir = load_ir(sys.argv[1])
    pin_net = build_nets(ir)
    save_ir(ir, sys.argv[2])
    print(f"nets: {len(ir['nets'])}")
    for n in ir["nets"]:
        pins = ", ".join(f"{m['component']}.{m['pin']}" for m in n["members"])
        print(f"  [{n['origin']:8s}] {n['name']:12s} ({n['pinCount']:2d})  {pins}")
