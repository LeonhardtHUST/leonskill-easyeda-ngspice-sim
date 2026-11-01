#!/usr/bin/env python3
"""v2.4 P0-D：vector-fit SPICE 模型的 round-trip 验证。

    original .sNp → 拟合 → 生成的 SPICE 子电路 → ngspice sp 分析 → 反算 S

然后逐端口对比较 |Sij| 与相位，输出 `FIT_VALIDATION.json`。

为什么要做这一步：拟合误差是"模型 vs 数据"，round-trip 误差是"**网表真的在 ngspice 里
跑出来的结果** vs 数据"。后者才会暴露导出/命名/端口接错这类问题（实测过：内部 50Ω
端口网络与外部端口阻抗的关系搞错时，模型自检 0.001 dB，网表里 S21 却差 20 dB）。
"""
import json
import os
import shutil
import subprocess

import numpy as np

import parse_raw
import rf_network as rn

NGSPICE = os.environ.get("NGSPICE_BIN") or os.path.expanduser(
    "~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe")


class ValidationError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def build_deck(subckt_path, subckt_name, nports, f_start, f_stop, ppd=20,
               port_prefix="p"):
    """用 RF 端口包住子电路（`sp` 分析），与 PDN 用的是同一套机制。"""
    lines = [f"* round-trip validation deck for {subckt_name}",
             f'.include "{os.path.abspath(subckt_path).replace(os.sep, "/")}"']
    pins = " ".join(f"p{i + 1}" for i in range(nports))
    lines.append(f"X_DUT {pins} {subckt_name}")
    for i in range(nports):
        ac = 1 if i == 0 else 0
        lines.append(f"VPDN{i + 1} p{i + 1} 0 DC 0 AC {ac} portnum={i + 1} z0=50")
    lines += [".control", f"sp dec {ppd} {f_start:g} {f_stop:g}",
              "write rt.raw all", ".endc", ".end"]
    return "\n".join(lines) + "\n"


def _series(plot, name):
    for i, v in enumerate(plot.get("vars") or []):
        if v.lower() == name.lower():
            vals = []
            for row in plot["points"]:
                x = row[i]
                vals.append(complex(x[0], x[1]) if isinstance(x, list) else complex(x, 0))
            return np.array(vals)
    return None


def run_roundtrip(subckt_path, subckt_name, ref_net, workdir, ngspice=None,
                  ppd=20, timeout=300):
    """跑一次 round-trip，返回 (sim_net_dict, log)。"""
    ng = ngspice or NGSPICE
    os.makedirs(workdir, exist_ok=True)
    local = os.path.join(workdir, os.path.basename(subckt_path))
    if os.path.abspath(local) != os.path.abspath(subckt_path):
        shutil.copyfile(subckt_path, local)
    deck = build_deck(local, subckt_name, ref_net.nports,
                      float(ref_net.freq[0]), float(ref_net.freq[-1]), ppd)
    deck_path = os.path.join(workdir, "roundtrip.cir")
    with open(deck_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(deck)
    try:
        p = subprocess.run([ng, "-b", "roundtrip.cir"], cwd=workdir,
                           capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise ValidationError("NGSPICE_NOT_FOUND", f"{ng}: {e}") from e
    out = (p.stdout or b"").decode("utf-8", "replace") + "\n" + \
          (p.stderr or b"").decode("utf-8", "replace")
    if p.returncode != 0:
        raise ValidationError("ROUNDTRIP_SIM_FAILED",
                              f"ngspice 返回 {p.returncode}: " + out[-600:])
    raw = os.path.join(workdir, "rt.raw")
    if not os.path.exists(raw):
        raise ValidationError("ROUNDTRIP_NO_RAW", "没有生成 rt.raw: " + out[-600:])
    plots = parse_raw.parse_all(raw)
    plot = next((x for x in plots if "complex" in str(x.get("flags"))), plots[0])
    freqs = np.array([float(r[0][0] if isinstance(r[0], list) else r[0])
                      for r in plot["points"]])
    n = ref_net.nports
    s = np.zeros((len(freqs), n, n), dtype=complex)
    for i in range(n):
        for j in range(n):
            v = _series(plot, f"s_{i + 1}_{j + 1}")
            if v is None:
                raise ValidationError(
                    "ROUNDTRIP_NO_S", f"sp 分析没有 S_{i + 1}_{j + 1}")
            s[:, i, j] = v
    return {"freq_hz": freqs, "s": s, "nports": n}, out


def compare(ref_net, sim, freq_tol=0.02, floor_db=-120.0):
    """把仿真的 S 插值到参考频点，算幅度/相位/群时延误差。"""
    f_ref = np.asarray(ref_net.freq, dtype=float)
    f_sim = np.asarray(sim["freq_hz"], dtype=float)
    keep = (f_ref >= f_sim.min() * (1 - freq_tol)) & (f_ref <= f_sim.max() * (1 + freq_tol))
    if not np.any(keep):
        raise ValidationError("ROUNDTRIP_BAND_MISMATCH",
                              f"仿真频段 {f_sim.min():g}..{f_sim.max():g} 与参考"
                              f" {f_ref.min():g}..{f_ref.max():g} 不重叠")
    f = f_ref[keep]
    n = ref_net.nports
    s_ref = ref_net.s[keep]
    s_sim = np.zeros_like(s_ref)
    for i in range(n):
        for j in range(n):
            for k, fk in enumerate(f):
                idx = int(np.argmin(np.abs(f_sim - fk)))
                s_sim[k, i, j] = sim["s"][idx, i, j]
    mag_sim = 20 * np.log10(np.maximum(np.abs(s_sim), 1e-30))
    mag_ref = 20 * np.log10(np.maximum(np.abs(s_ref), 1e-30))
    # 对"接近 0 的 Sij"比较 dB 是没有意义的（-300 dB 与 -280 dB 都是零）。
    # 用 floor 把这些点排除，并如实报告排除了多少。
    floor = float(floor_db)
    valid = mag_ref > floor
    mag_err = (mag_sim - mag_ref)[valid]
    if mag_err.size == 0:
        raise ValidationError("ROUNDTRIP_ALL_BELOW_FLOOR",
                              f"所有 |Sij| 都低于 {floor} dB，无法评估")
    dphi = (np.angle(s_sim) - np.angle(s_ref))[valid]
    ph_err = np.degrees(np.mod(dphi + np.pi, 2 * np.pi) - np.pi)
    # 群时延误差（对相位差再取频率导数）
    gd = {}
    if len(f) > 3:
        w = 2 * np.pi * f
        gd_ref = -np.gradient(np.unwrap(np.angle(s_ref[:, 1, 0]), axis=0), w) if n > 1 else None
        gd_sim = -np.gradient(np.unwrap(np.angle(s_sim[:, 1, 0]), axis=0), w) if n > 1 else None
        if gd_ref is not None:
            gd = {"rms_group_delay_error_s": float(
                np.sqrt(np.mean((gd_sim - gd_ref) ** 2))),
                "max_group_delay_error_s": float(np.max(np.abs(gd_sim - gd_ref)))}
    return {
        "n_points_compared": int(len(f)),
        "n_entries_compared": int(mag_err.size),
        "n_entries_below_floor": int((~valid).sum()),
        "floor_db": floor,
        "band_hz": [float(f.min()), float(f.max())],
        "rms_mag_error_db": float(np.sqrt(np.mean(mag_err ** 2))),
        "max_mag_error_db": float(np.max(np.abs(mag_err))),
        "rms_phase_error_deg": float(np.sqrt(np.mean(ph_err ** 2))),
        "max_phase_error_deg": float(np.max(np.abs(ph_err))),
        **gd,
    }


def validate(subckt_path, subckt_name, ref_net, workdir, ngspice=None,
             passivity=None, tol_db=0.5, floor_db=-120.0):
    """完整 round-trip + 写 FIT_VALIDATION.json。返回 (report, path)。"""
    sim, log = run_roundtrip(subckt_path, subckt_name, ref_net, workdir, ngspice)
    err = compare(ref_net, sim, floor_db=floor_db)
    report = {
        "model": subckt_name,
        "subckt_file": os.path.abspath(subckt_path),
        "reference": ref_net.meta,
        "nports": ref_net.nports,
        "passivity": passivity,
        "reciprocity": ref_net.is_reciprocal() if ref_net.s is not None else None,
        **err,
        "tolerance_db": tol_db,
        "verdict": ("PASS" if err["max_mag_error_db"] <= tol_db else "FAIL"),
        "note": ("round-trip = 网表在 ngspice 里真跑出来的 S，与原始 .sNp 对比；"
                 "不是拟合残差"),
    }
    path = os.path.join(workdir, "FIT_VALIDATION.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return report, path


def summary_lines(rep):
    return [
        f"round-trip: {rep['n_points_compared']} pts "
        f"{rep['band_hz'][0]:.4g}..{rep['band_hz'][1]:.4g} Hz",
        f"  |S| error: rms {rep['rms_mag_error_db']:.4f} dB, "
        f"max {rep['max_mag_error_db']:.4f} dB (tol {rep['tolerance_db']} dB)",
        f"  phase error: rms {rep['rms_phase_error_deg']:.3f} deg, "
        f"max {rep['max_phase_error_deg']:.3f} deg",
        f"  verdict: {rep['verdict']}",
    ]
