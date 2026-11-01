# CHANGELOG v2.4

`easyeda-ngspice-sim` 2.3.0 → 2.4.0

主题：**模型真实性 / 宽带 / 差分 SI / 实测闭环**。不改变 v2.3 canonical workflow。

---

## 0. 先做的审计（§0）

新增 `REFERENCE_PROJECTS.md`：10 个项目的 license 与"能借什么"逐个登记。
关键结论：

- **scikit-rf = BSD-3** → 直接作为 RF 主干依赖；
- openEMS = GPL-3.0、pyems = GPL-3.0、Qucs-S = GPL-2.0、Xyce = GPL-3.0 → **只调外部可执行 / 只借架构**，不复制代码；
- gerber2ems = Apache-2.0、EasyEDA 官方扩展 = Apache-2.0 → 可借鉴实现思路；
- **SPIKE 的 license 是 NOASSERTION（README 说 Apache-2.0）→ 按不可复用处理**，只借它的三句工程纪律。

## 1. P0 新增能力

| 阶段 | 模块 | 结果 |
|---|---|---|
| P0-B | `rf_network.py` | skrf 适配层：读/转换/renorm/级联/无源/互易；缺依赖时降级到 numpy |
| P0-C | `vector_fit.py` | 自适应阶数 VF + passivity + SPICE 子电路导出 |
| P0-D | `network_validate.py` | **round-trip**：.sNp → 宏模型 → ngspice `sp` → 比对 → `FIT_VALIDATION.json` |
| P0-E | `diff_port.py` | 差分端口 + 混合模变换（Sdd/Sdc/Scd/Scc） |
| P0-F | `ibis_parser/model` | `[Diff Pin]` + 高级段 `IBIS_FEATURE_UNSUPPORTED` |
| P0-G | `coupled_line.py` | 耦合微带/带状线解析估计 + **几何差分对识别** |
| P0-H | `measurement_import.py` / `correlate.py` | CSV/Touchstone 导入 + 时域/频域相关 |
| P0-I | `parameter_fit.py` | 有界最小二乘 + 训练/验证分离 + 灵敏度 + 可辨识性 |

## 2. 关键实测数字

| 项 | 数字 |
|---|---|
| VF 拟合 `lc_lpf` | 2 极点，rms **0.0007 dB**，max 0.0051 dB，相位 0.078° |
| **round-trip（ngspice 复算）** | 同一模型在 ngspice 里 rms **0.0007 dB** / max **0.0051 dB** → PASS |
| 黄金语料 VF | 7 项全部 rms ≤ 0.5 dB（阶数 2–8） |
| 差分混合模 | 平衡对 Scd −300 dB；两腿 5% 失配 → **−38.3 dB** |
| 耦合线极限 | s/h→∞ 时 Zdiff→2·Z0se（误差 <1Ω） |
| 测量相关 | 自相关 RMSE <1e-12；注入 200 ps 时延检出误差 <20 ps |
| 参数校准 | F0 复原 ±2 kHz、Q ±0.2；乘积型参数正确报不可辨识 |

## 3. P1 / P2

| 能力 | 状态 |
|---|---|
| 求解器抽象 `SolverBackend` | ✅ ngspice=QUALIFIED，Xyce=EXPERIMENTAL |
| openEMS 桥（CSXCAD 脚本 + ROI） | ✅ **EXPERIMENTAL**（不内嵌求解器） |
| 灵敏度 / 可辨识性 | ✅ 在 `parameter_fit` 内 |
| Caching / incremental rerun | ⚠️ 未实现（见"未做"） |
| Eye diagram | ⚠️ 未实现（见"未做"） |
| RLGC ladder / LTRA | ⚠️ 未实现（v2.3 的 T 线保留） |
| De-embedding | ⚠️ 接口在 `rf_network`（skrf 提供），未做 fixture 流程封装 |

## 4. 兼容性

- v2.3 的 124 项测试全部继续通过；现在共 **175 项**。
- 新依赖在 `requirements-rf.txt`；**没装 scikit-rf 时 v2.3 全部功能照常**，
  只是 `backend=vectorfit` 会明确报 `RF_BACKEND_UNAVAILABLE`。
- 旧 CLI/配置全部不变；新功能全部 opt-in（`--network` / `--backend` / `--diff-pair` 等）。

## 5. 本轮踩到并写进代码的坑

| 坑 | 现象 | 对策 |
|---|---|---|
| passivity 判据用错对象 | 拿原始数据算奇异值 → 非无源拟合被判成"无源" | 用**模型响应**算 σmax，并区分 strict/tol |
| passivity enforcement 修坏模型 | rms 从 0.02 dB 恶化到 29.8 dB 还报成功 | 恶化 >max(3×,1dB) 直接回滚 + `rejected` |
| 纯时延不可拟合 | 300 ps 无损线 VF rms 149 dB | 明确 `VECTOR_FIT_NOT_CONVERGED`，文档写明用 T 线/RLGC |
| 近零 Sij 的 dB 比较 | S11≈−300 dB，误差算成 20 dB | `floor_db=-120` 排除并计数 |
| 阶跃信号互相关失效 | 两个几乎相同的阶跃，lag 恒为 0 | 改用 50% 穿越差 |
| 差分模式转换的物理 | 把失配放在"跨对"上 → Scd 恒为 0 | 失配必须在**同一对的 p↔n 两条腿**之间 |
| skrf `z0` 是二维 | `complex(arr)` 崩 | 归一成每端口代表值 |
| skrf `get_model_response(i,j,f)` | 签名是 (i,j,freqs) 不是 (freqs) | 按端口对逐个取 |

## 6. 明确未做（本轮禁止或来不及）

- 自研 Maxwell / FDTD / FEM 求解器（§41 禁止）；
- 自研 Vector Fitting / RF network algebra（§41 禁止，已用 skrf）；
- 完整 IBIS 7.x / IBIS-AMI / DDR / PCIe / USB compliance；
- 全板 3D EM 默认运行；
- **eye diagram**（§15）与 **RLGC ladder / LTRA**（§13/§14）——接口位置已留，未实现；
- de-embedding 的 fixture 流程（Open/Short/OpenShort 的自动套用）；
- cache / incremental rerun（§23/§24）；
- 实验数据库（§22）。

这些在 `VALIDATION_STATUS.md` 里都有登记，不会让人误以为"已经有了"。
