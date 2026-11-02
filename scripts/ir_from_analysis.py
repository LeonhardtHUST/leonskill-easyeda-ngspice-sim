#!/usr/bin/env python3
"""依赖产物 `schematic_<页>.json` → 本 skill 的 IR（ir.json）。

两个 skill 的 IR 字段本来就很接近（components / pins 绝对坐标 / wires / page），
差的只是本 skill 额外要求的元字段（version / source / lines / name / pinPrefix）。
这里只做字段补齐，**不重新解释 .epru 记录**——那是依赖的职责。

用法:
  python ir_from_analysis.py <schematic_页.json> <out_ir.json> [--pages-from <产物目录>]
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ir_common import IR_VERSION, normalize, save_ir, validate  # noqa: E402

SOURCE_TAG = "analysis"


def _component_name(comp):
    """本 skill 的 IR 有 name 字段，依赖只有 deviceTitle；netflag 命名会用到它。"""
    if comp.get("name"):
        return comp["name"]
    title = comp.get("deviceTitle")
    if title:
        return title
    attrs = comp.get("attrs") or {}
    return attrs.get("Name")


def convert(schematic, project=None, pages=None):
    """schematic IR dict → 本 skill IR dict。"""
    page = dict(schematic.get("page") or {})
    ir = {
        "version": IR_VERSION,
        "source": SOURCE_TAG,
        "project": project if project is not None else schematic.get("project"),
        "page": page,
        "components": [],
        "wires": [],
        "lines": [],
        "nets": [],
        "warnings": list(schematic.get("warnings") or []),
    }
    for src in schematic.get("components") or []:
        comp = dict(src)
        comp["name"] = _component_name(src)
        comp.setdefault("pins", [])
        comp.setdefault("attrs", {})
        ir["components"].append(comp)
    for src in schematic.get("wires") or []:
        ir["wires"].append(dict(src))
    if pages:
        ir["_pages"] = pages
    normalize(ir)
    return ir


def pages_index(out_dir):
    """产物目录里所有 schematic_*.json 的页清单（同名页会带 _2 后缀，必须读 page 字段）。"""
    pages = []
    for path in sorted(glob.glob(os.path.join(out_dir, "schematic_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                meta = (json.load(f).get("page") or {})
        except (OSError, ValueError):
            continue
        pages.append({"uuid": meta.get("uuid"), "name": meta.get("name") or "",
                      "docType": meta.get("docType")})
    return pages


def load_schematic(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    args = [a for a in sys.argv[1:]]
    pages_from = None
    if "--pages-from" in args:
        i = args.index("--pages-from")
        pages_from = args[i + 1]
        del args[i:i + 2]
    if len(args) < 2:
        print("usage: ir_from_analysis.py <schematic_page.json> <out_ir.json> "
              "[--pages-from <out_dir>]")
        return 2
    src, out = args[0], args[1]
    schematic = load_schematic(src)
    pages = pages_index(pages_from) if pages_from else None
    ir = convert(schematic, pages=pages)
    save_ir(ir, out)
    print(f"IR written: {out}")
    print(f"  project: {ir.get('project')}  page: {(ir.get('page') or {}).get('name')} "
          f"({(ir.get('page') or {}).get('docType')})")
    print(f"  components: {len(ir['components'])}  wires: {len(ir['wires'])}")
    for w in validate(ir):
        print("  WARN:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
