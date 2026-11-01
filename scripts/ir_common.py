#!/usr/bin/env python3
"""IR（中间表示）定义与转换工具。

IR 是 bridge 在线提取与 .epro2 离线解析两个前端的统一输出格式，
供 connectivity.py / netlist_gen.py / svg_schematic.py 消费。

用法:
  from ir_common import from_bridge_extract, load_ir, save_ir, normalize
"""
import json
import math
import re

IR_VERSION = 1


def _r(v, nd=2):
    """坐标归一化：消除浮点噪声（324.99999999 -> 325）。"""
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return v


def clean_value(v):
    """清洗元件值：删 Ω/欧姆符号、µ→u、统一空白。"""
    if v is None:
        return None
    s = str(v).strip()
    s = s.replace("Ω", "").replace("欧", "").replace("µ", "u").replace("μ", "u")
    s = re.sub(r"\s+", "", s)
    return s


def new_ir(source, project=None, page=None):
    return {
        "version": IR_VERSION,
        "source": source,
        "project": project,
        "page": page or {},
        "components": [],
        "wires": [],
        "lines": [],
        "nets": [],
        "warnings": [],
    }


def _attr_pairs_to_dict(attr_pairs):
    d = {}
    for ap in attr_pairs or []:
        k = ap.get("k")
        if k:
            d[k] = ap.get("v")
    return d


def from_bridge_extract(data, project=None, page=None):
    """把工作区 extract_*.js 的输出（{nComps,data:{components,wires,...}}）转成 IR。

    extract 结构：
      data.components[]: {ct, des, name, x, y, rot, mir, lu, cu, attrPairs[{k,v}], pins[{n,num,x,y,net}]}
      data.wires[]: {id, line[], net}
    """
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    ir = new_ir("bridge", project, page)
    for c in data.get("components", []):
        ct = c.get("ct") or c.get("componentType") or "part"
        attrs = _attr_pairs_to_dict(c.get("attrPairs") or c.get("attrs"))
        comp = {
            "id": c.get("id"),
            "componentType": ct,
            "designator": c.get("des") or attrs.get("Designator"),
            "name": c.get("name"),
            "deviceUuid": attrs.get("Device") or c.get("cu"),
            "deviceTitle": attrs.get("Name") or c.get("name"),
            "manufacturerPart": attrs.get("Manufacturer Part"),
            "value": clean_value(attrs.get("Value")),
            "x": _r(c.get("x")),
            "y": _r(c.get("y")),
            "rotation": c.get("rot") or 0,
            "mirror": bool(c.get("mir")),
            "pinPrefix": None,
            "pins": [],
            "attrs": {k: v for k, v in attrs.items()
                      if k in ("Value", "Manufacturer Part", "Name", "Device", "Symbol", "designator")},
        }
        if ct == "netflag":
            comp["net"] = c.get("net") or c.get("name")
        for p in c.get("pins") or []:
            comp["pins"].append({
                "number": str(p.get("num")) if p.get("num") is not None else None,
                "name": p.get("n"),
                "x": _r(p.get("x")),
                "y": _r(p.get("y")),
                "net": p.get("net"),
            })
        ir["components"].append(comp)
    for w in data.get("wires", []):
        line = [_r(v) for v in (w.get("line") or [])]
        ir["wires"].append({"id": w.get("id"), "line": line, "net": w.get("net") or ""})
    for ln in data.get("lines", []) or []:
        ir["lines"].append(ln)
    normalize(ir)
    return ir


def normalize(ir):
    """统一归一化：坐标 round、值清洗、补 pinPrefix。"""
    for c in ir.get("components", []):
        c["x"] = _r(c.get("x"))
        c["y"] = _r(c.get("y"))
        if c.get("value"):
            c["value"] = clean_value(c["value"])
        for p in c.get("pins", []):
            p["x"] = _r(p.get("x"))
            p["y"] = _r(p.get("y"))
        des = (c.get("designator") or "").strip()
        m = re.match(r"^[A-Za-z]+", des)
        c["pinPrefix"] = m.group(0) if m else None
    for w in ir.get("wires", []):
        w["line"] = [_r(v) for v in w.get("line", [])]
    return ir


def from_bridge_parts(comps, pins_by_id, wires, project=None, page=None):
    """合并三段式提取结果（comps / pins / wires 分别来自三次 bridge 调用）为 IR。

    comps: [{id, ct, des, name, x, y, rot, mir, attrPairs, lu, cu}]
    pins_by_id: {componentId: [{n, num, x, y, net}]}
    wires: [{id, line, net}]
    """
    for c in comps or []:
        c["pins"] = (pins_by_id or {}).get(c.get("id")) or []
    return from_bridge_extract(
        {"data": {"components": comps or [], "wires": wires or []}},
        project=project, page=page)


def load_ir(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_ir(ir, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ir, f, ensure_ascii=False, indent=1)


def validate(ir):
    """基本完整性检查，返回 warning 列表（不抛异常）。"""
    warn = []
    comps = ir.get("components", [])
    parts = [c for c in comps if c.get("componentType") == "part"]
    if not parts:
        warn.append("IR 中没有任何 part 元件")
    n_pins = sum(len(c.get("pins") or []) for c in parts)
    if parts and n_pins == 0:
        warn.append("part 元件均无引脚数据（提取可能失败）")
    if not ir.get("wires") and not ir.get("lines"):
        warn.append("IR 中没有任何导线/线段（连通性将仅依赖引脚坐标重合）")
    no_des = [c for c in parts if not c.get("designator")]
    if no_des:
        warn.append(f"{len(no_des)} 个元件缺少位号")
    return warn


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "--merge":
        # python ir_common.py --merge comps.json pins.json wires.json out_ir.json [project] [page]
        comps = json.load(open(sys.argv[2], encoding="utf-8"))
        pins = json.load(open(sys.argv[3], encoding="utf-8"))
        wires = json.load(open(sys.argv[4], encoding="utf-8"))
        project = sys.argv[6] if len(sys.argv) > 6 else None
        page = json.loads(sys.argv[7]) if len(sys.argv) > 7 else None
        ir = from_bridge_parts(comps, pins, wires, project=project, page=page)
        save_ir(ir, sys.argv[5])
        print(f"IR written: {sys.argv[5]}  components={len(ir['components'])} "
              f"wires={len(ir['wires'])}")
        for w in validate(ir):
            print("WARN:", w)
        sys.exit(0)
    if len(sys.argv) < 3:
        print("usage: ir_common.py <extract.json> <out_ir.json>")
        print("       ir_common.py --merge <comps.json> <pins.json> <wires.json> <out_ir.json>")
        sys.exit(2)
    raw = json.load(open(sys.argv[1], encoding="utf-8"))
    if isinstance(raw, dict) and "result" in raw:
        raw = json.loads(raw["result"])
    ir = from_bridge_extract(raw)
    save_ir(ir, sys.argv[2])
    print(f"IR written: {sys.argv[2]}  components={len(ir['components'])} wires={len(ir['wires'])}")
    for w in validate(ir):
        print("WARN:", w)
