# VECTOR_FITTING — 宽带宏模型（替代窄带 lumped）

```json
{"sparams": [{"name": "FB1", "from": "V_IN", "to": "AVDD",
              "file": "bead.s2p", "backend": "vectorfit",
              "order": "auto", "max_order": 20, "target_rms_db": 0.2,
              "enforce_passivity": true}]}
```

## 为什么加它

v2.3 的 `backend=lumped` 是 3 元件窄带等效（跨 3 个十倍频程误差 20–25%）。
v2.4 用 **scikit-rf 的 VectorFitting**（BSD-3）做极点-留数拟合，得到宽带有理模型，
再导出成 ngspice 子电路。**没有自己实现 VF 算法**（需求文档 §41 明确禁止）。

## 流水线

```
.sNp → RfNetwork → VectorFitting(auto_fit/vector_fit) → 极点/留数
    → passivity 检查（可选 enforce）→ write_spice_subcircuit_s
    → ngspice sp 复算 → 与原始 .sNp 比对 → FIT_VALIDATION.json
```

## 自适应阶数（§3）

从 `(1 实极点, 1 复极点)` 起，误差不达标就加一对；每轮记录：

```json
{"order": 2, "rms_mag_error_db": 0.0007, "max_mag_error_db": 0.0051,
 "rms_phase_error_deg": 0.010, "max_phase_error_deg": 0.078}
```

达到 `target_rms_db` 即停；到 `max_order` 仍不达标 → **`VECTOR_FIT_NOT_CONVERGED`**，
并把整条阶数梯写进错误信息（不硬凑一个"看起来能跑"的模型）。

`order` 给整数时不走自适应（固定阶数，便于 A/B）。

## Passivity（§4）

- 判据用**模型**的 S 算奇异值（σ_max ≤ 1+tol）。注意：拿原始数据算会把非无源拟合判成无源
  （实测踩过）。
- 无损网络（|S|≡1）会卡在边界：`passive_strict=False` 但 `max_sv=1.000001`，
  按容差记 `passive=True` 并附 `note`，不隐瞒。
- `enforce_passivity=true` 时：记录 **before/after** 误差与 `success`。
- **拒绝失真**：若 enforcement 让 rms 误差恶化到 `> max(3×before, 1 dB)`，
  直接回滚到原拟合、标 `rejected=true` 并写明原因。绝不允许"为了无源把模型改废了还报 HIGH"。

## SPICE 宏模型

`write_spice()` 调 skrf 的 `write_spice_subcircuit_s`，再改写命名 + 加 provenance 头：

```
* ===== vector-fit N-port macromodel (scikit-rf VectorFitting) =====
* model: S_AUTO_FB1  order=2 poles
* fit: rms=0.0007 dB max=0.0051 dB phase_rms=0.010 deg
* band: 1e5..1e9 Hz
.SUBCKT S_AUTO_FB1 p1 p2
V1 p1 s1 0
R1 s1 0 50.0
...
```

## Round-trip 验证（§5）

拟合误差 ≠ 网表能跑对。`network_validate` 会把子电路包在两个 RF 端口之间，
用 ngspice `sp` 分析复算 S，然后逐端口对比较：

| 指标 | `lc_lpf` 实测 |
|---|---|
| rms \|S\| 误差 | **0.0007 dB** |
| max \|S\| 误差 | **0.0051 dB** |
| max 相位误差 | **0.078°** |
| 判定 | PASS（tol 0.5 dB） |

对"接近 0 的 Sij"（如匹配良好的 S11 ≈ −300 dB）不比较 dB：低于 `floor_db`（默认 −120 dB）
的条目被排除并计数（`n_entries_below_floor`），否则 −300 与 −280 会变成"20 dB 误差"。

## 明确的适用边界

| 适合 | 不适合 |
|---|---|
| 铁氧体、滤波器、连接器、封装、短互连 | **电长线**（纯时延 e^{−sτ} 不是有理函数） |
| 有损、频响平滑的网络 | 深零陷/理想全通 |
| ≤ 4 端口 | > 4 端口（不导出，只出指标） |

电长线请用 v2.3 的 `T` 线（无损）或 RLGC ladder。实测：300 ps 无损线的 VF 一定
`VECTOR_FIT_NOT_CONVERGED`——这不是 bug，是数学。
