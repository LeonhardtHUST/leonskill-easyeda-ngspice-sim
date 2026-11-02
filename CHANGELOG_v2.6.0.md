# CHANGELOG v2.6.0

`easyeda-ngspice-sim` 2.5.1 → 2.6.0 —— **职责剥离（不新增分析能力）**。

把「脱机工程工作」整体移出本 skill，改为对
[`leonskill-easyeda-project-analysis`](https://github.com/LeonhardtHUST/leonskill-easyeda-project-analysis)
的显式依赖。本版本不改变任何仿真物理、模型或指标算法。

---

## 1. 破坏性变更

| # | 变更 | 影响 |
|---|---|---|
| 1 | 删除 `scripts/epro2_parse.py` | 不再自己解 ZIP / `.epru` 记录流 / ticket 归约；离线解析改由依赖 `analyze.py` 提供 |
| 2 | 删除 `scripts/epro2_writeback.py` | **工程文件写回能力移除**（不能再往 `.epro2` 写仿真页）；依赖对源工程只读 |
| 3 | 删除 `scripts/extract_simlib.py` | 不再从参考工程抽 `simlib_pack.json`；现有 `models/simlib_pack.json` 保留给 `build_sim_page.sh` |
| 4 | `pcb_geometry.py` 不再接受 `.epro2` | 直接传 `.epro2` 报 `PCB_INPUT_NOT_PARSED`；请在 shell 层传（会自动转换）或用依赖产物 |
| 5 | 离线入口新增硬依赖 | 找不到依赖时报 `DEPENDENCY_MISSING` 并列出已尝试路径，不再静默降级 |

`sim_file.sh` 的命令行形态**没有变**：
`sim_file.sh <工程.epro2> <mode> [参数] [页名] [工作目录] [选项]`。

## 2. 新增

| 文件 | 作用 |
|---|---|
| `scripts/analysis_dep.py` | 三级定位依赖（环境变量 → `~/.workbuddy/skills/` → 同级仓库）；子进程调用 `analyze.py`；把失败翻译成 `DEPENDENCY_MISSING` / `ANALYSIS_FAILED` / `PAGE_NOT_FOUND`；`--ir` 一步产出本 skill 的 IR |
| `scripts/ir_from_analysis.py` | `schematic_<页>.json` → 本 skill IR：补 `version/source/name/pinPrefix/lines`，再走 `normalize()` + `validate()` |
| `pcb_geometry.raw_from_analysis()` | 依赖的 `pcb_geometry.json` → raw 图元（tracks/vias/pads/layers/stackup_raw/extent）；只改名，不重算坐标；剔除丝印/机械层 |

行为改进（同一命令写法更省事）：

- `--pcb board.epro2` 自动换成依赖已产出的 `pcb_geometry.json`（同一工程直接复用 `eda-out/`，不同工程另跑一次到 `pcb-out/`）。
- `--auto-parasitics` 不给 `--pcb` 时，默认用本工程的几何产物。
- `em` 模式不需要再单独传 `.epro2`，内部用 `eda-out/pcb_geometry.json`。

## 3. 依赖侧配套（project-analysis 1.3.0）

原来的 `pcb_geometry.json` 只有按网络汇总的 `traces`（总长/段数/线宽众数），
没有端点坐标，无法支撑寄生提参与 EM coupon。1.3.0 增加：

- `segments[]`：`{net, layer_id, x1, y1, x2, y2, width}`（mil）
- `pads[]`：`{component, pin, net, x, y, layer_id, w, h}`（绝对坐标，mil）
- `pad_count` / `segment_count`

焊盘只收带位号的元件（剔除删除残留），`PAD_NET` 取最大 ticket。

## 4. 测试

- 删除依赖已删模块的用例（`.epro2` 直读、写回）。
- 新增 `tests/test_v26_dependency.py`（11 项）：定位优先级与 `DEPENDENCY_MISSING`、
  IR 适配字段/坐标/选页、PCB 产物适配（丝印剔除、残留焊盘剔除、叠层字段归一、拒绝 `.epro2`）、
  以及用依赖自带 fixture 现场造工程跑通 `sim_file.sh op`。
- 真实 `.epro2` 的端到端用例改为「依赖可用才跑，否则 skip」。
- 全量：**231 passed, 1 skipped**（~38 s）。

## 5. 验证等级

`VALIDATION_STATUS.md` 重新生成：`CROSS_CHECKED 15 / TESTED 17 / EXPERIMENTAL 1`。
新增一项 `TESTED`「离线解析依赖边界」——有单元测试与合成工程端到端，
但没有真实板卡对照，因此不标 `CROSS_CHECKED`。其余后端证据强度未变。
