"""pytest 公共夹具：把 skill 的 scripts 目录放进 sys.path，并提供造 IR 的小工具。"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
SCRIPTS = os.path.join(SKILL, "scripts")
FIXTURES = os.path.join(HERE, "fixtures")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


def make_ir(components, nets, project="test", page="P1"):
    """components: [(des, prefix, [(pin, name), ...], {value/mpn/title})]"""
    comps = []
    for item in components:
        des, prefix, pins = item[0], item[1], item[2]
        extra = item[3] if len(item) > 3 else {}
        comps.append({
            "componentType": "part",
            "designator": des,
            "pinPrefix": prefix,
            "deviceTitle": extra.get("title", ""),
            "manufacturerPart": extra.get("mpn", ""),
            "value": extra.get("value", ""),
            "pins": [{"number": str(n), "name": nm} for n, nm in pins],
        })
    netlist = []
    for name, members in nets:
        netlist.append({
            "name": name,
            "origin": "netflag" if name.upper().startswith(("GND", "AGND", "DGND")) or
                      name.startswith(("+", "-")) else "wire",
            "members": [{"component": d, "pin": str(p)} for d, p in members],
            "pinCount": len(members),
        })
    return {"version": 1, "source": "test", "project": project,
            "page": {"name": page}, "components": comps, "nets": netlist,
            "warnings": []}


def pin_net_of(ir):
    pn = {}
    for n in ir["nets"]:
        for m in n["members"]:
            pn.setdefault(m["component"], {})[str(m["pin"])] = n["name"]
    return pn


def run_gen(ir, tmp_path, **opts):
    """调用 netlist_gen.generate，返回 (deck_text, report)。"""
    import netlist_gen
    o = {"deck_dir": str(tmp_path)}
    o.update(opts)
    text, report = netlist_gen.generate(ir, pin_net_of(ir), o)
    return text, report


def lines_of(text):
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("*") and ln.strip() != ".end"]


def element(text, name):
    """按元件名找 deck 行（返回 tokens）。"""
    for ln in lines_of(text):
        toks = ln.split()
        if toks and toks[0].upper() == name.upper():
            return toks
    return None


def write_json(path, obj):
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return path


@pytest.fixture
def empty_model_map(tmp_path):
    return write_json(tmp_path / "model_map.json", {"version": 2, "parts": {}, "generics": []})


@pytest.fixture
def fixture_dir():
    def _get(name):
        return os.path.join(FIXTURES, name)
    return _get
