# ARCHITECTURE v2.3

## 0. 第一原则：谁负责什么

```
EasyEDA  = 设计数据源（CAD source of truth）
ngspice  = 仿真引擎（external, batch, headless）
Skill    = 编排层（extraction → model → deck → post-process → report）
```

**所有 batch 仿真都必须在"没打开 EasyEDA、没切到 Simulation 页面、没有任何 UI 操作"的
情况下完整跑完。** 这条不是风格偏好，是因为 `easyeda-bridge` 无法自动切换到工程的
仿真页——一旦把自动化挂在嘉立创内部的 simulation document 上，每次仿真前都要一个人
工点击，sweep / Monte Carlo / A-B comparison 就全部失去意义。

嘉立创的仿真页在 v2.3 里**降级为可选的可视化前端**：

| 用法 | 状态 |
|---|---|
| 外部 ngspice 跑批量仿真 | **authoritative / reproducible** |
| 嘉立创仿真页看波形、手动调试 | optional visualization |

对应的硬性测试：`tests/test_v23_fixtures.py::test_headless_external_simulation`
（EDA 停在 PCB 页也照样 PASS）。回写仿真页的测试另列为可选，不在主链路里。

## 1. Canonical workflow

```
EasyEDA schematic + PCB
        │  easyeda-bridge / epro2_parse（只读设计数据）
        ▼
    extracted IR
        ├── schematic topology      (connectivity.py)
        ├── PCB geometry            (pcb_geometry.py)  → PCB_GEOMETRY.json
        ├── component metadata      (model_map / behavioral / IBIS / Touchstone)
        └── stackup                 (stackup.py, 可用户补)
        ▼
 easyeda-ngspice-sim（本 skill）
        ├── model resolution        model_resolver.py → .SUBCKT
        ├── IBIS                    ibis_parser/model/to_spice
        ├── Touchstone              touchstone/sparam_element/sparam_metrics
        ├── parasitics              trace_extract / via_extract / pcb_to_override
        └── simulation config       sim_config / sim_override（含 net segmentation）
        ▼
 generated ngspice project
        ├── deck.cir                build_deck.py
        ├── models/                 .include 去重 + 路径 quote
        ├── config/                 sim_config / override / ibis_map
        └── manifest.json           sim_manifest.py
        ▼
     ngspice-47（外部进程）
        ▼
 metrics / CSV / plots / report（metrics / fft_analysis / compare_runs / report）
```

## 2. 工作区（脱离 GUI 状态）

每次从真实工程启动分析都会生成：

```
sim-workspace/
├── source/          schematic_ir.json / pcb_geometry.json / stackup.json
├── models/          拷贝进来的 .lib/.ibs/.sNp（便于 git 化）
├── configs/         实际生效的 sim_config / override / ibis_map
├── generated/       deck.cir
├── runs/            case-0001/…（每个 case 独立目录：deck/stdout/stderr/raw/csv/metrics）
├── plots/           *.png / *.csv
└── simulation_manifest.json
```

`--workspace <dir>` 触发。用途：rerun / compare / sweep / montecarlo 都不再依赖
EDA 会话状态，整包可以进 Git。

## 3. 分层与依赖方向

```
pcb_geometry.py   ← 只解析 .epro2 / PCB IR，不认识 SPICE
     ↓ PCB_GEOMETRY.json
stackup.py        ← 只做叠层校验 + 解析式 Z0/TD
trace_extract.py  ← 几何 → 电参数（Z0/TD），不认识 ngspice 语法
via_extract.py    ← 过孔几何 → R/L/C
     ↓
pcb_to_override.py ← 生成 v2.2 override（虚拟节点 + T 线 + via R/L）
     ↓ sim_override.generated.json
override_apply.py ← 节点图 + 网段切分 + 寄生/RLC/Touchstone 注入
netlist_gen.py    ← 只出 SPICE 文本
     ↓ deck.cir
build_deck.py → sim_exec.py → ngspice → parse_raw → metrics/fft/pdn/compare
```

**EasyEDA 解析层绝不 import ngspice 相关东西**（除了 `pcb_geometry` 里对
虚拟节点命名的约定）。这样将来换仿真器、或者只做几何统计，都不用动解析层。

## 4. 虚拟节点命名

| 来源 | 写法 | deck 里的节点 |
|---|---|---|
| 手写 override | `$CLK_ADC` | `__CLK_ADC` |
| PCB 自动生成 | `__AUTO_CLK_B1` / `_E1` / `_V3` | 原样（已是安全名） |
| 焊盘端点 | `U5.89` | 该引脚所在节点 |
| 过孔换层 | `__AUTO_CLK_V3` + `__AUTO_CLK_V3_BOTTOM_LAYER` | 两个独立节点（中间插 R/L） |

关键决策：**过孔不做节点合并**。v2.2 的 override 是按节点分段建模的，一旦把两层
合并成一个节点，过孔的 R/L 就没有地方落。所以 `pcb_geometry` 为每层生成独立节点，
`pcb_to_override` 再在两者之间插 `series_rl`。

## 5. 精度分级（Level 0–4）

| Level | 含义 | 判定 |
|---|---|---|
| 0 | topology only | R/C/L + generic |
| 1 | behavioral | behavioral.json 命中 |
| 2 | vendor SPICE / IBIS | model_map 的 .SUBCKT，或 IBIS 派生模型 |
| 3 | PCB parasitic | override 有走线/via/RLC/网段切分 |
| **4** | **SI/PI enhanced** | 几何已提取 **且** 叠层完整 **且**（IBIS 或 S 参数）**且**（多端口 PDN 或链路指标） |

L4 **不是** full-wave EM。报告必须写清"哪些网络没有寄生建模"。

## 6. Confidence（置信度）规则

`compare_runs.confidence(manifest)` 用固定加减分表，不用主观判断：

| 事实 | 分值 |
|---|---|
| 有 IBIS 驱动/接收模型 | +2/个（最多 2 个） |
| 有厂商 SPICE 模型 | +1/个（最多 3） |
| PCB 几何已提取 | +2 |
| 叠层完整 | +1 |
| S 参数拟合误差 ≤ 1 dB | +1 |
| S 参数拟合误差 > 1 dB | −1 |
| behavioral 占位 | −1/个 |
| 有 POOR / STACKUP_INCOMPLETE 告警 | −1 |
| 有被排除器件 | −1 |

`≥5 → HIGH`，`≥2 → MEDIUM`，否则 `LOW`。规则列表会逐条打印在输出里。

## 7. 失败策略

严格模式（`--strict`）继续遵循"明确失败优于可信度不足的成功"：

| 情况 | 错误码 |
|---|---|
| 要 auto-Z0 但叠层缺项 | `STACKUP_INCOMPLETE`（绝不默认 FR4=4.5） |
| IBIS 引脚不存在 | `IBIS_PIN_NOT_FOUND` |
| IBIS 条目指的文件不存在 | `IBIS_FILE_MISSING` |
| Touchstone 端口数不匹配 | `SPARAM_PORT_MISMATCH` |
| S 参数集总拟合误差过大 | `SPARAM_FIT_TOO_POOR` |
| 几何端点无法解析 | `OVERRIDE_ENDPOINT_MISSING` / `PCB_NET_NOT_FOUND` |
| PDN 端口悬空 / 被理想源短路 | `PDN_PORT_SHORTED_BY_IDEAL_SOURCE` |
| Monte Carlo 参数无实际对象 | `SWEEP_PARAM_NOT_FOUND` |
| 单位无法识别 | `PCB_UNIT_UNKNOWN` / `TL_PARAM_INVALID` |
| 不兼容模型 | `MODEL_MAPPING_ERROR` |
