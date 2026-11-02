# v2.6.1 修复：板框范围取不到

日期：2026-11-02 ｜ 类型：依赖互操作修复（无接口变更）

## 问题

`ARCHITECTURE_v2.6.md` 写明板级范围来自 `outlines[].{min_x,max_x,min_y,max_y}`，
`scripts/pcb_geometry.py` 也按这四个字段过滤。但依赖方 `leonskill-easyeda-project-analysis`
1.3.0 的 `outlines[]` 实际只输出 `{width_mil, height_mil, width_mm, height_mm, doc}`，
没有 `min_*` / `max_*`。

后果：过滤结果恒为空 → `extent` 退回用**走线范围**冒充板框。板上有走线时范围偏小，
板上没有走线时直接得到 `[0, 0]`。EM coupon 布点与板级尺寸判断都会跟着错。

## 修复

| 侧 | 改动 |
|---|---|
| 依赖（1.3.1） | `pcbnet.build_geometry()` 的 `outlines[]` 与 `outline` 补 `min_x / max_x / min_y / max_y`（纯增量，老字段全保留） |
| 本 skill（2.6.1） | `raw_from_analysis()` 三层取值：`outlines`（需 min/max）→ `outline`（并集）→ 走线范围；最后一层必须附 warning「依赖未提供板框范围，extent 由走线范围推算（不是板子尺寸）」 |

第 3 层保留是为了兼容仍装 1.3.0 的用户：老依赖下行为不静默变化，而是带告警。

## 验证

用 `make_fixture.py` 造 1000 × 500 mil 板框的合成工程：

| 场景 | `extent` | warnings |
|---|---|---|
| 新依赖（1.3.1） | `x=[0,1000] y=[0,500]` | 空 |
| 模拟旧依赖（剥掉 min/max） | `x=[0,1000] y=[0,0]`（单条水平走线） | 板框范围缺失告警 |

即修复前 `extent.y` 是错的，修复后正确；旧依赖下不再静默给出错误尺寸。

## 契约

字段承诺与后续变更流程见依赖方的 `references/output-contract.md`（本 skill 是消费方）。
