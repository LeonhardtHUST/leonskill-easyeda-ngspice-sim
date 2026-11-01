#!/usr/bin/env python3
"""从"参考工程"（含完整仿真页的 .epro2）提取仿真器件模板包（simlib_pack.json）。

用法: python extract_simlib.py <reference.epro2> <out_pack.json>

产物：每个仿真器件（电阻/电容/SS9013(Sim)/直流源/正弦源/Power 标志/Ground 标志）的
DEVICE 文档段 + SYMBOL 文档段（逐行原文，uuid 原样保留），以及实例化的 partId/属性模板。
写回时若目标工程缺少这些文档，直接按原文注入。
"""
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from epro2_parse import decode_name, parse_records, doc_elements  # noqa: E402

# 目标器件名（与系统库 (Sim) 器件名对应）
WANTED = {
    "R": "电阻_Resistor",
    "C": "电容_Capacitor",
    "Q": "SS9013(Sim)",
    "VDC": "直流电压源_Voltage Source(DC)",
    "VSIN": "正弦电压源_Voltage Source(AC)",
    "FLAG_POWER": "Power-5V",
    "FLAG_GND": "Ground-GND",
}


def read_epru(path):
    zf = zipfile.ZipFile(path)
    for info in zf.infolist():
        if decode_name(info.filename).endswith(".epru"):
            return zf.read(info).decode("utf-8", "replace"), zf, info
    raise ValueError("no .epru")


def doc_segments(text):
    """{uuid: [行原文...]} —— 每个 DOCHEAD 段的原始行。"""
    segs = {}
    cur_uuid = None
    cur_lines = []
    for ln in text.split("\n"):
        if not ln.strip():
            continue
        if '"DOCHEAD"' in ln:
            if cur_uuid and cur_lines:
                segs[cur_uuid] = cur_lines
            cur_lines = [ln]
            try:
                p = json.loads(ln.split("||")[1].rstrip("|"))
                cur_uuid = p.get("uuid")
            except (json.JSONDecodeError, IndexError):
                cur_uuid = None
        else:
            if cur_uuid is not None:
                cur_lines.append(ln)
    if cur_uuid and cur_lines:
        segs[cur_uuid] = cur_lines
    return segs


def build_pack(ref_path, out_path):
    text, _, _ = read_epru(ref_path)
    docs = parse_records(text)
    segs = doc_segments(text)

    # 1) 找仿真页段（SIMULATION），收集实例：device uuid/name → partId、属性键集合
    inst = {}
    sim_pages = [d for d in docs if d["docType"] == "SIMULATION"]
    for d in sim_pages:
        by_type, _ = doc_elements(d)
        parent_attrs = {}
        for arid, a in by_type.get("ATTR", []):
            pid = a.get("parentId")
            if pid:
                parent_attrs.setdefault(pid, {})[a.get("key")] = a.get("value")
        comp_ids = {}
        for rid, c in by_type.get("COMPONENT", []):
            dev_uuid = None
            name = None
            dn = (c.get("attrs") or {}).get("DeviceName")
            if dn:
                try:
                    dev = json.loads(dn)
                    dev_uuid, name = dev.get("uuid"), dev.get("name")
                except json.JSONDecodeError:
                    pass
            if not dev_uuid:
                dev_uuid = parent_attrs.get(rid, {}).get("Device")
            if dev_uuid:
                comp_ids[rid] = (dev_uuid, name, c.get("partId"))
        for rid, (dev_uuid, name, part_id) in comp_ids.items():
            rec = inst.setdefault(dev_uuid, {"name": name, "partId": part_id,
                                             "attrKeys": set(), "attrSamples": {}})
            if name and not rec["name"]:
                rec["name"] = name
            for k, v in parent_attrs.get(rid, {}).items():
                if not k:
                    continue
                rec["attrKeys"].add(k)
                rec["attrSamples"].setdefault(k, v)

    # 2) 器件文档（DEVICE）— uuid 即实例 device uuid
    devices = {}
    for d in docs:
        if d["docType"] != "DEVICE":
            continue
        by_type, meta = doc_elements(d)
        title = meta.get("title") or ""
        for key, want_name in WANTED.items():
            pass
        devices[d["uuid"]] = {"title": title, "meta": meta,
                              "attributes": meta.get("attributes") or {}}

    # 3) 组装 pack
    pack = {"version": 1, "reference": os.path.basename(ref_path), "parts": {}}
    want_by_name = {v: k for k, v in WANTED.items()}
    used_sym = set()
    for dev_uuid, rec in inst.items():
        dev = devices.get(dev_uuid)
        if not dev:
            continue
        name = rec["name"] or dev["title"] or ""
        key = want_by_name.get(name)
        if not key:
            continue
        sym_uuid = dev["attributes"].get("Symbol") or ""
        sym_seg = segs.get(sym_uuid)
        if not sym_seg:
            continue
        used_sym.add(sym_uuid)
        pack["parts"][key] = {
            "name": name,
            "deviceUuid": dev_uuid,
            "symbolUuid": sym_uuid,
            "partId": rec["partId"],
            "attrKeys": sorted(k for k in rec["attrKeys"] if k),
            "attrSamples": {k: v for k, v in rec["attrSamples"].items() if k},
            "deviceSegment": segs.get(dev_uuid, []),
            "symbolSegment": sym_seg,
            "valueKey": ("Voltage[V]" if "电压源" in name else "Value"),
        }
    # 4) 尺寸符号（图框）也带上——生成的仿真页需要图框
    for dev_uuid, dev in devices.items():
        if "Drawing-Symbol" in (dev["title"] or ""):
            sym_uuid = dev["attributes"].get("Symbol")
            if sym_uuid and segs.get(sym_uuid):
                pack["parts"]["SHEET"] = {
                    "name": dev["title"], "deviceUuid": dev_uuid, "symbolUuid": sym_uuid,
                    "partId": "pid8a0e77bacb214e", "attrKeys": [], "attrSamples": {},
                    "deviceSegment": segs.get(dev_uuid, []),
                    "symbolSegment": segs[sym_uuid], "valueKey": "Value",
                }
            break
    json.dump(pack, open(out_path, "w", encoding="utf-8"), ensure_ascii=False)
    return pack


if __name__ == "__main__":
    pack = build_pack(sys.argv[1], sys.argv[2])
    print(f"pack written: {sys.argv[2]}")
    for k, v in pack["parts"].items():
        print(f"  {k:10s} {v['name']:32s} dev={v['deviceUuid'][:12]} sym={v['symbolUuid'][:12]} "
              f"partId={v['partId']} segLines={len(v['deviceSegment'])}+{len(v['symbolSegment'])}")
