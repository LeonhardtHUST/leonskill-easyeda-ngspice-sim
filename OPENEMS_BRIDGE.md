# OPENEMS_BRIDGE — 裁剪式 EM coupon（**EXPERIMENTAL**）

```bash
bash sim_file.sh board.epro2 em --stackup stackup.json \
  --net RF_OUT --fmax 3e9 --margin 5 --em-out ./em
```

等价于直接调用（参数更全，也支持喂 `PCB_GEOMETRY.json`）：

```bash
python scripts/pcb_em.py board.epro2 --net CLK_ADC --net GND \
  --stackup stackup.json --fmax 3e9 --board-thickness 1.6 --out ./em
# 参考面不确定时显式指定网络 / 平面范围：
python scripts/pcb_em.py board.epro2 --net CLK_ADC --reference-net GND \
  --reference-plane-box 0,-5,40,5 --stackup stackup.json --out ./em
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
| 多层板 | 所有铜都摆在同一个 z | 按叠层逐层 `AddLinPoly(..., elevation, copper_thickness)` |
| 介质 | 压成单 slab | 每层介质一个 `AddMaterial` + `AddBox`，`kappa` 由 tanδ 换算 |
| 端口 | 一律用顶层铜的 z | 用**各自走线层**的 z，另一侧接**选定并真实建出**的参考平面 |

实现见 `em_bridge.segment_polygon()` / `layer_stack()` / `csxcad_script()`，
自检用鞋带公式：`polygon_area(poly) == 中心线长度 × 线宽`（测试里断言到 1e-9）。

## 参考平面（v2.5.1）

lumped port 只是"起点→终点 + 方向"，它不会自己造一个回流导体。
所以 coupon 里必须同时有：**信号走线 + 真实参考导体 + 介质 + 端口**。

`select_reference()` 的规则：

1. 取走线所在层的相邻层（先下后上）；
2. 该层上必须存在 ground-ish 网络（`GND*` / `AGND` / `DGND` / `PGND` / `EARTH` / `VSS`），
   或用户 `--reference-net` 指定的网络；
3. 找不到 → `EM_REFERENCE_PLANE_UNRESOLVED`（**不静默假装有平面**）。

平面几何：EasyEDA 的铺铜(POUR) 暂未展开，所以默认按 ROI 全幅铺一片整块铜，
manifest 标 `REFERENCE_PLANE_APPROXIMATION`；要更贴近实际就用
`--reference-plane-box x0,y0,x1,y1`（mm）显式给范围。

生成的脚本里它长这样：

```python
ref = CSX.AddMetal('REF_GND')
ref.AddBox([-3, -3, 1.565], [23, 3, 1.6])     # 参考层的 z 区间
```

端口竖向跨度也随之改成「走线层 → 选定的参考层」，而不是 v2.5.0 的"相邻下一片铜"。

`em_coupon.py` 里的铜是这样生成的（v2.5.1 按当前 openEMS/CSXCAD 的 API）：

```python
import CSXCAD
CSX = CSXCAD.ContinuousStructure()

# 面积自检：6 mm^2（中心线 30 mm × 线宽 0.2 mm）
metal0 = CSX.AddMetal('CLK_ADC_1')
metal0.AddLinPoly([[0, 0.1], [30, 0.1], [30, -0.1], [0, -0.1]], 'z', 0.0, 0.035)
#                            顶点集合                         法向  起始z  铜厚
```

> **v2.5.0 这里写的是错的**：`CSX.AddPolygon(...)` / `CSXCAD.Polygon(...)` /
> `SetElevation(bottom, top)` 在当前绑定里都不存在（`SetElevation` 还把
> "单侧 elevation + 厚度"写成了区间）。本机没装 openEMS，所以一直没人发现。
> 现在有 `test_em_script_has_no_dead_api` 做静态检查，
> 以及一条 skip 的 `test_em_script_executes_against_real_csxcad`：
> 装好 openEMS 的机器上会真正 `exec` 生成脚本。

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
{"version": 4, "status": "EXPERIMENTAL",
 "region": {"nets": ["RF_OUT"], "bbox_mm": {...}, "cell_mm": 0.133,
            "lambda_mm": 60.0, "cells_estimate": {"x": 480, "y": 300}},
 "ports": [{"name": "P1_RF_OUT", "net": "RF_OUT", "z0": 0.0, "z1": 1.565}],
 "stack": {"copper": [{"name": "L1", "z_bottom_mm": 0, "z_top_mm": 0.035}, ...],
           "dielectrics": [{"between": ["L1","L2"], "er": 4.3, ...}],
           "total_thickness_mm": 1.6},
 "reference": {"layer": "L2", "net": "GND", "z_bottom_mm": 1.565,
               "z_top_mm": 1.6, "trace_layer": "L1", "source": "ground-ish net"},
 "reference_plane": "REFERENCE_PLANE_APPROXIMATION",
 "script": ".../em_coupon.py",
 "limitations": ["只生成几何与端口，不做求解（需要用户自己的 openEMS）",
                 "铜 = 「中心线 + 线宽」沿法向偏移的旋转矩形，用 AddLinPoly 拉伸铜厚",
                 "参考平面按 REFERENCE_PLANE_APPROXIMATION 处理：铺铜(POUR) 未展开，"
                 "默认铺一片覆盖 ROI 的整块铜；可用 --reference-plane-box 显式给范围",
                 "介质按叠层逐层建 slab，未做频率相关 εr / 宽带损耗拟合",
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
