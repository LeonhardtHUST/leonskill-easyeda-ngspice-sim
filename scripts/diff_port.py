#!/usr/bin/env python3
"""v2.4 P0-E：差分端口抽象与混合模（mixed-mode）变换。

    {type: "differential_port", positive: "CLK_P", negative: "CLK_N", z_diff: 100}

单端 ↔ 混合模的变换矩阵（每对端口）：

    M = 1/√2 · [[ 1, -1],
                [ 1,  1]]

    S_mm = M · S_se · M⁻¹     （对每对端口做块变换）

给出 Sdd / Sdc / Scd / Scc 四个子矩阵。z_diff 只影响"从 Sdd 反算 Zdiff"这一步，
不改变 S 本身（S 的定义已含 z0）。
"""
import numpy as np

# 混合模 4 块的名字（d=differential, c=common）
BLOCKS = ("dd", "dc", "cd", "cc")


class DiffPortError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class DifferentialPort:
    def __init__(self, positive, negative, z_diff=100.0, z_se=None, name=None):
        if str(positive) == str(negative):
            raise DiffPortError("DIFF_PORT_INVALID", f"正负端相同: {positive}")
        self.positive = str(positive)
        self.negative = str(negative)
        self.z_diff = float(z_diff)
        # 单端参考阻抗：差分 100Ω 通常对应单端 50Ω
        self.z_se = float(z_se) if z_se else self.z_diff / 2.0
        self.name = name or f"{positive}/{negative}"

    def to_dict(self):
        return {"type": "differential_port", "name": self.name,
                "positive": self.positive, "negative": self.negative,
                "z_diff": self.z_diff, "z_se": self.z_se}

    @classmethod
    def from_cfg(cls, cfg):
        return cls(cfg.get("positive"), cfg.get("negative"),
                   cfg.get("z_diff", 100.0), cfg.get("z_se"), cfg.get("name"))


def mixed_mode_matrix(n_pairs):
    """返回 (M, Minv)，形状 (2P, 2P)。端口顺序：[p1+, p1-, p2+, p2-, ...]"""
    blocks = np.array([[1, -1], [1, 1]], dtype=float) / np.sqrt(2.0)
    M = np.zeros((2 * n_pairs, 2 * n_pairs))
    for k in range(n_pairs):
        M[2 * k:2 * k + 2, 2 * k:2 * k + 2] = blocks
    return M, np.linalg.inv(M)


def se_to_mm(s, n_pairs):
    """单端 S (npoints, 2P, 2P) → 混合模 S (npoints, 2P, 2P)。

    输出端口顺序：[d1, c1, d2, c2, ...]
    """
    s = np.asarray(s, dtype=complex)
    n = s.shape[-1]
    if n != 2 * n_pairs:
        raise DiffPortError("DIFF_PORT_COUNT",
                            f"单端端口数 {n} 与差分对数 {n_pairs} 不匹配（应为 {2 * n_pairs}）")
    M, Minv = mixed_mode_matrix(n_pairs)
    return np.einsum("ij,kjl,lm->kim", M, s, Minv)


def mm_to_se(s_mm, n_pairs):
    M, Minv = mixed_mode_matrix(n_pairs)
    return np.einsum("ij,kjl,lm->kim", Minv, np.asarray(s_mm, dtype=complex), M)


def blocks(s_mm, n_pairs):
    """拆出 dd / dc / cd / cc 四个子矩阵（形状 (npoints, P, P)）。"""
    s = np.asarray(s_mm, dtype=complex)
    idx = {}
    for k in range(n_pairs):
        idx[("d", k)] = 2 * k
        idx[("c", k)] = 2 * k + 1
    out = {}
    for a in ("d", "c"):
        for b in ("d", "c"):
            m = np.zeros((s.shape[0], n_pairs, n_pairs), dtype=complex)
            for i in range(n_pairs):
                for j in range(n_pairs):
                    m[:, i, j] = s[:, idx[(a, i)], idx[(b, j)]]
            out[a + b] = m
    return out


def diff_impedance(sdd, z_diff=100.0, z_se=None):
    """从 Sdd 反算 Zdiff（1 端口对视角）：Zdiff = 2·z_se·(1+Sdd)/(1-Sdd)。"""
    zse = z_se if z_se else z_diff / 2.0
    sdd = np.asarray(sdd, dtype=complex)
    denom = np.where(np.abs(1 - sdd) < 1e-12, 1e-12, 1 - sdd)
    return 2.0 * zse * (1 + sdd) / denom


def analyze(net, port_cfgs, z0_se=None):
    """把单端多端口网络变成混合模指标。

    port_cfgs: [{positive, negative, z_diff, name}] —— 顺序必须与网表端口顺序一致，
               即 [pos1, neg1, pos2, neg2, ...]。
    """
    pairs = [DifferentialPort.from_cfg(c) for c in port_cfgs]
    P = len(pairs)
    if net.nports != 2 * P:
        raise DiffPortError(
            "DIFF_PORT_COUNT",
            f"网络有 {net.nports} 个单端端口，{P} 对差分需要 {2 * P} 个；"
            f"请检查 .sNp 的端口顺序（约定 [p1+, p1-, p2+, p2-, …]）")
    s_mm = se_to_mm(net.s, P)
    blk = blocks(s_mm, P)
    f = np.asarray(net.freq, dtype=float)
    out = {"pairs": [p.to_dict() for p in pairs], "freq_hz": f.tolist(),
           "blocks": {}, "z": {}}
    for name, m in blk.items():
        # 只保留主对角（同对内部），跨对留全矩阵
        out["blocks"][name] = {
            "db": (20 * np.log10(np.maximum(np.abs(m), 1e-30))).tolist(),
            "n_pairs": P,
        }
    zdd = np.zeros((len(f), P, P), dtype=complex)
    for i in range(P):
        for j in range(P):
            if i == j:
                zdd[:, i, j] = diff_impedance(blk["dd"][:, i, j], pairs[i].z_diff,
                                              pairs[i].z_se)
    out["z"]["zdiff_ohm"] = np.abs(zdd).tolist()
    # 常用标量指标
    out["metrics"] = {}
    for i, p in enumerate(pairs):
        sdd_ii = blk["dd"][:, i, i]
        out["metrics"][p.name] = {
            "sdd_max_db": float(np.max(20 * np.log10(np.maximum(np.abs(sdd_ii), 1e-30)))),
            "sdd_min_db": float(np.min(20 * np.log10(np.maximum(np.abs(sdd_ii), 1e-30)))),
            "scd_max_db": float(np.max(20 * np.log10(
                np.maximum(np.abs(blk["cd"][:, i, i]), 1e-30)))),
            "sdc_max_db": float(np.max(20 * np.log10(
                np.maximum(np.abs(blk["dc"][:, i, i]), 1e-30)))),
            "z_diff_at_f_start": float(np.abs(zdd[0, i, i])),
        }
    return out


def summary_lines(res):
    out = ["differential ports: " + ", ".join(
        f"{p['name']}(+{p['positive']}/-{p['negative']}, {p['z_diff']}Ω)"
        for p in res["pairs"])]
    for name, m in res["metrics"].items():
        out.append(f"  {name}: Sdd max {m['sdd_max_db']:.2f} dB, "
                   f"Scd max {m['scd_max_db']:.2f} dB (mode conversion), "
                   f"Zdiff(f0) {m['z_diff_at_f_start']:.1f} Ω")
    out.append("  注意：Scd/Sdc 是模式转换（平衡度），数值越接近 Sdd 越差")
    return out
