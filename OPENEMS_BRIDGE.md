# OPENEMS_BRIDGE — 裁剪式 EM coupon（**EXPERIMENTAL**）

```bash
sim_file.sh board.epro2 em --net RF_OUT --region region.json --backend openems
```

## 边界（很重要）

- 本 skill **只生成 openEMS/CSXCAD 工程脚本**，不链接、不分发、不修改 openEMS（GPL-3.0）。
- 求解由用户自己的 openEMS 完成；本机目前**没有** openEMS，因此状态是 `EXPERIMENTAL`。
- 不做全板 full-wave：只裁一个 ROI（region of interest）。
- 不做网格自适应（thirds rule）、不做铜皮多边形展开（只按走线段落生成盒子）。

## 为什么必须裁

FDTD 的成本 ∝ 体积/网格³。一块 60×60 mm 的板在 5 GHz、0.1 mm 网格下是 10⁹ 量级网格，
不可行。gerber2ems（Apache-2.0）的做法就是"先把 ROI 切小，再送求解"，我们沿用这个策略。

## ROI 与网格

`plan_region()` 的规则：

| 项 | 规则 |
|---|---|
| 边界 | 网络所有端点/过孔的外包框 |
| 余量 | `max(3 mm, 5% 宽度)`（PML 需要空间） |
| 网格 | `min(λ_max/20, 0.2 mm)`，下限 0.02 mm |

输出 `EM_PLAN.json`：

```json
{"status": "EXPERIMENTAL",
 "region": {"nets": ["RF_OUT"], "bbox_mm": {...}, "cell_mm": 0.133,
            "lambda_mm": 60.0, "cells_estimate": {"x": 480, "y": 300}},
 "script": ".../em_coupon.py",
 "limitations": ["只生成几何与端口，不做求解", "..."]}
```

## 生成的脚本长什么样

```python
FDTD = openEMS(NrTS=20000, EndCriteria=1e-4)
FDTD.SetGaussExcite(2.5e9, 2.5e9)
FDTD.SetBoundaryCond(['PML_8'] * 6)
sub = CSX.AddMaterial('substrate', epsilon=3.3); sub.AddBox(...)
csx_RF_OUT_1 = CSX.AddMetal('RF_OUT_1'); csx_RF_OUT_1.AddBox(...)
P1 = FDTD.AddLumpedPort(1, 50, [...], [...], 'z', 1.0, excite=1.0)
mesh.AddLine('x', np.arange(...))
# FDTD.Run(...)   ← 留给你自己打开
```

## 闭环路径（长期最重要的一条）

```
PCB → (ROI crop) → openEMS → .sNp → scikit-rf → vector fitting → ngspice 宏模型
```

拿到 `.sNp` 之后所有 v2.4 能力都能直接用：

```bash
sim_file.sh board.epro2 rf --network coupon.s2p --backend vectorfit
```

## 已知坑（写进代码注释）

- 端口必须放在金属边缘、方向沿电流（`'z'` vs `'x'`），放错会得到"看着合理"的错误 S 参数；
- 网格太粗 → 谐振频率偏移；太细 → 跑不完；
- `EndCriteria` 未收敛就 FFT → 泄漏；
- 单位：`SetDeltaUnit(1e-3)` 之后所有坐标是 mm，混用会整体缩放。
