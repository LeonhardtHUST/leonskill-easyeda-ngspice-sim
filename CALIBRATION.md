# CALIBRATION — 参数校准 / 灵敏度 / 可辨识性

```bash
sim_file.sh board.epro2 calibrate --measurement scope.csv --fit calibration.json
```

```json
{"fit": [
  {"name": "C25.ESL", "initial": "0.7n", "bounds": ["0.2n", "2n"]},
  {"name": "VIA1.L",   "initial": "0.9n", "bounds": ["0.3n", "2n"]},
  {"name": "XG1.Rout", "initial": "10",   "bounds": ["5", "50"]}
],
 "fit_slice": [0, 1200], "validation_slice": [1200, 2000],
 "max_nfev": 60}
```

## 硬性约束

- **必须有 bounds**：没给 `bounds` 直接 `PARAM_BOUNDS_MISSING`（禁止无界拟合）。
- 优化器：`scipy.optimize.least_squares`（TRF + 边界），残差按 `targets` 的峰值归一。
- 每次迭代都会重新生成网表并跑 ngspice（`n_simulator_calls` 记录次数），所以 `max_nfev`
  要克制。

## 训练 / 验证分离（§19）

```json
{"fit_slice": [0, 1200], "validation_slice": [1200, 2000]}
```

报告同时给 `fit_error` 与 `validation_error`（rms/max/n）。只给 `fit_slice` 时
`validation_error=null`，且 `sample` 标 `IN_SAMPLE`。

## 灵敏度（§20）

归一化灵敏度 `∂M/∂p · p / |M|rms`（有限差分，步长 `max(|p|·1e-3, range·1e-6)`），
排序回答"哪个寄生最值得实测"：

```
sensitivity ranking: C25.ESL(0.842), VIA1.L(0.391), XG1.Rout(0.017)
```

## 可辨识性（§21）

把各参数灵敏度向量去均值后算两两相关；`|ρ| ≥ 0.95` 视为不可区分：

```json
{"code": "PARAMETER_IDENTIFIABILITY_LOW",
 "message": "以下参数在当前频段/波形上高度相关，拟合值不可单独采信：C25.ESL↔VIA1.L(0.987)"}
```

典型场景：trace L / via L / cap ESL 在窄带里几乎产生同一种频响变化，**不能**从一次拟合里
"精确"分出 ESL。报告只说"这组参数能解释数据"，不说"ESL = 0.71 nH 是准确的"。

## 实测校验（合成数据）

可解析电路 `H(f) = 1/√((1-x²)² + (x/Q)²)`，真值 `F0=1 MHz, Q=8`，初值 `F0=1.2 MHz, Q=5`：

| 项 | 结果 |
|---|---|
| F0 复原 | 误差 < 2 kHz |
| Q 复原 | 误差 < 0.2 |
| 训练 rms | < 0.1% |
| 验证 rms | < 0.5% |

乘积型参数（`A×B` 只以乘积出现）→ 正确报 `PARAMETER_IDENTIFIABILITY_LOW`。

## 尚未做

- 全局优化（多起点、差分进化）——目前是局部最小二乘；
- 参数不确定度的协方差估计（现在只有灵敏度排序）；
- 自动选择"哪些参数值得拟合"（现在要人指定）。
