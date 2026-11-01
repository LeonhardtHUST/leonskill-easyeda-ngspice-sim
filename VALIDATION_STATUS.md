# VALIDATION_STATUS — 每个后端的"可信到什么程度"

需求文档 §33 禁止只写 `supported: true`。本文件定义六级状态，并逐个后端登记。

| 状态 | 含义（必须按这个口径理解） |
|---|---|
| `IMPLEMENTED` | 代码能跑，但只有 smoke 级证据 |
| `TESTED` | 有单元测试，断言的是"我们自己定义的行为" |
| `CROSS_CHECKED` | 与**独立实现**（解析式 / scikit-rf / 原生 ngspice）在容差内一致 |
| `CORRELATED` | 与**实测数据**在容差内一致（有 measurement 证据） |
| `QUALIFIED` | 上面三者都满足，且长期回归无回退 |
| `EXPERIMENTAL` | 接口存在、结果不可用于决策；随时可能变 |

纪律（借 SPIKE 的三句话，见 REFERENCE_PROJECTS.md）：
> `missing evidence != pass` · `solver available != numerically qualified` · `plot exists != validated`

## 后端登记表

| 能力 | 状态 | 验证来源 | 备注 |
|---|---|---|---|
| 原理图 IR / 连通性（v2.1 起） | `QUALIFIED` | 真实 `.epro2` 逐引脚对照 + 3 个工程回归 | 两个入口结果一致 |
| 网表生成（R/C/L/Q/D/M） | `QUALIFIED` | 与解析工作点对照（9013 共射） | — |
| 地映射策略（v2.2） | `CROSS_CHECKED` | 单元测试 + 真实板 AGND/GND | 默认不合并，报告逐条打印 |
| 外部 `.SUBCKT/.MODEL`（v2.2） | `QUALIFIED` | 真实 `.lib` + 自写 test double | pin mapping 严格校验 |
| behavioral 占位（v2.2） | `TESTED` | 单元 + 回归 fixture | 明确标 BEHAVIORAL_APPROXIMATION |
| PCB 几何提取（v2.3） | `CORRELATED` | 真实板走线长度与原始线段**逐段相等**（31.2303 mm） | 单位 mil 实测确认 |
| 叠层 / Z0 / TD（v2.3） | `CROSS_CHECKED` | 与 45.08mm×6ps/mm=270.48ps 解析值一致 | `ANALYTIC_ESTIMATE`，±10% |
| 过孔 R/L/C（v2.3） | `TESTED` | 与 Johnson 经验式文献量级对照 | `ENGINEERING_ESTIMATE` |
| Touchstone 解析（v2.3） | `CROSS_CHECKED` | 同一器件 MA/Hz 与 DB/MHz 两份文件差 ≤5e-5 dB | — |
| Touchstone 窄带 lumped 拟合（v2.3） | `TESTED` | 对 C-L-C 梯形精确还原（0.000 dB） | 跨 3 十倍频程误差 20–25% |
| **scikit-rf 网络适配（v2.4）** | `CROSS_CHECKED` | renorm 往返复原 1e-9；cascade 与 skrf 直算一致 | 依赖 BSD-3 库 |
| **Vector fitting（v2.4）** | `CROSS_CHECKED` | 黄金语料 7 项全部 rms ≤0.5 dB；阶数梯记录 | 纯时延**不适用**（见 VECTOR_FITTING.md） |
| **SPICE 宏模型 round-trip（v2.4）** | `CORRELATED` | 黄金语料 ngspice 复算：`lc_lpf` max 0.0051 dB / phase 0.078° | 这是"网表真跑出来"的证据 |
| **Passivity（v2.4）** | `CROSS_CHECKED` | 奇异值判据 + 与 skrf `is_passive` 对照 | 无损网络卡边界，容差内通过会注明 |
| **差分端口 / 混合模（v2.4）** | `TESTED` | 变换可逆（1e-9）；平衡对 Scd < -300 dB；两腿 5% 失配 → -38 dB | — |
| **耦合线解析估计（v2.4）** | `TESTED` | s/h→∞ 时 Zdiff→2·Z0se（误差 <1Ω） | `ANALYTIC_COUPLED_LINE_ESTIMATE` |
| **差分对识别（v2.4）** | `TESTED` | 合成几何：间距/失配识别正确；改名后仍能识别 | 只看几何，net 名仅加分 |
| **IBIS 基础（v2.3）** | `TESTED` | 自写 IBIS test double（非厂商数据） | Diff Pin 基础支持 |
| **IBIS clamp（v2.4）** | `IMPLEMENTED` | 仅解析 V-I 表，未建模为 PWL 源 | 见 CHANGELOG 的"未做" |
| **测量相关（v2.4）** | `TESTED` | 合成含噪波形：自相关 0 误差、注入 200ps 时延可检出 | 尚未接入真实示波器数据 |
| **参数校准（v2.4）** | `TESTED` | 合成可解析电路：参数复原（F0 ±2kHz, Q ±0.2） | 未在真实板上闭环 |
| **灵敏度 / 可辨识性（v2.4）** | `TESTED` | 乘积型参数 → 正确报 IDENTIFIABILITY_LOW | — |
| **PDN 多端口（v2.3）** | `CROSS_CHECKED` | 与手算并联阻抗一致（8.7mΩ vs 8.75mΩ） | 依赖 ngspice `sp` |
| **Monte Carlo（v2.3）** | `TESTED` | 同 seed 同结果；统计分位正确 | — |
| **求解器抽象（v2.4）** | `TESTED` | ngspice probe/能力表 | Xyce 仅 `EXPERIMENTAL` |
| **openEMS 桥（v2.4）** | `EXPERIMENTAL` | 只生成 CSXCAD 脚本，未在本机跑过求解 | 不阻塞发布 |

## 什么情况下不能说"已验证"

- 只有 `.raw` 文件、没有与解析值或实测对比 → 最多 `TESTED`
- 与"自己另一份实现"对比 → 不算 `CROSS_CHECKED`（要有独立信源）
- 拟合残差小 → 不是 `CORRELATED`（那是模型 vs 数据，不是仿真 vs 实测）
- 依赖本机没装的外部求解器 → `EXPERIMENTAL`

## 数值证据的写法

每个功能在 manifest / 报告里必须能追溯到下面的标签：

```json
{"feature": "vector_fit_roundtrip",
 "validation": ["golden_corpus", "ngspice_roundtrip"],
 "evidence": {"max_mag_error_db": 0.0051, "tol_db": 0.5, "verdict": "PASS"}}
```

标签取值：`analytic` / `synthetic_fixture` / `cross_tool` / `measurement` / `golden_corpus`。
