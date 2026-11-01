# DIFFERENTIAL_SI — 差分端口、混合模、耦合线、差分对识别

## 1. 端口抽象

```json
{"type": "differential_port", "name": "CLK",
 "positive": "CLK_P", "negative": "CLK_N", "z_diff": 100}
```

单端 ↔ 混合模（每对端口一个 2×2 块）：

```
M = 1/√2 · [[1, -1],
            [1,  1]]        S_mm = M · S_se · M⁻¹
```

输出 `Sdd / Sdc / Scd / Scc` 四块。端口顺序约定 **`[p1, n1, p2, n2, …]`**。

**实测校验**：

| 用例 | Scd max |
|---|---|
| 完全对称的差分对 | −300 dB（数值零） |
| 两腿 5% 失配 | **−38.3 dB** |

注意：模式转换来自**两条腿**（P1→P2 与 N1→N2）的不对称。同一端 p/n 之间的差异不产生模式转换
——我在造 golden 语料时踩过这个坑（写成 `(p,n)` 配对，结果 Scd 恒为 −325 dB）。

## 2. 耦合线解析估计（§11）

```python
cl.coupled_microstrip(stackup, "Top", w_mm=0.2, s_mm=0.15, length_mm=30)
# → z_odd 47.5, z_even 71.9, z_diff 95.0, z_common 35.9, td_odd/td_even
```

模型：`Zodd = Z0se·(1 − 0.48·e^(−0.96·s/h))`（IPC-2141 经验耦合因子），
`Zeven` 由对称性 `Zodd·Zeven ≈ Z0se²` 反推。标 **`ANALYTIC_COUPLED_LINE_ESTIMATE`**。

极限检查（有测试）：`s/h → ∞` 时 Zdiff → 2·Z0se（误差 <1Ω）；间距越大 Zdiff 越大。
奇模有效介电常数略高（场更多在介质里）→ `td_odd > td_even`，仅一阶修正，**模间时延误差可达 ±20%**。

## 3. 差分对自动识别（§12）

```python
rep = cl.identify_diff_pairs(geo, max_gap_mm=1.0, min_gap_mm=0.05, angle_tol_deg=8)
# → DIFF_PAIR_GEOMETRY.json
```

判定**只看几何**：

1. 同层；
2. 走向夹角 < 8°；
3. 中线间距 ∈ [0.05, 1.0] mm；
4. 沿走线方向投影重叠 > 30%。

net 名（`_P/_N`、`+/-`、编号）**只加分不判定**——有测试专门验证：把名字改成 `AAAA/BBBB`
后仍能识别出同一对。

输出还带：长度失配、过孔数是否对称、间距、重叠率、置信度（0.55 + 0.25×重叠 + 0.1×平行
+ 0.1×命名线索）与 `evidence` 列表。

## 4. IBIS [Diff Pin]（§9）

```python
r = ibis_model.resolve_diff(doc, "FIXTURE_FPGA", "60")
# → {pair: {positive:60, negative:61, vdiff:0.3}, positive: spec, negative: spec}
```

- 支持 `[Diff Pin] inv_pin vdiff tdelay_typ/min/max`；
- 两侧各自解析成单端模型（C_comp/封装/驱动能力）；
- 互补脚写错 → `IBIS_DIFF_PIN_MISMATCH`；找不到 → `IBIS_DIFF_PIN_NOT_FOUND`；
- 文件里出现 `[Series Pin Mapping]` / `[Aggressor]` / `[Submodel]` 等高级段 →
  **`IBIS_FEATURE_UNSUPPORTED`**（明确拒绝，不猜）。

## 5. 尚未做（写出来免得被误以为有）

- IBIS 差分模型的**耦合建模**（我们只是两个单端模型并排）；
- 差分对的**共模回流路径**（需要平面/参考地几何）；
- 多对之间的**串扰**（4 端口只覆盖单对）。
