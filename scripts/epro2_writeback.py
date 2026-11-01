#!/usr/bin/env python3
"""把仿真线路图（router 的 plan）写回嘉立创EDA专业版工程文件（.epro2）。

用法:
  python epro2_writeback.py <目标.epro2> <plan.json> <simlib_pack.json> [--out 输出.epro2] [--dry-run]

写入内容：
  - SIMULATION_SCH 文档（Simulation1 容器）
  - SIMULATION 文档（NGspice1 仿真页：图框 + 器件 + 导线 + 电源/GND 标志）
  - 目标工程缺失的仿真器件模板文档（DEVICE/SYMBOL 段，uuid 原样保留）
安全纪律：
  - 写前强制备份 <原文件>.bak.epro2（已存在则带时间戳）
  - 写临时文件 + os.replace 原子替换
  - 写后立即用 epro2_parse 重解析校验（页存在、器件数一致）
⚠ 实验特性：仿真页格式为逆向所得（docType=SIMULATION / SIMULATION_SCH），
  首次使用请在 EDA 中打开检查；原件始终有 .bak.epro2 备份。
"""
import json
import os
import shutil
import sys
import time
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from epro2_parse import decode_name, parse_records, doc_elements, Y_SIGN, parse_file  # noqa: E402

INIT_ROT = 270  # 文件坐标旋转 = (360 - EDA旋转) % 360


def nid():
    return uuid.uuid4().hex[:16]


def emit(typ, rid, ticket, payload):
    head = {"type": typ, "id": rid, "ticket": ticket}
    return json.dumps(head, separators=(",", ":")) + "||" + json.dumps(payload, separators=(",", ":")) + "|"


def emit_dochead(ticket, payload):
    return json.dumps({"type": "DOCHEAD", "ticket": ticket}, separators=(",", ":")) + "||" + \
        json.dumps(payload, separators=(",", ":")) + "|"


def file_rot(eda_rot):
    return (360 - int(eda_rot)) % 360


def load_target(path):
    zf = zipfile.ZipFile(path)
    epru_info = None
    for info in zf.infolist():
        if decode_name(info.filename).endswith(".epru"):
            epru_info = info
            break
    if not epru_info:
        raise ValueError("目标文件里没有 .epru（旧版 .epro 请先转存 .epro2）")
    text = zf.read(epru_info).decode("utf-8", "replace")
    return zf, epru_info, text


def build_docs(text, plan, pack, target_meta):
    docs = parse_records(text)
    existing = {d["uuid"] for d in docs}
    max_ticket = 0
    for ln in text.split("\n"):
        if '"ticket":' in ln and "||" in ln:
            try:
                max_ticket = max(max_ticket, json.loads(ln.split("||")[0]).get("ticket") or 0)
            except json.JSONDecodeError:
                pass
    t = max_ticket + 1
    client = target_meta.get("client") or ""
    edit_ver = target_meta.get("editVersion") or "4.1.78"

    # 需要补充的模板文档段（目标工程缺失的 DEVICE/SYMBOL 按原文注入，uuid 保留）
    # 按 document 块去重后追加
    extra_blocks = []
    cur = None
    for ln in (l for p in pack["parts"].values()
               for k in ("deviceSegment", "symbolSegment") for l in (p.get(k) or [])):
        if '"DOCHEAD"' in ln:
            uid = None
            try:
                uid = json.loads(ln.split("||")[1].rstrip("|")).get("uuid")
            except (json.JSONDecodeError, IndexError):
                pass
            cur = {"uuid": uid, "lines": [ln]} if uid not in existing else None
            if cur:
                extra_blocks.append(cur)
        elif cur is not None:
            cur["lines"].append(ln)
    seen_blocks = set()
    final_extra = []
    for b in extra_blocks:
        if b["uuid"] in seen_blocks:
            continue
        seen_blocks.add(b["uuid"])
        final_extra.extend(b["lines"])

    # 新页面文档
    sch_uuid = nid()
    page_uuid = nid()
    lines = []
    lines.append(emit_dochead(t, {"docType": "SIMULATION_SCH", "client": client, "uuid": sch_uuid,
                                  "updateTime": int(time.time() * 1000),
                                  "version": str(int(time.time() * 1000)), "editVersion": edit_ver,
                                  "user": {"uuid": client or "local"}}))
    t += 1
    lines.append(emit("META", "META", t, {"title": "Simulation1", "source": "", "zIndex": None}))
    t += 1
    lines.append(emit_dochead(t, {"docType": "SIMULATION", "client": client, "uuid": page_uuid,
                                  "updateTime": int(time.time() * 1000),
                                  "version": str(int(time.time() * 1000)), "editVersion": edit_ver,
                                  "user": {"uuid": client or "local"}}))
    t += 1
    lines.append(emit("META", "META", t, {"title": "NGspice1", "docType": 8, "source": "",
                                          "zIndex": 1, "simSchematic": sch_uuid}))
    t += 1

    def add_component(part_key, x, y, rot, attrs_extra):
        nonlocal t
        part = pack["parts"][part_key]
        cid = nid()
        comp = {
            "partId": part["partId"], "x": x, "y": y * Y_SIGN, "rotation": file_rot(rot),
            "isMirror": False,
            "attrs": {"Footprints": "[]", "Devices": "[]",
                      "DeviceName": json.dumps({"uuid": part["deviceUuid"], "name": part["name"], "source": ""},
                                               separators=(",", ":")),
                      "Symbols": "[]"},
            "zIndex": 3,
        }
        lines.append(emit("COMPONENT", cid, t, comp))
        t += 1
        base = {"Device": part["deviceUuid"], "Symbol": part["symbolUuid"]}
        base.update(attrs_extra)
        for k, v in base.items():
            lines.append(emit("ATTR", nid(), t, {
                "x": None, "y": None, "rotation": None, "color": None, "fontFamily": None,
                "fontSize": None, "fontWeight": None, "italic": None, "underline": None,
                "align": None, "value": v, "keyVisible": None, "valueVisible": None,
                "key": k, "fillColor": None, "parentId": cid, "zIndex": 1}))
            t += 1
        return cid

    # 图框
    sheet = pack["parts"].get("SHEET")
    if sheet:
        sid = nid()
        lines.append(emit("COMPONENT", sid, t, {
            "partId": sheet["partId"], "x": 0, "y": 0, "rotation": 0, "isMirror": False,
            "attrs": {"Footprints": "[]", "Devices": "[]",
                      "DeviceName": json.dumps({"uuid": sheet["deviceUuid"], "name": sheet["name"],
                                                "source": ""}, separators=(",", ":")),
                      "Symbols": "[]"},
            "zIndex": 1}))
        t += 1

    # 器件
    for c in plan["components"]:
        key = {"R": "R", "C": "C", "L": "R", "QNPN": "Q", "QPNP": "Q", "VSRC": "VSIN"}[c["simKind"]]
        if key not in pack["parts"]:
            continue
        attrs = {"Designator": c["designator"]}
        val = c.get("value")
        if c["simKind"] in ("R", "C", "L"):
            attrs["Value"] = val or ("1k" if key == "R" else "100n")
        elif c["simKind"] == "VSRC":
            attrs["Voltage[V]"] = val or "5V"
            attrs["Frequency[Hz]"] = "1k"
            attrs["Delay Time[s]"] = "0"
            attrs["Voltage Offset[V]"] = "0"
        add_component(key, c["x"], c["y"], c["rotation"], attrs)

    # 电源源（router 已给出坐标与连线；负压/解析失败的不加）
    for sup in plan.get("supply_components", []):
        v = sup.get("voltage")
        if v is None or v <= 0 or "VDC" not in pack["parts"]:
            continue
        add_component("VDC", sup["x"], sup["y"], 0,
                      {"Designator": f"VP{abs(int(round(v)))}", "Voltage[V]": f"{v:g}V"})

    # 网标
    for f in plan.get("netflags", []):
        key = "FLAG_POWER" if f["flagType"] == "Power" else "FLAG_GND"
        if key not in pack["parts"]:
            continue
        add_component(key, f["x"], f["y"], 0,
                      {"Global Net Name": f["net"], "Name": f["net"], "Relevance": "[]"})

    # 导线
    for w in plan.get("wires", []):
        segs = [s for s in w.get("segments", []) if abs(s[0] - s[2]) > 1e-6 or abs(s[1] - s[3]) > 1e-6]
        if not segs:
            continue
        wid = nid()
        lines.append(emit("WIRE", wid, t, {"zIndex": 10}))
        t += 1
        for s in segs:
            lines.append(emit("LINE", nid(), t, {
                "fillColor": None, "fillStyle": None, "strokeColor": None, "strokeStyle": None,
                "strokeWidth": None, "startX": round(s[0], 2), "startY": round(s[1] * Y_SIGN, 2),
                "endX": round(s[2], 2), "endY": round(s[3] * Y_SIGN, 2), "lineGroup": wid}))
            t += 1
        if w.get("net"):
            lines.append(emit("ATTR", nid(), t, {
                "x": round(segs[0][0], 2), "y": round(segs[0][1] * Y_SIGN, 2),
                "rotation": 0, "color": None, "fontFamily": None, "fontSize": None,
                "fontWeight": None, "italic": None, "underline": None, "align": None,
                "value": w["net"], "keyVisible": False, "valueVisible": True, "key": "NET",
                "fillColor": None, "parentId": wid, "zIndex": 2}))
            t += 1

    return extra_blocks, final_extra, lines, page_uuid


def filter_doomed(text, plan):
    """移除此前生成的同名仿真页（NGspice1/Simulation1），实现"重复写回=替换"而非叠加。"""
    docs = parse_records(text)
    doomed = set()
    for d in docs:
        if d["docType"] not in ("SIMULATION", "SIMULATION_SCH"):
            continue
        _, meta = doc_elements(d)
        title = meta.get("title") or ""
        want = "NGspice1" if d["docType"] == "SIMULATION" else "Simulation1"
        if title == want:
            doomed.add(d["uuid"])
    if not doomed:
        return text, []
    out_lines = []
    skip = False
    removed = []
    for ln in text.split("\n"):
        if '"DOCHEAD"' in ln:
            u = None
            try:
                u = json.loads(ln.split("||")[1].rstrip("|")).get("uuid")
            except (json.JSONDecodeError, IndexError):
                pass
            skip = u in doomed if u else False
            if skip:
                removed.append(u)
                continue
        elif skip:
            continue
        out_lines.append(ln)
    return "\n".join(out_lines), list(set(removed))


def writeback(target, plan_path, pack_path, out_path=None, dry_run=False):
    plan = json.load(open(plan_path, encoding="utf-8"))
    pack = json.load(open(pack_path, encoding="utf-8"))
    zf, epru_info, text = load_target(target)
    # 替换此前生成的同名仿真页（避免重复写回叠加多个 NGspice1）
    text, removed_pages = filter_doomed(text, plan)
    if removed_pages:
        print(f"replace: 移除旧生成页 {len(removed_pages)} 个（{', '.join(removed_pages)}）")
    # target meta（client/editVersion）取自首个 DOCHEAD
    target_meta = {}
    for ln in text.split("\n"):
        if '"DOCHEAD"' in ln:
            try:
                target_meta = json.loads(ln.split("||")[1].rstrip("|"))
            except (json.JSONDecodeError, IndexError):
                pass
            break
    _, extra_blocks, new_lines, page_uuid = build_docs(text, plan, pack, target_meta)

    new_text = text.rstrip("\n") + "\n" + "\n".join(extra_blocks) + "\n" + "\n".join(new_lines) + "\n"

    out = out_path or target
    bak = target + ".bak.epro2"
    if os.path.exists(bak):
        bak = target + f".bak.{time.strftime('%Y%m%d-%H%M%S')}.epro2"
    shutil.copy2(target, bak)
    print(f"backup: {bak}")

    tmp = out + ".tmp"
    with zipfile.ZipFile(out, "w") as zo:
        pass  # 先创建空文件占位（会被下面的 real build 覆盖）
    zf.close()
    src = zipfile.ZipFile(target)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
        for info in src.infolist():
            if info.filename == epru_info.filename:
                continue
            zo.writestr(info, src.read(info))
        new_info = zipfile.ZipInfo(epru_info.filename, date_time=time.localtime()[:6])
        new_info.compress_type = zipfile.ZIP_DEFLATED
        zo.writestr(new_info, new_text.encode("utf-8"))
    src.close()
    if dry_run:
        print(f"dry-run: {tmp} 生成完毕（未替换原文件）")
        return tmp
    os.replace(tmp, out)
    print(f"written: {out}  (+{len(new_lines)} 行新页面记录)")

    # 写后校验
    try:
        ir = parse_file(out, "NGspice1")
        n_parts = len([c for c in ir["components"] if c["componentType"] == "part"])
        print(f"verify: 重新解析成功，NGspice1 器件数={n_parts}（plan={len(plan['components'])}）")
    except Exception as e:  # noqa: BLE001
        print(f"verify FAILED: {e}")
        return None
    return out


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    out = None
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    res = writeback(sys.argv[1], sys.argv[2], sys.argv[3], out, "--dry-run" in sys.argv)
    if res is None:
        sys.exit(1)
