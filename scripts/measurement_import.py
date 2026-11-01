#!/usr/bin/env python3
"""v2.4 P0-H(1)：测量数据导入（示波器 CSV / 电源轨 CSV / VNA Touchstone）。

设计要点：
  * 列映射可配（不同厂家的 CSV 表头千奇百怪，`time`/`Time(s)`/`时间` 都见过）；
  * 单位自动推断（s/ms/µs/ns；V/mV）；
  * 时间列单调性与重复点会被检查并报告，**不静默排序**（可能掩盖数据问题）。
"""
import csv
import os
import re

import numpy as np

TIME_UNITS = {"s": 1.0, "sec": 1.0, "second": 1.0,
              "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ns": 1e-9,
              "ps": 1e-12}
VOLT_UNITS = {"v": 1.0, "mv": 1e-3, "uv": 1e-6, "µv": 1e-6, "kv": 1e3}
AMP_UNITS = {"a": 1.0, "ma": 1e-3, "ua": 1e-6, "µa": 1e-6}


class MeasurementError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _unit_scale(header, table):
    h = str(header or "").lower()
    for u, k in sorted(table.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"[\(\[]\s*" + re.escape(u) + r"\s*[\)\]]", h) or h.strip() == u:
            return k
    return 1.0


def _find_col(headers, candidates, default_index=None):
    low = [h.lower().strip() for h in headers]
    for cand in candidates:
        for i, h in enumerate(low):
            if cand in h:
                return i
    return default_index


def load_csv(path, mapping=None, has_header=True):
    """读取测量 CSV。

    mapping: {"time": "Time (s)", "voltage": "CH1", "current": "I_rail"}
             也接受列序号（整数）。
    返回 {"time_s": [...], "voltage": [...], "current": [...], "meta": {...}}
    """
    if not os.path.exists(path):
        raise MeasurementError("MEAS_FILE_MISSING", f"文件不存在: {path}")
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        rows = list(csv.reader(f, dialect))
    if not rows:
        raise MeasurementError("MEAS_PARSE_ERROR", "CSV 是空的")
    meta = {"file": os.path.abspath(path), "n_rows": len(rows), "warnings": []}
    headers = rows[0] if has_header else [f"c{i}" for i in range(len(rows[0]))]
    body = rows[1:] if has_header else rows
    mapping = mapping or {}
    i_t = _col(mapping, "time", headers, ["time", "t ", "时间"], 0)
    i_v = _col(mapping, "voltage", headers, ["volt", "v(", "ch1", "幅度", "电压"], 1)
    i_i = _col(mapping, "current", headers, ["curr", "i(", "电流"], None)
    st = _unit_scale(headers[i_t] if i_t is not None else "", TIME_UNITS)
    sv = _unit_scale(headers[i_v] if i_v is not None else "", VOLT_UNITS)
    si = _unit_scale(headers[i_i] if i_i is not None else "", AMP_UNITS) if i_i is not None else 1.0

    t, v, cur = [], [], []
    for r in body:
        if len(r) <= max(i for i in (i_t, i_v) if i is not None):
            continue
        try:
            tv = float(r[i_t]) * st
            vv = float(r[i_v]) * sv
        except (TypeError, ValueError):
            continue
        t.append(tv)
        v.append(vv)
        cur.append(float(r[i_i]) * si if i_i is not None else None)
    if len(t) < 4:
        raise MeasurementError("MEAS_PARSE_ERROR", f"只解析到 {len(t)} 个点，检查列映射")
    t = np.array(t, dtype=float)
    v = np.array(v, dtype=float)
    d = np.diff(t)
    if np.any(d < 0):
        meta["warnings"].append("时间列非单调递减，已按原顺序保留（未排序）")
    if np.any(d == 0):
        meta["warnings"].append("存在重复时间戳")
    meta.update({"columns": {"time": headers[i_t] if i_t is not None else None,
                             "voltage": headers[i_v] if i_v is not None else None,
                             "current": headers[i_i] if i_i is not None else None},
                 "units": {"time": st, "voltage": sv, "current": si},
                 "dt_median_s": float(np.median(d)) if len(d) else None})
    return {"time_s": t, "voltage": v,
            "current": None if all(c is None for c in cur) else np.array(
                [c if c is not None else np.nan for c in cur], dtype=float),
            "meta": meta}


def _col(mapping, key, headers, pats, default):
    if key in mapping:
        m = mapping[key]
        if isinstance(m, int):
            return m
        i = _find_col(headers, [str(m).lower()])
        if i is None:
            raise MeasurementError("MEAS_COLUMN_NOT_FOUND",
                                   f"找不到列 {m!r}（表头: {headers}）")
        return i
    return _find_col(headers, pats, default)


def load_any(path, mapping=None):
    """按扩展名分派：.csv/.txt → 波形；.s1p/.s2p → RF 网络。"""
    low = path.lower()
    if re.search(r"\.s\d+p$", low):
        import rf_network
        return {"type": "sparam", "network": rf_network.RfNetwork.from_touchstone(path)}
    return {"type": "waveform", **load_csv(path, mapping)}


def summary_lines(m):
    if m["type"] == "sparam":
        n = m["network"]
        return [f"measurement: {os.path.basename(n.meta.get('file', ''))} "
                f"({n.nports}-port, {n.npoints} pts)"]
    meta = m["meta"]
    return [f"measurement: {os.path.basename(meta['file'])} "
            f"{len(m['time_s'])} pts, dt={meta['dt_median_s']:.3g} s",
            f"  columns: {meta['columns']}"] + \
           [f"  warning: {w}" for w in meta["warnings"]]
