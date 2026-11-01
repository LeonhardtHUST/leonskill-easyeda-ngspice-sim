#!/usr/bin/env python3
"""v2.5 P1-B：RLGC / 有损线（阶梯网络）。

无损 `T` 线（v2.2）只表达延迟；真实走线还有导体损耗 R(f) 与介质损耗 G(f)。
这里给出**分段 RLGC 阶梯**，可直接进 ngspice：

    R(f) = Rdc + Rac·√f            （趋肤效应近似，Rac 由 w/t/ρ 估）
    L    = 常数                    （或由 Z0/td 反推）
    C    = 常数
    G(f) = 2πf·C·tanδ              （介质损耗）

阶梯切 N 段，每段：串联 R+L，并联 G+C。
"""
import math

FIDELITY = "RLGC_LADDER"

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
    tr = stk.trace_params(stackup, layer, width_mm, length_mm)
    z0, eps_eff, td_ps_per_mm = tr["z0_ohm"], tr["eps_eff"], tr["td_ps_per_mm"]
    td = length_mm * td_ps_per_mm * 1e-12
    L = z0 * td / length_mm                     # H/m
    C = td / z0 / length_mm                     # F/m
    cu = (stackup.layer(layer) or {}).get("copper_um") or 35
    t_m = cu * 1e-6
    w_m = width_mm * 1e-3
    rdc = CU_RHO * (length_mm * 1e-3) / (w_m * t_m)          # Ω（整段）
    # 趋肤深度 δ=√(ρ/(πfμ0))；Rac ≈ Rdc·(t/(2δ)) 的常见近似上限为 Rdc
    delta = math.sqrt(CU_RHO / (math.pi * f_ref_hz * 4e-7 * math.pi))
    rsf = max(1.0, t_m / (2 * delta))
    rac = rdc * (rsf - 1.0)
    lt = loss_tangent
    if lt is None:
        ds = (stackup.dielectric_for(layer) or (0, 0, None, None))
        lt = ds[2] if len(ds) > 2 and ds[2] else 0.02
    g_ref = 2 * math.pi * f_ref_hz * C * length_mm * lt        # S（整段）
    return {
        "layer": layer, "width_mm": width_mm, "length_mm": length_mm,
        "z0_ohm": z0, "eps_eff": eps_eff,
        "r_dc_ohm": rdc, "r_ac_ohm_sqrt_hz": rac / math.sqrt(f_ref_hz),
        "l_h_per_m": L, "c_f_per_m": C,
        "g_s_per_hz": g_ref / f_ref_hz, "loss_tangent": lt,
        "td_s": td, "n_seg": n_seg, "fidelity": FIDELITY,
        "model_fidelity": "ANALYTIC_ESTIMATE",
    }


def at_frequency(model, f_hz):
    """整段的 R/L/G/C（在频率 f 处的等效值）。"""
    l_m = model["length_mm"] * 1e-3
    return {
        "r": model["r_dc_ohm"] + model["r_ac_ohm_sqrt_hz"] * math.sqrt(f_hz),
        "l": model["l_h_per_m"] * l_m,
        "c": model["c_f_per_m"] * l_m,
        "g": model["g_s_per_hz"] * f_hz,
    }


def emit(ctx, model, n1, n2, name="TL"):
    """生成 N 段 RLGC 阶梯（每段：串联 R+L，并联 R/C）。

    注意：并联合电导**必须**用 `R`（阻值 = 1/G）而不是 `G` 元件——
    ngspice 的 `G` 是四端 VCCS（需要控制电压节点），写成两端会直接报错。
    返回 (lines, info)。"""
    n = max(1, int(model.get("n_seg") or 20))
    L = model["l_h_per_m"] * model["length_mm"] * 1e-3
    C = model["c_f_per_m"] * model["length_mm"] * 1e-3
    R = model["r_dc_ohm"]
    G = max(model["g_s_per_hz"] * 1e9, 1e-12)     # 用 1GHz 处的 G 作常数近似
    segs = []
    cur = n1
    lines = [f"* RLGC ladder ({n} segs) {name}: R={R:.4g}Ω L={L * 1e9:.4g}nH "
             f"C={C * 1e12:.4g}pF over {model['length_mm']:g} mm"]
    for i in range(n):
        nxt = n2 if i == n - 1 else ctx.node(f"{name}_{i + 1}")
        rn_ = ctx.name("R", f"{name}_R{i + 1}")
        ln = ctx.name("L", f"{name}_L{i + 1}")
        rsh = ctx.name("R", f"{name}_GSH{i + 1}")
        cn = ctx.name("C", f"{name}_C{i + 1}")
        lines += [f"{rn_} {cur} {nxt} {R / n:.6g}",
                  f"{ln} {cur} {nxt} {L / n:.6g}",
                  f"{rsh} {nxt} 0 {n / G:.6g}",
                  f"{cn} {nxt} 0 {C / n:.6g}"]
        segs.append({"seg": i + 1, "r": R / n, "l": L / n, "g": G / n, "c": C / n})
        cur = nxt
    info = {"name": name, "backend": "rlgc_ladder", "n_seg": n,
            "fidelity": FIDELITY, "model": model, "per_segment": segs[0] if segs else None,
            "dc_resistance_ohm": R, "total_l_h": L, "total_c_f": C}
    return lines, info


def summary_lines(info):
    m = info["model"]
    return [f"RLGC: {m['length_mm']:g} mm, Z0={m['z0_ohm']:g}Ω, "
            f"Rdc={m['r_dc_ohm'] * 1e3:.3g} mΩ, L={info['total_l_h'] * 1e9:.3g} nH, "
            f"C={info['total_c_f'] * 1e12:.3g} pF, {info['n_seg']} segs",
            f"  loss tangent={m['loss_tangent']:g}, td={m['td_s'] * 1e12:.1f} ps"]


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
    model = from_stackup(st, layer, width, length, n_seg=nseg)
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
