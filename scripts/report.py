#!/usr/bin/env python3
"""v2.3 P2：报告生成（Markdown）+ 可选图表导出。

    report.py <workspace|run目录> [--out report.md] [--plots]

不含 GUI、不依赖 matplotlib（有就画图，没有就只出 CSV——测试不依赖 GUI）。
报告内容：模型保真度 / 拓扑 / 扫描 / 指标 / 判据 / 假设 / 局限。
"""
import json
import os
import sys

DISCLAIMER = (
    "本报告由 easyeda-ngspice-sim 生成。Z0/TD 为解析估计（ANALYTIC_ESTIMATE），"
    "via 为经验估计（ENGINEERING_ESTIMATE），S 参数为窄带集总拟合。"
    "结论用于工程判断与 ECO 排序，不代替实测或场求解。"
)


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def collect(root):
    """从工作区/运行目录收集可用产物。"""
    if os.path.isfile(root):
        root = os.path.dirname(root)
    out = {"root": root, "manifest": None, "metrics": None, "sweep": None,
           "montecarlo": None, "pdn": None, "geometry": None, "netlist": None}
    cand = {
        "manifest": ["simulation_manifest.json"],
        "metrics": ["metrics.json"],
        "sweep": ["sweep_summary.json"],
        "montecarlo": ["montecarlo_summary.json"],
        "pdn": ["pdn.json"],
        "netlist": ["netlist_report.json"],
    }
    for key, names in cand.items():
        for n in names:
            for base in (root, os.path.join(root, "runs")):
                p = os.path.join(base, n)
                if os.path.exists(p):
                    try:
                        out[key] = _load(p)
                    except Exception:
                        pass
                    break
    for p in (os.path.join(root, "source", "pcb_geometry.json"),
              os.path.join(root, "pcb_geometry.json")):
        if os.path.exists(p):
            out["geometry"] = _load(p)
            break
    return out


def render(data, with_plots=False):
    L = []
    L.append("# 仿真报告\n")
    m = data.get("manifest") or {}
    L.append("## 概要\n")
    L.append(f"- 工程：{m.get('source_project') or '-'}")
    L.append(f"- skill / ngspice：{m.get('skill_version')} / {m.get('ngspice_version')}")
    L.append(f"- 分析：{(m.get('analysis') or {}).get('mode')} "
             f"{(m.get('analysis') or {}).get('args') or ''}")
    L.append(f"- power policy / strict：{m.get('power_policy')} / {m.get('strict')}")
    conf = m.get("confidence") or {}
    if conf:
        L.append(f"- 置信度：**{conf.get('level')}** (score {conf.get('score'):+d})")
        for d, why in conf.get("rules") or []:
            L.append(f"  - `{d}` {why}")
    L.append("")

    nr = data.get("netlist") or {}
    L.append("## 模型保真度\n")
    fid = (nr.get("fidelity") or {}).get("blocks") or {}
    if fid:
        L.append("| 模块 | Level | 说明 |")
        L.append("|---|---:|---|")
        for name, info in sorted(fid.items(), key=lambda kv: -kv[1]["level"]):
            L.append(f"| {name} | {info['level']} | {info.get('what') or ''} |")
    else:
        L.append("（无 fidelity 数据）")
    L.append("")
    if nr.get("models"):
        L.append("### 厂商 SPICE 模型\n")
        L.append("| 器件 | 型号 | 源文件 | subckt | pin mapping |")
        L.append("|---|---|---|---|---|")
        for x in nr["models"]:
            L.append(f"| {x.get('designator')} | {x.get('mpn') or x.get('key')} | "
                     f"{x.get('file_display')} | {x.get('subckt')} | {x.get('status')} |")
        L.append("")
    if nr.get("ibis"):
        L.append("### IBIS 派生模型（IBIS_DERIVED_BEHAVIORAL_MODEL）\n")
        L.append("| 器件.引脚 | 模型 | C_total (pF) | Rout (Ω) |")
        L.append("|---|---|---:|---:|")
        for x in nr["ibis"]:
            L.append(f"| {x.get('designator')}.{x.get('pin')} | {x.get('model')} | "
                     f"{x.get('c_total_pf')} | {x.get('rout_ohm') or '-'} |")
        L.append("")
    if nr.get("behavioral"):
        L.append("### 行为级占位（近似）\n")
        for b in nr["behavioral"]:
            L.append(f"- **{b['designator']}** {b.get('part')}: {b.get('type')}"
                     + (f"；未仿真：{', '.join(b.get('not_simulated') or [])}"
                        if b.get("not_simulated") else ""))
        L.append("")
    if nr.get("excluded"):
        L.append("### 未进入仿真的器件\n")
        L.append(", ".join(f"{e['designator']}({e.get('mpn') or e.get('title')})"
                           for e in nr["excluded"]))
        L.append("")

    g = data.get("geometry")
    if g:
        L.append("## PCB 几何\n")
        L.append(f"- 层：{', '.join(l['name'] for l in g.get('layers') or [])}")
        L.append(f"- 单位：{g['units']['internal']} → mm（×{g['units']['scale_mm_per_unit']}）")
        L.append("")
        L.append("| 网络 | 段数 | 总长 (mm) | 过孔 | 端点 |")
        L.append("|---|---:|---:|---:|---:|")
        for nm, net in list((g.get("nets") or {}).items())[:15]:
            L.append(f"| {nm} | {len(net['segments'])} | {net.get('total_length_mm')} | "
                     f"{len(net.get('vias') or [])} | {len(net.get('terminals') or {})} |")
        L.append("")

    p = data.get("pdn")
    if p:
        L.append("## 多端口 PDN\n")
        L.append("| 端口 | 节点 | 返回 | |Z|max (Ω) | f (MHz) |")
        L.append("|---|---|---|---:|---:|")
        for key, rec in (p.get("impedance") or {}).items():
            i, j = (int(x) for x in key.split(","))
            if i != j:
                continue
            L.append(f"| {rec['from']} | {rec['from']} | | {rec.get('z_max_ohm'):.4g} | "
                     f"{(rec.get('z_max_f') or 0) / 1e6:.3g} |")
        for r in (p.get("resonances") or [])[:6]:
            L.append(f"- 谐振候选：{r['port']} @ {r['f_hz'] / 1e6:.3g} MHz "
                     f"({r['z_ohm']:.4g} Ω)")
        L.append(f"\n> {p.get('note', '')}\n")

    sw = data.get("sweep")
    if sw:
        L.append("## 参数扫描\n")
        L.append(f"- 参数：{', '.join(sw.get('parameters') or [])}")
        L.append(f"- case 数：{len(sw.get('cases') or [])}")
        L.append("")
    mc = data.get("montecarlo")
    if mc:
        s = mc.get("summary") or {}
        L.append("## Monte Carlo\n")
        L.append(f"- case 数：{s.get('n_cases')}")
        if s.get("failure_rate") is not None:
            L.append(f"- 失败率：**{s['failure_rate'] * 100:.1f}%** "
                     f"({s['failures']}/{s['n_cases']})")
        L.append("")
        L.append("| 指标 | mean | std | p5 | p95 |")
        L.append("|---|---:|---:|---:|---:|")
        for k, st in list((s.get("metrics") or {}).items())[:12]:
            L.append(f"| {k} | {st['mean']:.4g} | {st['std']:.4g} | "
                     f"{st['p5']:.4g} | {st['p95']:.4g} |")
        L.append("")

    mt = data.get("metrics")
    if mt:
        L.append("## 指标\n")
        L.append("| 指标 | 结果 |")
        L.append("|---|---|")
        for k, v in mt.items():
            L.append(f"| {k} | {_short(v)} |")
        L.append("")

    L.append("## 假设与局限\n")
    L.append(f"- {DISCLAIMER}")
    for w in (m.get("warnings") or [])[:8]:
        L.append(f"- {w}")
    L.append("")
    if with_plots:
        L.append("## 图表\n")
        L.append("（plots/ 目录下的 png/csv；本文件不内嵌二进制）")
        L.append("")
    return "\n".join(L)


def _short(v):
    if isinstance(v, dict):
        keep = {k: v[k] for k in ("max", "min", "peak_to_peak", "overshoot_pct",
                                  "ripple_pct", "z_max_ohm", "z_max_f", "skew",
                                  "total", "ringing")
                if k in v}
        return json.dumps(keep, ensure_ascii=False) if keep else "(见 json)"
    return str(v)


def plot_available():
    try:
        import matplotlib  # noqa: F401
        return True
    except Exception:
        return False


def try_plots(data, outdir):
    """有 matplotlib 才画；失败不影响报告。"""
    if not plot_available():
        return []
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return []
    made = []
    os.makedirs(outdir, exist_ok=True)
    p = data.get("pdn")
    if p:
        try:
            fig, ax = plt.subplots(figsize=(6, 3.5))
            f = p.get("freq_hz") or []
            for key, rec in (p.get("impedance") or {}).items():
                i, j = (int(x) for x in key.split(","))
                if i == j or i < j:
                    ax.loglog(f, rec["|z|_ohm"], label=f"Z{i}{j}")
            ax.set_xlabel("Hz")
            ax.set_ylabel("|Z| (ohm)")
            ax.legend()
            ax.grid(True, which="both", alpha=0.3)
            path = os.path.join(outdir, "pdn_impedance.png")
            fig.tight_layout()
            fig.savefig(path, dpi=120)
            plt.close(fig)
            made.append(path)
        except Exception:
            pass
    return made


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        sys.exit(2)
    root = argv[0]
    out = "report.md"
    with_plots = "--plots" in argv
    for i, a in enumerate(argv):
        if a == "--out" and i + 1 < len(argv):
            out = argv[i + 1]
    data = collect(root)
    text = render(data, with_plots)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"report written: {out}")
    if with_plots:
        plots = try_plots(data, os.path.join(root, "plots"))
        if plots:
            for p in plots:
                print(f"  plot: {p}")
        else:
            print("  (matplotlib 不可用，已跳过图表；CSV 仍在 runs/ 下)")


if __name__ == "__main__":
    main()
