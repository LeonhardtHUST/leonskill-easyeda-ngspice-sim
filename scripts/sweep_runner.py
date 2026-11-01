#!/usr/bin/env python3
"""v2.2 P1-A：参数扫描（一维 / 二维）——Python 侧编排，不依赖 .step 语法。

思路（需求文档指定）：不做 LTspice 风格 `.step`，而是
  1. 复制 base deck；
  2. 改参数（元件值 / .param）；
  3. 独立跑一次 ngspice；
  4. 收集 raw → metrics；
  5. 汇总 sweep_summary.csv / .json。

每个 case 产物：
  runs/<case-id>/{deck.cir, stdout.txt, stderr.txt, *.raw, result.csv, metrics.json}
"""
import itertools
import json
import os
import re
import shutil
import subprocess

import metrics as metrics_mod
import parse_raw


class SweepError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def normalize_sweep(spec, max_cases=200):
    """把 config 里的 sweep 段规范成 [{"name":..,"values":[..]}, ...]。"""
    if not spec:
        return []
    if isinstance(spec, dict):
        if spec.get("parameter") or spec.get("name"):
            spec = [{"name": spec.get("parameter") or spec.get("name"),
                     "values": spec.get("values") or []}]
        else:
            raise SweepError("SWEEP_INVALID", f"sweep 段缺少 parameter/values: {spec}")
    dims = []
    for d in spec:
        name = d.get("name") or d.get("parameter")
        values = d.get("values") or []
        if not name:
            raise SweepError("SWEEP_INVALID", f"sweep 维度缺少 name: {d}")
        if not values:
            raise SweepError("SWEEP_INVALID", f"sweep 维度 {name} 没有 values")
        dims.append({"name": str(name), "values": list(values)})
    total = 1
    for d in dims:
        total *= len(d["values"])
    if total > max_cases:
        raise SweepError(
            "SWEEP_TOO_MANY_CASES",
            f"sweep 展开出 {total} 个 case，超过上限 max_cases={max_cases}；"
            f"请收敛参数范围或显式提高 max_cases")
    return dims


def expand_cases(dims):
    """[{'R':[1,2]},{'C':[3,4]}] → [{...}, ...]（笛卡尔积，顺序稳定）。"""
    if not dims:
        return []
    names = [d["name"] for d in dims]
    out = []
    for i, combo in enumerate(itertools.product(*[d["values"] for d in dims])):
        params = dict(zip(names, combo))
        slug = "_".join(f"{n}={_slug(v)}" for n, v in params.items())
        out.append({"index": i + 1, "id": f"{i + 1:03d}_{slug}", "params": params})
    return out


def _slug(v):
    s = str(v)
    return re.sub(r"[^0-9A-Za-z.+-]", "", s) or "v"


def apply_params(deck_text, params):
    """按元件名 / .param 名改值，返回 (新 deck, 每个参数替换次数)。

    电阻扫到 0 时自动换成 1 mΩ：ngspice 对 0Ω 只 clamp 到 1e-12，
    且 1µΩ 量级会让 tran 出现 "timestep too small" 挂死（实测）。
    """
    text = deck_text
    counts = {}
    for name, value in params.items():
        n = 0
        elem_pat = re.compile(r"(?im)^([ \t]*" + re.escape(name) + r"[ \t]+)(\S.*)$")

        def _rep(m):
            nonlocal n
            toks = m.group(2).split()
            if not toks:
                return m.group(0)
            new = str(value)
            if name[:1].upper() == "R":
                try:
                    if float(new) == 0.0:
                        new = "1m"
                except ValueError:
                    pass
            toks[-1] = new
            n += 1
            return m.group(1) + " ".join(toks)

        text = elem_pat.sub(_rep, text)
        par_pat = re.compile(
            r"(?im)^([ \t]*\.param[^\n]*?\b" + re.escape(name) + r"[ \t]*=[ \t]*)(\S+)")
        text, m2 = par_pat.subn(lambda m: m.group(1) + str(value), text)
        n += m2
        if n == 0:
            raise SweepError(
                "SWEEP_PARAM_NOT_FOUND",
                f"参数 {name} 在 deck 里既不是元件名也不是 .param；"
                f"扫描只能改已存在的元件值（可给元件显式命名，如 element_name: {name}）")
        counts[name] = n
    return text, counts


def retarget_raw(deck_text, case_dir_rel):
    """把 write x.raw 改写到 case 子目录，避免各 case 互相覆盖。"""
    prefix = str(case_dir_rel).replace("\\", "/").strip("/") + "/"
    return re.sub(r"(?im)^([ \t]*write[ \t]+)(\S+\.raw)", lambda m: m.group(1) + prefix + m.group(2),
                  deck_text)


def run_ngspice(ngspice_bin, deck_path, cwd, timeout=600):
    try:
        proc = subprocess.run([ngspice_bin, "-b", os.path.basename(deck_path)],
                              cwd=cwd, capture_output=True, timeout=timeout)
        return (proc.returncode,
                (proc.stdout or b"").decode("utf-8", "replace"),
                (proc.stderr or b"").decode("utf-8", "replace"))
    except FileNotFoundError as e:
        raise SweepError("NGSPICE_NOT_FOUND", f"找不到 ngspice: {ngspice_bin}") from e
    except subprocess.TimeoutExpired:
        return (-9, "", f"ngspice 超时（>{timeout}s）")


def _raw_files(case_dir):
    return sorted(f for f in os.listdir(case_dir) if f.lower().endswith(".raw"))


def run_case(ngspice_bin, base_deck, case, workdir, mode, metric_specs,
             retarget=True, timeout=600):
    """跑一个 case，返回 (record, plots)。"""
    case_id = case["id"]
    rel = os.path.join("runs", case_id)
    case_dir = os.path.join(workdir, rel)
    os.makedirs(case_dir, exist_ok=True)
    deck_text, counts = apply_params(base_deck, case["params"])
    if retarget:
        deck_text = retarget_raw(deck_text, rel)
    deck_name = f"deck_{case_id}.cir"
    deck_path = os.path.join(workdir, deck_name)
    with open(deck_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(deck_text)
    rc, out, err = run_ngspice(ngspice_bin, deck_path, workdir, timeout=timeout)
    with open(os.path.join(case_dir, "stdout.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    with open(os.path.join(case_dir, "stderr.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(err)
    shutil.copyfile(deck_path, os.path.join(case_dir, "deck.cir"))

    record = {"id": case_id, "index": case["index"], "params": case["params"],
              "returncode": rc, "dir": rel.replace("\\", "/"),
              "raw": [], "metrics": {}, "metric_errors": []}
    if rc != 0:
        record["error"] = f"ngspice exit {rc}"
        tail = [ln for ln in (out + "\n" + err).splitlines() if ln.strip()][-6:]
        record["log_tail"] = tail

    plots = []
    for rf in _raw_files(case_dir):
        path = os.path.join(case_dir, rf)
        try:
            p = parse_raw.parse_all(path)
        except Exception as e:      # raw 损坏不影响其它 case
            record.setdefault("raw_errors", []).append(f"{rf}: {e}")
            continue
        plots.extend(p)
        record["raw"].append(rf)
        try:
            parse_raw.plots_to_csv(p, os.path.join(case_dir, rf[:-4] + ".csv"))
        except Exception:
            pass
    if plots and metric_specs:
        res, errs = metrics_mod.compute(plots, metric_specs, mode=mode)
        record["metrics"] = res.get("metrics") or {}
        record["metric_errors"] = errs
    with open(os.path.join(case_dir, "metrics.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(record, f, ensure_ascii=False, indent=1)
    return record, plots


def flatten_metrics(m):
    """metrics dict → 一层标量，供 CSV 汇总。"""
    flat = {}
    for label, val in (m or {}).items():
        if not isinstance(val, dict):
            continue
        if val.get("error"):
            flat[label] = f"ERROR:{val['error']}"
            continue
        for k, v in val.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                flat[f"{label}.{k}"] = v
    return flat


def run_sweep(ngspice_bin, base_deck, dims, workdir, mode, metric_specs,
              max_cases=200, timeout=600, log=print):
    cases = expand_cases(dims)
    if len(cases) > max_cases:
        raise SweepError("SWEEP_TOO_MANY_CASES",
                         f"{len(cases)} 个 case 超过 max_cases={max_cases}")
    log(f"[sweep] {len(cases)} case(s): "
        + " x ".join(f"{d['name']}∈{d['values']}" for d in dims))
    records = []
    for case in cases:
        rec, _ = run_case(ngspice_bin, base_deck, case, workdir, mode,
                          metric_specs, timeout=timeout)
        records.append(rec)
        log(f"  [{rec['index']:>3}/{len(cases)}] {rec['id']} rc={rec['returncode']} "
            + ", ".join(metrics_mod.summary_line(k, v) for k, v in rec["metrics"].items()))
    csv_path, json_path = write_summary(records, dims, workdir)
    return records, csv_path, json_path


def write_summary(records, dims, workdir):
    keys = []
    for rec in records:
        for k in flatten_metrics(rec["metrics"]):
            if k not in keys:
                keys.append(k)
    param_names = [d["name"] for d in dims]
    header = ["case_id", "index", "returncode"] + param_names + keys
    rows = [header]
    for rec in records:
        flat = flatten_metrics(rec["metrics"])
        row = [rec["id"], rec["index"], rec["returncode"]]
        row += [rec["params"].get(n, "") for n in param_names]
        row += [flat.get(k, "") for k in keys]
        rows.append(row)
    csv_path = os.path.join(workdir, "sweep_summary.csv")
    with open(csv_path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(",".join(_csv_cell(c) for c in r) + "\n")
    json_path = os.path.join(workdir, "sweep_summary.json")
    with open(json_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"parameters": param_names, "metric_columns": keys,
                   "cases": records}, f, ensure_ascii=False, indent=1)
    return csv_path, json_path


def _csv_cell(v):
    s = str(v)
    if any(c in s for c in ",\"\n"):
        s = '"' + s.replace('"', '""') + '"'
    return s
