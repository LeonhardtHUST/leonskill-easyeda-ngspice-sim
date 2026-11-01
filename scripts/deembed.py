#!/usr/bin/env python3
"""v2.5 P0-D：de-embedding workflow（复用 scikit-rf 的校准类）。

    deembed.apply(dut_meas, fixtures, method="open_short")
    → (dut_deembedded, DEEMBED_MANIFEST.json)

支持（都来自 skrf.calibration.deembedding）：
  Open / Short / OpenShort / ShortOpen / SplitPi / SplitTee / Through(inverse)

**不自研网络运算**（需求文档 §41），只做：
  * fixture 文件读取 + 端口数/频率轴一致性检查；
  * 方法分派；
  * manifest（含每个 fixture 的 sha256、方法、端口数、频率范围）。
"""
import hashlib
import json
import os

FIDELITY = "SKRF_DEEMBED"

METHODS = {
    "open": ("Open", 1, ("open",)),
    "short": ("Short", 1, ("short",)),
    "open_short": ("OpenShort", 2, ("open", "short")),
    "openshort": ("OpenShort", 2, ("open", "short")),
    "short_open": ("ShortOpen", 2, ("short", "open")),
    "split_pi": ("SplitPi", 1, ("thru",)),
    "split_tee": ("SplitTee", 1, ("thru",)),
}


class DeembedError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_fixtures(fixtures):
    """fixtures: {role: path} 或 [{role, file}]。返回 {role: RfNetwork}。"""
    import rf_network as rn
    if isinstance(fixtures, dict):
        items = fixtures.items()
    else:
        items = [(f.get("role"), f.get("file")) for f in fixtures or []]
    out = {}
    for role, path in items:
        if not role or not path:
            raise DeembedError("DEEMBED_FIXTURE_INVALID", f"fixture 缺 role/file: {role}, {path}")
        if not os.path.exists(path):
            raise DeembedError("DEEMBED_FIXTURE_MISSING", f"{role}: 文件不存在 {path}")
        out[role] = rn.RfNetwork.from_touchstone(path, name=role)
    return out


def _check_consistent(dut, fixtures):
    for role, f in fixtures.items():
        if f.nports != dut.nports:
            raise DeembedError(
                "DEEMBED_PORT_MISMATCH",
                f"{role} 有 {f.nports} 端口，DUT 有 {dut.nports} 端口")
        if not (abs(f.freq[0] - dut.freq[0]) < 1 and abs(f.freq[-1] - dut.freq[-1]) < 1):
            raise DeembedError(
                "DEEMBED_FREQ_MISMATCH",
                f"{role} 频率轴 {f.freq[0]:g}..{f.freq[-1]:g} 与 DUT "
                f"{dut.freq[0]:g}..{dut.freq[-1]:g} 不一致（请先重采到同一网格）")


def apply(dut, fixtures, method="open_short", workdir=None, name="dut"):
    """执行 de-embedding。返回 (deembedded_network, manifest_dict)。

    手写 fixture 的"未知直通"情形请用 method="through" + 显式 inverse，
    本函数只覆盖对称对称 fixture 的标准四类。
    """
    import rf_network as rn
    if not rn.available():
        raise DeembedError("RF_BACKEND_UNAVAILABLE",
                          "de-embedding 需要 scikit-rf（requirements-rf.txt）")
    key = str(method).lower().replace("-", "_")
    if key not in METHODS:
        raise DeembedError("DEEMBED_METHOD_UNKNOWN",
                          f"未知方法 {method!r}（支持 {sorted(METHODS)}）")
    cls_name, need, roles = METHODS[key]
    missing = [r for r in roles if r not in fixtures]
    if missing:
        raise DeembedError("DEEMBED_FIXTURE_MISSING",
                          f"{method} 需要 fixture role={missing}（已有 {sorted(fixtures)}）")
    _check_consistent(dut, fixtures)

    import skrf
    from skrf.calibration import deembedding as de
    cls = getattr(de, cls_name)

    def need_role(role):
        if role not in fixtures:
            raise DeembedError("DEEMBED_FIXTURE_MISSING",
                              f"{method} 需要 fixture role={role!r}")
        return fixtures[role]._net

    # 注意：skrf 的构造签名各不相同，且**第一个位置参数是 dummy 而不是 DUT**。
    # 全部用关键字传，避免把 DUT 当成假件（v2.5 修过这个 bug）。
    if cls_name == "Open":
        dm = cls(dummy_open=need_role("open"))
    elif cls_name == "Short":
        dm = cls(dummy_short=need_role("short"))
    elif cls_name == "OpenShort":
        dm = cls(dummy_open=need_role("open"), dummy_short=need_role("short"))
    elif cls_name == "ShortOpen":
        dm = cls(dummy_short=need_role("short"), dummy_open=need_role("open"))
    elif cls_name in ("SplitPi", "SplitTee"):
        dm = cls(dummy_thru=need_role("thru"))
    else:                                                  # pragma: no cover
        raise DeembedError("DEEMBED_METHOD_UNKNOWN", f"未实现的类 {cls_name}")
    try:
        out = dm.deembed(dut._net)
    except Exception as e:
        raise DeembedError("DEEMBED_FAILED", f"{cls_name}: {type(e).__name__}: {e}")

    res = rn.RfNetwork(s=out.s, freq_hz=out.f, z0=out.z0, name=f"{name}_deembedded",
                       backend="skrf", meta={"deembed": method})
    manifest = {
        "version": 1, "kind": "deembedding", "fidelity": FIDELITY,
        "method": method, "class": cls_name,
        "dut": {"name": dut.name, "nports": dut.nports,
                "file": dut.meta.get("file"),
                "sha256": _sha256(dut.meta["file"]) if dut.meta.get("file") else None},
        "fixtures": [{"role": r, "file": f.meta.get("file"),
                      "sha256": _sha256(f.meta["file"]) if f.meta.get("file") else None,
                      "nports": f.nports}
                     for r, f in fixtures.items()],
        "band_hz": [float(dut.freq[0]), float(dut.freq[-1])],
        "note": "fixture 数据必须是同一夹具、同一校准状态下测的；换线/换夹具要重测",
    }
    if workdir:
        os.makedirs(workdir, exist_ok=True)
        p = os.path.join(workdir, "DEEMBED_MANIFEST.json")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        manifest["manifest_path"] = p
    return res, manifest


def summary_lines(m):
    return [f"de-embedding: {m['method']} ({m['class']}), "
            f"{m['dut']['nports']}-port {m['band_hz'][0]:.4g}..{m['band_hz'][1]:.4g} Hz",
            "  fixtures: " + ", ".join(
                f"{f['role']}={os.path.basename(str(f['file']))}"
                f"({str(f['sha256'])[:8]})" for f in m["fixtures"])]


def main():
    argv = __import__("sys").argv[1:]
    if not argv:
        print(__doc__)
        raise SystemExit(2)
    dut_path = None
    fixtures = {}
    method = "open_short"
    outdir = "."
    name = "dut"
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dut":
            dut_path = argv[i + 1]; i += 2
        elif a == "--fixture":
            role, _, path = argv[i + 1].partition("=")
            fixtures[role] = path; i += 2
        elif a in ("--open", "--short", "--split-pi", "--split-tee", "--thru"):
            fixtures[{"--open": "open", "--short": "short",
                      "--split-pi": "split_pi", "--split-tee": "split_tee",
                      "--thru": "thru"}[a]] = argv[i + 1]; i += 2
        elif a == "--method":
            method = argv[i + 1]; i += 2
        elif a == "--out":
            outdir = argv[i + 1]; i += 2
        elif a == "--name":
            name = argv[i + 1]; i += 2
        else:
            i += 1
    if not dut_path:
        raise SystemExit("必须给 --dut <measured.sNp>")
    if not fixtures:
        raise SystemExit("必须给 fixture：--open/--short（或 --fixture role=path）")
    import rf_network as rn
    if not rn.available():
        raise SystemExit("RF_BACKEND_UNAVAILABLE: 需要 scikit-rf（requirements-rf.txt）")
    dut = rn.RfNetwork.from_touchstone(dut_path)
    fx = load_fixtures(fixtures)
    res, manifest = apply(dut, fx, method=method, workdir=outdir, name=name)
    sp = os.path.join(outdir, f"{name}_deembedded.s{res.nports}p")
    res._net.write_touchstone(sp, form="ri")
    manifest["output"] = sp
    with open(os.path.join(outdir, "DEEMBED_MANIFEST.json"), "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print("\n".join(summary_lines(manifest)))
    print(f"  output: {sp}")
    print(f"  manifest: {os.path.join(outdir, 'DEEMBED_MANIFEST.json')}")
    print("  注意：fixture 必须是同一夹具/同一校准状态下测的；换线要重测")


if __name__ == "__main__":
    main()
