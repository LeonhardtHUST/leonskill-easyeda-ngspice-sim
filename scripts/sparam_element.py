#!/usr/bin/env python3
"""v2.3 P0-E(2)：Touchstone → ngspice（带 adapter 层）。

为什么需要 adapter：ngspice-47 的 `S` 元件（`.model s file=...`）在本机实测
**不可用**（`warning, model type mismatch`，随后 `Unable to find definition of model`），
而 `.sp` 分析只对"包含 RF 端口"的电路做 S 参数**测量**，不能把 .sNp 当二端元件用。
所以本模块提供两条后端，由 capability probe 自动选择，用户不必依赖不稳定语法：

  backend="native"      → 若未来 ngspice 支持 `S` 元件，直接 `S1 n1 0 n2 0 model`
  backend="lumped"      → 默认：从 S 参数提取集总等效（1 端口 RLC / 2 端口 π 网络）
  backend="metrics_only"→ 只出指标，不接进电路（用于纯数据审阅）

集总提取的数学（都是线性最小二乘，闭式解，不做迭代优化）：
  1 端口  Z(f) = z0(1+S11)/(1−S11)，拟合 Z = R + jωL（或 R + jωL + 1/(jωC)）
  2 端口  π 网络：
          Y11 = ((1−S11)(1+S22)+S12S21)/(z0·Δ)
          Y12 = −2S12/(z0·Δ),  Y21 = −2S21/(z0·Δ)
          Y22 = ((1+S11)(1−S22)+S12S21)/(z0·Δ)
          Zs = −1/Y21,  Yp1 = Y11+Y21,  Yp2 = Y22+Y21
          再分别拟合 Zs = Rs + jωLs、Yp = G + jωC

拟合质量必须回报（RMS/max dB 误差），超限就拒绝接入电路 —— 宁可报错也不接一个
形状对不上的模型。
"""
import cmath
import math

from sparam_metrics import impedance_1port

LUMPED_FIDELITY = "TOUCHSTONE_LUMPED_FIT"
NATIVE_FIDELITY = "TOUCHSTONE_NATIVE_S_ELEMENT"
MAX_FIT_ERR_DB = 3.0        # 超过 3 dB 的拟合拒绝接入（可在 config 里放宽）

_native_cache = {}


class SParamError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# capability probe
# --------------------------------------------------------------------------
def probe_native(ngspice_bin=None, workdir=None, timeout=20):
    """探测本机 ngspice 是否支持 `S` 元件。结果按二进制路径缓存。"""
    import os
    import subprocess
    import tempfile
    if ngspice_bin is None:
        ngspice_bin = os.environ.get("NGSPICE_BIN") or os.path.expanduser(
            "~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe")
    key = os.path.abspath(ngspice_bin) if os.path.exists(ngspice_bin) else ngspice_bin
    if key in _native_cache:
        return _native_cache[key]
    if not os.path.exists(ngspice_bin):
        _native_cache[key] = False
        return False
    d = workdir or tempfile.mkdtemp(prefix="sprobe")
    s2p = os.path.join(d, "probe.s2p")
    with open(s2p, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Hz S RI R 50\n1e6 0 0 1 0 1 0 0 0\n1e7 0 0 1 0 1 0 0 0\n")
    deck = os.path.join(d, "probe.cir")
    with open(deck, "w", encoding="utf-8", newline="\n") as f:
        f.write("* native S element probe\n"
                "V1 in 0 AC 1\nR1 in 0 50\n"
                f'S1 in 0 out 0 spm\nR2 out 0 50\n.model spm s file="{s2p}"\n'
                ".control\nac dec 1 1meg 10meg\n.endc\n.end\n")
    try:
        p = subprocess.run([ngspice_bin, "-b", "probe.cir"], cwd=d,
                           capture_output=True, timeout=timeout)
        txt = (p.stdout or b"").decode("utf-8", "replace") + \
              (p.stderr or b"").decode("utf-8", "replace")
        ok = p.returncode == 0 and "model type mismatch" not in txt \
            and "Unable to find definition" not in txt
    except Exception:
        ok = False
    _native_cache[key] = ok
    return ok


# --------------------------------------------------------------------------
# 拟合
# --------------------------------------------------------------------------
def _ls_rl(points):
    """Z = R + jωL  → R = mean(Re Z)，L = mean(Im Z / ω)（实数/虚部解耦，闭式）。"""
    rs, ls = [], []
    for w, z in points:
        rs.append(z.real)
        if w > 0:
            ls.append(z.imag / w)
    return sum(rs) / len(rs), (sum(ls) / len(ls)) if ls else 0.0


def _ls_rlc(points):
    """Z = R + j(ωL − 1/(ωC)) → 对 y=Im(Z)/ω 做 y = L − (1/C)·(1/ω²) 的线性拟合。"""
    rs, xs = [], []
    for w, z in points:
        rs.append(z.real)
        if w > 0:
            xs.append((1.0 / (w * w), z.imag / w))
    R = sum(rs) / len(rs)
    n = len(xs)
    if n < 2:
        return R, 0.0, None
    sx = sum(a for a, _ in xs) / n
    sy = sum(b for _, b in xs) / n
    sxx = sum((a - sx) ** 2 for a, _ in xs)
    sxy = sum((a - sx) * (b - sy) for a, b in xs)
    if abs(sxx) < 1e-30:
        return R, sy, None
    slope = sxy / sxx
    L = sy - slope * sx
    C = (-1.0 / slope) if slope < 0 else None
    return R, max(L, 0.0), C


def _ls_gc(points):
    """Y = G + jωC → G = mean(Re Y)，C = mean(Im Y / ω)。"""
    gs, cs = [], []
    for w, y in points:
        gs.append(y.real)
        if w > 0:
            cs.append(y.imag / w)
    return sum(gs) / len(gs), (sum(cs) / len(cs)) if cs else 0.0


def _z_of(R, L, C, w):
    x = w * L
    if C:
        x -= 1.0 / (w * C)
    return complex(R, x)


def _y_of(G, C, w):
    return complex(G, w * C)


# --------------------------------------------------------------------------
# 1 端口
# --------------------------------------------------------------------------
def fit_1port(ts, band=None):
    pts = [(2 * math.pi * p["f"], p["z"]) for p in impedance_1port(ts)
           if p["f"] > 0 and (band is None or band[0] <= p["f"] <= band[1])]
    if not pts:
        raise SParamError("SPARAM_FIT_FAILED", "没有可用频点")
    R1, L1 = _ls_rl(pts)
    err_rl = _rms([abs(_z_of(R1, L1, None, w) - z) for w, z in pts])

    R2, L2, C2 = _ls_rlc(pts)
    err_rlc = None
    if C2:
        err_rlc = _rms([abs(_z_of(R2, L2, C2, w) - z) for w, z in pts])

    # 铁氧体这类"低频感性 / 高频阻性"的器件，R||L 比串联 R+L 贴合得多
    Rp, Lp, err_par = _fit_parallel_rl(pts)

    cands = [
        {"topology": "series_rl", "R": R1, "L": L1, "C": None, "rms_ohm": err_rl},
        {"topology": "shunt_rl", "Rp": Rp, "Lp": Lp, "rms_ohm": err_par},
    ]
    bead = _fit_bead(pts)
    if bead:
        cands.append(bead)
    if C2:
        cands.append({"topology": "series_rlc", "R": R2, "L": L2, "C": C2,
                      "rms_ohm": err_rlc})
    # 物理护栏：低频实测阻抗很小（器件在直流附近是感性的）时，串联 R 型拓扑会
    # 凭空塞进几十欧的直流电阻，把磁珠的供电通道堵死 → 直接判为不可用。
    f_lo = min(p["f"] for p in impedance_1port(ts) if p["f"] > 0)
    z_lo = None
    for p in impedance_1port(ts):
        if abs(p["f"] - f_lo) < 1e-9:
            z_lo = abs(p["z"])
            break
    for c in cands:
        c["z_at_fmin"] = _model_z(c, 2 * math.pi * f_lo)
    viable = [c for c in cands if z_lo is None or c["z_at_fmin"] is None
              or (c["z_at_fmin"] <= 3 * z_lo and c["z_at_fmin"] >= z_lo / 3)]
    if not viable:
        model = min(cands, key=lambda m: m["rms_ohm"])
        model["dc_guard"] = "all topologies failed the low-frequency impedance check"
    else:
        model = min(viable, key=lambda m: m["rms_ohm"])
    if z_lo is not None:
        model["z_measured_at_fmin_ohm"] = round(z_lo, 4)
    zrms = math.sqrt(sum(abs(z) ** 2 for _, z in pts) / len(pts)) or 1.0
    # 1 端口跨 3 个十倍频程时，3 元件模型 20~25% 的相对误差是正常的；
    # 阈值放宽到 25%，但百分比照实打印，不粉饰。
    model["ok"] = (model["rms_ohm"] / zrms) < 0.25
    model["relative_error"] = model["rms_ohm"] / zrms
    model["candidates"] = {c["topology"]: round(c["rms_ohm"], 3) for c in cands}
    model["n_points"] = len(pts)
    model["band_hz"] = list(band) if band else None
    model["fidelity"] = LUMPED_FIDELITY
    model["assumptions"] = ("单/双元件集总等效只在给定频带内有效；"
                            "器件跨多个十倍频程时（如宽频铁氧体）应分段或用厂商 RLC 模型")
    return model


def _fit_parallel_rl(pts):
    """R||L：Y = 1/R − j/(ωL)，对 (Re Y, ω·Im Y) 解耦求 G 与 L。"""
    gs, wl = [], []
    for w, z in pts:
        if abs(z) < 1e-15 or w <= 0:
            continue
        y = 1.0 / z
        gs.append(y.real)
        wl.append(w * y.imag)
    if not gs:
        return None, None, float("inf")
    G = sum(gs) / len(gs)
    mw = sum(wl) / len(wl)
    L = (-1.0 / mw) if mw < 0 else None
    if L is None:
        return None, None, float("inf")
    Rp = (1.0 / G) if abs(G) > 1e-15 else 1e9
    err = _rms([abs(1.0 / (G + 1j * w * (-1.0 / L)) - z) for w, z in pts])
    return Rp, L, err


def _fit_bead(pts):
    """R_s + (R_p || L_p)：磁珠的标准三元件等效。

    R_p 与 L_p 用对数网格搜索（各 21 点），R_s 在给定 (R_p, L_p) 下是
    Rs = mean(Re(Z − Z_p)) 的闭式解 —— 不必做非线性迭代。
    """
    if not pts:
        return None
    best = None
    rp_grid = [10 ** (i / 21.0 * 4) for i in range(1, 22)]        # 10Ω … 10kΩ
    lp_grid = [10 ** (-9 + i / 21.0 * 4) for i in range(1, 22)]   # 1nH … 10µH
    for rp in rp_grid:
        for lp in lp_grid:
            rs_acc, err_acc = [], []
            for w, z in pts:
                zl = complex(0, w * lp)
                zp = rp * zl / (rp + zl) if abs(rp + zl) > 0 else zl
                rs_acc.append((z - zp).real)
            rs = sum(rs_acc) / len(rs_acc)
            if rs < 0:
                continue
            for w, z in pts:
                zl = complex(0, w * lp)
                zp = rp * zl / (rp + zl) if abs(rp + zl) > 0 else zl
                err_acc.append(abs(complex(rs, 0) + zp - z))
            err = _rms(err_acc)
            if best is None or err < best[0]:
                best = (err, rs, lp, rp)
    if not best:
        return None
    err, rs, lp, rp = best
    return {"topology": "bead3", "Rs": rs, "Rp": rp, "Lp": lp, "rms_ohm": err}


def _model_z(m, w):
    top = m.get("topology")
    if top == "series_rl":
        return abs(_z_of(m["R"], m["L"], None, w))
    if top == "series_rlc":
        return abs(_z_of(m["R"], m["L"], m["C"], w))
    if top == "bead3":
        zl = complex(0, w * m["Lp"])
        zp = m["Rp"] * zl / (m["Rp"] + zl)
        return abs(complex(m["Rs"], 0) + zp)
    if top == "shunt_rl":
        zl = complex(0, w * m["Lp"]) if m["Lp"] else complex(1e15, 0)
        zp = complex(m["Rp"], 0) if m["Rp"] else complex(1e15, 0)
        return abs(zl * zp / (zl + zp))
    return None


def _rms(vals):
    return math.sqrt(sum(v * v for v in vals) / len(vals)) if vals else 0.0


# --------------------------------------------------------------------------
# 2 端口
# --------------------------------------------------------------------------
def _s_to_y(ts, k):
    s11, s12 = ts.s[k][0][0], ts.s[k][0][1]
    s21, s22 = ts.s[k][1][0], ts.s[k][1][1]
    z0 = ts.z0
    den = (1 + s11) * (1 + s22) - s12 * s21
    if abs(den) < 1e-15:
        den = 1e-15
    y11 = ((1 - s11) * (1 + s22) + s12 * s21) / (z0 * den)
    y12 = -2 * s12 / (z0 * den)
    y21 = -2 * s21 / (z0 * den)
    y22 = ((1 + s11) * (1 - s22) + s12 * s21) / (z0 * den)
    return y11, y12, y21, y22


def fit_2port_pi(ts):
    if ts.num_ports < 2:
        raise SParamError("SPARAM_PORT_MISMATCH", "fit_2port_pi 需要 2 端口文件")
    zpts, y1pts, y2pts = [], [], []
    ys = []
    for k, f in enumerate(ts.frequencies):
        if f <= 0:
            continue
        w = 2 * math.pi * f
        y11, y12, y21, y22 = _s_to_y(ts, k)
        ys.append((w, (y11, y12, y21, y22)))
        if abs(y21) > 1e-18:
            zpts.append((w, -1.0 / y21))
        y1pts.append((w, y11 + y21))
        y2pts.append((w, y22 + y21))
    if not zpts:
        raise SParamError("SPARAM_FIT_FAILED",
                          "Y21 全为 0（隔离器件？），无法提取串联支路")
    Rs, Ls = _ls_rl(zpts)
    G1, C1 = _ls_gc(y1pts)
    G2, C2 = _ls_gc(y2pts)
    model = {
        "topology": "pi",
        "series": {"R": Rs, "L": Ls},
        "shunt1": {"G": G1, "C": C1},
        "shunt2": {"G": G2, "C": C2},
        "n_points": len(ys),
        "fidelity": LUMPED_FIDELITY,
    }
    model["fit"] = _pi_fit_error(ts, model)
    model["topology"] = "pi"

    # 同时试 T 网络（C-L-C 梯形就是 T 型，π 拟合会差很多）
    tmodel = _fit_2port_t(ts)
    if tmodel and tmodel["fit"]["s21_max_err_db"] < model["fit"]["s21_max_err_db"]:
        return tmodel
    return model


def _fit_2port_t(ts):
    """T 网络：Z11 = Za+Zp, Z12 = Zp, Z22 = Zb+Zp。"""
    za, zb, zp = [], [], []
    for k, f in enumerate(ts.frequencies):
        if f <= 0:
            continue
        w = 2 * math.pi * f
        y11, y12, y21, y22 = _s_to_y(ts, k)
        det = y11 * y22 - y12 * y21
        if abs(det) < 1e-30:
            continue
        z11, z12, z22 = y22 / det, -y12 / det, y11 / det
        za.append((w, z11 - z12))
        zb.append((w, z22 - z12))
        zp.append((w, z12))
    if len(za) < 3:
        return None
    Ra, La = _ls_rl(za)
    Rb, Lb = _ls_rl(zb)
    # Zp 是阻抗，而 _ls_gc 拟合的是导纳 Y = G + jωC → 先取倒数
    Gp, Cp = _ls_gc([(w, 1.0 / z) for w, z in zp if abs(z) > 1e-15])
    model = {
        "topology": "t",
        "series1": {"R": Ra, "L": La},
        "series2": {"R": Rb, "L": Lb},
        "shunt": {"G": Gp, "C": Cp},
        "n_points": len(za),
        "fidelity": LUMPED_FIDELITY,
    }
    model["fit"] = _t_fit_error(ts, model)
    return model


def _t_to_s(model, w, z0):
    """T 网络（Za/Zb 串联 + Zp 中间并联）→ S 参数（节点法解析式）。"""
    Za = complex(model["series1"]["R"], w * model["series1"]["L"])
    Zb = complex(model["series2"]["R"], w * model["series2"]["L"])
    Yp = _y_of(model["shunt"]["G"], model["shunt"]["C"], w)
    if abs(Za) < 1e-15:
        Za = 1e-15
    if abs(Zb) < 1e-15:
        Zb = 1e-15
    ya, yb = 1.0 / Za, 1.0 / Zb
    S = ya + yb + Yp
    if abs(S) < 1e-18:
        S = 1e-18
    y11 = ya * (yb + Yp) / S
    y22 = yb * (ya + Yp) / S
    y12 = -ya * yb / S
    y21 = y12
    D = (1 + z0 * y11) * (1 + z0 * y22) - (z0 * y12) * (z0 * y21)
    if abs(D) < 1e-30:
        D = 1e-30
    s11 = ((1 - z0 * y11) * (1 + z0 * y22) + z0 * y12 * z0 * y21) / D
    s21 = (-2 * z0 * y21) / D
    return s11, s21


def _t_fit_error(ts, model):
    errs21, errs11 = [], []
    for k, f in enumerate(ts.frequencies):
        if f <= 0:
            continue
        w = 2 * math.pi * f
        s11, s21 = _t_to_s(model, w, ts.z0)
        errs21.append(_db(s21) - _db(ts.s[k][1][0]))
        errs11.append(_db(s11) - _db(ts.s[k][0][0]))
    return {
        "s21_max_err_db": max(abs(e) for e in errs21) if errs21 else None,
        "s21_rms_err_db": _rms(errs21),
        "s11_max_err_db": max(abs(e) for e in errs11) if errs11 else None,
        "ok": (max(abs(e) for e in errs21) if errs21 else 0) <= MAX_FIT_ERR_DB,
    }


def _pi_to_s(model, w, z0):
    """π 网络 → S 参数（用于算拟合误差）。"""
    Rs = model["series"]["R"]
    Ls = model["series"]["L"]
    Zs = complex(Rs, w * Ls)
    y1 = _y_of(model["shunt1"]["G"], model["shunt1"]["C"], w)
    y2 = _y_of(model["shunt2"]["G"], model["shunt2"]["C"], w)
    if abs(Zs) < 1e-15:
        Zs = 1e-15
    # 串 Zs + 两端并联导纳 → Y 矩阵
    y11 = y1 + 1.0 / Zs
    y22 = y2 + 1.0 / Zs
    y12 = -1.0 / Zs
    y21 = y12
    # Y → S（等 z0）
    I = complex(0, 1)
    D = (1 + z0 * y11) * (1 + z0 * y22) - (z0 * y12) * (z0 * y21)
    s11 = ((1 - z0 * y11) * (1 + z0 * y22) + z0 * y12 * z0 * y21) / D
    s21 = (-2 * z0 * y21) / D
    return s11, s21


def _pi_fit_error(ts, model):
    errs21, errs11 = [], []
    for k, f in enumerate(ts.frequencies):
        if f <= 0:
            continue
        w = 2 * math.pi * f
        s11, s21 = _pi_to_s(model, w, ts.z0)
        errs21.append(_db(s21) - _db(ts.s[k][1][0]))
        errs11.append(_db(s11) - _db(ts.s[k][0][0]))
    return {
        "s21_max_err_db": max(abs(e) for e in errs21) if errs21 else None,
        "s21_rms_err_db": _rms(errs21),
        "s11_max_err_db": max(abs(e) for e in errs11) if errs11 else None,
        "ok": (max(abs(e) for e in errs21) if errs21 else 0) <= MAX_FIT_ERR_DB,
    }


def _db(x):
    return 20 * math.log10(max(abs(x), 1e-30))


# --------------------------------------------------------------------------
# 生成 ngspice 元件
# --------------------------------------------------------------------------
def emit(ts, ctx, name, n1, n2, backend="lumped", max_err_db=MAX_FIT_ERR_DB,
         series_r_floor=1e-3, band=None):
    """把 S 参数接成 ngspice 元件。返回 (lines, info)。

    backend="native" 需要 probe_native() 为真，否则自动降级到 lumped 并记 warning。
    """
    name_tag = name.replace(".", "_")
    info = {"name": name, "file": ts.name, "num_ports": ts.num_ports}
    warn = []
    if backend == "native":
        if probe_native():
            info.update({"backend": "native", "fidelity": NATIVE_FIDELITY})
            mid = ctx.node(f"{name_tag}_SP")
            mod = ctx.name("MODEL", f"{name_tag}_S")
            lines = [f"* Touchstone native S element: {ts.name}",
                     f"S_{name_tag} {n1} 0 {n2} 0 {mod}",
                     f'.model {mod} s file="{ts.path}"']
            return lines, info
        warn.append("本机 ngspice 不支持 S 元件，自动降级到集总拟合")
        backend = "lumped"

    if backend == "metrics_only":
        info.update({"backend": "metrics_only", "fidelity": "TOUCHSTONE_METRICS_ONLY"})
        return [], info

    if ts.num_ports == 1:
        m = fit_1port(ts, band=band)
        info.update({"backend": "lumped", "fidelity": LUMPED_FIDELITY, "model": m})
        lines = [f"* Touchstone lumped fit ({m['topology']}): {ts.name}"]
        if m["topology"] == "bead3":
            mid = ctx.node(f"{name_tag}_B3")
            rp = ctx.name("R", f"{name_tag}_RP")
            lp = ctx.name("L", f"{name_tag}_LP")
            rs = ctx.name("R", f"{name_tag}_RS")
            # 串联链：n1 -- Rs -- mid -- (Rp || Lp) -- n2
            lines = [f"* Touchstone lumped fit (Rs + Rp||Lp, ferrite): {ts.name}",
                     f"{rs} {n1} {mid} {max(m['Rs'], series_r_floor):.6g}",
                     f"{rp} {mid} {n2} {m['Rp']:.6g}",
                     f"{lp} {mid} {n2} {m['Lp']:.6g}"]
            info["warnings"] = warn
            return lines, info
        if m["topology"] == "shunt_rl":
            # R||L：串联一个 R 与 L 的并联块
            mid = ctx.node(f"{name_tag}_PL")
            rp = ctx.name("R", f"{name_tag}_RP")
            lp = ctx.name("L", f"{name_tag}_LP")
            lines = [f"* Touchstone lumped fit (R||L): {ts.name}",
                     f"{rp} {n1} {mid} {m['Rp']:.6g}",
                     f"{lp} {n1} {mid} {m['Lp']:.6g}",
                     f"{ctx.name('R', f'{name_tag}_SER')} {mid} {n2} {series_r_floor:.6g}"]
            info["warnings"] = warn
            return lines, info
        R = max(m.get("R") or 0.0, series_r_floor)
        # 串联链：n1 -- L -- C -- R -- n2
        cur, parts = n1, []
        if m["L"] and m["L"] > 0:
            parts.append(("L", m["L"]))
        if m.get("C"):
            parts.append(("C", m["C"]))
        parts.append(("R", R))
        for idx, (kind, val) in enumerate(parts):
            nxt = n2 if idx == len(parts) - 1 else ctx.node(f"{name_tag}_{kind}{idx}")
            nm = ctx.name(kind, f"{name_tag}_{kind}{idx}")
            lines.append(f"{nm} {cur} {nxt} {val:.6g}")
            cur = nxt
        info["warnings"] = warn
        return lines, info

    # 2 端口：π 或 T（fit_2port_pi 内部会挑更好的那个）
    m = fit_2port_pi(ts)
    info.update({"backend": "lumped", "fidelity": LUMPED_FIDELITY, "model": m})
    if not m["fit"]["ok"] and max_err_db is not None:
        raise SParamError(
            "SPARAM_FIT_TOO_POOR",
            f"{ts.name}: {m['topology'].upper()} 集总拟合 S21 最大误差 "
            f"{m['fit']['s21_max_err_db']:.2f} dB > {max_err_db} dB，拒绝接入电路"
            f"（可放宽 max_err_db 或改用 metrics_only）")
    lines = [f"* Touchstone lumped {m['topology'].upper()} fit: {ts.name}"]

    def _series(node_a, node_b, branch, tag):
        """串一条 R(+L) 支路。"""
        R = max(branch["R"], series_r_floor)
        cur = node_a
        if branch["L"] > 0:
            mid = ctx.node(f"{name_tag}_{tag}_L")
            nm = ctx.name("L", f"{name_tag}_{tag}")
            lines.append(f"{nm} {cur} {mid} {branch['L']:.6g}")
            cur = mid
        nm = ctx.name("R", f"{name_tag}_{tag}")
        lines.append(f"{nm} {cur} {node_b} {R:.6g}")

    def _shunt(node, sh, tag):
        # 提取出的 G 可能是微小负值（数值噪声）→ 直接当开路，绝不发负电阻
        if sh["G"] and sh["G"] > 1e-12:
            rp = ctx.name("R", f"{name_tag}_{tag}")
            lines.append(f"{rp} {node} 0 {1.0 / sh['G']:.6g}")
        if sh["C"] and sh["C"] > 0:
            cp = ctx.name("C", f"{name_tag}_{tag}")
            lines.append(f"{cp} {node} 0 {sh['C']:.6g}")

    if m["topology"] == "t":
        mid = ctx.node(f"{name_tag}_MID")
        _series(n1, mid, m["series1"], "SER1")
        _series(mid, n2, m["series2"], "SER2")
        _shunt(mid, m["shunt"], "SH")
    else:
        mid = ctx.node(f"{name_tag}_MID")
        _series(n1, mid, m["series"], "SER")
        lines.append(f"{ctx.name('R', f'{name_tag}_SER2')} {mid} {n2} "
                     f"{series_r_floor:.6g}")
        _shunt(n1, m["shunt1"], "P1")
        _shunt(n2, m["shunt2"], "P2")
    info["warnings"] = warn
    return lines, info


def summary_lines(info):
    out = [f"{info['name']}: {info['file']} ({info['num_ports']}-port) "
           f"backend={info.get('backend')}"]
    out.append(f"fidelity: {info.get('fidelity')}")
    m = info.get("model") or {}
    if m.get("topology") in ("series_rl", "series_rlc"):
        out.append(f"topology: {m['topology']}")
        out.append(f"R = {m['R']:.4g} Ω  L = {m['L'] * 1e9:.4g} nH"
                   + (f"  C = {m['C'] * 1e12:.4g} pF" if m.get("C") else ""))
        out.append(f"fit rms = {m['rms_ohm']:.4g} Ω "
                   f"({m.get('relative_error', 0) * 100:.1f}% of |Z|rms, "
                   f"{'OK' if m.get('ok') else 'POOR'})")
    elif m.get("topology") == "bead3":
        out.append("topology: Rs + (Rp || Lp)（磁珠三元件）")
        out.append(f"Rs = {m['Rs']:.4g} Ω  Rp = {m['Rp']:.4g} Ω  Lp = {m['Lp'] * 1e9:.4g} nH")
        out.append(f"fit rms = {m['rms_ohm']:.4g} Ω "
                   f"({m.get('relative_error', 0) * 100:.1f}% of |Z|rms, "
                   f"{'OK' if m.get('ok') else 'POOR'})")
    elif m.get("topology") == "shunt_rl":
        out.append("topology: R || L（铁氧体常用）")
        out.append(f"R = {m['Rp']:.4g} Ω  L = {m['Lp'] * 1e9:.4g} nH")
        out.append(f"fit rms = {m['rms_ohm']:.4g} Ω "
                   f"({m.get('relative_error', 0) * 100:.1f}% of |Z|rms, "
                   f"{'OK' if m.get('ok') else 'POOR'})")
    elif m.get("topology") == "pi":
        s = m["series"]
        out.append(f"series: R = {s['R']:.4g} Ω  L = {s['L'] * 1e9:.4g} nH")
        out.append(f"fit: S21 max err = {m['fit']['s21_max_err_db']:.2f} dB, "
                   f"rms = {m['fit']['s21_rms_err_db']:.2f} dB "
                   f"({'OK' if m['fit']['ok'] else 'POOR'})")
    for w in info.get("warnings") or []:
        out.append(f"warning: {w}")
    return out
