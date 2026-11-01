#!/usr/bin/env python3
"""v2.2 P1-C：simulation_manifest.json + Simulation fidelity 展示。

目的：让下一个 agent（或三个月后的自己）能一眼看出"这次的波形到底基于
什么假设"——用了哪些厂商模型、哪些是行为级近似、哪些器件被排除、地怎么接的、
电源是谁供的、加了哪些 PCB 寄生 override。

同时统一 fidelity 措辞：禁止"已成功仿真整个 PCB"这种含糊说法，必须按
Level 0-3 逐块标注。
"""
import hashlib
import json
import os
import re
import subprocess

from sim_config import SKILL_VERSION

LEVEL_NAMES = {
    0: "Level 0 — topology only (R/C/L + generic)",
    1: "Level 1 — behavioral placeholders",
    2: "Level 2 — vendor SPICE (.SUBCKT/.MODEL)",
    3: "Level 3 — PCB parasitic (trace/via/RLC/transmission line)",
}


def file_hash(path, algo="sha256"):
    if not path or not os.path.exists(path):
        return None
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ngspice_version(bin_path):
    if not bin_path or not os.path.exists(bin_path):
        return None
    try:
        out = subprocess.run([bin_path, "-v"], capture_output=True, timeout=20)
        text = (out.stdout or b"").decode("utf-8", "replace") + \
               (out.stderr or b"").decode("utf-8", "replace")
        m = re.search(r"ngspice-(\d+[\w.\-]*)", text)
        return f"ngspice-{m.group(1)}" if m else text.strip().splitlines()[1].strip()
    except Exception:
        return None


def fidelity_lines(report):
    """把 fidelity 表渲染成 SKILL.md 要求的那种段落。"""
    fid = (report or {}).get("fidelity") or {}
    blocks = fid.get("blocks") or {}
    lines = ["Simulation fidelity:"]
    if not blocks:
        lines.append("  (no components included)")
    for name, info in sorted(blocks.items(), key=lambda kv: (-kv[1]["level"], kv[0])):
        lines.append(f"  {name}: Level {info['level']}")
    overall = fid.get("overall_level", 0)
    lines.append(f"  overall: Level {overall} — {LEVEL_NAMES.get(overall, '')}")
    lines.append("  注意：未列出的网络/器件没有寄生建模；本结果不代表整块 PCB 已被仿真。")
    return lines


def build(netlist_report, analysis, project=None, source_hash=None,
          ngspice=None, workdir=None, runs=None, errors=None):
    """组装 manifest dict（严格按需求文档第十九节的字段，另加少量扩展）。"""
    nr = netlist_report or {}
    return {
        "source_project": project or nr.get("project"),
        "source_hash": source_hash,
        "skill_version": SKILL_VERSION,
        "ngspice_version": ngspice,
        "analysis": analysis,
        "workdir": workdir,
        "models": [
            {
                "designator": m.get("designator"),
                "part": m.get("mpn") or m.get("key"),
                "type": m.get("type"),
                "source": m.get("file_display"),
                "subckt": m.get("subckt") or m.get("model"),
                "pin_mapping": m.get("pin_mapping"),
                "pin_mapping_status": m.get("status"),
                "matched_by": m.get("matched_by"),
                "levels": "Level 2",
            } for m in nr.get("models") or []
        ],
        "behavioral_models": [
            {
                "designator": b.get("designator"),
                "part": b.get("part"),
                "type": b.get("type"),
                "level": b.get("level"),
                "models": b.get("models"),
                "not_simulated": b.get("not_simulated"),
                "elements": b.get("elements"),
            } for b in nr.get("behavioral") or []
        ],
        "excluded_components": [
            {"designator": e.get("designator"), "part": e.get("mpn") or e.get("title"),
             "reason": e.get("reason"), "level": "EXCLUDED"}
            for e in nr.get("excluded") or []
        ],
        "ground_mapping": nr.get("ground_mapping") or {},
        "power_sources": nr.get("powers") or [],
        "overrides": nr.get("overrides") or [],
        "net_segments": nr.get("net_segments") or [],
        "virtual_nodes": nr.get("virtual_nodes") or [],
        "includes": nr.get("includes") or [],
        "fidelity": nr.get("fidelity") or {},
        "power_policy": nr.get("power_policy"),
        "strict": nr.get("strict"),
        "warnings": (nr.get("warnings") or []) + [w for w in (errors or [])],
        "errors": nr.get("errors") or [],
        "runs": runs or [],
    }


def write(manifest, path):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return path


def summary_lines(manifest):
    """给 stdout 的 manifest 摘要（不打印全部细节）。"""
    out = [f"skill: {manifest.get('skill_version')}   ngspice: {manifest.get('ngspice_version')}",
           f"power policy: {manifest.get('power_policy')}   strict: {manifest.get('strict')}"]
    gm = manifest.get("ground_mapping") or {}
    if gm:
        out.append("ground: " + ", ".join(f"{k}->{v}" for k, v in gm.items()))
    if manifest.get("models"):
        out.append(f"vendor models: {', '.join(m['designator'] for m in manifest['models'])}")
    if manifest.get("behavioral_models"):
        out.append("behavioral: " + ", ".join(
            f"{b['designator']} ({b['type']})" for b in manifest["behavioral_models"]))
    if manifest.get("excluded_components"):
        out.append("excluded: " + ", ".join(
            e["designator"] for e in manifest["excluded_components"]))
    return out
