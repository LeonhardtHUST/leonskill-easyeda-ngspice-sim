#!/usr/bin/env python3
"""v2.4 P0-I / P1-A：参数校准 + 灵敏度 + 可辨识性。

    fit = parameter_fit.fit(simulator, targets, params, ...)

* 优化器用 `scipy.optimize.least_squares`（TRF + 硬边界），**禁止无界拟合**：
  每个参数必须给 `bounds`，否则直接报 `PARAM_BOUNDS_MISSING`。
* 训练/验证分离（P1）：`fit_slice` 与 `validation_slice` 分开，两个误差都报，
  避免"拿同一份数据拟合又宣布准确"。
* 灵敏度（P1-A）：归一化 ∂M/∂p（有限差分），给出排序——回答"哪个寄生最值得实测"。
* 可辨识性（P1-A）：灵敏度向量之间的相关系数；高度相关 → `PARAMETER_IDENTIFIABILITY_LOW`
  （trace L / via L / cap ESL 在窄带里几乎不可区分，必须说出来）。
"""
import json

import numpy as np


class FitError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _norm(name):
    return str(name)


def prepare_params(specs):
    """[{name, initial, bounds, scale}] → 规范化的列表。"""
    out = []
    for s in specs:
        name = s.get("name") or s.get("parameter")
        if not name:
            raise FitError("PARAM_SPEC_INVALID", f"缺少 name: {s}")
        b = s.get("bounds")
        if not b or len(b) != 2 or b[0] >= b[1]:
            raise FitError(
                "PARAM_BOUNDS_MISSING",
                f"{name}: 必须给严格的 bounds=[lo, hi]（禁止无界拟合）")
        init = s.get("initial", s.get("nominal"))
        if init is None:
            init = float(b[0]) + 0.5 * (float(b[1]) - float(b[0]))
        lo, hi = float(b[0]), float(b[1])
        init = min(max(float(init), lo), hi)
        out.append({"name": _norm(name), "initial": init, "bounds": (lo, hi),
                    "scale": s.get("scale", "linear"),
                    "label": s.get("label")})
    if len({p["name"] for p in out}) != len(out):
        raise FitError("PARAM_SPEC_INVALID", "参数名重复")
    return out


def _slice(arr, sl):
    if sl is None:
        return arr
    if isinstance(sl, (list, tuple)) and len(sl) == 2:
        return arr[sl[0]:sl[1]]
    return arr[sl]


def fit(simulator, targets, param_specs, weights=None, fit_slice=None,
        validation_slice=None, max_nfev=60, log=print):
    """最小二乘校准。

    simulator(params: dict) → np.ndarray（与 targets 同形状）
    targets: np.ndarray
    """
    import scipy.optimize as opt

    params = prepare_params(param_specs)
    names = [p["name"] for p in params]
    x0 = np.array([p["initial"] for p in params], dtype=float)
    lo = np.array([p["bounds"][0] for p in params], dtype=float)
    hi = np.array([p["bounds"][1] for p in params], dtype=float)
    tgt = np.asarray(targets, dtype=float)
    if weights is None:
        w = np.ones_like(tgt)
    else:
        w = np.asarray(weights, dtype=float) * np.ones_like(tgt)
    scale = max(float(np.max(np.abs(tgt))), 1e-15)

    evals = {"n": 0}

    def residual(x):
        evals["n"] += 1
        vals = {n: float(v) for n, v in zip(names, x)}
        sim = np.asarray(simulator(vals), dtype=float)
        if sim.shape != tgt.shape:
            raise FitError("PARAM_FIT_SHAPE",
                           f"simulator 返回 {sim.shape}，targets 是 {tgt.shape}")
        r = (sim - tgt) / scale * w
        return _slice(r, fit_slice)

    log(f"  [fit] {len(params)} params, {len(tgt)} targets, "
        f"fit slice={fit_slice}, nfev cap={max_nfev}")
    sol = opt.least_squares(residual, x0, bounds=(lo, hi), max_nfev=max_nfev,
                            xtol=1e-10, ftol=1e-10, gtol=1e-10)
    fitted = {n: float(v) for n, v in zip(names, sol.x)}
    log(f"  [fit] cost={sol.cost:.3e} nfev={sol.nfev} optimality={sol.optimality:.2e}")

    # 残差（训练 / 验证分开）
    sim_full = np.asarray(simulator(fitted), dtype=float)
    r_full = (sim_full - tgt) / scale
    fit_err = _err(_slice(r_full, fit_slice))
    val_err = (_err(_slice(r_full, validation_slice))
               if validation_slice is not None else None)

    # 灵敏度（归一化 ∂M/∂p）
    sens = []
    base = sim_full
    for i, p in enumerate(params):
        h = max(abs(fitted[p["name"]]) * 1e-3, (hi[i] - lo[i]) * 1e-6, 1e-18)
        xp = dict(fitted)
        xp[p["name"]] = min(fitted[p["name"]] + h, hi[i])
        sp = np.asarray(simulator(xp), dtype=float)
        d = (sp - base) / h
        # 归一化：(∂M/∂p)·p / M_rms
        m_rms = max(float(np.sqrt(np.mean(base ** 2))), 1e-15)
        vec = d * fitted[p["name"]] / m_rms
        sens.append({"parameter": p["name"],
                     "normalized_sensitivity": float(np.linalg.norm(vec)),
                     "vector": np.asarray(vec, dtype=float)})

    order = sorted(range(len(sens)), key=lambda i: -sens[i]["normalized_sensitivity"])
    ranking = [{"rank": r + 1, "parameter": sens[i]["parameter"],
                "normalized_sensitivity": sens[i]["normalized_sensitivity"]}
               for r, i in enumerate(order)]

    ident = _identifiability(sens)
    return {
        "params": params, "fitted": fitted, "x0": x0.tolist(),
        "bounds": [list(p["bounds"]) for p in params],
        "cost": float(sol.cost), "nfev": int(sol.nfev),
        "status": int(sol.status), "message": str(sol.message),
        "fit_error": fit_err, "validation_error": val_err,
        "sensitivity_ranking": ranking,
        "identifiability": ident,
        "n_simulator_calls": evals["n"],
    }


def _err(r):
    r = np.asarray(r, dtype=float)
    return {"rms": float(np.sqrt(np.mean(r ** 2))),
            "max": float(np.max(np.abs(r))),
            "n": int(r.size)}


def _identifiability(sens, threshold=0.95):
    """灵敏度向量两两相关；高相关 = 无法区分。"""
    if len(sens) < 2:
        return {"warning": None, "pairs": []}
    mat = np.vstack([s["vector"] for s in sens])
    # 去均值后算相关，避免共同的 DC 分量把一切都"相关"起来
    mat = mat - mat.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    normed = mat / np.maximum(norms, 1e-15)
    corr = normed @ normed.T
    pairs = []
    for i in range(len(sens)):
        for j in range(i + 1, len(sens)):
            c = float(corr[i, j])
            if abs(c) >= threshold:
                pairs.append({"a": sens[i]["parameter"], "b": sens[j]["parameter"],
                              "correlation": c})
    if pairs:
        return {"code": "PARAMETER_IDENTIFIABILITY_LOW",
                "message": "以下参数在当前频段/波形上高度相关，拟合值不可单独采信："
                           + "; ".join(f"{p['a']}↔{p['b']}({p['correlation']:.3f})"
                                       for p in pairs),
                "pairs": pairs}
    return {"code": None, "pairs": []}


def write_report(result, path, meta=None, sample="OUT_OF_SAMPLE"):
    rep = {"version": 1, "kind": "parameter_calibration",
           "sample": sample, "meta": meta or {},
           "fit_error": result["fit_error"],
           "validation_error": result["validation_error"],
           "fitted": result["fitted"], "initial": dict(
               zip([p["name"] for p in result["params"]], result["x0"])),
           "bounds": dict(zip([p["name"] for p in result["params"]],
                              [list(p["bounds"]) for p in result["params"]])),
           "sensitivity_ranking": result["sensitivity_ranking"],
           "identifiability": result["identifiability"],
           "cost": result["cost"], "nfev": result["nfev"],
           "note": ("sample=IN_SAMPLE 表示这批数据同时用于拟合，"
                    "不能当验证依据")}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    return rep


def summary_lines(res):
    out = [f"calibration: {len(res['fitted'])} params, cost={res['cost']:.3e}, "
           f"nfev={res['nfev']}"]
    for p in res["params"]:
        n = p["name"]
        out.append(f"  {n}: {p['initial']:.6g} → {res['fitted'][n]:.6g} "
                   f"(bounds {p['bounds'][0]:.4g}..{p['bounds'][1]:.4g})")
    fe = res["fit_error"]
    out.append(f"  fit error: rms {fe['rms'] * 100:.3f}%, max {fe['max'] * 100:.3f}%")
    if res["validation_error"]:
        ve = res["validation_error"]
        out.append(f"  validation error: rms {ve['rms'] * 100:.3f}%, "
                   f"max {ve['max'] * 100:.3f}%")
    if res["sensitivity_ranking"]:
        out.append("  sensitivity ranking: " + ", ".join(
            f"{x['parameter']}({x['normalized_sensitivity']:.3f})"
            for x in res["sensitivity_ranking"][:4]))
    if res["identifiability"].get("code"):
        out.append(f"  ⚠ {res['identifiability']['code']}: "
                   f"{res['identifiability']['message']}")
    return out
