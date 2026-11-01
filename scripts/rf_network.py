#!/usr/bin/env python3
"""v2.4 P0-B：RF 网络引擎（scikit-rf 适配层）。

原则：**不重写成熟的 network algebra**。S/Y/Z/ABCD 转换、renormalize、级联、
connect、de-embedding 全部交给 scikit-rf（BSD-3-Clause）；本模块只做：

  * 依赖探测与优雅降级（没装 scikit-rf 时退回 v2.3 的 touchstone.py 解析器）；
  * 统一的 `RfNetwork` 门面（频率轴 + S 矩阵 + 每端口 z0 + 端口名）；
  * 端口阻抗契约检查（Touchstone 的 z0 ≠ 电路 z0 时必须 renorm 或 strict fail）；
  * 与项目内节点的对接（把 N 端口映射到 SPICE 节点组）。

依赖策略见 requirements-rf.txt：没有 RF extras 时，v2.3 的全部功能照常工作，
本模块的 `available()` 返回 False，调用方走 lumped 后端。
"""
import json
import os

import numpy as np

_SKRf = None
_STATUS = {"available": False, "reason": None, "version": None}


def _load():
    global _SKRf
    if _SKRf is not None:
        return _SKRf
    try:
        import skrf
        _SKRf = skrf
        _STATUS.update(available=True, version=getattr(skrf, "__version__", "?"),
                       reason=None)
    except Exception as e:                                  # pragma: no cover
        _SKRf = False
        _STATUS.update(available=False, version=None,
                       reason=f"{type(e).__name__}: {e}")
    return _SKRf


def available():
    _load()
    return bool(_STATUS["available"])


def capability_report():
    """给 manifest / 报告用的能力说明（缺失原因必须可见）。"""
    _load()
    return dict(_STATUS, features=(
        ["network_io", "renormalize", "cascade", "connect", "deembed",
         "vector_fitting", "passivity"] if _STATUS["available"] else []))


class RfNetworkError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class RfNetwork:
    """N 端口 S 参数网络的统一门面。

    内部持有 skrf.Network（若可用）；不可用时只用 numpy 做最小子集
    （读取 / 幅度相位 / 单端口阻抗），并标 `backend="numpy_fallback"`。
    """

    def __init__(self, s=None, freq_hz=None, z0=50.0, name="network",
                 backend="skrf", port_names=None, meta=None):
        self.name = name
        self.port_names = list(port_names or [])
        self.meta = dict(meta or {})
        self.backend = backend
        self.s = None if s is None else np.asarray(s, dtype=complex)
        self.freq = None if freq_hz is None else np.asarray(freq_hz, dtype=float)
        self.z0 = z0
        self._net = None
        if s is not None and freq_hz is not None and backend == "skrf":
            skrf = _load()
            if skrf:
                f = skrf.Frequency.from_f(self.freq, unit="hz")
                z0flat = np.atleast_1d(np.asarray(z0, dtype=complex))
                z0arr = np.full(self.nports, float(np.mean(np.real(z0flat))))
                self._net = skrf.Network(frequency=f, s=self.s, z0=z0arr, name=name)

    # ---------------------------------------------------------------- 属性
    @property
    def nports(self):
        return int(self.s.shape[-1]) if self.s is not None else 0

    @property
    def npoints(self):
        return int(self.s.shape[0]) if self.s is not None else 0

    def z0_vector(self):
        """每端口的 z0（复数）。skrf 会存成 (npoints, nports)，取代表值。"""
        z = np.asarray(self.z0, dtype=complex)
        if z.ndim == 2:
            z = np.median(np.real(z), axis=0) + 1j * np.median(np.imag(z), axis=0)
        z = np.atleast_1d(z)
        if z.size == 1:
            z = np.repeat(z, self.nports)
        return z

    # ---------------------------------------------------------------- 读取
    @classmethod
    def from_touchstone(cls, path, name=None, port_names=None):
        if not os.path.exists(path):
            raise RfNetworkError("TOUCHSTONE_FILE_MISSING", f"文件不存在: {path}")
        skrf = _load()
        meta = {"file": os.path.abspath(path)}
        if skrf:
            try:
                n = skrf.Network(path)
                z0 = np.asarray(n.z0, dtype=complex)
                return cls(s=np.asarray(n.s), freq_hz=np.asarray(n.f, dtype=float),
                           z0=z0, name=name or os.path.basename(path),
                           backend="skrf", port_names=port_names, meta=meta)
            except Exception as e:
                meta["skrf_error"] = f"{type(e).__name__}: {e}"
        # 回退到 v2.3 自带解析器（只用 numpy）
        import touchstone
        ts = touchstone.parse(path)
        s = np.array(ts.s, dtype=complex)
        return cls(s=s, freq_hz=np.array(ts.frequencies, dtype=float), z0=ts.z0,
                   name=name or os.path.basename(path), backend="numpy_fallback",
                   port_names=port_names, meta=meta)

    @classmethod
    def from_spice_nodes(cls, pairs, z0=50.0):
        """占位：把 (n+, n-) 端口对记到 meta，供网表生成使用。"""
        return cls.__new__(cls)

    # ---------------------------------------------------------------- 转换
    def _require_skrf(self, what):
        if self._net is None:
            raise RfNetworkError(
                "RF_BACKEND_UNAVAILABLE",
                f"{what} 需要 scikit-rf（pip install -r requirements-rf.txt）；"
                f"当前 backend={self.backend}"
                + (f"，原因: {_STATUS['reason']}" if _STATUS.get("reason") else ""))
        return self._net

    def s_db(self):
        return 20 * np.log10(np.maximum(np.abs(self.s), 1e-30))

    def s_deg(self):
        return np.degrees(np.angle(self.s))

    def to_y(self):
        return np.asarray(self._require_skrf("Y 参数转换").y)

    def to_z(self):
        return np.asarray(self._require_skrf("Z 参数转换").z)

    def to_abcd(self):
        if self.nports != 2:
            raise RfNetworkError("RF_PORT_COUNT", "ABCD 只对 2 端口有定义")
        return np.asarray(self._require_skrf("ABCD 转换").a)

    # ---------------------------------------------------------------- 变换
    def renormalize(self, z0_new, strict=False):
        """把端口阻抗改成 z0_new；返回新对象，并把过程记进 meta。"""
        z_new = np.atleast_1d(np.asarray(z0_new, dtype=complex))
        if z_new.size == 1:
            z_new = np.repeat(z_new, self.nports)
        old = self.z0_vector()
        if z_new.size != self.nports:
            raise RfNetworkError("RF_PORT_COUNT",
                                 f"z0 要给 {self.nports} 个或 1 个，收到 {z_new.size} 个")
        if np.allclose(old, z_new):
            out = self.copy()
            out.meta["renormalized"] = False
            return out
        if strict and not available():
            raise RfNetworkError(
                "RF_BACKEND_UNAVAILABLE", "strict 模式下要求重新归一化，但 scikit-rf 不可用")
        net = self._require_skrf("端口重新归一化")
        try:
            new = net.copy()
            new.renormalize(z_new)
        except Exception as e:
            raise RfNetworkError(
                "RF_RENORM_FAILED",
                f"renormalize 失败（scikit-rf 对复数 z0 支持有限）: {e}")
        out = RfNetwork(s=np.asarray(new.s), freq_hz=self.freq, z0=z_new,
                        name=self.name, backend="skrf",
                        port_names=self.port_names, meta=dict(self.meta))
        out.meta.update({"renormalized": True,
                         "original_z0": [complex(float(np.real(v)), float(np.imag(v)))
                                         for v in np.atleast_1d(old)],
                         "target_z0": [complex(float(np.real(v)), float(np.imag(v)))
                                       for v in np.atleast_1d(z_new)]})
        return out

    def copy(self):
        return RfNetwork(s=None if self.s is None else self.s.copy(),
                         freq_hz=None if self.freq is None else self.freq.copy(),
                         z0=self.z0, name=self.name, backend=self.backend,
                         port_names=list(self.port_names), meta=dict(self.meta))

    def resample(self, freq_hz):
        """把网络重采到新的频率网格（原地修改，用 skrf 的复数插值）。

        v2.5.1：de-embedding 的 fixture 与 DUT 网格不一致时必须显式重采，
        不能"首尾对得上"就往下算。原始网格记进 meta，便于 manifest 交代。
        """
        net = self._require_skrf("频率重采")
        skrf = _load()
        target = np.asarray(freq_hz, dtype=float)
        self.meta["resampled_from"] = {
            "n_points": int(self.npoints),
            "f_start": float(self.freq[0]) if self.npoints else None,
            "f_stop": float(self.freq[-1]) if self.npoints else None,
        }
        self.meta["resample_method"] = "skrf.Network.resample (linear on S)"
        new = net.copy()
        new.resample(skrf.Frequency.from_f(target, unit="hz"))
        self.s = np.asarray(new.s)
        self.freq = target
        self._net = new
        return self

    def is_passive(self, tol=1e-3):
        net = self._require_skrf("passivity 检查")
        try:
            return bool(net.is_passive())
        except Exception:
            # 退化判据：每个频点的最大奇异值 ≤ 1+tol
            sv = np.linalg.svd(self.s, compute_uv=False)
            return bool(np.all(sv <= 1 + tol))

    def is_reciprocal(self, tol=1e-6):
        if self.s is None:
            return False
        return bool(np.allclose(self.s, np.transpose(self.s, (0, 2, 1)), atol=tol))

    # ---------------------------------------------------------------- 组合
    def cascade(self, other):
        a, b = self._require_skrf("级联"), other._require_skrf("级联")
        if self.nports != 2 or other.nports != 2:
            raise RfNetworkError("RF_PORT_COUNT", "cascade 需要两个 2 端口网络")
        new = a ** b
        return RfNetwork(s=np.asarray(new.s), freq_hz=np.asarray(new.f, dtype=float),
                         z0=np.asarray(new.z0), name=f"{self.name}**{other.name}",
                         backend="skrf", meta={"cascade": [self.name, other.name]})

    def connect(self, port_a, port_b):
        skrf = _load()
        net = self._require_skrf("connect")
        new = skrf.connect(net, port_a, port_b)
        return RfNetwork(s=np.asarray(new.s), freq_hz=np.asarray(new.f, dtype=float),
                         z0=np.asarray(new.z0), name=self.name, backend="skrf",
                         meta={"connect": [port_a, port_b]})

    # ---------------------------------------------------------------- 输出
    def to_dict(self):
        return {
            "name": self.name, "nports": self.nports, "npoints": self.npoints,
            "f_start_hz": float(self.freq[0]) if self.npoints else None,
            "f_stop_hz": float(self.freq[-1]) if self.npoints else None,
            "z0": [complex(float(np.real(v)), float(np.imag(v)))
                   for v in np.atleast_1d(self.z0_vector())],
            "backend": self.backend,
            "port_names": self.port_names,
            "passive": (self.is_passive() if self.backend == "skrf" else None),
            "reciprocal": (self.is_reciprocal() if self.s is not None else None),
            "source": self.meta,
        }

    def write_touchstone(self, path):
        net = self._require_skrf("写回 Touchstone")
        net.write_touchstone(os.path.splitext(path)[0])
        return path


# --------------------------------------------------------------------------
def ensure_z0(net, target_z0, strict=True):
    """端口阻抗契约：Touchstone 的 z0 与电路 z0 不一致时必须处理。

    返回 (network, info)。strict=False 时仅记录告警。
    """
    cur = net.z0_vector()
    tgt = np.atleast_1d(np.asarray(target_z0, dtype=complex))
    if tgt.size == 1:
        tgt = np.repeat(tgt, net.nports)
    if np.allclose(cur, tgt):
        return net, {"original_z0": [complex(v) for v in cur],
                     "target_z0": [complex(v) for v in tgt],
                     "renormalized": False}
    if strict and not available():
        raise RfNetworkError(
            "RF_Z0_MISMATCH",
            f"端口 z0={list(cur)} 与电路 z0={list(tgt)} 不一致，且 scikit-rf 不可用；"
            f"请安装 requirements-rf.txt 或显式允许近似")
    out = net.renormalize(tgt, strict=False)
    return out, {"original_z0": [complex(float(np.real(v)), float(np.imag(v))) for v in cur],
                 "target_z0": [complex(float(np.real(v)), float(np.imag(v))) for v in tgt],
                 "renormalized": True}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    print(json.dumps(capability_report(), ensure_ascii=False, indent=1))
    n = RfNetwork.from_touchstone(sys.argv[1])
    print(json.dumps(n.to_dict(), ensure_ascii=False, indent=1, default=str))
