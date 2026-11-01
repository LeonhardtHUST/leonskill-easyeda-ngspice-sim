#!/usr/bin/env python3
"""v2.4 P0-C/D：Vector Fitting → SPICE N-port macromodel（宽带，替代窄带 lumped）。

**不自己实现 vector fitting**：极点迭代、极点增补、passivity enforcement 全部用
scikit-rf 的 `VectorFitting`（BSD-3-Clause）。本模块负责：

  1. 自适应阶数（从低阶起，误差达标即停，记录每一轮的 pole 数与误差）；
  2. passivity 检查与可选 enforcement（before/after 都记）；
  3. 调 `write_spice_subcircuit_s` 生成 ngspice 子电路，并改写成本项目命名 + provenance；
  4. 与 `network_validate` 配合做 round-trip 验证。

精度契约：拟合误差以 dB 报（rms/max）+ 相位误差（deg），全部写进 FIT_VALIDATION.json。
"""
import json
import os
import tempfile

import numpy as np

import rf_network as rn

FIDELITY = "VECTOR_FIT_MACROMODEL"
SUBEXT = ".sp"


class VectorFitError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _db(x):
    return 20 * np.log10(np.maximum(np.abs(x), 1e-30))


def _fit_error(vf, net):
    """用 skrf 的 model response 逐端口对算 dB 误差与相位误差。"""
    f = net.freq
    mag_err, ph_err = [], []
    for i in range(net.nports):
        for j in range(net.nports):
            model = np.asarray(vf.get_model_response(i, j, f))
            ref = net.s[:, i, j]
            mag_err.append(_db(model) - _db(ref))
            d = np.angle(model) - np.angle(ref)
            ph_err.append(np.degrees(np.mod(d + np.pi, 2 * np.pi) - np.pi))
    mag = np.concatenate(mag_err) if mag_err else np.array([0.0])
    ph = np.concatenate(ph_err) if ph_err else np.array([0.0])
    return {
        "rms_mag_error_db": float(np.sqrt(np.mean(mag ** 2))),
        "max_mag_error_db": float(np.max(np.abs(mag))),
        "rms_phase_error_deg": float(np.sqrt(np.mean(ph ** 2))),
        "max_phase_error_deg": float(np.max(np.abs(ph))),
    }


def fit(net, order="auto", max_order=20, target_rms_db=0.2, enforce_passivity=False,
        max_iter_per_step=3, log=print):
    """自适应阶数拟合。

    order: "auto" 或给定整数（= 目标 pole 数，此时不做自适应）
    返回 dict：{vf, poles, order, history[], error{}, passivity{}, enforced{}}
    """
    if not rn.available():
        raise VectorFitError(
            "RF_BACKEND_UNAVAILABLE",
            "vector fitting 需要 scikit-rf（pip install -r requirements-rf.txt）")
    skrf = rn._load()
    from skrf.vectorFitting import VectorFitting

    if net._net is None:
        raise VectorFitError("RF_NETWORK_INVALID", "网络没有底层 skrf.Network")

    history = []

    def _one(n_real, n_cmplx):
        vf = VectorFitting(net._net)
        vf.vector_fit(n_poles_real=n_real, n_poles_cmplx=n_cmplx,
                      fit_constant=True, fit_proportional=False, enforce_dc=True)
        err = _fit_error(vf, net)
        return vf, err

    if isinstance(order, int) and order > 0:
        n_real = max(1, order // 2)
        n_cmplx = max(0, order - n_real)
        vf, err = _one(n_real, n_cmplx)
        history.append({"order": order, "n_poles_real": n_real,
                        "n_poles_cmplx": n_cmplx, **err})
    else:
        # 自适应：阶数 2 → max_order，逐步加 2 个实极点 + 2 个复极点，
        # 一旦 rms 达标就停（并记录收敛过程）
        vf, err, order = None, None, 0
        n_real, n_cmplx = 1, 1
        while order < max_order:
            vf_try, err_try = _one(n_real, n_cmplx)
            order = int(np.atleast_1d(vf_try.poles).size)
            history.append({"requested_real": n_real, "requested_cmplx": n_cmplx,
                            "order": order, **err_try})
            log(f"  [vf] order={order} rms={err_try['rms_mag_error_db']:.4f} dB "
                f"max={err_try['max_mag_error_db']:.4f} dB")
            vf, err = vf_try, err_try
            if err["rms_mag_error_db"] <= target_rms_db:
                break
            n_real += 1
            if n_cmplx < max(1, max_order // 4):
                n_cmplx += 1
        if err["rms_mag_error_db"] > target_rms_db:
            raise VectorFitError(
                "VECTOR_FIT_NOT_CONVERGED",
                f"到 max_order={max_order} 仍未达到 target_rms_db={target_rms_db}；"
                f"最后 rms={err['rms_mag_error_db']:.3f} dB。"
                f"请提高 max_order 或放宽 target（记录在 history 里）")

    passivity = _passivity(vf, net)
    enforced = None
    if enforce_passivity and not passivity["passive"]:
        import copy
        before = dict(err)
        backup = copy.deepcopy(vf)          # enforcement 会就地改 vf，先留一份
        ok, note = True, None
        try:
            # 采样点太少时 skrf 会放弃（实测 200 点在高频段仍非无源），给足再试
            vf.passivity_enforce(n_samples=1000)
        except Exception as e:                                  # pragma: no cover
            ok, note = False, f"{type(e).__name__}: {e}"
        err_after = _fit_error(vf, net)
        p_after = _passivity(vf, net)
        ok = ok and p_after["passive"]
        # 需求文档明确禁止"为了 passivity 把模型修到完全失真"：
        # 误差恶化超过 3 倍或越过 1 dB，就直接拒绝这次 enforcement，保留原拟合。
        degraded = (err_after["rms_mag_error_db"] > max(3 * before["rms_mag_error_db"], 1.0))
        if degraded:
            ok = False
            note = (f"passivity enforcement 把 rms 误差从 "
                    f"{before['rms_mag_error_db']:.3f} dB 恶化到 "
                    f"{err_after['rms_mag_error_db']:.3f} dB，已拒绝并保留原拟合")
            vf = backup
            err_after = before
            p_after = passivity
        enforced = {"before": before, "after": err_after,
                    "passivity_after": p_after, "success": bool(ok),
                    "rejected": bool(degraded), "note": note if not ok else None}
        err = err_after
        passivity = p_after
        if not ok:
            passivity["enforcement_failed"] = True

    return {
        "vf": vf, "poles": np.atleast_1d(vf.poles), "order": int(
            np.atleast_1d(vf.poles).size),
        "history": history, "error": err, "passivity": passivity,
        "enforced": enforced, "fidelity": FIDELITY,
        "band_hz": [float(net.freq[0]), float(net.freq[-1])],
    }


def _model_s(vf, net):
    """把拟合模型的 S 矩阵取出来（用于 passivity / 误差，而不是拿原始数据糊弄）。"""
    f = net.freq
    n = net.nports
    s = np.zeros((len(f), n, n), dtype=complex)
    for i in range(n):
        for j in range(n):
            s[:, i, j] = np.asarray(vf.get_model_response(i, j, f))
    return s


def _passivity(vf, net, tol=1e-3):
    """严格 passivity + 容差内判定都要报（无损网络会卡在 |S|=1 边界）。

    注意：必须用**模型**的 S 算奇异值。拿原始数据算的话，非无源的拟合也会被判成无源
    （实测踩到：passivity_test 明明报警，这里却报 passive=True）。
    """
    try:
        strict = bool(vf.is_passive())
    except Exception:
        strict = None
    try:
        sm = _model_s(vf, net)
        sv = np.linalg.svd(sm, compute_uv=False)
        max_sv = float(np.max(sv))
    except Exception:
        max_sv = None
    tol_ok = (max_sv is not None and max_sv <= 1.0 + tol)
    return {"passive": bool(strict) or bool(tol_ok), "passive_strict": strict,
            "max_singular_value": max_sv, "tolerance": tol,
            "note": ("无损网络 |S| 奇异值恰在 1 附近，数值误差会判成非无源；"
                     "容差内视为通过并如实记录" if (not strict and tol_ok) else None)}


def write_spice(fit_result, path, name="s_equiv", header_extra=None):
    """生成 ngspice 子电路文件（skrf 写，我们改命名 + 加 provenance 头）。"""
    vf = fit_result["vf"]
    tmpdir = tempfile.mkdtemp(prefix="vfit")
    tmp = os.path.join(tmpdir, "model" + SUBEXT)
    vf.write_spice_subcircuit_s(tmp, name)
    with open(tmp, encoding="utf-8") as f:
        body = f.read()
    head = ["* ===== vector-fit N-port macromodel (scikit-rf VectorFitting) =====",
            f"* model: {name}  order={fit_result['order']} poles",
            f"* fit: rms={fit_result['error']['rms_mag_error_db']:.4f} dB "
            f"max={fit_result['error']['max_mag_error_db']:.4f} dB "
            f"phase_rms={fit_result['error']['rms_phase_error_deg']:.3f} deg",
            f"* band: {fit_result['band_hz'][0]:.6g}..{fit_result['band_hz'][1]:.6g} Hz",
            f"* fidelity: {FIDELITY}"]
    if header_extra:
        for k, v in header_extra.items():
            head.append(f"* {k}: {v}")
    # 去掉 skrf 原来的两行注释头（避免重复），保留 .SUBCKT 起
    lines = [l for l in body.splitlines() if not l.startswith("* ")]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(head) + "\n" + "\n".join(lines) + "\n")
    return path


def summary_lines(res):
    e = res["error"]
    p = res["passivity"]
    out = [f"vector fit: order={res['order']} poles over "
           f"{res['band_hz'][0]:.4g}..{res['band_hz'][1]:.4g} Hz",
           f"  rms |S| error = {e['rms_mag_error_db']:.4f} dB, "
           f"max = {e['max_mag_error_db']:.4f} dB",
           f"  rms phase error = {e['rms_phase_error_deg']:.3f} deg, "
           f"max = {e['max_phase_error_deg']:.3f} deg",
           f"  passive: {p['passive']} (strict={p['passive_strict']}, "
           f"max|S|_sv={p['max_singular_value']:.6f})"]
    if p.get("note"):
        out.append(f"  note: {p['note']}")
    if res.get("enforced"):
        out.append(f"  passivity enforced: after rms = "
                   f"{res['enforced']['after']['rms_mag_error_db']:.4f} dB "
                   f"(before {res['enforced']['before']['rms_mag_error_db']:.4f})")
    return out


def to_manifest(res, path=None):
    e = res["error"]
    return {
        "fidelity": FIDELITY,
        "order": res["order"],
        "poles": [complex(v) for v in np.atleast_1d(res["poles"])],
        "band_hz": res["band_hz"],
        "rms_mag_error_db": e["rms_mag_error_db"],
        "max_mag_error_db": e["max_mag_error_db"],
        "rms_phase_error_deg": e["rms_phase_error_deg"],
        "max_phase_error_deg": e["max_phase_error_deg"],
        "passivity": res["passivity"],
        "enforced": res.get("enforced"),
        "history": res["history"],
        "spice_file": path,
    }
