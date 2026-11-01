---
name: easyeda-ngspice-sim
description: >-
  用本机 ngspice-47 仿真嘉立创EDA（EasyEDA Pro）工程中的电路，支持双入口：
  ① easyeda-bridge（运行中的 EDA，自动从原理图页提取）② 直接传入工程文件（.epro2，离线解析）。
  能从普通原理图页导出 NGspice 网表、自动生成仿真线路图（SVG / 写回工程文件的仿真页）、
  无仿真页时自算网表降级、跑静态工作点/瞬态/AC 扫频/DC 扫描/自定义分析。
  当用户要求"仿真 EDA 里的电路"、"跑一下放大电路"、"扫频/波特图"、"从原理图直接仿真"、
  "离线仿真工程文件"、"生成仿真线路图"、"故障注入仿真"时使用。
  与 easyeda-api skill 配合（bridge 由它提供）。触发词: 仿真, ngspice, spice, 扫频,
  幅频特性, 波特图, 瞬态分析, 工作点, 参数扫描, 故障注入, 工程文件仿真。
version: 2.1.0
license: MIT
compatibility: Windows + ngspice-47（~/.workbuddy/binaries/ngspice/）；在线模式需 easyeda-api bridge
---

# easyeda-ngspice-sim v2.0

用本地 ngspice 全自动仿真嘉立创EDA工程。**两个入口**：

| 入口 | 命令 | 需要 |
|---|---|---|
| **在线（bridge）** | `sim_from_sch.sh <工程名> [页名] <模式>` | EDA 打开工程 + bridge 运行 |
| **离线（工程文件）** | `sim_file.sh <工程.epro2> <模式> [参数] [页名]` | 仅 .epro2 文件，无需 EDA |

两个入口共用同一条后端：IR（中间表示）→ 并查集连通性 → 自算网表（内置模型库）→ SVG 仿真线路图 → ngspice 仿真。

## 快速开始

```bash
SKILL=~/.workbuddy/skills/easyeda-ngspice-sim/scripts

# ── 在线：从原理图页直接仿真（不要求预先有仿真页） ──
bash $SKILL/sim_from_sch.sh "2026秋季硬件2" P1 op
bash $SKILL/sim_from_sch.sh "2026秋季硬件2" P1 ac "dec 50 1 10meg" ./out
PROBE=1 bash $SKILL/sim_from_sch.sh "工程名" P1 op        # 附带探测 EDA 直导网表能力（有挂起风险）

# ── 离线：直接吃工程文件 ──
bash $SKILL/sim_file.sh ~/Downloads/xxx.epro2 op
WRITEBACK=1 bash $SKILL/sim_file.sh xxx.epro2 op "" P1     # 并把仿真页写回工程文件（自动备份）

# ── 仿真页已有内容时（v1 老入口，不经过原理图） ──
bash $SKILL/sim_pipeline.sh "工程名" op                     # 需仿真页已打开

# 产物：ir.json / ir_nets.json / schematic.cir / sim_diagram.svg / deck.cir / *.raw
```

## 工作流

```
[bridge 提取]  js/extract_sch.js（3 段，规避 30s 硬超时）  ─┐
[.epro2 解析]  epro2_parse.py（ZIP→.epru→DOCHEAD→ticket 归约）─┤
                                                  IR (JSON)
                                                      │
                        connectivity.py（坐标精确 key + 线段判定 + 并查集）
                                                      │
        ┌──────────────┬──────────────────────────────┼─────────────────┐
  netlist_gen.py   svg_schematic.py            router.py            epro2_writeback.py
  （网表+警告）      （SVG 仿真线路图）      （布局布线 plan）      （写回 SIMULATION 页）
        │                                         │                     │
  build_deck.py → ngspice-47 → parse_raw.py   build_sim_page.sh    （离线，备份后写回）
  （op/tran/ac/dc/custom）                    （在线重建·实验特性）
```

## 器件模型政策（三级）

1. **bridge + 仿真页**：优先用 EDA 导出网表里的嵌入模型（最精确，如 SS9013 完整卡片）
2. **自算/离线**：内置模型库 `models/discrete.json`（SS9013/2N3904/2N3906/1N4148/1N4007 + 别名表）
3. **兜底**：`models/generic.json` 类型级泛化卡（NPN/PNP/二极管/LED/MOSFET）+ **显著警告**
4. **无模型器件**（继电器/排针/端子/IC）：默认排除并列入警告清单；`KEEP_OPEN=1` 保留注释

**Power 网络自动补源**：网名可解析电压（`+12V`→12、`-15V`→-15、`5V`/`3V3`）且该网络无真实源驱动时，自动补 `VPWR_xxx` 直流源（AC 幅度 0，防扫频污染）。

## ❗ 关键限制与降级（实测结论）

| 事项 | 结论 |
|---|---|
| **创建仿真页** | 公开 API **无法创建**（120 类穷尽验证）。在线无仿真页时自动降级为"自算网表"（照样能仿真），并提示手动建页；**离线入口通过写回工程文件可以真正创建仿真页** |
| `getSimulationNetlistFile` 普通页 | 行为未验证（空白页会弹阻塞框）。默认不用，`PROBE=1` 才探测 |
| 仿真页重建 | `build_sim_page.sh` 为**实验特性**：会清空目标仿真页（含手工探针），需 `--yes` 确认 |
| 工程写出 | `epro2_writeback.py` 为实验特性：仿真页格式为逆向所得，写前强制备份 `.bak.epro2`、写后重解析校验 |

## 实测坑（全部踩过）

| 坑 | 对策 |
|---|---|
| +12V Power 标志 ≠ 电源 | 网标只是网络名；仿真需真实电压源（或依赖 netlist_gen 的自动补源） |
| 无 SPICE 模型器件放仿真页 | create 会**静默挂死** → 必须用系统库 (Sim) 器件 + race(6s) |
| 仿真页 API 盲区 | `getPrimitivesInRegion`/`getPrimitiveAtPoint`/`wire.getAll` 全失效；元件/属性/引脚 getAll 正常 |
| 仿真页 designator 不自动分配 | 手动 setState_Value |
| 导线自动合并 | 同网多根会并成一根；**同列重叠会短路**（布线器用"专用竖井列"规避） |
| `.epro2` ZIP 成员名 GBK | `info.filename.encode('cp437').decode('gbk')` 转码，否则中文名乱码 |
| `.epru` 坐标 | **y 为 EDA 值的负数**；旋转方向在文件坐标下取反（文件旋转 = (360−EDA旋转)%360） |
| 引脚位置 | 桥接：getAllPinsByPrimitiveId 实测坐标；文件：PIN.x/y 是连接端（不需 length 偏移） |
| 电压源极性 | AC 源 pin2=N+；**DC 源 pin1=N+**（不同符号！）；无把握时导出网表核对 |
| 位号特殊字符 | `C2'` 会破坏 ngspice（撇号当表达式）→ 自动转 `C2_` |
| 属性位置 | 原理图页属性在 ATTR(parentId)；仿真页 COMPONENT.attrs 内联 DeviceName；器件真值（Value）在 **DEVICE 文档** attributes |
| SVG 引脚引线方向 | 引线必须"符号体边缘→引脚"**整段覆盖**（曾把起笔位置写反，导致电容极板与走线间出现空隙） |
| SVG 网标方向 | 网标符号画在导线**反侧**（检测引脚处导线走向），文字保持正立；标签统一 34px + 与走线/符号/标签重叠打分避让 |
| 布线轨道碰撞 | 信号轨道带自适应压缩（不得侵入电源/GND 带，曾因第 11 条信号轨与 +5V 轨重合短路）；电源源用相对 COL_SPACING 的安全列、±脚分列 |
| 重复写回 | epro2_writeback 对同名生成页（NGspice1/Simulation1）**替换**而非叠加 |
| 网表值格式 | `43kΩ` 需去 Ω；`10uF`/`10nF` 可直接用；GND 自动映射 0 节点 |
| bridge 窗口 | ID 每隔几分钟重连即变，必须每次按工程名重扫；`/eda-windows/select` 有 bug 禁用 |

## 已验证的参考案例

- **2026秋季硬件2 / P1**（27 元件含故障注入排针）：在线提取 IR 与离线 `.epro2` 解析结果**逐引脚零误差**；两入口自算网表 17 个网络**结构完全一致**；`+12V` 自动补源；7 个跳线排针被排除并给出"链路可能断开"警告
- **正负电源板模块v2**（67 元件）：离线解析 → GND 15 脚、±15V/5V 电源网、`-15V` 负压正确补源
- **多功能焊接过滤风扇**（43 元件，无仿真页）：离线解析 → 网表 + SVG + **写回仿真页**（14 器件，重复写回自动替换）→ **电气等价验证通过**（可仿真器件过滤后 6/6 网络一致）→ ngspice 工作点 +5V/+12V 正确
- **9013 共射放大器**：Q 点 V_C=7.35V、增益 155（43.9dB）、f_L=288Hz、f_H=163kHz，与 PPT 理论一致
- **SVG 渲染**：57 引脚连接性程序化审计 0 断点；网标反侧自适应、标签 34px 统一避让（用户实测反馈三轮迭代验证）

## 文件清单

```
scripts/
├── [在线] eda_exec.sh / export_netlist.sh / sim_pipeline.sh / sim_from_sch.sh / bridge_extract.sh
├── [离线] sim_file.sh / epro2_parse.py / epro2_writeback.py / extract_simlib.py
├── [核心] ir_common.py / connectivity.py / netlist_gen.py / svg_schematic.py（默认原理图坐标紧凑版，--plan 为拉开版）/ router.py
├── [仿真] build_deck.py / parse_raw.py
├── [实验] build_sim_page.sh
├── js/  extract_sch.js / probe_netlist.js / check_simtab.js
└── models/  discrete.json / generic.json / simlib_pack.json（(Sim) 器件/SYMBOL 模板包）
```

环境要求：ngspice-47 于 `~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe`（`NGSPICE_BIN` 可覆盖）；在线模式需 easyeda-api bridge（端口 49620-49629）。
