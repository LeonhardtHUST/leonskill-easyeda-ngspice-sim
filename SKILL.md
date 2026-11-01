---
name: easyeda-ngspice-sim
description: >-
  用本机 ngspice-47 仿真嘉立创EDA（EasyEDA Pro）工程中的电路（模型保真度/实现相关性增强版），支持双入口：
  ① easyeda-bridge（运行中的 EDA，自动从原理图页提取）② 直接传入工程文件（.epro2，离线解析）。
  v2.4 起支持 scikit-rf 网络代数 + vector fitting 宽带宏模型（含 passivity 与
  网表 round-trip 验证）、差分端口/混合模、[Diff Pin]、耦合线与差分对几何识别、
  实测相关（示波器/VNA）与有界参数校准（含灵敏度与可辨识性）；
  v2.3 起可从真实 PCB 自动提取走线/过孔/叠层并生成寄生模型（含 T 线、via R/L）、
  支持 IBIS 与 Touchstone(.sNp)、多端口 PDN（Z11/Z21）、FFT/谐波/互调、Monte Carlo 与 corner、
  两次仿真 A/B 对比与 PASS/FAIL 判据；v2.2 起可直接吃厂商 .SUBCKT/.MODEL/.LIB、
  用 override 层加 PCB 走线/via/RLC/传输线寄生并做原理图网络的物理分段，
  支持参数扫描（1D/2D）、自动量化指标（过冲/振铃/skew/PDN 阻抗/PWM 谐波）、
  结构化 simulation_manifest 与 Level 0-4 fidelity 标注 + 置信度。
  当用户要求"仿真 EDA 里的电路"、"跑一下放大电路"、"扫频/波特图"、"从原理图直接仿真"、
  "离线仿真工程文件"、"仿真 buck/LDO 电源树"、"ADC PDN 去耦"、"clock 走线振铃/skew"、
  "参数扫描"、"生成仿真线路图"时使用。与 easyeda-api skill 配合（bridge 由它提供）。
  触发词: 仿真, ngspice, spice, 扫频, 幅频特性, 波特图, 瞬态分析, 工作点, 参数扫描,
  故障注入, 工程文件仿真, PDN, 去耦, 电源完整性, 过冲, 振铃, skew, 传输线,
  PCB 提参, 叠层, Z0, IBIS, Touchstone, S参数, 蒙特卡洛, corner, 蒙特卡洛容差, FFT, 谐波,
  vector fitting, 宏模型, 无源性, 混合模, 差分对, 差模/共模, 实测对比, 参数校准, 灵敏度。
version: 2.4.0
license: MIT
compatibility: Windows + ngspice-47（~/.workbuddy/binaries/ngspice/）；在线模式需 easyeda-api bridge
---

# easyeda-ngspice-sim v2.4

> **架构边界（第一原则）**：
> `EasyEDA = CAD 数据源` · `ngspice = 仿真引擎` · `本 skill = 编排层`。
> 所有 batch 仿真都在外部完成——**不需要打开 EasyEDA、不需要切到 Simulation 页面、
> 不需要任何 UI 操作**。嘉立创仿真页只是可选的可视化前端（写回能力仍在，不在主链路）。

用本地 ngspice 全自动仿真嘉立创EDA工程。**两个入口**：

| 入口 | 命令 | 需要 |
|---|---|---|
| **在线（bridge）** | `sim_from_sch.sh <工程名> [页名] <模式>` | EDA 打开工程 + bridge 运行（仅原理图页） |
| **离线（工程文件）** | `sim_file.sh <工程.epro2> <模式> [参数] [页名]` | 仅 .epro2 文件，无需 EDA |

两个入口共用同一条后端：IR → 并查集连通性 → 自算网表（模型映射 / 地策略 / IBIS / override）
→ SVG 线路图 → deck → ngspice → metrics → manifest。

> PCB 几何来自 `.epro2` 文件（`--pcb`），在线入口目前只提取原理图页——
> PCB 的 bridge 提取（实时的 PCB 文档）尚未实现，需要几何时请走离线/文件路径。

## 快速开始

```bash
SKILL=~/.workbuddy/skills/easyeda-ngspice-sim/scripts

# ── 老用法完全不变 ──
bash $SKILL/sim_file.sh ~/Downloads/xxx.epro2 op
bash $SKILL/sim_from_sch.sh "2026秋季硬件2" P1 ac "dec 50 1 10meg" ./out

# ── v2.2：复杂板卡（推荐把配置全写进 json） ──
bash $SKILL/sim_file.sh board.epro2 tran "10p 120n" "" ./out \
  --config sim.json --power-policy explicit --strict

# 只加 override（不改 config）
bash $SKILL/sim_file.sh board.epro2 tran "" "" ./out --override sim_override.json

# ── v2.3：从 PCB 自动提参 + IBIS + 工作区 ──
bash $SKILL/sim_file.sh board.epro2 tran "10p 120n" "" ./out \
  --pcb board.epro2 --stackup stackup.json --auto-parasitics --auto-net NET-CLOCK \
  --ibis ibis_map.json --config sim.json --strict --workspace sim-workspace

# 多端口 PDN / Monte Carlo
bash $SKILL/sim_file.sh board.epro2 pdn --config sim.json --ports ports.json
bash $SKILL/sim_file.sh board.epro2 montecarlo --config mc.json --cases 200 --seed 1234
bash $SKILL/sim_file.sh board.epro2 corner --config corner.json

# ── v2.4：宽带宏模型（.sNp → vector fitting → ngspice 复算验证） ──
bash $SKILL/sim_file.sh board.epro2 rf --network filter.s2p --backend vectorfit

# 差分 SI / 实测相关 / 参数校准 / EM coupon
bash $SKILL/sim_file.sh board.epro2 si --diff-pair CLK_P,CLK_N --network clk.s4p
bash $SKILL/sim_file.sh board.epro2 correlate --simulation ./run01 --measurement scope.csv
bash $SKILL/sim_file.sh board.epro2 calibrate --measurement scope.csv --fit calibration.json
bash $SKILL/sim_file.sh board.epro2 em --net RF_OUT --region region.json --backend openems

# 报告
python $SKILL/report.py sim-workspace --out report.md --plots

# 产物：ir.json / ir_nets.json / schematic.cir / sim_diagram.svg / netlist_report.json /
#       deck.cir / *.raw / *.csv / metrics.json / simulation_manifest.json
#       （扫描时另有 runs/<case>/… 与 sweep_summary.csv|json）
```

## CLI 选项（v2.2 新增，两个入口一致）

```
--config <sim.json>          复杂仿真主入口（下面这些都能写在里面）
--models <model_map.json>    外部 SPICE 模型映射
--behavioral <file|off>      行为级模型库；off = 关闭
--override <sim_override.json>
--power-policy auto|explicit auto=按网名补理想源(v2.1行为,带显著告警)
                             explicit=只认真实源与 config 声明（复杂板卡推荐）
--strict                     拓扑可疑就失败，不产出"看着正常"的波形
--allow-partial-model        pin mapping 不完整时降级为警告
```

v2.3 新增（全部 opt-in，不给就完全是 v2.2 行为）：

```
--pcb <epro2|PCB_GEOMETRY.json>    PCB 几何来源
--stackup <stackup.json>           叠层（缺项会 STACKUP_INCOMPLETE）
--auto-parasitics                  自动生成 override（T 线 + via R/L）
--auto-net <NET>                   指定要提参的网络（可多次；省略=全部）
--board-thickness <mm>             过孔长度（缺叠层时用）
--ibis <ibis_map.json>             IBIS 驱动/接收模型
--workspace <dir>                  sim-workspace/ 工作区
--ports <ports.json>               PDN 端口定义
--cases N --seed N --criteria <json>    Monte Carlo 控制
```

v2.4 新增（全部 opt-in；没装 scikit-rf 时 v2.3 行为不变）：

```
--network <file.sNp>          S 参数网络
--backend vectorfit|lumped    宏模型后端（vectorfit 需要 requirements-rf.txt）
--order auto|N --max-order N --target-rms-db X    vector fitting 控制
--enforce-passivity           无源化（恶化会自动回滚）
--diff-pair P,N               差分对（也接受 4 端口 + ports 配置）
--measurement <csv|sNp>       实测数据
--fit <calibration.json>      参数校准
--solver ngspice|xyce         求解器后端（xyce 为 EXPERIMENTAL）
```

`sim_config.json` 里可写：`analysis{mode,args}` / `sweep` / `metrics` / `max_cases` /
`ground_aliases` / `power_policy` / `strict` / `models` / `behavioral_models` /
`override` / `external_supplies` / `fidelity_labels`。

## 工作流

```
[bridge 提取]  js/extract_sch.js（3 段，规避 30s 硬超时）  ─┐
[.epro2 解析]  epro2_parse.py（ZIP→.epru→DOCHEAD→ticket）  ─┤
                                                  IR (JSON)
                                                      │
                        connectivity.py（坐标精确 key + 线段判定 + 并查集）
                                                      │
        ┌──────────────┬──────────────────────────────┼─────────────────┐
  netlist_gen.py   svg_schematic.py            router.py            epro2_writeback.py
  ground/model/    （SVG 仿真线路图）          （布局布线 plan）    （写回 SIMULATION 页）
  behavioral/
  override 五件套
        │
  sim_exec.py → build_deck.py → ngspice-47 → parse_raw.py → metrics.py → sim_manifest.py
  （单次 / sweep，runs/<case>/ 目录化产物）
```

## Simulation fidelity levels

每次运行都按块标注，**禁止**"已成功仿真整个 PCB"这种措辞：

| Level | 含义 | 触发 |
|---|---|---|
| 0 | topology only | R/C/L + generic 分立 |
| 1 | behavioral | 命中 `behavioral.json` |
| 2 | vendor SPICE | 命中 `model_map.json` 的 `.SUBCKT/.MODEL` |
| 3 | PCB parasitic | override 里有走线/via/RLC/传输线/网段切分 |
| **4** | **SI/PI enhanced** | 几何已提取 + 叠层完整 + (IBIS 或 S 参数) + (多端口 PDN 或链路指标) |
| **5** | **correlated / field-assisted** | measurement correlated 或 full-wave 提取的 S 参数，**且带 validation evidence** |

输出形如：

```
Simulation fidelity:
  clock routing: Level 3
  ADC clock load: Level 1
  FPGA clock load: Level 1
  clock source: Level 1
  C1 (C): Level 0
  overall: Level 3
  注意：未列出的网络/器件没有寄生建模；本结果不代表整块 PCB 已被仿真。
```

`fidelity_labels` 可以把位号映射成人话（`{"U1": "power stage (buck)"}`）。

## 器件模型政策（四级 → 五级）

1. **bridge + 仿真页**：EDA 导出网表里的嵌入模型（最精确）
2. **`model_map.json`**：外部 `.SUBCKT/.MODEL/.LIB`（Level 2）——匹配优先级
   `designator > MPN > DeviceName > alias > generic`，pin mapping 严格校验
3. **`behavioral.json`**：FPGA/MCU/ADC/背光驱动的行为级占位（Level 1）
4. **内置 `models/discrete.json`**：SS9013/2N3904/1N4148… + `generic.json` 泛化兜底
5. **排除**：继电器/排针/端子/未知 IC → 列入 `excluded`；`strict` 下未映射 IC
   若在有效电流路径上直接失败

**Power 策略**：`auto` 按网名补源但会告警，且**不再在 regulator 的 `power_output`
节点上补源**（v2.1 这个 bug 会让输出阻抗恒为 0）；`explicit` 只认真实源。

**地策略**：默认只把 `GND/GROUND/0` 映射到节点 0；`AGND/DGND/PGND/CHASSIS/EARTH`
保持独立，报告逐条打印 `Ground mapping`，合并必须在 `ground_aliases` 里显式声明。

## 文档索引

| 想做什么 | 看这里 |
|---|---|
| 厂商模型怎么登记 | `MODEL_MAP_SCHEMA.md` |
| 加 PCB 走线/via/ESR/传输线/网段切分 | `SIM_OVERRIDE_SCHEMA.md` |
| FPGA/MCU 行为级占位 | `BEHAVIORAL_SCHEMA.md` |
| 内部结构与失败策略 | `ARCHITECTURE_v2.2.md` |
| 改了什么 / 破坏性变更 | `CHANGELOG_v2.2.md` |
| 现成配置长什么样 | `examples/*.example.json` |
| PCB 几何怎么提 | `PCB_GEOMETRY.md` |
| 叠层 / Z0 / TD | `STACKUP_SCHEMA.md` |
| IBIS | `IBIS_SUPPORT.md` |
| Touchstone / S 参数 | `TOUCHSTONE_SUPPORT.md` |
| 多端口 PDN | `PDN_ANALYSIS.md` |
| Monte Carlo / corner | `MONTE_CARLO.md` |
| v2.3 架构与边界 | `ARCHITECTURE_v2.3.md` |
| v2.3 改了什么 | `CHANGELOG_v2.3.md` |
| 开源生态 / license 审计 | `REFERENCE_PROJECTS.md` |
| scikit-rf 适配层 | `RF_NETWORK_ENGINE.md` |
| vector fitting / 无源 / round-trip | `VECTOR_FITTING.md` |
| 差分 SI / 耦合线 / 差分对识别 | `DIFFERENTIAL_SI.md` |
| 仿真 vs 实测 | `MEASUREMENT_CORRELATION.md` |
| 参数校准 / 灵敏度 | `CALIBRATION.md` |
| 求解器后端 | `SOLVER_BACKENDS.md` |
| openEMS 桥（实验） | `OPENEMS_BRIDGE.md` |
| 每个后端可信到什么程度 | `VALIDATION_STATUS.md` |

## Metrics 速查

```json
{"metric": "tran_stats", "node": "$CLK_ADC", "t0": 6e-8}
      → max/min/peak_to_peak/mean/rms/overshoot_pct/undershoot_pct/rise_time/fall_time
      电源轨自动切到 signal_kind=rail，只报 ripple_pct / deviation_pct

{"metric": "threshold_crossings", "node": "$CLK_ADC", "threshold": 1.65}
      → total/rises/falls/edges/crossings_per_edge/ringing/max_crossings_in_edge

{"metric": "time_delta", "a": "$CLK_ADC@1.65", "b": "$CLK_FPGA@1.65"}
      → skew

{"metric": "impedance", "node": "AVDD", "amplitude": 1, "at": [1e6, 1e8]}
      → max_impedance / peak_frequency / z_at_1e+06 / z_at_1e+08

{"metric": "fft_harmonics", "node": "I(V1)", "f0": 100000, "n": 5, "t0": 3e-5, "t1": 6e-5}
      → harmonics[].{magnitude, relative_db}
```

节点名可以写 `$CLK_ADC` / `CLK_ADC` / `3V3`（自动兼容 `N_3V3`）；电流写 `I(V1)`。

## ❗ 关键限制与降级（实测结论）

| 事项 | 结论 |
|---|---|
| **创建仿真页** | 公开 API **无法创建**（120 类穷尽验证）。在线无仿真页时自动降级为"自算网表"；**离线入口通过写回工程文件可以真正创建仿真页** |
| `getSimulationNetlistFile` 普通页 | 默认不用，`PROBE=1` 才探测 |
| 仿真页重建 | `build_sim_page.sh` 为**实验特性**：会清空目标仿真页，需 `--yes` |
| 工程写出 | `epro2_writeback.py` 为实验特性：写前强制备份 `.bak.epro2`、写后重解析校验 |
| 传输线 | 只支持**无损** `T` 线；`LTRA`/lossy/差分耦合未做 |
| `.step` | 不支持也不建议；扫描由 `sweep_runner` 编排 |

## 实测坑（全部踩过，v2.2 新增的标 ★）

| 坑 | 对策 |
|---|---|
| ★ `$` 是 ngspice 行内注释符 | `V1 $SRC 0 1` 报 "Not enough parameters"；虚拟节点落 deck 时统一 `$X → __X`，报告保留 `$X` |
| ★ 子电路实例必须 `X` 开头 | `U1 VIN … BUCK` 会被当 **URC 传输线**，报 `can't find model 'vout'`；一律 `XU1` |
| ★ `0Ω` 电阻 | ngspice 只 clamp 到 1e-12 并警告；`1µΩ` 会让 tran `timestep too small` **挂死**；统一用 `1mΩ` |
| ★ `tran` 的 tstep 必须远小于最快边沿 | 300 ps 边沿配 `tran 100p 600n`：ngspice 陷入 "Reference value" 收敛风暴，**25 s 都不出结果**；改 `tran 10p 600n` 后 2.4 s 完成 |
| ★ AC raw 的 X 轴也是复数对 | `parse_raw` 把 `frequency` 也存成 `[re, im]`，metrics 直接 `float()` 会崩 |
| ★ DC 电源轨不能拿纹波当摆幅算过冲 | 会得出 120% 这种数字；先判 `signal_kind=edge/rail` |
| ★ `.lib file section` 需要标记包裹 | 文件里必须有 `.LIB TT … .ENDL TT`，否则报 `section definition TT not found` |
| ★ `.end` 插入控制块不能字符串替换 | `.ENDS` 会被误命中；用整行正则锚定 |
| ★ 元件名首字母决定器件类型 | 把电阻命名成 `L_xxx` 会被当电感；`element_name` 做校验 |
| ★ 100% 占空比 PULSE 可用 | `pw == period` 合法，不会报错（Case C duty=1.0 验证） |
| ★ AC 耦合电容的直流建立 | 时间常数 = C·(R_bias‖Z_line)，别指望短窗口就能测；45 mm/61.44 MHz 实测需要 ~50 ns 才稳 |
| +12V Power 标志 ≠ 电源 | 网标只是网络名；仿真需真实电压源或 `--power-policy auto` |
| 无 SPICE 模型器件放仿真页 | create 会**静默挂死** → 必须用系统库 (Sim) 器件 + race(6s) |
| 仿真页 API 盲区 | `getPrimitivesInRegion`/`getPrimitiveAtPoint`/`wire.getAll` 全失效 |
| 仿真页 designator 不自动分配 | 手动 setState_Value |
| 导线自动合并 | 同网多根并成一根；**同列重叠会短路** |
| `.epro2` ZIP 成员名 GBK | `info.filename.encode('cp437').decode('gbk')` |
| `.epru` 坐标 | **y 为 EDA 值的负数**；文件旋转 = (360−EDA旋转)%360 |
| 电压源极性 | AC 源 pin2=N+；**DC 源 pin1=N+** |
| 位号特殊字符 | `C2'` 破坏 ngspice → 自动转 `C2_` |
| 网表值格式 | `43kΩ` 需去 Ω；`10uF` 可直接用 |
| bridge 窗口 | ID 每隔几分钟重连即变，必须每次按工程名重扫 |
| ★ PCB 坐标单位 | `CANVAS.unit` 可能写 `mm` 但坐标是 **mil**（实测）；按 mil 读，`--unit` 可覆盖 |
| ★ `LAYER` 表恒有 60+ 条 | 两层板也会列 INNER1..30；只保留"被走线/焊盘引用"的铜层 |
| ★ 元素 id 可能是 JSON 字符串 | `["LAYER",1]` 当字符串传进来，`int()` 会崩 |
| ★ 过孔不能合并节点 | 换层后 via 的 R/L 无处安放；每层一个节点 + 串联元件 |
| ★ 铺铜缝合孔 | 两端没有走线 → 插入 R/L 会造悬空节点 → 奇异矩阵；只保留走线路径上的过孔 |
| ★ T 型接头 | 路由器不保证在分支点分段，必须自己打断长段 |
| ★ ngspice `S` 元件不可用 | `model type mismatch`；用 adapter 的集总拟合 |
| ★ `.sp` 的 `lin` 语法 | `sp lin a b n` 返回 0 行；只用 `dec`/`oct` |
| ★ PDN 端口被理想源短路 | Z11 ≡ 0；工具会拦 `PDN_PORT_SHORTED_BY_IDEAL_SOURCE` |
| ★ 串联 R 型磁珠拟合 | 直流通道被堵死（AVDD 掉到 0V）；低频阻抗护栏 + 三元件拓扑 |
| ★ 同元件两脚之间插元件 | 若按"网段切分"处理会把两端都摘空；判为"替换该器件" |
| ★ passivity 判据要看模型 | 拿原始数据算 σmax → 非无源拟合被判成无源；必须用 `get_model_response` |
| ★ passivity enforcement 会修坏模型 | 实测 rms 从 0.02 dB 恶化到 29.8 dB；恶化超限就回滚并标 `rejected` |
| ★ 纯时延不可 vector fit | 300 ps 无损线 rms 149 dB；电长线用 T 线 / RLGC |
| ★ 近零 Sij 不比 dB | S11≈−300 dB 会把误差算成 20 dB；用 `floor_db=-120` 排除 |
| ★ 阶跃信号互相关失效 | 两个几乎相同的阶跃 lag 恒为 0；用 50% 穿越差 |
| ★ 差分模式转换的物理 | 失配必须落在同一对的 p↔n 两条腿上；跨对失配 Scd 恒为 0 |
| ★ skrf 的 z0 是二维 | `complex(arr)` 崩；按端口取代表值 |

## 已验证的参考案例

- **2026秋季硬件2 / P1**（27 元件）：在线/离线逐引脚零误差；17 网络结构一致
- **正负电源板模块v2**（67 元件）：GND 15 脚、±15V/5V、`-15V` 负压补源
- **多功能焊接过滤风扇**（43 元件）：写回仿真页 + 电气等价验证 + 工作点校验
- **9013 共射放大器**：V_C=7.35V、增益 155（43.9dB）、f_L=288Hz、f_H=163kHz
- **Wolf-Lite Case A（61.44 MHz clock）**：45.08 mm × 6 ps/mm → TD=270.5 ps；
  阻尼 0/10/22/33/47/68 Ω 扫 6 个 case，峰值 3.37→2.12 V 单调下降，
  ADC/FPGA skew ≈ 18–19 ps
- **Wolf-Lite Case B（ADC PDN）**：AGND 与 GND 独立；C_HF 47n–470n × L_VIA 0.2n–2n
  12 case；|Z| 随 via 电感单调上升（0.215→0.262 Ω @100 MHz）
- **Wolf-Lite Case C（Front PWM）**：duty 10/50/100% × 滤波电感 9 case，
  主线电流纹波与 PWM 基波（100 kHz）随占空比变化
- **Wolf-Lite Acceptance**：13.8 V → buck(.SUBCKT) → 3V3 → LDO(.SUBCKT) → trace+via
  → ADC 去耦(10 µ/100 n + ESR/ESL) + 行为级动态负载，`strict` 下零错误

## 文件清单

```
SKILL.md  CHANGELOG_v2.2.md  ARCHITECTURE_v2.2.md
MODEL_MAP_SCHEMA.md  SIM_OVERRIDE_SCHEMA.md  BEHAVIORAL_SCHEMA.md
examples/          sim_config / model_map / sim_override / behavioral 四份示例
tests/             pytest（75 项）+ fixtures/wolf_lite_{clock,pdn,pwm,acceptance}
scripts/
├── [在线] eda_exec.sh / export_netlist.sh / sim_pipeline.sh / sim_from_sch.sh / bridge_extract.sh
├── [离线] sim_file.sh / _opts.sh / epro2_parse.py / epro2_writeback.py / extract_simlib.py
├── [核心] ir_common.py / connectivity.py / netlist_gen.py / svg_schematic.py / router.py
├── [v2.2 模型层] sim_config.py / ground.py / model_resolver.py / behavioral_models.py
│                override_apply.py / parasitics.py
├── [v2.3 PCB]   pcb_geometry.py / stackup.py / trace_extract.py / via_extract.py /
│                pcb_to_override.py
├── [v2.3 SI]    ibis_parser.py / ibis_model.py / ibis_to_spice.py /
│                touchstone.py / sparam_element.py / sparam_metrics.py /
│                fft_analysis.py / spectral_metrics.py / pdn_multiport.py /
│                monte_carlo.py / compare_runs.py / sim_workspace.py / report.py
├── [v2.4 RF/SI] rf_network.py / vector_fit.py / network_validate.py /
│                diff_port.py / coupled_line.py
├── [v2.4 闭环]  measurement_import.py / correlate.py / parameter_fit.py
├── [v2.4 基础设施] solver_backend.py / em_bridge.py
├── [仿真] build_deck.py / sim_exec.py / sweep_runner.py / parse_raw.py / metrics.py /
│          sim_manifest.py
├── [实验] build_sim_page.sh
├── js/  extract_sch.js / probe_netlist.js / check_simtab.js
├── models/  discrete.json / generic.json / simlib_pack.json /
│            model_map.json / behavioral.json / external/（用户自放厂商模型）
└── tests/（在 skill 根目录）
```

环境要求：ngspice-47 于 `~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe`
（`NGSPICE_BIN` 可覆盖）；在线模式需 easyeda-api bridge（端口 49620-49629）。

自测：`cd <skill> && python -m pytest tests -q`（175 项；`-m "not slow"` 跳过需要 ngspice 的 24 项）。

依赖：`pip install -r requirements-rf.txt`（numpy/scipy/scikit-rf）。**不装也能用 v2.3 的全部功能**，
只是 `backend=vectorfit` 会报 `RF_BACKEND_UNAVAILABLE`。

v2.3 回归 fixture：`wolf_lite_clock`（A/D/G）、`wolf_lite_pdn`（B）、`wolf_lite_pwm`（C）、
`wolf_lite_ferrite`（E）、`wolf_lite_pdn_multi`（F）、`wolf_lite_acceptance`、
`ibis/`、`touchstone/`。头号用例 `test_headless_external_simulation`
（EDA 停在 PCB 页也须 PASS）。
