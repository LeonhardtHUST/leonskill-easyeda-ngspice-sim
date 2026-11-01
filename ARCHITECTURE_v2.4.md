# ARCHITECTURE v2.4

## 0. 不变的边界

```
EasyEDA = CAD 数据源     ngspice = 仿真引擎     skill = 编排与建模层
```

v2.4 **没有**改变 v2.3 的 canonical workflow；所有新能力都是"在既有流水线上加一级模型质量"。
EasyEDA 仿真页依旧是可选可视化前端。

## 1. 新增的四条支路

```
                            ┌─ rf_network ── vector_fit ── network_validate ─┐
.sNp / IBIS / 测量数据 ──────┤                                               ├─→ deck.cir
                            ├─ diff_port ── coupled_line                    │
                            └─ measurement_import ── correlate ── parameter_fit
```

| 支路 | 输入 | 输出 | 落点 |
|---|---|---|---|
| RF 宏模型 | `.sNp` | ngspice 子电路 + `FIT_VALIDATION.json` | `override.sparams[*].backend="vectorfit"` |
| 差分 SI | 4 端口 S / PCB 几何 | 混合模指标、`DIFF_PAIR_GEOMETRY.json` | 指标 / override |
| 实测闭环 | 示波器 CSV / VNA | `CORRELATION_REPORT.json` / 校准结果 | 报告 / 参数回写 |
| EM coupon | PCB 几何 + 叠层 | `em_coupon.py`（openEMS 脚本） | 用户自己的 openEMS |

## 2. 模块图（v2.4 全量）

```
入口  sim_file.sh / sim_from_sch.sh（_opts.sh 解析 v2.2–v2.4 全部选项）
  │
  ├─ 设计数据  epro2_parse / connectivity / pcb_geometry / stackup
  │                                   ↓ IR + PCB_GEOMETRY
  ├─ 建模       model_resolver(vendor) / ibis_* / behavioral / trace_extract / via_extract
  │             rf_network + vector_fit + sparam_element（S 参数三后端）
  │             coupled_line（差分对识别与耦合线估计）
  │                                   ↓ deck.cir
  ├─ 求解       solver_backend（NgspiceBackend / XyceBackend-EXPERIMENTAL）
  │                                   ↓ raw
  └─ 后处理     metrics / fft_analysis / pdn_multiport / sweep / monte_carlo
                correlate / parameter_fit / compare_runs / report
                                   ↓
                simulation_manifest.json（+ FIT_VALIDATION / CORRELATION_REPORT）
```

## 3. 依赖分层（§37）

| 文件 | 内容 | 缺失时 |
|---|---|---|
| `requirements-core.txt` | numpy, scipy | v2.3 的基础功能仍可用（纯 stdlib 路径） |
| `requirements-rf.txt` | scikit-rf | `vectorfit` 报 `RF_BACKEND_UNAVAILABLE`；`lumped` 照常 |
| `requirements-em.txt` | openEMS/CSXCAD（外部） | EM 桥只生成脚本，不需要安装 |

代码里统一用 `rf_network.available()` 判断，不做 `import skrf` 的硬依赖。

## 4. Fidelity 分级（§42）

| Level | 含义 | 触发 |
|---|---|---|
| 0 | topology | R/C/L + generic |
| 1 | behavioral | behavioral.json |
| 2 | vendor / IBIS | `.SUBCKT` 或 IBIS 派生 |
| 3 | PCB parasitic | override 的走线/via/RLC/T 线 |
| 4 | SI/PI enhanced | 几何 + 叠层 + (IBIS 或 S 参数) + (PDN 或链路指标) |
| **5** | **correlated / field-assisted** | **measurement correlated** 或 **full-wave 提取的 S 参数**，且带 validation evidence |

L5 的判定是**显式的**：manifest 里必须有 `correlation` 或 `em_coupon` 证据，
且 `VALIDATION_STATUS.md` 里对应条目的状态 ≥ `CORRELATED`。不允许"自己宣布 L5"。

## 5. 失败策略（延续 v2.3）

| 情况 | 错误码 |
|---|---|
| VF 到 max_order 仍不达标 | `VECTOR_FIT_NOT_CONVERGED` |
| 拟合非无源且未要求 enforce | `PASSIVITY_VIOLATION` |
| enforcement 把模型修坏 | 回滚 + `rejected: true`（不抛错，但记失败） |
| Touchstone z0 ≠ 电路 z0 且无 skrf | `RF_Z0_MISMATCH` |
| 端口数不符 | `RF_PORT_COUNT` / `DIFF_PORT_COUNT` |
| 校准参数没给 bounds | `PARAM_BOUNDS_MISSING` |
| 多参数不可区分 | `PARAMETER_IDENTIFIABILITY_LOW`（警告级，但报告必须出现） |
| IBIS 高级特性 | `IBIS_FEATURE_UNSUPPORTED` |
| 未知求解器 | `SOLVER_UNKNOWN` |

## 6. 测试分层

```
tests/
├── test_v23_*.py / test_*.py       v2.2–v2.3 回归（166 项，其中 15 项需 ngspice）
├── test_v24_rf.py                  网络/拟合/差分/耦合/相关/校准（42 项，纯 CPU）
├── test_v24_roundtrip.py           .sNp → SPICE → ngspice 复算（9 项，需 ngspice）
└── golden/                         make_corpus.py 生成 14 个 S 参数 + 3 个测量文件
```

golden corpus 的"期望值"来自闭式公式（ABCD / Z 矩阵），不是"上次跑出来的数"。
