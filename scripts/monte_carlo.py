#!/usr/bin/env python3
"""v2.3 P0-H：Monte Carlo 引擎 + Corner 扫描。

Monte Carlo 与 Corner 的区别（需求文档 §23）：
  * Monte Carlo：按分布随机抽样，回答"良率/最坏情况"；
  * Corner：确定性的笛卡尔积（VIN × TEMP × LOAD），回答"边界条件下还成立吗"。

两者都复用 sweep_runner 的"改值 → 独立跑 ngspice"机制，只是"值"的来源不同。
可复现性：所有随机数来自 `random.Random(seed)`，case 顺序固定 → 同 seed 同结果。

失败判据（§22）例：``{"metric": "vcc.peak_to_peak", "op": ">", "value": 0.05}``
（metric 名与 sweep_summary 的列名一致：`<指标名>.<字段>`）
"""
import math
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import sweep_runner

MAX_CASES_DEFAULT = 1000
OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
       ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
       "==": lambda a, b: abs(a - b) < 1e-12,
       "!=": lambda a, b: abs(a - b) >= 1e-12}


class MonteCarloError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# 分布
# --------------------------------------------------------------------------
def sample_one(spec, rng):
    dist = str(spec.get("distribution") or "gaussian").lower()
    nom = spec.get("nominal")
    if nom is None:
        raise MonteCarloError("MC_SPEC_INVALID", f"参数缺少 nominal: {spec}")
    if dist == "discrete":
        values = spec.get("values") or []
        weights = spec.get("weights")
        if not values:
            raise MonteCarloError("MC_SPEC_INVALID",
                                  f"discrete 分布需要 values: {spec}")
        if weights and len(weights) == len(values):
            return rng.choices(values, weights=weights, k=1)[0]
        return rng.choice(values)
    if dist == "uniform":
        pct = float(spec.get("tolerance_percent") or spec.get("sigma_percent") or 0)
        lo = spec.get("min", _scale(nom, 1 - pct / 100.0))
        hi = spec.get("max", _scale(nom, 1 + pct / 100.0))
        return _fmt(rng.uniform(_num(lo), _num(hi)), nom)
    if dist in ("gaussian", "normal"):
        sigma_pct = float(spec.get("sigma_percent") or 0)
        if sigma_pct <= 0:
            raise MonteCarloError("MC_SPEC_INVALID",
                                  f"gaussian 分布需要 sigma_percent > 0: {spec}")
        n = _num(nom)
        v = rng.gauss(n, abs(n) * sigma_pct / 100.0)
        clip = spec.get("clip_sigma")
        if clip:
            lo, hi = n - clip * abs(n) * sigma_pct / 100.0, n + clip * abs(n) * sigma_pct / 100.0
            v = min(max(v, lo), hi)
        return _fmt(max(v, 0.0) if spec.get("positive", True) else v, nom)
    raise MonteCarloError("MC_DISTRIBUTION_UNKNOWN",
                          f"未知分布 {dist!r}（支持 gaussian / uniform / discrete）")


def _num(v):
    from parasitics import parse_eng
    return parse_eng(v, "mc")


def _scale(v, k):
    return _fmt(_num(v) * k, v)


def _fmt(value, like):
    """保持用户写法的数量级后缀（100n → 语义一致的输出串）。"""
    s = str(like)
    import re
    m = re.fullmatch(r"([+-]?[\d.]+)([A-Za-zµμ]*)", s.strip())
    if not m:
        return f"{value:.6g}"
    mult = {"": 1.0, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
            "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "meg": 1e6}.get(m.group(2), 1.0)
    unit = m.group(2)
    if unit in ("p", "n", "u", "µ", "μ", "m", "k", "K", "M"):
        return f"{value / mult:.6g}{unit}"
    return f"{value:.6g}"


def expand(specs, n_cases, seed=1, max_cases=MAX_CASES_DEFAULT):
    """参数分布 → n_cases 组取值。"""
    if not specs:
        raise MonteCarloError("MC_SPEC_MISSING", "没有参数分布定义")
    if n_cases > max_cases:
        raise MonteCarloError(
            "MC_TOO_MANY_CASES",
            f"请求 {n_cases} 个 case，超过上限 {max_cases}（可用 --max-cases 提高）")
    if n_cases < 1:
        raise MonteCarloError("MC_SPEC_INVALID", "case 数必须 >= 1")
    rng = random.Random(seed)
    names = list(specs.keys())
    cases = []
    for i in range(n_cases):
        params = {}
        for nm in names:
            params[nm] = sample_one(specs[nm], rng)
        cases.append({"index": i + 1, "id": f"{i + 1:04d}",
                      "params": params})
    return cases


def corner(spec, max_cases=MAX_CASES_DEFAULT):
    """确定性笛卡尔积。spec: {"VIN": [...], "TEMP": [...], "LOAD": [...]}"""
    if not spec:
        raise MonteCarloError("MC_SPEC_MISSING", "没有 corner 定义")
    import itertools
    keys = list(spec.keys())
    combos = list(itertools.product(*[spec[k] for k in keys]))
    if len(combos) > max_cases:
        raise MonteCarloError(
            "MC_TOO_MANY_CASES",
            f"corner 展开 {len(combos)} 个 case，超过上限 {max_cases}")
    out = []
    for i, combo in enumerate(combos, start=1):
        params = dict(zip(keys, combo))
        slug = "_".join(f"{k}={v}" for k, v in params.items())
        out.append({"index": i, "id": f"{i:04d}_{slug}", "params": params})
    return out


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
def apply_case(deck_text, params, temp_key="TEMP"):
    """把 case 参数写进 deck：温度走 `.temp`，其余走元件名 / .param。"""
    text = deck_text
    temp = params.get(temp_key)
    kv = {k: v for k, v in params.items() if k != temp_key}
    if kv:
        text, _counts = sweep_runner.apply_params(text, kv)
    if temp is not None:
        import re
        if re.search(r"(?im)^[ \t]*\.temp[ \t]", text):
            text = re.sub(r"(?im)^[ \t]*\.temp.*$", f".temp {temp}", text)
        else:
            text = text.replace(".end", f".temp {temp}\n.end", 1) \
                if "\n.end" in text else text + f"\n.temp {temp}\n"
    return text


def run(deck_text, cases, ngspice_bin, workdir, mode, metric_specs,
        workers=None, timeout=600, log=print, label="mc"):
    """并行跑所有 case。返回 records 列表。"""
    workers = workers if workers is not None else min(os.cpu_count() or 1, 8)
    workers = max(1, workers)
    records = []
    t0 = time.time()
    done = 0
    log(f"[{label}] {len(cases)} case(s), workers={workers}")

    def one(case):
        deck = apply_case(deck_text, case["params"])
        rec, _plots = sweep_runner.run_case(
            ngspice_bin, deck, case, workdir, mode, metric_specs, timeout=timeout)
        return rec

    if workers == 1:
        for case in cases:
            rec = one(case)
            records.append(rec)
            done += 1
            log(f"  [{done}/{len(cases)}] {rec['id']} rc={rec['returncode']} "
                f"({time.time() - t0:.1f}s)")
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(one, c): c for c in cases}
            for f in as_completed(futs):
                rec = f.result()
                records.append(rec)
                done += 1
                log(f"  [{done}/{len(cases)}] {rec['id']} rc={rec['returncode']} "
                    f"({time.time() - t0:.1f}s)")
    records.sort(key=lambda r: r["index"])
    return records


# --------------------------------------------------------------------------
# 统计与判据
# --------------------------------------------------------------------------
def stats(values):
    vs = sorted(v for v in values if isinstance(v, (int, float)))
    if not vs:
        return {}
    n = len(vs)
    mean = sum(vs) / n
    var = sum((v - mean) ** 2 for v in vs) / (n - 1) if n > 1 else 0.0

    def pct(p):
        if n == 1:
            return vs[0]
        k = (n - 1) * p / 100.0
        f = int(math.floor(k))
        c = min(f + 1, n - 1)
        return vs[f] + (vs[c] - vs[f]) * (k - f)

    return {"n": n, "mean": mean, "std": math.sqrt(var),
            "p5": pct(5), "p50": pct(50), "p95": pct(95),
            "min": vs[0], "max": vs[-1]}


def criteria_result(flat_metrics, criteria):
    """逐条判据 → PASS/FAIL，及整体结论。"""
    rows, unknown = [], []
    for c in criteria or []:
        name = c.get("metric")
        op = c.get("op")
        val = c.get("value")
        if op not in OPS:
            raise MonteCarloError("CRITERIA_INVALID", f"未知比较符 {op!r}")
        if name not in flat_metrics:
            unknown.append(name)
            rows.append({"metric": name, "op": op, "value": val,
                         "actual": None, "pass": None})
            continue
        actual = flat_metrics[name]
        ok = None
        try:
            ok = OPS[op](float(actual), float(val))
        except (TypeError, ValueError):
            try:
                ok = OPS[op](actual, val)
            except Exception:
                ok = None
        rows.append({"metric": name, "op": op, "value": val,
                     "actual": actual, "pass": ok})
    if not rows:
        return {"verdict": "INCONCLUSIVE", "rows": [], "unknown": []}
    if any(r["pass"] is None for r in rows):
        verdict = "INCONCLUSIVE"
    elif all(r["pass"] for r in rows):
        verdict = "PASS"
    else:
        verdict = "FAIL"
    return {"verdict": verdict, "rows": rows, "unknown": unknown}


def summarize(records, criteria=None):
    """records → 每个指标列的统计 + 失败率。"""
    flat_all = [sweep_runner.flatten_metrics(r.get("metrics") or {}) for r in records]
    keys = []
    for f in flat_all:
        for k in f:
            if k not in keys:
                keys.append(k)
    per_metric = {k: stats([f.get(k) for f in flat_all]) for k in keys}
    failures, per_case = 0, []
    if criteria:
        for i, f in enumerate(flat_all):
            res = criteria_result(f, criteria)
            per_case.append({"case": records[i]["id"], "verdict": res["verdict"],
                             "rows": res["rows"]})
            if res["verdict"] == "FAIL":
                failures += 1
    n = len(records) or 1
    return {
        "n_cases": len(records),
        "metrics": per_metric,
        "failures": failures,
        "failure_rate": failures / n if criteria else None,
        "criteria": criteria or [],
        "per_case": per_case,
        "rc_failures": sum(1 for r in records if r["returncode"] != 0),
    }


def write_outputs(records, summary, workdir, dims=None):
    import json
    import csv
    os.makedirs(workdir, exist_ok=True)
    json_path = os.path.join(workdir, "montecarlo_summary.json")
    with open(json_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"cases": records, "summary": summary}, f,
                  ensure_ascii=False, indent=1)
    csv_path = os.path.join(workdir, "montecarlo_cases.csv")
    keys = list((summary.get("metrics") or {}).keys())
    param_names = sorted({k for r in records for k in (r.get("params") or {})})
    with open(csv_path, "w", encoding="utf-8", newline="\n") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "returncode"] + param_names + keys)
        for r in records:
            flat = sweep_runner.flatten_metrics(r.get("metrics") or {})
            w.writerow([r["id"], r["returncode"]]
                       + [r["params"].get(k, "") for k in param_names]
                       + [flat.get(k, "") for k in keys])
    return csv_path, json_path
