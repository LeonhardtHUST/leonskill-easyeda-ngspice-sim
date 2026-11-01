#!/usr/bin/env python3
"""v2.3 仿真执行器：deck → ngspice → raw → metrics → manifest（全部外部执行）。

架构边界（v2.3 第一原则）：
    EasyEDA = CAD 数据源；ngspice = 仿真引擎；本脚本 = 编排层。
    任何 batch 仿真都不需要打开 EasyEDA、不需要切到 Simulation 页面、不需要 UI 操作。

模式：
  op | tran | ac | dc | custom        单次分析
  sweep                               config.sweep（v2.2 机制）
  pdn                                 config.ports → 多端口 Z 矩阵
  sparam                              config.sparam → sp 分析，输出 S11/S21 并与 .sNp 对比
  montecarlo                          config.montecarlo → 分布抽样 + 统计 + 失败率
  corner                              config.corner → VIN×TEMP×LOAD 笛卡尔积

用法：
  sim_exec.py <schematic.cir> <mode> [args] --config sim.json --report netlist_report.json
              --workdir . [--workspace sim-workspace] [--ngspice BIN]
"""
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_deck                                        # noqa: E402
import compare_runs                                      # noqa: E402
import metrics as metrics_mod                            # noqa: E402
import parse_raw                                         # noqa: E402
import sim_manifest                                      # noqa: E402
import sweep_runner                                      # noqa: E402
from sim_config import load_config, load_json            # noqa: E402

DEFAULT_NGSPICE = os.path.expanduser(
    "~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe")

PDN_MODES = ("pdn",)
MC_MODES = ("montecarlo", "mc")
CORNER_MODES = ("corner",)
DECKLESS_MODES = PDN_MODES + MC_MODES + CORNER_MODES + ("sparam",)


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
    mode = mode.lower()
    rest = argv[2:]
    flags = {
        "config": _pop(rest, "--config"),
        "report": _pop(rest, "--report"),
        "workdir": _pop(rest, "--workdir") or ".",
        "project": _pop(rest, "--project"),
        "source": _pop(rest, "--source"),
        "ngspice": _pop(rest, "--ngspice"),
        "workspace": _pop(rest, "--workspace"),
        "ports": _pop(rest, "--ports"),
        "cases": _pop(rest, "--cases"),
        "seed": _pop(rest, "--seed"),
        "criteria": _pop(rest, "--criteria"),
    }
    arg = rest[0] if rest else None
    workdir = os.path.abspath(flags["workdir"])
    os.makedirs(workdir, exist_ok=True)
    cfg = load_config(flags["config"]) if flags["config"] else load_config(None)
    if flags["ports"]:
        cfg["ports"] = load_json(flags["ports"])
    if flags["cases"]:
        cfg.setdefault("montecarlo", {})["cases"] = int(flags["cases"])
    if flags["seed"]:
        cfg.setdefault("montecarlo", {})["seed"] = int(flags["seed"])
    if flags["criteria"]:
        crit = load_json(flags["criteria"])
        cfg["criteria"] = crit.get("criteria") if isinstance(crit, dict) else crit
    ng_bin = flags["ngspice"] or os.environ.get("NGSPICE_BIN") or DEFAULT_NGSPICE
    nr = {}
    if flags["report"] and os.path.exists(flags["report"]):
        nr = load_json(flags["report"])

    ws = None
    tool_dir = workdir
    if flags["workspace"]:
        import sim_workspace
        ws = sim_workspace.Workspace(flags["workspace"])
        if flags["config"]:
            with open(os.path.join(ws.configs, os.path.basename(flags["config"])),
                      "w", encoding="utf-8", newline="\n") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1, default=str)
        if flags["report"]:
            ws.copy_in(flags["report"], "source")
        ws.write_json("source", "schematic_ir.json", nr)
        tool_dir = ws.runs

    with open(schematic, encoding="utf-8") as f:
        raw = f.read()

    analysis = {"mode": mode, "args": arg, "sweep": cfg.get("sweep") or None}
    metric_specs = cfg.get("metrics") or []
    max_cases = int(cfg.get("max_cases") or 200)

    print(f"[sim] mode={mode} args={arg or '-'} workdir={workdir}"
          + (f" workspace={ws.root}" if ws else ""))
    if nr.get("fidelity"):
        for ln in sim_manifest.fidelity_lines(nr):
            print("  " + ln)

    deck_mode = "op" if mode in DECKLESS_MODES else mode
    deck_arg = None if mode in DECKLESS_MODES else arg
    try:
        base_deck = build_deck.build(raw, deck_mode, deck_arg)
    except ValueError as e:
        print(f"deck 生成失败: {e}")
        sys.exit(2)

    runs, exit_ok, extra = [], True, {}
    try:
        if mode in PDN_MODES:
            exit_ok, runs, extra = _run_pdn(cfg, base_deck, ng_bin, tool_dir)
        elif mode == "sparam":
            exit_ok, runs, extra = _run_sparam(cfg, base_deck, ng_bin, tool_dir)
        elif mode in MC_MODES or mode in CORNER_MODES:
            exit_ok, runs, extra = _run_cases(cfg, base_deck, ng_bin, tool_dir, mode,
                                             metric_specs, max_cases)
        else:
            exit_ok, runs = _run_single(cfg, base_deck, ng_bin, tool_dir, mode, arg,
                                        metric_specs)
    except Exception as e:
        code = getattr(e, "code", "SIM_ERROR")
        msg = getattr(e, "message", str(e))
        print(f"[ERROR] {code}: {msg}")
        manifest = sim_manifest.build(
            nr, analysis, project=flags["project"] or nr.get("project"),
            source_hash=sim_manifest.file_hash(flags["source"]) if flags["source"] else None,
            ngspice=sim_manifest.ngspice_version(ng_bin), workdir=workdir, runs=[],
            errors=[{"code": code, "message": msg}])
        manifest["confidence"] = compare_runs.confidence(manifest, extra)
        path = (ws.save_manifest(manifest) if ws else
                sim_manifest.write(manifest, os.path.join(tool_dir,
                                                          "simulation_manifest.json")))
        print(f"[sim] manifest: {path}")
        sys.exit(6)

    src_hash = (sim_manifest.file_hash(flags["source"]) if flags["source"]
                else sim_manifest.file_hash(schematic))
    manifest = sim_manifest.build(
        nr, analysis, project=flags["project"] or nr.get("project"),
        source_hash=src_hash, ngspice=sim_manifest.ngspice_version(ng_bin),
        workdir=workdir, runs=runs)
    for k in ("pdn", "sparam", "montecarlo", "corner", "geometry", "stackup"):
        if k in extra:
            manifest[k] = extra[k]
    manifest["confidence"] = compare_runs.confidence(manifest, extra)
    if ws:
        if extra.get("geometry"):
            ws.write_json("source", "pcb_geometry.json", extra["geometry"])
        if extra.get("stackup"):
            ws.write_json("source", "stackup.json", extra["stackup"])
        path = ws.save_manifest(manifest)
    else:
        path = sim_manifest.write(manifest, os.path.join(tool_dir,
                                                         "simulation_manifest.json"))
    print(f"[sim] manifest: {path}")
    for ln in sim_manifest.summary_lines(manifest):
        print("  " + ln)
    conf = manifest["confidence"]
    print(f"  confidence: {conf['level']} (score {conf['score']:+d})")
    for d, why in conf["rules"][:6]:
        print(f"    {d:>3s} {why}")
    if not exit_ok:
        print("[sim] 有 case 失败，结果不完整")
        sys.exit(5)


# --------------------------------------------------------------------------
def _run_single(cfg, deck_text, ng_bin, workdir, mode, arg, metric_specs):
    deck_path = os.path.join(workdir, "deck.cir")
    with open(deck_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(deck_text)
    if cfg.get("sweep"):
        dims = sweep_runner.normalize_sweep(cfg["sweep"], cfg.get("max_cases") or 200)
        records, csv_path, json_path = sweep_runner.run_sweep(
            ng_bin, deck_text, dims, workdir, mode, metric_specs,
            max_cases=int(cfg.get("max_cases") or 200))
        print(f"[sweep] summary: {csv_path}")
        print(f"[sweep] summary: {json_path}")
        return all(r["returncode"] == 0 for r in records), records
    rc, out, err = sweep_runner.run_ngspice(ng_bin, deck_path, workdir)
    for name, txt in (("stdout.txt", out), ("stderr.txt", err)):
        with open(os.path.join(workdir, name), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write(txt)
    tail = [ln for ln in (out + "\n" + err).splitlines() if ln.strip()]
    print("\n".join(tail[-25:]))
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
        with open(os.path.join(workdir, "metrics.json"), "w", encoding="utf-8",
                  newline="\n") as f:
            json.dump(run["metrics"], f, ensure_ascii=False, indent=1)
    return rc == 0, [run]


def _run_pdn(cfg, deck_text, ng_bin, workdir):
    import pdn_multiport
    ports = cfg.get("ports") or {}
    res, log = pdn_multiport.run(
        deck_text, ports, ng_bin, workdir,
        f_start=cfg.get("f_start", "1k"), f_stop=cfg.get("f_stop", "100meg"),
        ppd=int(cfg.get("ppd") or 20), strict=bool(cfg.get("strict")))
    with open(os.path.join(workdir, "pdn.json"), "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    for ln in pdn_multiport.summary_lines(res):
        print("  " + ln)
    return True, [{"id": "pdn", "index": 1, "params": {}, "returncode": 0,
                   "dir": ".", "raw": ["pdn.raw"], "metrics": {}}], {"pdn": {
                       "ports": res["ports"], "note": res["note"],
                       "resonances": res.get("resonances"),
                       "anti_resonances": res.get("anti_resonances")}}


def _run_sparam(cfg, deck_text, ng_bin, workdir):
    """对含端口的电路做 sp 分析，输出 S11/S21，并与 .sNp 参考对比。"""
    import pdn_multiport
    import sparam_metrics
    import touchstone
    spec = cfg.get("sparam") or {}
    ports = spec.get("ports") or cfg.get("ports")
    if not ports:
        raise ValueError("sparam 模式需要 config.sparam.ports（两个端口）")
    norm = pdn_multiport.normalize_ports({"ports": ports})
    if len(norm) != 2:
        raise ValueError("sparam 模式目前只支持 2 端口")
    deck = pdn_multiport.inject_ports(deck_text, norm)
    ctrl = pdn_multiport.build_sp_control(2, spec.get("f_start", "1k"),
                                         spec.get("f_stop", "100meg"),
                                         int(spec.get("ppd") or 20))
    m = re.search(r"(?im)^[ \t]*\.end[ \t]*$", deck)
    deck = (deck[:m.start()] + ctrl + ".end\n" + deck[m.end():]) if m else deck + ctrl
    path = os.path.join(workdir, "sparam_deck.cir")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(deck)
    rc, out, err = sweep_runner.run_ngspice(ng_bin, path, workdir)
    if rc != 0:
        raise RuntimeError("ngspice sp 分析失败: " + (out + err)[-400:])
    plots = parse_raw.parse_all(os.path.join(workdir, "pdn.raw"))
    plot = next((p for p in plots if "complex" in str(p.get("flags"))), plots[0])
    freqs = []
    for row in plot["points"]:
        x = row[0]
        freqs.append(float(x[0]) if isinstance(x, list) else float(x))

    def series(name):
        idx = pdn_multiport._var_index(plot, name)
        if idx is None:
            return None
        vals = []
        for row in plot["points"]:
            v = row[idx]
            vals.append(complex(v[0], v[1]) if isinstance(v, list) else complex(v, 0))
        return vals

    s11, s21 = series("s_1_1"), series("s_2_1")
    if s11 is None or s21 is None:
        raise RuntimeError("sp 分析没有输出 S_1_1 / S_2_1")
    db = lambda v: 20 * math.log10(max(abs(v), 1e-15))
    sim_metrics = {
        "file": "simulated", "num_ports": 2, "n_points": len(freqs),
        "s11_db": [db(v) for v in s11], "s21_db": [db(v) for v in s21],
        "insertion_loss_db": -max(db(v) for v in s21),
        "return_loss_db": -min(db(v) for v in s11),
    }
    print(f"  [sparam ] simulated: insertion loss "
          f"{sim_metrics['insertion_loss_db']:.2f} dB, "
          f"return loss {sim_metrics['return_loss_db']:.2f} dB")
    extra = {"sparam": {"simulated": sim_metrics}}
    ref = spec.get("reference")
    if ref:
        refp = ref if os.path.isabs(ref) else os.path.join(cfg.get("_dir") or ".", ref)
        ts = touchstone.parse(refp)
        rm = sparam_metrics.metrics(ts)
        extra["sparam"]["reference"] = rm
        print(f"  [sparam ] reference {os.path.basename(refp)}: insertion loss "
              f"{rm.get('insertion_loss_db', 0):.2f} dB")
        diffs = []
        for i, f in enumerate(freqs):
            j = min(range(len(ts.frequencies)),
                    key=lambda k: abs(ts.frequencies[k] - f))
            if abs(ts.frequencies[j] - f) / max(f, 1) < 0.05:
                diffs.append(sim_metrics["s21_db"][i] - ts.db(1, 0)[j])
        if diffs:
            mx = max(abs(d) for d in diffs)
            extra["sparam"]["s21_max_err_db"] = mx
            print(f"  [sparam ] max |ΔS21| = {mx:.2f} dB over {len(diffs)} pts")
    return True, [{"id": "sparam", "index": 1, "params": {}, "returncode": 0,
                   "dir": ".", "raw": ["pdn.raw"], "metrics": {}}], extra


def _run_cases(cfg, deck_text, ng_bin, workdir, mode, metric_specs, max_cases):
    import monte_carlo as mc
    if mode in MC_MODES:
        block = cfg.get("montecarlo") or cfg.get("mc") or {}
        specs = block.get("parameters") or {}
        n = int(block.get("cases") or cfg.get("cases") or 100)
        seed = int(block.get("seed", cfg.get("seed", 1)))
        cases = mc.expand(specs, n, seed=seed,
                          max_cases=int(block.get("max_cases") or 1000))
        label = "montecarlo"
    else:
        block = cfg.get("corner") or {}
        cases = mc.corner(block.get("axes") or block, max_cases=max_cases)
        label = "corner"
    workers = block.get("workers", cfg.get("workers"))
    criteria = block.get("criteria") or cfg.get("criteria")
    records = mc.run(deck_text, cases, ng_bin, workdir, "tran", metric_specs,
                     workers=workers, label=label)
    summary = mc.summarize(records, criteria)
    csv_p, json_p = mc.write_outputs(records, summary, workdir)
    print(f"[{label}] summary: {csv_p}")
    print(f"[{label}] summary: {json_p}")
    for k, st in list(summary["metrics"].items())[:12]:
        print(f"  {k}: mean={st['mean']:.4g} std={st['std']:.4g} "
              f"p5={st['p5']:.4g} p95={st['p95']:.4g}")
    if summary["failure_rate"] is not None:
        print(f"  failure_rate = {summary['failure_rate'] * 100:.1f}% "
              f"({summary['failures']}/{summary['n_cases']})")
    ok = summary["rc_failures"] == 0
    return ok, records, {label: {k: v for k, v in summary.items() if k != "per_case"}}


if __name__ == "__main__":
    main()
