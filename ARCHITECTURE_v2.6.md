# ARCHITECTURE v2.6 —— 依赖边界

## 1. 一句话

本 skill 只做「仿真编排」：IR → 网表 → deck → ngspice → 指标 → manifest。
**`.epro2` 的一切解析都归 `leonskill-easyeda-project-analysis`。**

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│ leonskill-easyeda-          │        │ leonskill-easyeda-           │
│ project-analysis            │        │ ngspice-sim（本仓库）         │
│                             │        │                              │
│ .epro2 → ZIP → .epru        │        │ analysis_dep.py  定位/调用    │
│   ticket 归约 → IR          │        │ ir_from_analysis.py 字段适配  │
│   PCB 几何（段/焊盘/叠层）  │        │ connectivity → netlist_gen   │
│                             │        │ → ngspice → metrics          │
└──────────┬──────────────────┘        └──────────┬───────────────────┘
           │                                      │
           │  产物契约（只走 JSON，不 import）      │
           └──────────────►  schematic_<页>.json  ◄┘
                             pcb_geometry.json
                             nets.json / meta.json
```

调用方式是**子进程**，不是 Python import：两个仓库各自演进，接口只有文件。

## 2. 产物契约（依赖 → 本 skill）

`schematic_<页>.json`（本 skill 直接消费的字段）：

| 字段 | 用途 |
|---|---|
| `page.{uuid,name,docType}` | 选页（页名/uuid 精确匹配），报告与 manifest |
| `components[].{id,componentType,designator,deviceUuid,deviceTitle,manufacturerPart,value,x,y,rotation,mirror,attrs}` | 原理图器件；`componentType ∈ part/netflag/netport/sheet` |
| `components[].pins[].{number,name,x,y}` | 引脚**绝对坐标**（IR 连通性的唯一依据） |
| `wires[].{id,line:[x1,y1,x2,y2],net}` | 导线段与网名 |

适配器补齐本 skill 额外要求的：`version`、`source="analysis"`、`components[].name`、
`pinPrefix`、`lines=[]`、`_pages`；随后 `normalize()` 统一坐标/数值/值清洗。

`pcb_geometry.json`（`raw_from_analysis()` 消费的字段）：

| 字段 | 用途 |
|---|---|
| `segments[].{net,layer_id,x1,y1,x2,y2,width}` | 走线段（mil）→ tracks；非铜层剔除 |
| `pads[].{component,pin,net,x,y,layer_id}` | 焊盘绝对坐标 → 端子命名 `DES.PIN` |
| `vias[].{net,x,y,hole,diameter,type}` | 过孔 → 串联 R/L |
| `layers[].{id,type,name}` | 铜层判定与叠层映射 |
| `stackup[].{id,raw}` | `LAYER_PHYS` 原始记录 → `Stackup.from_pcb` |
| `outlines[].{min_x,max_x,min_y,max_y}` | 板框范围 → extent |

单位一律 **mil**（依赖实测口径）。本 skill 只做 ×0.0254 → mm，不做单位猜测。

字段承诺、稳定性与变更流程以依赖方的 `references/output-contract.md` 为准；本文只列本 skill
实际读取的字段。v2.6.1 修复：依赖 1.3.0 的 `outlines[]` 缺少 min/max，导致 extent 曾退回走线范围，
详见 `CHANGELOG_v2.6.1.md`。

## 3. 定位与失败策略

定位顺序（先命中先用）：

1. `$EASYEDA_PROJECT_ANALYSIS_DIR`
2. `~/.workbuddy/skills/leonskill-easyeda-project-analysis`
3. `<本仓库父目录>/leonskill-easyeda-project-analysis`
4. `~/leonskill-easyeda-project-analysis`

判定标准只有一个：`<root>/scripts/analyze.py` 存在。

| 错误码 | 触发 | 处置 |
|---|---|---|
| `DEPENDENCY_MISSING` | 四条路径都没有 `analyze.py` | 打印已尝试路径 + 安装/环境变量提示，exit 2 |
| `ANALYSIS_FAILED` | 依赖非零退出 / 超时 / 没有 `schematic_*.json` | 透传依赖输出末尾 2000 字符 |
| `PAGE_NOT_FOUND` | 指定页名/uuid 不存在 | 列出实际可用的页名 |
| `INPUT_NOT_FOUND` | `.epro2` 不存在 | exit 2 |
| `PCB_INPUT_NOT_PARSED` | 把 `.epro2` 直接塞给 `pcb_geometry.py` | 提示先用 `analysis_dep.py` 生成 JSON |

**没有 fallback 解析器**。宁可失败，也不产出「看起来正常」的网表。

## 4. 保留在本仓库的东西

| 模块 | 为什么不移走 |
|---|---|
| `connectivity.py` | 它是**仿真导向**的连通性（模型映射前置），与审查导向的网表归并口径不同 |
| `pcb_geometry.py::build_geometry` | 拓扑化（T 接头打断、过孔串联、端子命名）是仿真建模的一部分 |
| `router.py` / `svg_schematic.py` | 仿真线路图布局布线，与工程文件无关 |
| `models/*.json` | SPICE 模型映射与行为级占位，属于仿真域 |
| `build_sim_page.sh` | 在线实验特性（经 bridge 在 EDA 里放器件），不读写工程文件 |

## 5. 边界上的坑（已踩）

1. **页名不能从文件名猜**：依赖对同名页会加 `_2/_3` 后缀，必须读 `page.name` 比对。
2. **焊盘层 12 是 MULTI**：通孔盘要回退到元件所在层，否则同网络焊盘落不到同一节点。
3. **丝印/机械层也有 `LINE`**：不过滤会造出假网络（`segments` 里带 `layer_id`，靠层表判铜）。
4. **依赖的 `outline` 只有宽高**：`extent` 要用 `outlines[]` 的 min/max，不能用宽高反推原点。
5. **两套 IR 的 `name` 字段语义不同**：依赖用 `deviceTitle`，本 skill 的 netflag 命名读 `name`，适配器必须补。
