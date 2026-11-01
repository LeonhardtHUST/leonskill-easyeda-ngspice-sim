#!/usr/bin/env python3
"""v2.3 P1：两次仿真的对比 + 判据 + 置信度（全部规则化，不靠主观打分）。

    compare_runs.py original/ eco/

输出：
  * metric / before / after / delta / better|worse|same 表
  * 判据 PASS / FAIL / INCONCLUSIVE
  * confidence: LOW / MEDIUM / HIGH（逐条列出触发规则）

方向判定（哪些指标"越小越好"）是硬编码的白名单 + 后缀规则，避免"以为自己在优化"。
"""
import json
import math
import os
import sys

LOWER_IS_BETTER_HINTS = (
    "overshoot", "undershoot", "ripple", "droop", "deviation", "crossing", "ringing",
    "max_impedance", "z_max", "peak_to_peak", "err", "error", "skew", "settling",
    "insertion_loss", "peak_attenuation", "rise_time", "fall_time",
)
HIGHER_IS_BETTER_HINTS = (
    "return_loss", "margin", "z_min", "min_impedance", "bandwidth", "vik", "vih",
)


def _metric_direction(name):
    low = name.lower()
    for h in LOWER_IS_BETTER_HINTS:
        if h in low:
            return -1
    for h in HIGHER_IS_BETTER_HINTS:
        if h in low:
            return +1
    return 0


def load_run(path):
    """读一个 run 目录（或 summary json）→ {flat_metrics, manifest, raw}"""
    if os.path.isfile(path):
        files = [path]
        base = os.path.dirname(path)
    else:
        base = path
        files = [os.path.join(path, n) for n in (
            "sweep_summary.json", "montecarlo_summary.json", "metrics.json",
            "result.json", "simulation_manifest.json")]
    out = {"dir": base, "flat": {}, "manifest": None, "summary": None}
    for f in files:
        if not os.path.exists(f):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        name = os.path.basename(f)
        if name == "metrics.json" and isinstance(data, dict):
            out["flat"].update(_flatten(data))
        elif name == "sweep_summary.json":
            out["summary"] = data
            cases = data.get("cases") or []
            if cases:
                # 取"代表性"case：第一个成功的；同时保留全部 case 供判据统计
                ok = [c for c in cases if c.get("returncode") == 0] or cases
                out["flat"].update(_flatten(ok[0].get("metrics") or {}))
                out["cases"] = cases
        elif name == "montecarlo_summary.json":
            out["summary"] = data
            per = (data.get("summary") or {}).get("metrics") or {}
            for k, st in per.items():
                if isinstance(st, dict) and "mean" in st:
                    out["flat"][k + ".mean"] = st["mean"]
                    out["flat"][k + ".p5"] = st.get("p5")
                    out["flat"][k + ".p95"] = st.get("p95")
        elif name == "simulation_manifest.json":
            out["manifest"] = data
    return out


def _flatten(m):
    flat = {}
    for k, v in (m or {}).items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                if isinstance(vv, (int, float)):
                    flat[f"{k}.{kk}"] = vv
        elif isinstance(v, (int, float)):
            flat[k] = v
    return flat


def compare(before, after):
    keys = sorted(set(before["flat"]) | set(after["flat"]))
    rows = []
    for k in keys:
        b = before["flat"].get(k)
        a = after["flat"].get(k)
        if b is None or a is None:
            continue
        if not isinstance(b, (int, float)) or not isinstance(a, (int, float)):
            continue
        delta = a - b
        direction = _metric_direction(k)
        if direction == 0:
            verdict = "same" if abs(delta) < 1e-12 else "changed"
        elif abs(delta) < max(abs(b) * 1e-9, 1e-15):
            verdict = "same"
        else:
            improved = (delta < 0) if direction < 0 else (delta > 0)
            verdict = "better" if improved else "worse"
        rows.append({"metric": k, "before": b, "after": a, "delta": delta,
                     "delta_pct": (delta / b * 100.0) if b else None,
                     "direction": ("lower_is_better" if direction < 0 else
                                   "higher_is_better" if direction > 0 else "neutral"),
                     "verdict": verdict})
    return rows


def criteria_check(flat, criteria):
    ops = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
           ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
           "==": lambda a, b: abs(a - b) < 1e-12, "!=": lambda a, b: abs(a - b) >= 1e-12}
    rows, unknown = [], []
    for c in criteria or []:
        name, op, val = c.get("metric"), c.get("op"), c.get("value")
        if name not in flat:
            unknown.append(name)
            rows.append({"metric": name, "op": op, "value": val, "actual": None,
                         "pass": None})
            continue
        actual = flat[name]
        try:
            ok = ops[op](float(actual), float(val))
        except Exception:
            ok = None
        rows.append({"metric": name, "op": op, "value": val, "actual": actual,
                     "pass": ok})
    if not rows:
        verdict = "INCONCLUSIVE"
    elif any(r["pass"] is None for r in rows):
        verdict = "INCONCLUSIVE"
    elif all(r["pass"] for r in rows):
        verdict = "PASS"
    else:
        verdict = "FAIL"
    return {"verdict": verdict, "rows": rows, "unknown": unknown}


# --------------------------------------------------------------------------
# Confidence（规则透明）
# --------------------------------------------------------------------------
def confidence(manifest, extra=None):
    """按 manifest 里的客观事实打分，返回 {level, score, rules[]}。"""
    rules, score = [], 0
    m = manifest or {}
    fences = {"driver": [], "receiver": [], "power": []}
    for b in m.get("behavioral_models") or []:
        rules.append(("-1", f"behavioural 占位: {b.get('designator')} ({b.get('type')})"))
        score -= 1
    ibis = m.get("ibis_models") or []
    if ibis:
        rules.append((f"+{2 * min(len(ibis), 2)}",
                      f"IBIS 驱动/接收模型 × {len(ibis)}（IBIS_DERIVED_BEHAVIORAL_MODEL）"))
        score += 2 * min(len(ibis), 2)
    vendor = [x for x in (m.get("models") or []) if x.get("type") in ("SUBCKT", "MODEL")]
    if vendor:
        rules.append((f"+{min(len(vendor), 3)}", f"厂商 SPICE 模型 × {len(vendor)}"))
        score += min(len(vendor), 3)
    geo = (extra or {}).get("geometry") or m.get("geometry") or {}
    if geo.get("nets"):
        rules.append(("+2", "PCB 几何已提取（trace/via 拓扑来自实际布线）"))
        score += 2
    st = (extra or {}).get("stackup") or m.get("stackup") or {}
    if st.get("layers") and st.get("dielectrics"):
        rules.append(("+1", "叠层完整（Z0/TD 为 ANALYTIC_ESTIMATE）"))
        score += 1
    sp = m.get("sparams") or (extra or {}).get("sparams") or []
    for s in sp:
        fit = (s.get("model") or {}).get("fit") or {}
        err = fit.get("s21_max_err_db")
        if err is not None and err <= 1.0:
            rules.append(("+1", f"S 参数拟合误差 {err:.2f} dB ≤ 1 dB: {s.get('name')}"))
            score += 1
        elif err is not None:
            rules.append(("-1", f"S 参数拟合误差 {err:.2f} dB > 1 dB: {s.get('name')}"))
            score -= 1
    for w in (m.get("warnings") or []):
        if "POOR" in str(w) or "STACKUP_INCOMPLETE" in str(w):
            rules.append(("-1", f"告警: {str(w)[:80]}"))
            score -= 1
    if (m.get("excluded_components") or []):
        rules.append(("-1", f"被排除器件 {len(m['excluded_components'])} 个"))
        score -= 1
    level = "HIGH" if score >= 5 else ("MEDIUM" if score >= 2 else "LOW")
    return {"level": level, "score": score, "rules": rules,
            "note": "规则是固定的加减分表；LOW/MEDIUM/HIGH 只是对模型来源的汇总，"
                    "不代表结果正确性"}


# --------------------------------------------------------------------------
def render(rows, crit, conf, limit=40):
    lines = []
    if rows:
        lines.append(f"{'metric':38s} {'before':>12s} {'after':>12s} {'delta':>12s}  verdict")
        lines.append("-" * 92)
        for r in rows[:limit]:
            lines.append(f"{r['metric'][:38]:38s} {r['before']:12.4g} {r['after']:12.4g} "
                         f"{r['delta']:+12.4g}  {r['verdict']}")
        if len(rows) > limit:
            lines.append(f"... ({len(rows) - limit} more metrics)")
    if crit and crit.get("rows"):
        lines.append("")
        lines.append(f"criteria: {crit['verdict']}")
        for r in crit["rows"]:
            mark = {True: "PASS", False: "FAIL", None: "N/A "}[r["pass"]]
            lines.append(f"  [{mark}] {r['metric']} {r['op']} {r['value']} "
                         f"(actual {r['actual']})")
    if conf:
        lines.append("")
        lines.append(f"confidence: {conf['level']} (score {conf['score']:+d})")
        for delta, why in conf["rules"]:
            lines.append(f"  {delta:>4s} {why}")
    return lines


def main():
    argv = sys.argv[1:]
    if len(argv) < 2:
        print(__doc__)
        sys.exit(2)
    before_dir, after_dir = argv[0], argv[1]
    criteria = None
    for i, a in enumerate(argv):
        if a == "--criteria" and i + 1 < len(argv):
            with open(argv[i + 1], encoding="utf-8") as f:
                cfg = json.load(f)
            criteria = cfg.get("criteria") if isinstance(cfg, dict) else cfg
    before, after = load_run(before_dir), load_run(after_dir)
    rows = compare(before, after)
    crit = criteria_check(after["flat"], criteria) if criteria else None
    conf = confidence(after.get("manifest") or before.get("manifest"))
    for ln in render(rows, crit, conf):
        print(ln)
    if crit and crit["verdict"] == "FAIL":
        sys.exit(1)


if __name__ == "__main__":
    main()
