# OPENEMS_BRIDGE — 裁剪式 EM coupon（**EXPERIMENTAL**）

```bash
bash sim_file.sh board.epro2 em --stackup stackup.json \
  --net RF_OUT --fmax 3e9 --margin 5 --em-out ./em
```

等价于直接调用（参数更全，也支持喂 `PCBB_GEOMETRY.json`）：

```bash
python scripts/pcb_em.py board.epro2 --net CLK_ADC --net GND \
  --stackup stackup.json --fmax 3e9 --board-thickness 1.6 --out ./em
```

产出：`em/em_coupon.py`（openEMS/CSXCAD 脚本）+ `em/EM_PLAN.json`（ROI、网格、端口、叠层快照、限制清单）。

## 边界（很重要）

- 本 skill **只生成 openEMS/CSXCAD 工程脚本**，不链接、不分发、不修改 openEMS（GPL-3.0，外部进程）。
- 求解由用户自己的 openEMS 完成；本机目前**没有** openEMS，所以状态恒为 `EXPERIMENTAL`，
  结果**不得**进入工程决策。
- 不做全板 full-wave，只裁一个 ROI。
- 不做网格自适应（thirds rule）、不做铜皮/铺铜（POUR）展开、不建 3D 过孔孔壁。

## v2.5 修掉的几何缺陷（这是本节存在的原因）

v2.4 用 `AddBox(x1,y1 → x2,y2)` 表示一段走线。**这是错的**：

| 情形 | v2.4 的结果 | v2.5 |
|---|---|---|
| 45° 斜线 (0,0)→(3,3)，线宽 0.2 | 3×3 = **9 mm²** 的方块（应为 0.849 mm²） | 沿法向 ±w/2 偏移的旋转矩形，面积 0.849 mm² |
| 水平/垂直线 | 有一维宽度为 0 → 零厚度片 | 正常 4 顶点多边形 |
| 线宽 | 完全丢失（盒子由端点决定） | 严格 = 中心线长度 × 线宽 |
| 多层板 | 所有铜都摆在同一个 z | 按叠层逐层 `SetElevation(z_bottom, z_top)` |
| 介质 | 压成单 slab | 每层介质一个 `AddMaterial` + `AddBox`，`kappa` 由 tanδ 换算 |
| 端口 | 一律用顶层铜的 z | 用**各自走线层**的 z，另一侧接相邻铜层（参考平面） |

实现见 `em_bridge.segment_polygon()` / `layer_stack()` / `csxcad_script()`，
自检用鞋带公式：`polygon_area(poly) == 中心线长度 × 线宽`（测试里断言到 1e-9）。

`em_coupon.py` 里的铜是这样生成的：

```python
def make_polygon(csx, name):
    """兼容 CSXCAD 的两种写法：AddPolygon(...) 或 CSXCAD.Polygon(metal)。"""
    if hasattr(csx, 'AddPolygon'):
        return csx.AddPolygon(name)
    return CSXCAD.Polygon(csx.AddMetal(name))

# 面积自检：6 mm^2（中心线 30 mm × 线宽 0.2 mm）
poly0 = make_polygon(CSX, 'CLK_ADC_1')
for _xy in [[0, 0.1], [30, 0.1], [30, -0.1], [0, -0.1]]:
    poly0.AddPoint([_xy[0], _xy[1]])
poly0.SetElevation(0, 0.035)
```

（未在本机跑过 openEMS，所以脚本用双写法兜住 CSXCAD API 差异；如果两种都不认，
请按你装的版本改 `make_polygon` 一处即可。）

## 为什么必须裁

FDTD 的成本 ∝ 体积/网格³。一块 60×60 mm 的板在 5 GHz、0.1 mm 网格下是 10⁹ 量级网格，
不可行。gerber2ems（Apache-2.0）的做法就是"先把 ROI 切小，再送求解"，我们沿用这个策略。

## ROI 与网格

`plan_region()` 的规则：

| 项 | 规则 |
|---|---|
| 边界 | 网络所有端点/过孔的外包框 |
| 余量 | `--margin`，默认 `max(3 mm, 5% 宽度)`（PML 需要空间） |
| 网格 | `min(λ_max/20, 0.2 mm)`，下限 0.02 mm |

`EM_PLAN.json`：

```json
{"version": 3, "status": "EXPERIMENTAL",
 "region": {"nets": ["RF_OUT"], "bbox_mm": {...}, "cell_mm": 0.133,
            "lambda_mm": 60.0, "cells_estimate": {"x": 480, "y": 300}},
 "ports": [{"name": "P1_RF_OUT", "net": "RF_OUT", "z0": 0.0, "z1": 1.565}],
 "stack": {"copper": [{"name": "L1", "z_bottom_mm": 0, "z_top_mm": 0.035}, ...],
           "dielectrics": [{"between": ["L1","L2"], "er": 4.3, ...}],
           "total_thickness_mm": 1.6},
 "script": ".../em_coupon.py",
 "limitations": ["只生成几何与端口，不做求解（需要用户自己的 openEMS）",
                 "铜 = 「中心线 + 线宽」沿法向偏移的旋转矩形；铺铜/铜皮(POUR)未展开",
                 "介质按叠层逐层建 slab，未做频率相关 εr / 宽带损耗拟合",
                 "端口竖向跨度为「走线铜层 → 相邻铜层（参考平面）」，不是统一取顶层",
                 "网格为均匀估计，未做 thirds-rule 边缘加密；细线（<3 格）会被网格吞掉",
                 "过孔只作为端点坐标参与 ROI，未在 EM 里建 3D 孔壁"]}
```

## 闭环路径（长期最重要的一条）

```
PCB → (ROI crop) → openEMS → .sNp → scikit-rf → vector fitting → ngspice 宏模型
```

拿到 `.sNp` 之后 v2.4 的 RF 能力都能直接用（这些是 **Python API**，不是 shell 子命令）：

```python
import rf_network as rn, vector_fit as vf, network_validate as nv
net = rn.RfNetwork.from_touchstone("coupon.s2p")
fit = vf.fit(net, order="auto", enforce_passivity=True)
fit.write_spice("coupon.sp"); nv.validate(net, fit, tol_db=0.5)
```

## 已知坑（写进代码注释）

- 端口必须放在金属边缘、方向沿电流（`'z'` vs `'x'`），放错会得到"看着合理"的错误 S 参数；
- 端口 z 跨度要覆盖走线到参考平面的**整段介质**，只跨铜厚等于没有激励；
- 网格太粗 → 谐振频率偏移；太细 → 跑不完；
- `EndCriteria` 未收敛就 FFT → 泄漏；
- 单位：`SetDeltaUnit(1e-3)` 之后所有坐标是 mm，混用会整体缩放；
- 叠层必须完整（`layers` + `dielectrics`），否则 `STACKUP_INCOMPLETE` 直接失败，
  不会默认 FR4=4.5 继续跑。
