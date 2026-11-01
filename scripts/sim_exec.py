#!/usr/bin/env python3
"""v2.2 仿真执行器：deck → ngspice → raw → metrics → manifest。

用法:
  sim_exec.py <schematic.cir> <mode> [mode_args] \
      --config sim.json --report netlist_report.json --workdir . \
      --project "Wolf-Lite" --source x.epro2

职责边界：只负责"网表已经有了"之后的事情（v2.1 里 build_deck + ngspice +
parse_raw 那一段），这样 netlist_gen 的模型/override 逻辑与仿真执行彻底解耦。
参数扫描、指标提取、manifest 都在这里编排。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_deck                                        # noqa: E402
import metrics as metrics_mod                            # noqa: E402
import parse_raw                                         # noqa: E402
import sim_manifest                                      # noqa: E402
import sweep_runner                                      # noqa: E402
from sim_config import load_config, load_json            # noqa: E402

DEFAULT_NGSPICE = os.path.expanduser(
    "~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe")


def _pop(args, name):
    if name in args:
        i = args.index(name)
        val = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2 if val is not None else i + 1]
        return val
    return None


def main():
    argv = sys.argv[1:]
    if len(argv) < 2:
        print(__doc__)
        sys.exit(2)
    schematic, mode = argv[0], argv[1]
    rest = argv[2:]
    flags = {
        "config": _pop(rest, "--config"),
        "report": _pop(rest, "--report"),
        "workdir": _pop(rest, "--workdir") or ".",
        "project": _pop(rest, "--project"),
        "source": _pop(rest, "--source"),
        "ngspice": _pop(rest, "--ngspice"),
    }
    arg = rest[0] if rest else None
    workdir = os.path.abspath(flags["workdir"])
    os.makedirs(workdir, exist_ok=True)
    cfg = load_config(flags["config"]) if flags["config"] else load_config(None)
    ng_bin = flags["ngspice"] or os.environ.get("NGSPICE_BIN") or DEFAULT_NGSPICE
    nr = {}
    if flags["report"] and os.path.exists(flags["report"]):
        nr = load_json(flags["report"])

    with open(schematic, encoding="utf-8") as f:
        raw = f.read()

    analysis = {"mode": mode, "args": arg,
                "sweep": cfg.get("sweep") or None}
    metric_specs = cfg.get("metrics") or []
    max_cases = int(cfg.get("max_cases") or 200)

    print(f"[sim] mode={mode} args={arg or '-'} workdir={workdir}")
    if nr.get("fidelity"):
        for ln in sim_manifest.fidelity_lines(nr):
            print("  " + ln)

    runs, exit_ok = [], True
    try:
        base_deck = build_deck.build(raw, mode, arg)
    except ValueError as e:
        print(f"deck 生成失败: {e}")
        sys.exit(2)

    if cfg.get("sweep"):
        try:
            dims = sweep_runner.normalize_sweep(cfg["sweep"], max_cases)
            records, csv_path, json_path = sweep_runner.run_sweep(
                ng_bin, base_deck, dims, workdir, mode, metric_specs,
                max_cases=max_cases)
        except sweep_runner.SweepError as e:
            print(f"[ERROR] {e}")
            sys.exit(4)
        runs = records
        exit_ok = all(r["returncode"] == 0 for r in records)
        print(f"[sweep] summary: {csv_path}")
        print(f"[sweep] summary: {json_path}")
        best = _best_row(records, metric_specs)
        if best:
            print(f"[sweep] best: {best}")
    else:
        deck_path = os.path.join(workdir, "deck.cir")
        with open(deck_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(base_deck)
        rc, out, err = sweep_runner.run_ngspice(ng_bin, deck_path, workdir)
        with open(os.path.join(workdir, "stdout.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write(out)
        with open(os.path.join(workdir, "stderr.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write(err)
        tail = [ln for ln in (out + "\n" + err).splitlines() if ln.strip()]
        print("\n".join(tail[-25:]))
        exit_ok = rc == 0
        run = {"id": "single", "index": 1, "params": {}, "returncode": rc,
               "dir": ".", "raw": [], "metrics": {}, "metric_errors": []}
        plots = []
        for fn in sorted(os.listdir(workdir)):
            if not fn.lower().endswith(".raw"):
                continue
            path = os.path.join(workdir, fn)
            try:
                p = parse_raw.parse_all(path)
            except Exception as e:
                run.setdefault("raw_errors", []).append(f"{fn}: {e}")
                continue
            plots.extend(p)
            run["raw"].append(fn)
            parse_raw.plots_to_csv(p, os.path.join(workdir, fn[:-4] + ".csv"))
        if plots and metric_specs:
            res, errs = metrics_mod.compute(plots, metric_specs, mode=mode)
            run["metrics"] = res.get("metrics") or {}
            run["metric_errors"] = errs
            for k, v in run["metrics"].items():
                print("  [metric ] " + metrics_mod.summary_line(k, v))
            for e in errs:
                print(f"  [metric!] {e['metric']}: {e['code']} {e['message']}")
        if run["metrics"]:
            with open(os.path.join(workdir, "metrics.json"), "w",
                      encoding="utf-8", newline="\n") as f:
                json.dump(run["metrics"], f, ensure_ascii=False, indent=1)
        runs = [run]

    src_hash = sim_manifest.file_hash(flags["source"]) if flags["source"] else None
    ngver = sim_manifest.ngspice_version(ng_bin)
    manifest = sim_manifest.build(
        nr, analysis,
        project=flags["project"] or nr.get("project"),
        source_hash=src_hash, ngspice=ngver, workdir=workdir, runs=runs)
    mpath = sim_manifest.write(manifest, os.path.join(workdir, "simulation_manifest.json"))
    print(f"[sim] manifest: {mpath}")
    for ln in sim_manifest.summary_lines(manifest):
        print("  " + ln)
    if not exit_ok:
        print("[sim] ngspice 返回非零，结果不完整")
        sys.exit(5)


def _best_row(records, metric_specs):
    """粗挑一个"最好的" case（指标最小化 overshoot / 阻抗峰）。"""
    def score(rec):
        flat = sweep_runner.flatten_metrics(rec["metrics"])
        keys = [k for k in flat if k.endswith(".overshoot_pct")
                or k.endswith(".undershoot_pct") or k.endswith(".max_impedance")]
        if not keys:
            return None
        return sum(float(flat[k]) for k in keys if isinstance(flat[k], (int, float)))

    scored = [(score(r), r) for r in records if r["returncode"] == 0]
    scored = [(s, r) for s, r in scored if s is not None]
    if not scored:
        return None
    s, r = min(scored, key=lambda x: x[0])
    return f"{r['id']} params={r['params']} score={s:.4g}"


if __name__ == "__main__":
    main()
