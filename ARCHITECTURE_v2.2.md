# ARCHITECTURE v2.2

本文说明 `easyeda-ngspice-sim` 2.2.0 的内部分层、数据流、节点命名规则与失败策略。
目标读者是"要改这个 skill 的人"和"要判断结果可不可信的人"。

---

## 1. 分层

```
┌──────────────────────────────── 入口 ────────────────────────────────┐
│ sim_file.sh（.epro2 离线）      sim_from_sch.sh（bridge 在线）        │
│        └─ 共用 _opts.sh 解析 v2.2 选项 ─┘                            │
└──────────────────────────────────────────────────────────────────────┘
                    │                                   │
        epro2_parse.py / bridge_extract.sh        （IR 仍与 v2.1 相同）
                    │
              ir.json ── connectivity.py ──► ir_nets.json
                                                    │
                              ┌─────────────────────┴──────────────────┐
                              │        netlist_gen.py（编排层）         │
                              │  ground.py            地映射策略        │
                              │  model_resolver.py    外部 .SUBCKT      │
                              │  behavioral_models.py 行为级占位        │
                              │  override_apply.py    网段/寄生/替换    │
                              │  parasitics.py        RLC / T 线构造    │
                              └─────────────────────┬──────────────────┘
                                                    │ schematic.cir
                                          netlist_report.json（结构化）
                                                    │
                                          sim_exec.py（执行编排层）
                                          build_deck.py   注入 .control
                                          sweep_runner.py 1D/2D 扫描
                                          parse_raw.py    raw → JSON/CSV
                                          metrics.py      量化指标
                                          sim_manifest.py manifest + fidelity
                                                    │
                                     deck.cir / *.raw / *.csv
                                     metrics.json / sweep_summary.*
                                     simulation_manifest.json
```

**职责边界**：`netlist_gen` 只回答"电路长什么样"；`sim_exec` 只回答"怎么跑、
跑出什么数"。两者通过 `netlist_report.json` 通信——这也让 agent 可以在不跑仿真
的情况下先审一遍拓扑假设。

---

## 2. 核心不变量

### 2.1 节点命名由 `override_apply` 唯一裁决

v2.1 里 `netlist_gen.sanitize_net()` 边生成边清洗网络名，导致"同一个网络在不同
分支可能得到不同节点名"。v2.2 改成先在 `OverridePlan.build()` 里一次性算出

```
(designator, pin) → SPICE 节点名
```

之后所有元件、subckt 实例、行为级模型、override 元件都只查这张表。好处：

- 网段切分（把引脚从原网络摘下）天然对所有元件生效，不需要打补丁；
- 不会出现"某个器件还挂在旧网络名上"的幽灵连接。

规则：

| 输入 | deck 中的节点 |
|---|---|
| `GND` / `GROUND` / `0`（默认） | `0` |
| `AGND`（未配置合并） | `AGND` |
| `NET-CLOCK` | `NET-CLOCK`（`-` 实测 ngspice 接受） |
| `3V3` | `N_3V3`（数字开头补 `N_`） |
| `$CLK_ADC`（虚拟节点） | `__CLK_ADC` |
| `A.B` 式端点（override 内） | 该引脚当前的节点 |

`$` 的坑：SPICE3 语法里 `$` 是行内注释起始符，ngspice-47 实测
`V1 $SRC 0 1` 直接报 `Not enough parameters for v source`。所以用户侧写
`$CLK_ADC`（可读、可 json），落 deck 时统一变成 `__CLK_ADC`，报告与 manifest
保留用户写法。

### 2.2 元件名首字母决定器件类型

ngspice 靠首字母判断器件：`R/L/C/D/Q/M/V/I/T/X`。因此：

- 子电路实例一律 `X` + 位号（`XU20`）。直接写 `U20` 会被当成 **URC 分布 RC 线**
  （实测报 `can't find model 'xxx'`）。
- `.MODEL` 类型器件必须在 model_map 里给 `device_letter`（`Q`/`M`/`D`/`J`），
  否则报错——猜错首字母 = 猜错器件类型。
- override 的 `element_name` / `element_names` 会校验首字母，不匹配直接
  `ELEMENT_NAME_MISMATCH`，不允许"把电阻命名成 L_xxx"。

### 2.3 网段切分（virtual node）的判定规则

原理图一个网络在 PCB 上是多段。override 用 `component.pin` 作为端点，触发
"把该引脚摘下来"：

| 元件另一端的形态 | 判定 | 是否切分 |
|---|---|---|
| 另一个 `component.pin`（同一网络） | 串联插入 | ✅ 两端都摘 |
| `$虚拟节点` | 分支端点 | ✅ 摘 |
| 另一个真实网络 | 网络间串联 | ✅ 摘 |
| 地（`0` / `AGND` / ground-like） | 对地并联 | ❌ 不摘 |
| `mode: "shunt"` | 显式并联 | ❌ 不摘 |

显式 `segments` 块优先级最高（可以给节点起可读名字，供 metrics 引用）：

```json
{"net": "NET-CLOCK", "endpoints": {"C1.2": "$CLK_SRC", "DA1.7": "$CLK_ADC"}}
```

若不写 `segments`，则按上表自动生成 `__<NET>_<DES>_<PIN>` 并在报告里列出。

`short` 通过在节点表上做并查集合并实现（`AGND → 0` 即一次 union），
合并后所有引用都会看到同一个节点名。

---

## 3. 模型解析优先级与失败策略

```
designator override  >  exact MPN  >  exact DeviceName  >  alias  >  generic
                                                              └─ 都没有 ↓
                                    behavioral.json（行为级占位）
                                                              └─ 都没有 ↓
                                    排除（strict 下 → UNMAPPED_IC 失败）
```

pin mapping 的校验顺序（`model_resolver._resolve_subckt`）：

1. 读模型文件，解析 `.SUBCKT <name>` 的**真实引脚顺序**（`subckt_order` 若与文件
   不一致，以文件为准并告警）；
2. 无 `pins` 时尝试**按引脚名**一一对应（语义匹配，不是几何猜测）；
3. 映射到 subckt 不存在的引脚名 → 失败；
4. 多个原理图引脚映射到同一 subckt 引脚：允许（VOUT 多焊盘很常见），但必须落在
   同一网络，否则失败；
5. 已连接的原理图引脚缺映射 → 失败（或 `--allow-partial-model` 降级）；
6. subckt 有引脚但无原理图引脚对应 → 失败，除非显式列在 `nc_pins`；
7. 通过后按 `subckt_order` 生成 `X` 行，缺的引脚用 `__NC_` 占位（绝不靠移位补齐）。

---

## 4. 电源与地

### 4.1 谁"驱动"了一个节点

`netlist_gen` 维护 `sourced_nodes: node → [来源]`，来源有四类：

1. 原理图里的真实电压源（`schematic`）
2. `config.external_supplies` 声明（`config`）
3. subckt 的 `power_output` 角色引脚（`power_output`）
4. 按网名自动补的理想源（`auto`，仅 auto 模式）

`auto` 模式下，节点若已在 (1)(2)(3) 中，就不再补源；其中 (3) 会给出
"已由 U1 的 power_output 驱动，跳过自动补源"的具体告警——这是 v2.1 最危险的
bug（regulator 输出被理想源硬顶住，输出阻抗恒为 0）。

### 4.2 地

默认只映射 `GND/GROUND/0`。其余 ground-like 保持独立，报告逐条打印，
未合并的会提示。`_ground_linked()` 会检查 override 里是否存在把独立地连到 0 的
元件（0Ω / ferrite / short）；只有"真的没有任何连接"时才在网表头加
"PDN 结果不可信"的警告。

---

## 5. 仿真执行与扫描

### 5.1 deck 组装

`build_deck.build(raw, mode, arg, raw_dir)`：

- 剥掉原有的 `.tran/.ac/.dc` 行（改由 `.control` 驱动）；
- 在**整行 `.end`** 之前插入 `.control` 块（用正则锚定整行，避免误命中 `.ENDS`）；
- `raw_dir` 让每个 case 的 raw 写到自己的子目录。

### 5.2 扫描

```
base deck ──apply_params(名字→值)──► deck_i ──ngspice──► runs/<case>/*.raw
                                                            │
                                              parse_raw ──► *.csv
                                                            │
                                              metrics   ──► metrics.json
                                                            │
                                              write_summary ──► sweep_summary.{csv,json}
```

`apply_params` 只改"元件行最后一个 token"或 `.param NAME=`，改不到就报
`SWEEP_PARAM_NOT_FOUND`（不做模糊匹配，避免静默改错值）。

### 5.3 指标

`metrics.compute(plots, specs)` 对每个 spec 独立 try/except，单个指标失败不会
拖垮整次扫描（错误以 `{"error": code}` 记录，CSV 里显示 `ERROR:code`）。

两个实测教训写进了实现：

- **complex plot 的 X 轴也是成对存储的**（AC 的频率存成 `[f, 0]`），不拆开会
  `float(list)` 崩；
- **DC 电源轨不能用 p10/p90 当摆幅算过冲**（会得到 120% 这种数字），
  所以先判 `signal_kind`，纹波 < 电平 10% 就只报 `ripple_pct`。

---

## 6. Fidelity 分级

| Level | 含义 | 触发条件 |
|---|---|---|
| 0 | topology only | R/C/L、generic discrete |
| 1 | behavioral | 命中 `behavioral.json` |
| 2 | vendor SPICE | 命中 `model_map.json` 的 SUBCKT/MODEL |
| 3 | PCB parasitic | override 里存在走线/寄生/网段切分 |

`report["fidelity"]` 是**逐块**的表（`{"block": {"level": n}}`），
`overall_level` 取最大值，输出时附带"未列出的网络没有寄生建模，不代表整块 PCB"。

---

## 7. 为什么不用 `.step`

需求文档明确要求 agent-friendly。`.step` 的问题：

- ngspice 的 `.step` 支持不完整（需要 `.control` 里手写 `foreach`）；
- 每个 case 的产物混在一个 raw 里，agent 需要额外解析；
- 无法在 case 级别做前置校验（比如参数名不存在要立刻失败）。

改成 Python 编排后，每个 case 都是独立目录 + 独立 metrics，坏 case 不影响好 case。

---

## 8. 测试

```
tests/
├── conftest.py                 造 IR / 跑 generate 的夹具
├── test_ground.py     (7)      P0-A
├── test_models.py     (16)     P0-B / P0-C
├── test_override.py   (13)     P0-E / P0-F
├── test_metrics.py    (8)      P1-B
├── test_sweep.py      (9)      P1-A
├── test_compat.py     (13)     §21 兼容性
└── test_regression_fixtures.py (9)  Wolf-Lite 端到端（需 ngspice，标记 slow）
```

```bash
python -m pytest tests -q                  # 全部 75 项
python -m pytest tests -q -m "not slow"    # 跳过需要 ngspice 的 9 项
```
