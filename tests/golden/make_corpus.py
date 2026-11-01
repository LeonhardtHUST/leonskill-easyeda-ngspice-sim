#!/usr/bin/env python3
"""v2.4 golden corpus 生成器（解析真值，不依赖任何外部数据）。

    python tests/golden/make_corpus.py

产出 `tests/golden/{rf,si,pdn,meas}/*` 与 `MANIFEST.json`。所有条目都由**闭式公式**
生成，因此"期望值"是解析真值而不是"上次跑出来的数"——这是 golden corpus 的意义。

RF 用 ABCD 级联；N 端口用阻抗矩阵 Z → S = (Z−Z0I)(Z+Z0I)⁻¹。
"""
import cmath
import json
import math
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
Z0 = 50.0


def freq_grid(f1=1e5, f2=1e9, ppd=20):
    n = int(round(math.log10(f2 / f1) * ppd)) + 1
    return np.logspace(math.log10(f1), math.log10(f2), n)


def abcd_series(z):
    return np.array([[1, z], [0, 1]], dtype=complex)


def abcd_shunt(y):
    return np.array([[1, 0], [y, 1]], dtype=complex)


def abcd_tline(z0, td):
    def f(w):
        th = w * td
        return np.array([[math.cos(th), 1j * z0 * math.sin(th)],
                         [1j * math.sin(th) / z0, math.cos(th)]], dtype=complex)
    return f


def abcd_to_s(a, z0=Z0):
    A, B, C, D = a[0, 0], a[0, 1], a[1, 0], a[1, 1]
    den = A + B / z0 + C * z0 + D
    return (A + B / z0 - C * z0 - D) / den, 2.0 / den


def z_to_s(zm, z0=Z0):
    n = zm.shape[0]
    I = np.eye(n)
    return np.linalg.solve(zm + z0 * I, zm - z0 * I).T


def write_touchstone(path, freqs, s, z0=Z0, comment=""):
    s = np.asarray(s, dtype=complex)
    if s.ndim == 2:                       # 与频率无关的网络 → 广播到每个频点
        s = np.tile(s, (len(freqs), 1, 1))
    n = s.shape[1]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Hz S RI R %g\n" % z0)
        if comment:
            for line in comment.splitlines():
                f.write(f"! {line}\n")
        for k, fr in enumerate(freqs):
            row = ["%.9g" % fr]
            for i in range(n):
                for j in range(n):
                    row += ["%.9g" % s[k, i, j].real, "%.9g" % s[k, i, j].imag]
            f.write(" ".join(row) + "\n")
    return path


def write_csv(path, t, v, current=None, header=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write((header or "time(s),voltage(V)") + "\n")
        for i, ti in enumerate(t):
            if current is None:
                f.write(f"{ti:.12g},{v[i]:.9g}\n")
            else:
                f.write(f"{ti:.12g},{v[i]:.9g},{current[i]:.9g}\n")
    return path


# --------------------------------------------------------------------------
def rf_items(f):
    """2 端口基准：ideal thru / 6dB 衰减 / LC 低通 / 带通 / 传输线 / 已知 S2P。"""
    out = {}
    w = 2 * np.pi * f

    # 1) ideal thru
    s = np.zeros((len(f), 2, 2), dtype=complex)
    s[:, 0, 1] = s[:, 1, 0] = 1.0
    out["ideal_thru"] = (s, {"s21_db": 0.0, "s11_db_extreme": True})

    # 2) 6 dB 对称 T 型衰减器（R1=R2=150.5, R3=37.35 → 匹配 50Ω）
    K = 10 ** (6.0 / 20.0)                 # 6 dB → 电压比 1.995
    R1 = R2 = Z0 * (K - 1) / (K + 1)       # 16.61 Ω
    R3 = 2 * Z0 * K / (K ** 2 - 1)         # 66.95 Ω
    zm = np.array([[R1 + R3, R3], [R3, R2 + R3]], dtype=complex)
    out["attenuator_6db"] = (z_to_s(zm), {"s21_db_expected": -6.02, "s11_db_max": -40})

    # 3) 3 阶 LC 低通（220n / 100p / 220n）
    L1, C2, L3 = 220e-9, 100e-12, 220e-9
    s = np.zeros((len(f), 2, 2), dtype=complex)
    for k, wk in enumerate(w):
        a = abcd_series(1j * wk * L1)
        a = a @ abcd_shunt(1j * wk * C2)
        a = a @ abcd_series(1j * wk * L3)
        s11, s21 = abcd_to_s(a)
        s[k] = [[s11, s21], [s21, s11]]
    out["lc_lpf"] = (s, {"cutoff_3db_hz": 50.1e6, "note": "与 fixture_lc_lpf 同源"})

    # 4) 带通：串联 LC + 并联 LC（f0≈100 MHz）
    f0 = 100e6
    Ls, Cs = 100e-9, 25.3e-12
    Lp, Cp = 25.3e-9, 100e-12
    s = np.zeros((len(f), 2, 2), dtype=complex)
    for k, wk in enumerate(w):
        zs = 1j * wk * Ls + 1 / (1j * wk * Cs)
        yp = 1j * wk * Cp + 1 / (1j * wk * Lp)
        a = abcd_series(zs) @ abcd_shunt(yp) @ abcd_series(zs)
        s11, s21 = abcd_to_s(a)
        s[k] = [[s11, s21], [s21, s11]]
    out["bpf_100mhz"] = (s, {"center_hz": f0,
                             "note": "串联/并联 LC 两级，作为带通形状基准"})

    # 5) 50Ω 传输线，td=300ps
    tl = abcd_tline(Z0, 300e-12)
    s = np.zeros((len(f), 2, 2), dtype=complex)
    for k, wk in enumerate(w):
        s11, s21 = abcd_to_s(tl(wk))
        s[k] = [[s11, s21], [s21, s11]]
    out["tline_50ohm_300ps"] = (s, {"s11_db_max": -60,
                                    "phase_at_1ghz_deg": -108.0,
                                    "note": "无损 50Ω 线，S11≈0，相位= -ω·td；"
                                            "**纯时延不是有理函数，vector fitting 拟合不了**"})

    # 5b) 有损线（0.7 dB @1GHz 的 conductor+dielectric 损耗，用作可拟合的对照）
    alpha_np_per_m = 0.7 / 8.686 / 0.3        # 0.3 m 线上 0.7 dB
    s = np.zeros((len(f), 2, 2), dtype=complex)
    for k, wk in enumerate(w):
        g = math.exp(-alpha_np_per_m * 0.3) * cmath.exp(-1j * wk * 300e-12)
        s[k] = [[0.0, g], [g, 0.0]]
    out["tline_lossy_300ps"] = (s, {"insertion_loss_db": -0.7,
                                    "note": "带 0.7 dB 损耗的 300 ps 线，VF 可拟合"})
    return out


def si_items(f):
    """SI 基准：单线 / T 型分支 / 差分对（平衡与不平衡）/ 过孔失配 / 源端端接。"""
    out = {}
    w = 2 * np.pi * f

    # 单线：串联 L + 并联 C（一段微带的集中近似）
    s = np.zeros((len(f), 2, 2), dtype=complex)
    for k, wk in enumerate(w):
        a = abcd_series(1j * wk * 2e-9) @ abcd_shunt(1j * wk * 1e-12)
        s11, s21 = abcd_to_s(a)
        s[k] = [[s11, s21], [s21, s11]]
    out["single_line"] = (s, {"note": "L=2nH + C=1pF 的短线段"})

    # T 型分支（3 端口）：主路 + 分支到地（用 Z 矩阵：50Ω 节点导纳网络）
    # 端口 1/2 主线，端口 3 分支；每段用串联 L，分支用并联 C
    Ls, Cb = 3e-9, 2e-12
    s = np.zeros((len(f), 3, 3), dtype=complex)
    for k, wk in enumerate(w):
        # 三端口 Y 矩阵：节点 A（主线中点）通过 Ls 连端口1/2，分支 C 连端口3
        # 简化：Y 直接构造后转 Z
        y = np.zeros((3, 3), dtype=complex)
        yl = 1 / (1j * wk * Ls)
        yc = 1j * wk * Cb
        y[0, 0] = yl + yc
        y[1, 1] = yl + yc
        y[2, 2] = yc
        y[0, 1] = y[1, 0] = -yl
        y[0, 2] = y[2, 0] = -yc
        y[1, 2] = y[2, 1] = -yc
        # 加对地电导保证可逆
        y += np.eye(3) * 1e-9
        zm = np.linalg.inv(y)
        s[k] = z_to_s(zm)
    out["t_junction"] = (s, {"note": "3 端口 T 分支，S31/S32 由分支电容决定"})

    # 差分对（4 端口）：两条串联 L，一条完全对称、一条失配 5%
    for tag, dL in (("diff_pair_balanced", 0.0), ("diff_pair_mismatch_5pct", 0.05)):
        # 失配必须发生在**一对内部**（p+ 与 p- 之间），跨对的差异不会产生模式转换
        La, Lb = 4e-9, 4e-9 * (1 + dL)
        s = np.zeros((len(f), 4, 4), dtype=complex)
        for k, wk in enumerate(w):
            z = np.zeros((4, 4), dtype=complex)
            # 端口序 [P1, N1, P2, N2]：**差分对的腿是 (P1,P2) 与 (N1,N2)**。
            # 模式转换来自两条腿的不对称，而不是同一端 p/n 之间的差异。
            for (a, b, L) in ((0, 2, La), (1, 3, Lb)):
                zl = 1j * wk * L
                z[a, a] += zl
                z[b, b] += zl
                z[a, b] -= zl
                z[b, a] -= zl
            z += np.eye(4) * 1e-6
            s[k] = z_to_s(z)
        out[tag] = (s, {"note": "4 端口，端口序 [P1, N1, P2, N2]；"
                                + ("完全对称 → Scd≈0" if dL == 0 else
                                   "两腿 5% 失配 → Scd 明显非零")})

    # 过孔失配：一段线 + 并联 C 失配（2 端口）
    s = np.zeros((len(f), 2, 2), dtype=complex)
    for k, wk in enumerate(w):
        a = (abcd_series(1j * wk * 1.3e-9) @ abcd_shunt(1j * wk * 0.45e-12)
             @ abcd_series(1j * wk * 1.3e-9))
        s11, s21 = abcd_to_s(a)
        s[k] = [[s11, s21], [s21, s11]]
    out["via_mismatch"] = (s, {"note": "L=1.3nH（过孔）+ C=0.45pF（反焊盘）"})

    # 源端端接：串联 33Ω + 线段（2 端口）
    s = np.zeros((len(f), 2, 2), dtype=complex)
    for k, wk in enumerate(w):
        a = abcd_series(33.0) @ abcd_series(1j * wk * 3e-9) @ abcd_shunt(1j * wk * 4e-12)
        s11, s21 = abcd_to_s(a)
        s[k] = [[s11, s21], [s21, s11]]
    out["source_termination"] = (s, {"note": "33Ω 源端串阻，用于对比阻尼效果"})
    return out


def pdn_items(f):
    """PDN 基准：并联电容 / 反谐振 / 双轨传递（1 端口与 4 端口）。"""
    out = {}
    w = 2 * np.pi * f

    # 并联电容（含 ESR/ESL）：Z = 并联多个 R+L+1/(jwC) 支路
    def cap_branch(wk, C, esr, esl):
        return esr + 1j * wk * esl + 1 / (1j * wk * C)

    s = np.zeros((len(f), 1, 1), dtype=complex)
    for k, wk in enumerate(w):
        z1 = cap_branch(wk, 10e-6, 5e-3, 1.2e-9)
        z2 = cap_branch(wk, 100e-9, 20e-3, 0.7e-9)
        z = 1 / (1 / z1 + 1 / z2)
        g = (z - Z0) / (z + Z0)
        s[k, 0, 0] = g
    out["parallel_caps_1port"] = (s, {"note": "10µ+100n 并联，含 ESR/ESL"})

    # 反谐振：L || C（有损）→ |Z| 峰
    s = np.zeros((len(f), 1, 1), dtype=complex)
    Lp, Cp, Rp = 100e-9, 100e-12, 500.0
    f_res = 1 / (2 * math.pi * math.sqrt(Lp * Cp))
    for k, wk in enumerate(f):
        ww = 2 * math.pi * wk
        zl = complex(0, ww * Lp)
        zc = 1 / (1j * ww * Cp)
        z = 1 / (1 / zl + 1 / zc + 1 / Rp)
        s[k, 0, 0] = (z - Z0) / (z + Z0)
    out["antiresonance_1port"] = (s, {"f_res_hz": round(f_res, 0),
                                      "note": "L||C||R，|Z| 在 f_res 达峰"})

    # 双轨传递（4 端口）：两条轨各有对地阻抗，中间 150mΩ 耦合
    s = np.zeros((len(f), 4, 4), dtype=complex)
    for k, wk in enumerate(w):
        ww = 2 * math.pi * wk
        y1 = 1 / 0.02 + 1j * ww * 10e-6          # rail1: 20mΩ + 10µF
        y2 = 1 / 0.035 + 1j * ww * 4.7e-6        # rail2: 35mΩ + 4.7µF
        yc = 1 / 0.15                            # 板级耦合 150mΩ
        y = np.zeros((4, 4), dtype=complex)
        y[0, 0] = y[1, 1] = y1
        y[2, 2] = y[3, 3] = y2
        y[0, 2] = y[2, 0] = y[1, 3] = y[3, 1] = -yc
        y[0, 0] += yc; y[1, 1] += yc; y[2, 2] += yc; y[3, 3] += yc
        y += np.eye(4) * 1e-9
        s[k] = z_to_s(np.linalg.inv(y))
    out["two_rail_transfer_4port"] = (s, {
        "note": "端口 [r1+/r1-, r2+/r2-]，用于 Z21 传递阻抗（负耦合导纳为常规约定）"})
    return out


def meas_items(root):
    """测量相关基准：合成含噪波形 + 已知参数扰动。"""
    import numpy as np
    os.makedirs(os.path.join(root, "meas"), exist_ok=True)
    rng = np.random.default_rng(20261101)
    # 1) 阶跃 + 振铃（已知过冲/上升时间）
    t = np.arange(0, 60e-9, 5e-12)
    f0, zeta = 1 / (2 * np.pi * 3e-9), 0.35
    wd = 2 * np.pi * f0 * math.sqrt(1 - zeta ** 2)
    step = 1 - np.exp(-zeta * 2 * np.pi * f0 * t) * (
        np.cos(wd * t) + zeta / math.sqrt(1 - zeta ** 2) * np.sin(wd * t))
    v = 1.65 + 1.65 * step
    noise = rng.normal(0, 3e-3, size=t.size)
    write_csv(os.path.join(root, "meas", "scope_step_noisy.csv"), t, v + noise,
              header="time(s),voltage(V)")
    # 2) 电源轨：直流 + 纹波 + 负载阶跃
    t2 = np.arange(0, 100e-6, 20e-9)
    droop = np.where(t2 > 20e-6, 0.018 * (1 - np.exp(-(t2 - 20e-6) / 8e-6)), 0.0)
    v2 = 5.0 - droop + 2e-3 * np.sin(2 * np.pi * 1e5 * t2)
    write_csv(os.path.join(root, "meas", "rail_droop.csv"), t2, v2,
              current=0.05 + 0.1 * (t2 > 20e-6),
              header="time(s),voltage(V),current(A)")
    # 3) 已知参数扰动（供 calibration 测试）
    truth = {"R_CLK_DAMP": 33.0, "C1": 220e-12}
    perturbed = {"R_CLK_DAMP": 22.0, "C1": 180e-12}
    manifest = {"truth": truth, "initial_guess": perturbed,
                "note": "合成真值：拟合应把 initial_guess 拉回 truth 附近"}
    with open(os.path.join(root, "meas", "param_perturbation.json"), "w",
              encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    return {"scope_step_noisy.csv": {"noise_rms_v": 3e-3},
            "rail_droop.csv": {"step_droop_v": 0.018},
            "param_perturbation.json": manifest}


def main():
    f = freq_grid()
    man = {"version": 1, "generated_by": "tests/golden/make_corpus.py",
           "z0": Z0, "n_points": int(len(f)),
           "freq_hz": [float(f[0]), float(f[-1])], "items": {}}
    for sub, gen in (("rf", rf_items), ("si", si_items), ("pdn", pdn_items)):
        for name, (s, meta) in gen(f).items():
            rel = os.path.join(sub, name + (".s1p" if s.shape[1] == 1 else
                                            f".s{s.shape[1]}p"))
            write_touchstone(os.path.join(HERE, rel), f, s,
                             comment=f"golden: {name}")
            man["items"][rel.replace("\\", "/")] = dict(meta, nports=int(s.shape[1]))
    man["measurement"] = meas_items(HERE)
    with open(os.path.join(HERE, "MANIFEST.json"), "w", encoding="utf-8",
              newline="\n") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=1)
    print(f"golden corpus written: {len(man['items'])} S-parameter files + "
          f"{len(man['measurement'])} measurement files")


if __name__ == "__main__":
    main()
