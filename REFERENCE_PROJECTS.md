# REFERENCE_PROJECTS — 开源生态与 license 审计（v2.4 P0-A）

审计日期：2026-11-01。**原则：未经 license 核验，禁止把 GPL/AGPL 代码粘进本仓库。**

本项目的复用边界：

| 动作 | 允许 |
|---|---|
| 调用库的公开 API（pip 安装，运行时依赖） | ✅ |
| 参考架构 / 接口设计 / 测试思路 | ✅ |
| 按公开论文或标准公式独立实现 | ✅ |
| 复制 GPL/AGPL 源码进本仓库 | ❌ |
| 复制 license 不明（NOASSERTION）的代码 | ❌ |

---

## 1. scikit-rf

| 项 | 内容 |
|---|---|
| project | scikit-rf（`scikit-rf`，本机 2.2.0） |
| license | **BSD-3-Clause**（PyPI / 仓库 LICENSE） |
| current role | RF network engine：`skrf.Network`、`skrf.VectorFitting` |
| reusable library? | **是**——v2.4 的直接依赖（`requirements-rf.txt`） |
| architecture worth referencing | `Network` 把"频率轴 + 复数 S 矩阵 + z0 向量"绑定成一个对象；`NetworkSet` 做批量；`media` 抽象负责 Z0 与传播常数 |
| algorithm worth referencing | Vector Fitting（Gustavsen 迭代重定位极点 + 极点增补）、passivity enforcement（Hamiltonian 矩阵特征值判据） |
| code may be reused? | **仅通过 API 调用**，不复制源码 |
| integration candidate | `scripts/rf_network.py`（适配层）、`scripts/vector_fit.py`（拟合 + 导出） |
| known limitations | 不生成 SPICE 网表（要自己写 state-space → 元件）；`VectorFitting` 对差分端口需要先做混合模变换；passivity enforcement 可能恶化 DC 附近拟合 |

> 结论：**v2.4 的 RF 主干完全建立在 scikit-rf 上**，我们只写"网络 ↔ SPICE ↔ 报告"的桥。

## 2. Qucs-S

| 项 | 内容 |
|---|---|
| project | Qucs-S（Qucs with SPICE） |
| license | **GPL-2.0** |
| current role | 参考"多求解器前端"的架构 |
| reusable library? | 否（GPL，且是 Qt 应用） |
| architecture worth referencing | **求解器抽象**：一个统一的 netlist/analysis 描述，后端可换 ngspice / Xyce / SpiceOpus；组件模型与求解器解耦 |
| algorithm worth referencing | 无（它本身不做数值算法） |
| code may be reused? | **否** |
| integration candidate | `scripts/solver_backend.py` 的接口形状参考它（`probe/prepare/run/parse/capabilities`） |
| known limitations | 桌面应用，不能作为库嵌入 |

> 结论：只借"后端可插拔"的**形状**，接口是本项目自定的。

## 3. openEMS

| 项 | 内容 |
|---|---|
| project | openEMS（FDTD 3D EM） |
| license | **GPL-3.0** |
| current role | v2.4 的 P2 实验性 EM 后端 |
| reusable library? | 通过 Python 绑定（`openEMS` / `CSXCAD` 模块）**调用**可以；源码不能并入 |
| architecture worth referencing | CSXCAD 的"几何属性 + 网格 + 激励/端口"分层；`AddLumpedPort` 的端口抽象；`EndCriteria` 收敛判据 |
| algorithm worth referencing | FDTD/Yee 网格、PML、lumped port 的 S 参数提取 |
| code may be reused? | **否**（GPL-3.0）。只生成 CSXCAD 脚本 → 交给用户自己的 openEMS 运行 |
| integration candidate | `scripts/em_bridge.py`（**生成脚本，不内嵌求解器**） |
| known limitations | 网格质量决定一切；端口去嵌与参考面设置错误会给出"看起来合理"的错误结果；Windows 上编译链复杂 |

> 结论：**只做"生成 openEMS 工程脚本"的桥**，不链接、不分发、不修改 openEMS。

## 4. gerber2ems

| 项 | 内容 |
|---|---|
| project | gerber2ems（Antmicro） |
| license | **Apache-2.0** ✅ |
| current role | 参考"Gerber → openEMS coupon"的完整流水线 |
| reusable library? | 是（可作为 CLI/Python 依赖，但需要 gerbv） |
| architecture worth referencing | ⭐ **"先裁一个最小区域再生网格"** 的思路：整板 full-wave 不可行，必须 ROI crop；端口用虚拟器件（`SP1`）标记；stackup.json 驱动介质 |
| algorithm worth referencing | Gerber → 多边形 → 网格加密（金属边缘 thirds rule）；端口激励与 S 参数提取 |
| code may be reused? | **可以（Apache-2.0）**，但 v2.4 只借鉴思路，不引入 gerbv 依赖 |
| integration candidate | P2 的 ROI crop 与端口标记策略 |
| known limitations | 只吃 Gerber（我们手里是 EasyEDA 的内部几何）；电容不建模 |

## 5. pyems

| 项 | 内容 |
|---|---|
| project | pyems（matthuszagh） |
| license | **GPL-3.0** |
| current role | 参考"高层 PCB 结构 + 自动网格"的 API 设计 |
| reusable library? | 否（GPL） |
| architecture worth referencing | `Microstrip/PCB/MicrostripCoupler` 这类"结构类"把几何参数 → 网格 → 端口封装起来；`Mesh` 负责自动加密 |
| algorithm worth referencing | 微带/耦合线的解析初值 + 网格自适应 |
| code may be reused? | **否** |
| integration candidate | 无（仅设计参考） |
| known limitations | 需要手工安装 openEMS；API 不稳定 |

## 6. EasyEDA `eext-paden-integration`

| 项 | 内容 |
|---|---|
| project | easyeda/eext-paden-integration |
| license | **Apache-2.0** ✅（扩展广场页明确标注） |
| current role | ⭐ **与我们最接近的官方扩展**：从 PCB 提取走线/过孔/焊盘/铺铜 → 客户端 earcut 三角剖分 → Python(FastAPI+numpy/scipy/shapely) 做**拉普拉斯 FEM 求电压分布** → WebGL 热力图 |
| reusable library? | 否（它是 EDA 插件 + 本地服务），但**架构可直接借鉴** |
| architecture worth referencing | ① 提取层与求解层用 HTTP 解耦；② 客户端预网格化降低后端压力；③ 结果面板拿"图片 + 连接点 + 层边界"；④ 每层铜厚/电导率从叠层推 |
| algorithm worth referencing | 铜皮三角剖分（earcut）+ 稀疏拉普拉斯求解（IR drop）。**注意：这是 DC 电阻网络，不是 EM** |
| code may be reused? | 可以（Apache-2.0），但 v2.4 不做 IR-drop FEM，只借"提取→预网格→外部求解"的形状 |
| integration candidate | P2 EM 桥的 ROI/端口/任务边界设计 |
| known limitations | 只做 DC 压降；不含电感/电容/高频效应 |

## 7. EasyEDA `eext-netlist-explorer`

| 项 | 内容 |
|---|---|
| project | easyeda/eext-netlist-explorer（"网表生成器"） |
| license | **Apache-2.0** ✅ |
| current role | 参考"从 EDA 侧导出网表 JSON + 拓扑图"的**数据契约** |
| reusable library? | 否（前端扩展） |
| architecture worth referencing | `getNetlistFile` 的 JSON 结构（器件/引脚/网络/连接点）；拓扑图按网络分组；GND 默认过滤 |
| algorithm worth referencing | 网络归组与"同名网络合并"的边界处理 |
| code may be reused? | 可以，但我们的 IR 已在 v2.1 定型，不迁移 |
| integration candidate | 交叉校验：把官方导出的网表与我们的 `ir_nets.json` 对比（未来可做） |
| known limitations | 只覆盖原理图；无几何 |

## 8. EasyEDA `eext-kirouting-integration`

| 项 | 内容 |
|---|---|
| project | easyeda/eext-kirouting-integration |
| license | 扩展本身按官方模板为 **Apache-2.0**（`extension.json` 默认值）；**接入的 KiRouting 是 Rust 项目，license 需单独核**（本仓库不引入） |
| current role | 参考"外部引擎接入"的边界：EDA 只负责把设计数据送出去、把结果收回来 |
| reusable library? | 否 |
| architecture worth referencing | ⭐ 任务边界：异步外部 job + 进度回调 + 结果回写；失败必须可见 |
| algorithm worth referencing | 无（布线算法不属本 skill） |
| code may be reused? | 否（依赖外部 Rust 服务） |
| integration candidate | 我们"外部 ngspice job"的异步/超时/日志策略与之同构 |
| known limitations | 需要本地服务；与仿真无关 |

## 9. SPIKE

| 项 | 内容 |
|---|---|
| project | SPIKE（wayri，KiCad PCB Simulation Workbench） |
| license | ⚠️ **不确定**：README 写 Apache-2.0，仓库元数据为 `NOASSERTION` → **按"不可复用"处理**，需人工核验后才能引用代码 |
| current role | 参考**工程纪律**（不是数值实现） |
| reusable library? | 否 |
| architecture worth referencing | ⭐ 三层状态：`solver module contracts`（probe/prepare/run/parse）、`provenance`（每个结果带输入 hash）、`qualification status`（实现了 ≠ 数值可信）；"unsupported / incomplete" 必须显式暴露 |
| algorithm worth referencing | 不参考（自称 early preview） |
| code may be reused? | **否**（license 待核） |
| integration candidate | 我们的 `validation_status`（IMPLEMENTED/TESTED/CROSS_CHECKED/CORRELATED/QUALIFIED/EXPERIMENTAL）与 `simulation_manifest` 的 provenance 字段 |
| known limitations | 早期预览，Windows 安装包未签名 |

> 借用它的三句话（已写进 `VALIDATION_STATUS.md`）：
> `missing evidence != pass`、`solver available != numerically qualified`、`plot exists != validated`。

## 10. Xyce

| 项 | 内容 |
|---|---|
| project | Xyce（Sandia） |
| license | **GPL-3.0-or-later** |
| current role | 实验性并行求解后端 |
| reusable library? | 作为**独立可执行文件**调用（不链接、不分发） |
| architecture worth referencing | 大规模线性/并行求解、`.STEP` 与参数扫描的原生支持、SPICE 兼容子集 |
| algorithm worth referencing | 不参考 |
| code may be reused? | **否**（GPL） |
| integration candidate | `solver_backend.py` 的 `XyceBackend`（**EXPERIMENTAL**，不阻塞发布） |
| known limitations | 不支持 XSPICE 的部分 device（含我们用的 `sp` 分析/端口）；netlist 方言与 ngspice 有差异 |

---

## 附：其它参考（未列入前十但影响设计）

| 项目 | license | 用途 |
|---|---|---|
| `atlc`（任意传输线计算器） | GPL-2.0 | 仅参考"2D 截面 → Z0"的思路，不引入 |
| KiCad `pcbnew` Python API | GPL-3.0 | 仅参考"叠层/焊盘/走线"的数据组织 |
| `pcb-tools` / `pygerber` | MIT | 未来若需要直接吃 Gerber，可作为依赖 |

---

## 审计结论（对 v2.4 的直接影响）

1. **RF 主干 = scikit-rf（BSD）**，我们只写适配与导出，不重写 network algebra / vector fitting。
2. **EM 主干 = 生成 openEMS 脚本（GPL-3.0，不内嵌）**，用户自己的 openEMS 执行；ROI crop 与端口标记借鉴 gerber2ems（Apache-2.0）。
3. **IR-drop / 预网格**：paden（Apache-2.0）证明"客户端预网格 + 外部 Python 求解"可行，但 v2.4 不做 DC FEM。
4. **求解器抽象**借 Qucs-S 的形状（GPL-2.0，不复制代码）。
5. **工程纪律**借 SPIKE 的三层状态（license 待核，只借概念）。
6. 任何 GPL/AGPL 组件都只作为**外部可执行程序**或**用户自备环境**，不作为 Python 依赖、不随仓库分发。
