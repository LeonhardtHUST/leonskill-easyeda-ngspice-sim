#!/usr/bin/env python3
"""v2.5.1：RLGC / 有损线（阶梯网络）—— 量纲与拓扑修正版。

无损 `T` 线（v2.2）只表达延迟；真实走线还有导体损耗 R 与介质损耗 G。
这里给出可直接进 ngspice 的**分段阶梯**：

    R  = Rdc + Rac                （在 f_ref 处取值后**固定**）
    L  = Z0·td / ℓ               （H/m）
    C  = td / (Z0·ℓ)             （F/m）
    G  = 2πf_ref·C·tanδ          （S/m，同样固定在 f_ref）

每段拓扑（**串联**，v2.5.1 修正）：

    cur ── R ── mid ── L ── next
                            ├── Rg(=1/G) ── 0
                            └── C ── 0

v2.5 的两个 bug（v2.5.1 修）：
  1. 长度用了 mm 而不是 m：`L = Z0·td/length_mm` 实际是 H/mm，再由 emit 乘
     `length_mm*1e-3` 又缩 1000 倍 → L/C 总量小 1000 倍，√(LC)（即传播延迟）也小 1000 倍。
  2. 每段的 R 与 L 直接并在 cur→next 两端，等于 R∥L；直流时 L 把 R 完全旁路，
     铜阻对结果毫无影响。

诚实标注：本模块生成的是 **RLGC_CONSTANT_AT_FREF**（R/G 取 f_ref 处的常数），
不是频率相关网络。manifest 里带 f_ref / R_used / G_used。
"""
import math

FIDELITY = "RLGC_LADDER"
MODEL_FIDELITY = "RLGC_CONSTANT_AT_FREF"

CU_RHO = 1.72e-8          # Ω·m


class RlgcError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def from_stackup(stackup, layer, width_mm, length_mm, n_seg=20, f_ref_hz=1e9,
                 loss_tangent=None):
    """由叠层 + 线宽 + 长度推出 RLGC 与阶梯。"""
    import stackup as stk
    if length_mm <= 0:
        raise RlgcError("RLGC_LENGTH_INVALID", f"length_mm 必须 > 0，收到 {length_mm}")
    tr = stk.trace_params(stackup, layer, width_mm, length_mm)
    z0, eps_eff, td_ps_per_mm = tr["z0_ohm"], tr["eps_eff"], tr["td_ps_per_mm"]
    td = length_mm * td_ps_per_mm * 1e-12          # s（整段）
    length_m = length_mm * 1e-3                    # ← v2.5.1：m，不是 mm
    L = z0 * td / length_m                         # H/m
    C = td / (z0 * length_m)                       # F/m
    cu = (stackup.layer(layer) or {}).get("copper_um") or 35
    t_m = cu * 1e-6
    w_m = width_mm * 1e-3
    rdc = CU_RHO * length_m / (w_m * t_m)          # Ω（整段）
    # 趋肤深度 δ=√(ρ/(πfμ0))；Rac ≈ Rdc·(t/(2δ)) 的经验上限
    delta = math.sqrt(CU_RHO / (math.pi * f_ref_hz * 4e-7 * math.pi))
    rsf = max(1.0, t_m / (2 * delta))
    rac = rdc * (rsf - 1.0)
    lt = loss_tangent
    if lt is None:
        ds = (stackup.dielectric_for(layer) or (0, 0, None, None))
        lt = ds[2] if len(ds) > 2 and ds[2] else 0.02
    g_ref = 2 * math.pi * f_ref_hz * C * length_m * lt      # S（整段，f_ref 处）
    return {
        "layer": layer, "width_mm": width_mm, "length_mm": length_mm,
        "z0_ohm": z0, "eps_eff": eps_eff,
        "r_dc_ohm": rdc, "r_ac_ohm_sqrt_hz": rac / math.sqrt(f_ref_hz),
        "l_h_per_m": L, "c_f_per_m": C,
        "g_s_per_hz": g_ref / f_ref_hz, "loss_tangent": lt,
        # 显式的"整段"量与参考频率：emit 只用这些，避免再乘一次长度
        "td_s": td,
        "l_total_h": L * length_m,
        "c_total_f": C * length_m,
        "r_at_f_ref_ohm": rdc + rac,
        "g_at_f_ref_s": g_ref,
        "f_ref_hz": f_ref_hz,
        "n_seg": n_seg,
        "fidelity": FIDELITY, "model_fidelity": MODEL_FIDELITY,
    }


def at_frequency(model, f_hz):
    """整段的 R/L/G/C 在频率 f 处的**解析**值。

    只用于报告/校验；emit 用的是 f_ref 处的常数（见 MODEL_FIDELITY）。
    """
    l_m = model["length_mm"] * 1e-3
    return {
        "r": model["r_dc_ohm"] + model["r_ac_ohm_sqrt_hz"] * math.sqrt(f_hz),
        "l": model["l_h_per_m"] * l_m,
        "c": model["c_f_per_m"] * l_m,
        "g": model["g_s_per_hz"] * f_hz,
    }


def emit(ctx, model, n1, n2, name="TL"):
    """生成 N 段 RLGC 阶梯。返回 (lines, info)。

    * 串联支路：`cur --R-- mid --L-- next`（**不是** R∥L）
    * 并联支路：`next --Rg(1/G)-- 0` 与 `next --C-- 0`
      （ngspice 的 `G` 是四端 VCCS，并联电导必须落成 `R = 1/G`）

    R 用 **Rdc**（低频/直流正确）；Rac(√f) 没有实现，因为单值电阻无法表达频率依赖。
    `r_at_f_ref_ohm` 只作为信息保留在 model 里，不进网表 —— 这点在 info 里写清楚。
    """
    n = max(1, int(model.get("n_seg") or 20))
    L = model["l_total_h"]
    C = model["c_total_f"]
    R = model["r_dc_ohm"]
    G = model["g_at_f_ref_s"]
    rsh_seg = (n / G) if G > 0 else 1e15          # 每段并联电阻（G=0 → 近似开路）
    segs = []
    cur = n1
    lines = [f"* RLGC ladder ({n} segs, {MODEL_FIDELITY}) {name}: "
             f"R={R:.4g}Ω L={L * 1e9:.4g}nH C={C * 1e12:.4g}pF "
             f"G={G * 1e6:.4g}µS @ {model['f_ref_hz'] / 1e9:g}GHz "
             f"over {model['length_mm']:g} mm"]
    for i in range(n):
        nxt = n2 if i == n - 1 else ctx.node(f"{name}_{i + 1}")
        mid = ctx.node(f"{name}_{i + 1}_MID")
        rn_ = ctx.name("R", f"{name}_R{i + 1}")
        ln = ctx.name("L", f"{name}_L{i + 1}")
        rsh = ctx.name("R", f"{name}_GSH{i + 1}")
        cn = ctx.name("C", f"{name}_C{i + 1}")
        lines += [f"{rn_} {cur} {mid} {R / n:.6g}",
                  f"{ln} {mid} {nxt} {L / n:.6g}",
                  f"{rsh} {nxt} 0 {rsh_seg:.6g}",
                  f"{cn} {nxt} 0 {C / n:.6g}"]
        segs.append({"seg": i + 1, "r": R / n, "l": L / n,
                     "g": G / n, "r_shunt": rsh_seg, "c": C / n})
        cur = nxt
    info = {"name": name, "backend": "rlgc_ladder", "n_seg": n,
            "fidelity": FIDELITY, "model_fidelity": MODEL_FIDELITY,
            "model": model, "per_segment": segs[0] if segs else None,
            "dc_resistance_ohm": R, "total_l_h": L, "total_c_f": C,
            "f_ref_hz": model["f_ref_hz"],
            "r_used_ohm": R, "g_used_s": G,
            "r_basis": "Rdc（低频/直流正确；Rac·√f 未实现，"
                       f"model.r_at_f_ref_ohm={model['r_at_f_ref_ohm']:.4g}Ω 仅作参考）",
            "g_basis": "G@f_ref（常数；不是 G(f)）",
            "note": ("常数元件阶梯：L/C 恒定，R 取 Rdc，G 取 f_ref 处值。"
                     "不要当成频率相关网络或 broadband 模型")}
    return lines, info


def summary_lines(info):
    m = info["model"]
    return [f"RLGC ({info['model_fidelity']}): {m['length_mm']:g} mm, "
            f"Z0={m['z0_ohm']:g}Ω, Rdc={m['r_dc_ohm'] * 1e3:.3g} mΩ, "
            f"L={info['total_l_h'] * 1e9:.4g} nH, C={info['total_c_f'] * 1e12:.4g} pF, "
            f"{info['n_seg']} segs",
            f"  f_ref={info['f_ref_hz'] / 1e9:g} GHz  R_used={info['r_used_ohm']:.4g}Ω  "
            f"G_used={info['g_used_s'] * 1e6:.4g}µS  tanδ={m['loss_tangent']:g}",
            f"  td={m['td_s'] * 1e12:.1f} ps   √(L·C)="
            f"{math.sqrt(info['total_l_h'] * info['total_c_f']) * 1e12:.1f} ps"]


class _CliCtx:
    """CLI 用最小命名上下文（不依赖 override_apply）。"""

    def __init__(self):
        self._n = 0
        self._nodes = 0

    def name(self, prefix, hint):
        self._n += 1
        return f"{prefix}_{hint}"

    def node(self, hint):
        self._nodes += 1
        return f"__{hint}_{self._nodes}"


def main():
    import json
    import sys
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        raise SystemExit(2)
    stackup_path = layer = None
    width = length = None
    nseg = 20
    fref = 1e9
    out = "rlgc_tl.cir"
    n1, n2 = "IN", "OUT"
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--stackup":
            stackup_path = argv[i + 1]; i += 2
        elif a == "--layer":
            layer = argv[i + 1]; i += 2
        elif a == "--width":
            width = float(argv[i + 1]); i += 2
        elif a == "--length":
            length = float(argv[i + 1]); i += 2
        elif a == "--nseg":
            nseg = int(argv[i + 1]); i += 2
        elif a == "--fref":
            fref = float(argv[i + 1]); i += 2
        elif a == "--out":
            out = argv[i + 1]; i += 2
        elif a == "--n1":
            n1 = argv[i + 1]; i += 2
        elif a == "--n2":
            n2 = argv[i + 1]; i += 2
        else:
            i += 1
    if not (stackup_path and layer and width and length):
        raise SystemExit("需要 --stackup --layer --width --length")
    import stackup as stk
    st = stk.Stackup.from_file(stackup_path)
    st.require_complete()
    model = from_stackup(st, layer, width, length, n_seg=nseg, f_ref_hz=fref)
    lines, info = emit(_CliCtx(), model, n1, n2, name="TL")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    with open(out.replace(".cir", ".json") if out.endswith(".cir") else out + ".json",
              "w", encoding="utf-8", newline="\n") as f:
        json.dump({"model": model, "info": {k: v for k, v in info.items()
                                            if k not in ("model",)}},
                  f, ensure_ascii=False, indent=1)
    for ln in summary_lines(info):
        print(ln)
    print(f"  netlist: {out}  ({len(lines) - 1} 行, {nseg} 段)")


if __name__ == "__main__":
    main()
