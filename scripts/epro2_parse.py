#!/usr/bin/env python3
"""嘉立创EDA专业版工程文件（.epro2）离线解析器：.epro2 → IR。

用法:
  python epro2_parse.py <工程.epro2> <out_ir.json> [页名或页uuid]

格式事实（实测 4.1.x）:
  - .epro2 = ZIP：project2.json + <工程名>.epru（纯文本日志；ZIP 成员名为 GBK 编码）
  - .epru 每行 `{"type","id","ticket"}||{payload}|`；同 type+id 取 ticket 最大；DOCHEAD 分文档段
  - docType: SCH_PAGE / SIMULATION(仿真页) / SYMBOL / DEVICE / PCB / ...
  - WIRE 是逻辑组（无几何），几何在 LINE 图元（lineGroup=WIRE.id）；线端坐标为 EDA 坐标的 Y 取负
  - COMPONENT{x,y,rotation,isMirror,attrs{DeviceName: JSON}}；组件属性是 ATTR(parentId=COMPONENT.id)
  - SYMBOL: PIN{x,y,length,rotation}（x,y 为连接端）+ ATTR(Pin Name/Pin Number)；META.docType 18=NetFlag
"""
import json
import math
import re
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ir_common import new_ir, normalize, save_ir, validate  # noqa: E402

Y_SIGN = -1.0          # 文件坐标 y → IR 坐标 y 的符号（实测 .epru 中 y 为 EDA 值的负数）
ROT_SIGN = -1.0        # 文件坐标系（Y 翻转）下的旋转方向修正


def decode_name(fn):
    for enc in ("gbk", "utf-8"):
        try:
            return fn.encode("cp437").decode(enc)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return fn


def open_epro2(path):
    if not zipfile.is_zipfile(path):
        raise ValueError("不是有效的 .epro2（ZIP）文件；旧版 .epro 请在 EDA 中另存为 .epro2")
    zf = zipfile.ZipFile(path)
    epru_info = None
    for info in zf.infolist():
        if decode_name(info.filename).endswith(".epru"):
            epru_info = info
            break
    if not epru_info:
        raise ValueError("未找到 .epru 工程日志（可能是旧版 .epro 目录式包）")
    text = zf.read(epru_info).decode("utf-8", "replace")
    project_meta = {}
    for info in zf.infolist():
        if decode_name(info.filename) == "project2.json":
            try:
                project_meta = json.loads(zf.read(info).decode("utf-8"))
            except Exception:
                pass
    return text, project_meta


def parse_records(text):
    """按 DOCHEAD 分段，段内 (type,id) 按 ticket 归约。"""
    docs = []
    cur = None
    for ln in text.split("\n"):
        s = ln.strip()
        if not s or "||" not in s:
            continue
        if s.endswith("|"):
            s = s[:-1]
        head_s, _, payload_s = s.partition("||")
        try:
            head = json.loads(head_s)
        except json.JSONDecodeError:
            continue
        t = head.get("type")
        rid = head.get("id")
        ticket = head.get("ticket") or 0
        if t == "DOCHEAD":
            try:
                payload = json.loads(payload_s)
            except json.JSONDecodeError:
                payload = {}
            cur = {"docType": payload.get("docType"), "uuid": payload.get("uuid"),
                   "editVersion": payload.get("editVersion"), "elems": {}}
            docs.append(cur)
            continue
        if cur is None:
            continue
        payload = None
        if payload_s.strip() not in ('""', ""):
            try:
                payload = json.loads(payload_s)
            except json.JSONDecodeError:
                payload = None
        key = (t, rid)
        prev = cur["elems"].get(key)
        if prev is None or ticket >= prev[0]:
            cur["elems"][key] = (ticket, payload)
    return docs


def doc_elements(doc):
    """返回 (按类型分组的 payload 列表, META)。payload 为 None（已删除）的跳过。"""
    by_type = {}
    meta = {}
    for (t, rid), (ticket, payload) in doc["elems"].items():
        if payload is None:
            continue
        if t == "META":
            meta = payload
            continue
        by_type.setdefault(t, []).append((rid, payload))
    return by_type, meta


def build_symbols(docs):
    """uuid → {docType, pins: {pinId: {number,name,x,y,rotation,length}}, globalNetName, title}"""
    symbols = {}
    for d in docs:
        if d["docType"] != "SYMBOL":
            continue
        by_type, meta = doc_elements(d)
        pins = {}
        for rid, p in by_type.get("PIN", []):
            pins[rid] = {
                "x": p.get("x") or 0.0, "y": p.get("y") or 0.0,
                "rotation": p.get("rotation") or 0, "length": p.get("length") or 0,
                "number": None, "name": None,
            }
        gnn = None
        for rid, a in by_type.get("ATTR", []):
            key = a.get("key")
            parent = a.get("parentId")
            if parent and parent in pins:
                if key == "Pin Number":
                    pins[parent]["number"] = a.get("value")
                elif key == "Pin Name":
                    pins[parent]["name"] = a.get("value")
            elif not parent and key == "Global Net Name":
                gnn = a.get("value")
        symbols[d["uuid"]] = {
            "docType": meta.get("docType"), "pins": pins, "globalNetName": gnn,
            "title": meta.get("title") or "",
        }
    return symbols


def build_devices(docs):
    """uuid → {title, attributes{}}"""
    devices = {}
    for d in docs:
        if d["docType"] != "DEVICE":
            continue
        for (t, rid), (ticket, payload) in d["elems"].items():
            if t == "META" and payload:
                devices[d["uuid"]] = {
                    "title": payload.get("title") or "",
                    "attributes": payload.get("attributes") or {},
                }
    return devices


def transform_pin(px, py, comp):
    """符号内坐标 → 器件绝对坐标（文件坐标系）。旋转→镜像→平移。"""
    th = math.radians((comp.get("rotation") or 0) * ROT_SIGN)
    x = px * math.cos(th) - py * math.sin(th)
    y = px * math.sin(th) + py * math.cos(th)
    if comp.get("isMirror"):
        x = -x
    return x + (comp.get("x") or 0.0), y + (comp.get("y") or 0.0)


def _val_like(name):
    """Name 字段若是具体值（非 ={Value} 之类公式）则当作 Value 兜底。"""
    if not name or str(name).startswith("="):
        return None
    s = str(name).strip()
    return s if re.match(r"^[\d.]", s) else None


def comp_type_of(symbol, device):
    dt = (symbol or {}).get("docType")
    title = ((symbol or {}).get("title") or "") + " " + ((device or {}).get("title") or "")
    if dt == 18 or "NetFlag" in title or "网络标识" in title:
        return "netflag"
    if dt == 19:
        return "netport"
    if dt == 20 or "Drawing-Symbol" in title or "Sheet" in title:
        return "sheet"
    return "part"


def page_to_ir(doc, symbols, devices, page_name, project=None):
    by_type, meta = doc_elements(doc)
    ir = new_ir("epro2", project, {"uuid": doc["uuid"], "name": page_name or meta.get("title") or ""})

    attrs_by_parent = {}
    for rid, a in by_type.get("ATTR", []):
        pid = a.get("parentId")
        if pid:
            attrs_by_parent.setdefault(pid, {})[a.get("key")] = a.get("value")

    for rid, c in by_type.get("COMPONENT", []):
        inline = c.get("attrs") or {}
        ca = dict(attrs_by_parent.get(rid, {}))
        for k, v in inline.items():
            ca.setdefault(k, v)
        dev_uuid = None
        dn = inline.get("DeviceName")
        if dn:
            try:
                dev_uuid = json.loads(dn).get("uuid")
            except (json.JSONDecodeError, TypeError):
                pass
        if not dev_uuid:
            dev_uuid = ca.get("Device")
        dev = devices.get(dev_uuid) or {}
        dattrs = dev.get("attributes") or {}
        sym_uuid = ca.get("Symbol") or dattrs.get("Symbol")
        sym = symbols.get(sym_uuid) or {}
        ctype = comp_type_of(sym, dev)
        comp = {
            "id": rid,
            "componentType": ctype,
            "designator": ca.get("Designator") or dattrs.get("Designator"),
            "name": dev.get("title") or ca.get("Name"),
            "deviceUuid": dev_uuid,
            "deviceTitle": dev.get("title") or ca.get("Name"),
            "manufacturerPart": ca.get("Manufacturer Part") or dattrs.get("Manufacturer Part") or dattrs.get("Name"),
            "value": (ca.get("Value") or dattrs.get("Value") or
                      _val_like(ca.get("Name")) or _val_like(dattrs.get("Name"))),
            "x": (c.get("x") or 0.0) * 1.0,
            "y": Y_SIGN * (c.get("y") or 0.0),
            "rotation": c.get("rotation") or 0,
            "mirror": bool(c.get("isMirror")),
            "pins": [],
            "attrs": {k: v for k, v in ca.items() if v is not None},
        }
        for pid_, p in (sym.get("pins") or {}).items():
            ax, ay = transform_pin(p["x"], p["y"], c)
            comp["pins"].append({
                "number": p.get("number"), "name": p.get("name"),
                "x": ax, "y": Y_SIGN * ay,
            })
        if ctype in ("netflag", "netport"):
            comp["net"] = (ca.get("Global Net Name") or sym.get("globalNetName")
                           or ca.get("Name") or dattrs.get("Name"))
        ir["components"].append(comp)

    # 导线：WIRE 逻辑组 + LINE 几何
    wire_net = {}
    for rid, w in by_type.get("WIRE", []):
        name = attrs_by_parent.get(rid, {}).get("NET") or ""
        wire_net[rid] = name
    for rid, ln in by_type.get("LINE", []):
        grp = ln.get("lineGroup")
        if grp not in wire_net:
            continue
        line = [ln.get("startX") or 0.0, Y_SIGN * (ln.get("startY") or 0.0),
                ln.get("endX") or 0.0, Y_SIGN * (ln.get("endY") or 0.0)]
        if abs(line[0] - line[2]) < 1e-9 and abs(line[1] - line[3]) < 1e-9:
            continue
        ir["wires"].append({"id": rid, "line": line, "net": wire_net.get(grp) or ""})

    normalize(ir)
    return ir


def parse_file(path, page_key=None):
    text, project_meta = open_epro2(path)
    docs = parse_records(text)
    symbols = build_symbols(docs)
    devices = build_devices(docs)
    proj_name = (project_meta.get("title") or project_meta.get("name") or
                 os.path.splitext(os.path.basename(path))[0])
    pages = [d for d in docs if d["docType"] in ("SCH_PAGE", "SIMULATION")]
    if not pages:
        raise ValueError("工程中没有原理图页/仿真页文档")
    target = None
    for d in pages:
        by_type, meta = doc_elements(d)
        title = meta.get("title") or ""
        if page_key and (d["uuid"] == page_key or title == page_key):
            target = d
            break
    if target is None:
        target = next((d for d in pages if d["docType"] == "SCH_PAGE"), pages[0])
    by_type, meta = doc_elements(target)
    ir = page_to_ir(target, symbols, devices, meta.get("title"), project=proj_name)
    ir["page"]["docType"] = target["docType"]
    ir["_pages"] = []
    for d in pages:
        _, m = doc_elements(d)
        ir["_pages"].append({"uuid": d["uuid"], "name": m.get("title") or "", "docType": d["docType"]})
    return ir


def main():
    if len(sys.argv) < 3:
        print("usage: epro2_parse.py <project.epro2> <out_ir.json> [page_name|page_uuid]")
        sys.exit(2)
    path, out = sys.argv[1], sys.argv[2]
    page_key = sys.argv[3] if len(sys.argv) > 3 else None
    ir = parse_file(path, page_key)
    save_ir(ir, out)
    print(f"IR written: {out}")
    print(f"  project: {ir.get('project')}  page: {(ir.get('page') or {}).get('name')} "
          f"({(ir.get('page') or {}).get('docType')})")
    print(f"  components: {len(ir['components'])}  wires: {len(ir['wires'])}")
    if ir.get("_pages"):
        print("  pages:", ", ".join(f"{p['name']}({p['docType']})" for p in ir["_pages"]))
    for w in validate(ir):
        print("  WARN:", w)


if __name__ == "__main__":
    main()
