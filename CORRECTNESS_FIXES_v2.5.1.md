# CORRECTNESS_FIXES v2.5.1

v2.5.0 → v2.5.1 只修**会改变物理结果**的错误，不新增分析类型。

审核报告点出 3 个 P0 + 2 个 P1。逐条登记如下：旧行为 / 根因 / 新行为 / 回归测试 / 影响版本。

| # | 问题 | 等级 | 影响版本 | 状态 |
|---|---|---|---|---|
| 1 | IBIS `[POWER Clamp]` 参考电压与电流方向语义错 | P0 严重 | 2.5.0（含 2.3/2.4 的 clamp 从未生效） | 已修 |
| 2 | RLGC 的 L/C 少了 10³，且 R、L 被并联 | P0 严重 | 2.5.0 | 已修 |
| 3 | openEMS 生成代码 API 不存在，参考面未建入几何 | P0 严重 | 2.5.0（2.4 是几何本身错） | 已修 |
| 4 | cache key 未含外部 `.MODEL/.SUBCKT/.LIB` 内容 | P1 高 | 2.5.0 | 已修 |
| 5 | de-embedding 只比频率首尾 | P1 中高 | 2.5.0 | 已修 |

另修 1 个自查发现的 P0：**IBIS 表内符号被多做了一次取负**（`_pwl_current` 仍在返回 `−I_table`）。
它和第 1 条纠缠在一起，单独看不出来。

---

## 1. IBIS `[POWER Clamp]` 语义

### 旧行为（2.5.0）

```spice
* 自变量是 V(DQ)，且表达式 = −I_table
B_IBIS_U5_X45_PCLAMP DQ VCC I = -0.095 + (0.0461111)*uramp(V(DQ)-(-6)) + ...
```

配套的 fixture 把 `[POWER Clamp]` 写成"V 越大电流越负"（3.6 V→−1 mA、4.2 V→−12 mA），
并把它解释成"Vout = 3.6/4.2/6.0 V"。

结果：DC 扫描看起来与 IBIS 表"完全吻合"（误差 0.7 µA），
但**表和转换都用错了语义，两个错误互相抵消**。这是最危险的一类错：
测试体系自我印证，换一份真实的厂商 IBIS 立刻失效。

### 根因

IBIS 4.0 §`[Pulldown],[Pullup],[GND Clamp],[POWER Clamp]` 原文（规范 PDF 第 40–42 页）：

```
The I-V table of the [Pullup] and the [POWER Clamp] structures are 'Vcc relative'
... Vtable = Vcc - Voutput
Therefore, for a 5 V model, -5 V in the table actually means 5 V above Vcc
Currents are considered positive when their direction is into the component.
```

规范给出的示例表（同一页）：

```
[GND Clamp]     -5.0V -3900.0m  / -0.7V -80.0m / -0.5V -2.4m / -0.4V 0 / 5.0V 0
[POWER Clamp]   -5.0V  4450.0m  / -0.7V  95.0m / -0.5V  2.4m / -0.4V 0 / 0.0V 0
```

两个关键事实：

1. `[POWER Clamp]` 的自变量是 `Vcc − Vpin`，导通区在 **Vtable < 0**；
2. 两张表在导通区的**符号相反**：POWER 为正、GND 为负。
   所以"统一取负"必然错一个。

v2.5.0 的 `_emit_clamps` 用 `V(pin)` 当 POWER clamp 的自变量，又对所有表统一 `−I`，
两处各自错一次；fixture 也用了非标准形状，于是三者吻合。

### 新行为（2.5.1）

```python
# POWER: I = I_table(V(power_ref) - V(pin))，B 源 DQ→VCC，符号照抄表值
# GND  : I = I_table(V(pin) - V(gnd_ref))，B 源 DQ→0，符号照抄表值
```

实测（3.3 V 器件，ngspice DC）：

| V(DQ) | 物理事实 | 期望电流 | ngspice 实测 |
|---|---|---|---|
| 4.0 V | 高于 Vcc+0.3 → POWER clamp 导通 | 从引脚灌回 VCC 0.325 mA | `i(VSW)` = −0.325 mA（源在输出）✓ |
| −1.0 V | 低于 GND−0.3 → GND clamp 导通 | 从地灌入引脚 1.0 mA | `i(VSW)` = +1.000 mA（源在吸收）✓ |
| 5.9 V | — | −6.867 mA | −6.867 mA，误差 < 1e-6 A |

过冲 A/B（0.2 A 注入 50 Ω，10 V → 8.11 V）：方向正确，能压过冲。

### 附带修改

* **参考电压关键字**：新增 `[Voltage Range]` / `[POWER Clamp Reference]` /
  `[GND Clamp Reference]` / `[Pullup Reference]` / `[Pulldown Reference]` 的解析
  （typ/min/max 全保留）。v2.5.0 之前 `[Voltage Range]` 的键名拼成了 `voltagetrange`，
  **整行被静默丢弃**。
* **优先级**：`[POWER Clamp Reference]` → `[Pullup Reference]` → `[Voltage Range]`；
  GND 侧 `[GND Clamp Reference]` → `[Pulldown Reference]` → 0 V。
  都没有时用映射里的 `vcc` 兜底并在 warnings 里说明；`strict` 下抛
  `IBIS_REFERENCE_UNRESOLVED`。
* **表内符号不再取负**：`_pwl_current(points, vexpr, sign=1.0)` 默认返回表值本身。
* **系数精度** 6 → 9 位有效数字：近阈值处（过冲刚起来的地方）误差从 ~20 nA 降到 ~0.02 nA。
* fixture 按规范重做：POWER clamp 全非负、GND clamp 全非正、都覆盖 −Vcc..+2Vcc；
  `[Pullup]/[Pulldown]` 也改成 Vcc 相对/ground 相对的标准形状，
  于是 `Rout` 从"50 Ω 兜底"变成**真从 IV 曲线反推出的 50.0 Ω**。

### 回归测试

| 测试 | 抓什么 |
|---|---|
| `test_clamp_pwl_uses_ibis_vcc_relative_voltage` | 自变量必须是 `(V(VCC)-V(DQ))`；不能出现 `uramp(V(DQ)-` |
| `test_ibis_fixture_is_standards_compliant` | fixture 自己必须合规（POWER 非负/GND 非正/覆盖范围/单调） |
| `test_ibis_reference_precedence` | 三个关键字优先级 + strict 拒绝猜 |
| `test_clamp_pwl_breakpoints_are_segment_starts` | 折点=线段起点、系数=斜率增量、**符号与表一致** |
| `test_clamp_dc_sweep_matches_ibis_table` | 逐点幅值（<5e-5 A）**且**方向（V>Vcc 灌回、V<0 灌入） |

> 旧实现会让 `test_clamp_pwl_uses_ibis_vcc_relative_voltage`（自变量断言）、
> `test_ibis_fixture_is_standards_compliant`（fixture 形状）、
> `test_clamp_dc_sweep_matches_ibis_table`（方向断言）同时失败。

**等级**：`IBIS clamp` 标 `TESTED`（不是 `CROSS_CHECKED`）——证据来自我们自己的合规 fixture，
没有厂商 IBIS 文件做独立对照。

---

## 2. RLGC 量纲与拓扑

### 2.1 L/C 少 1000 倍

**旧行为**

```python
td = length_mm * td_ps_per_mm * 1e-12       # s
L = z0 * td / length_mm                     # H/mm，但变量名叫 l_h_per_m
C = td / z0 / length_mm                     # F/mm
...
L_total = l_h_per_m * length_mm * 1e-3      # 又乘了 mm→m，等于再缩 1000
```

30 mm / 0.2 mm 走线：`td = 178.158 ps`，正确应 `L=11.12 nH / C=2.854 pF`，
旧代码给 `0.01112 nH / 0.002854 pF`，`√(L·C) = 0.178 ps` —— **正好差 1000 倍**。

**根因**：`L = Z0·td/ℓ` 里的 `ℓ` 必须是米；用 mm 得到的是"每毫米"量，
再由 emit 乘 `length_mm × 1e-3` 就是双重缩放。

**新行为**

```python
length_m = length_mm * 1e-3
L = z0 * td / length_m          # H/m
C = td / (z0 * length_m)        # F/m
"l_total_h": L * length_m, "c_total_f": C * length_m   # emit 只用整段量
```

### 2.2 R 与 L 被并联

**旧行为**

```spice
R_x cur next R
L_x cur next L        ← 同一对节点，等于 R ∥ L
```

直流时 L 短路掉 R，30 mm 铜阻（74 mΩ）对结果零影响。
原来的 DC 回归用了 `rel=2e-3` 的容差，恰好盖住了这 0.15% 的差异 —— 抓不住。

**新行为**

```spice
R_x  cur mid R
L_x  mid next L
Rg_x next 0 1/G
C_x  next 0 C
```

DC 回归改成 `rel=1e-5` 并且额外断言 `V(OUT) < 1 − 1e-4`
（R∥L 时 V(OUT) 恰好是 1.0，必然失败）。

### 2.3 能力声明

`emit()` 不使用 `Rac·√f`，G 也固定在 f_ref。现在如实标注：

```
model_fidelity = "RLGC_CONSTANT_AT_FREF"
info["r_basis"] = "Rdc（低频/直流正确；Rac·√f 未实现）"
info["g_basis"] = "G@f_ref（常数；不是 G(f)）"
info["f_ref_hz"] / r_used_ohm / g_used_s
```

网表首行也带 `RLGC_CONSTANT_AT_FREF`。`at_frequency()` 仍给解析 R(f)，但**不进网表**。

### 回归测试

| 测试 | 抓什么 |
|---|---|
| `test_rlgc_absolute_values_and_delay` | 绝对量（11.12 nH / 2.854 pF）与 `√(L·C) == td`；只测 `√(L'/C')==Z0` 发现不了共同比例错 |
| `test_rlgc_r_is_in_series_with_l` | 每段 R 的终点 == L 的起点，且两点集合不同（不能再并联） |
| `test_rlgc_ladder_dc_division` | `V(OUT) = 50/(Rdc+50)`，`rel<1e-5`，且必须 `< 1−1e-4` |
| `test_rlgc_is_honestly_labelled_constant_at_fref` | 不能继续声称 R(f)/G(f) |

> 旧实现会让前两条**与第三条同时**失败（量级差 1000 倍 → 延迟/阻抗全错；
> R∥L → V(OUT)=1.0）。

---

## 3. openEMS 生成代码

### 3.1 API 不存在

**旧行为**

```python
from CSXCAD import ContinuousStructure, CSXCAD
CSX = ContinuousStructure()
poly = CSX.AddPolygon('x') 或 CSXCAD.Polygon(CSX.AddMetal('x'))
poly.SetElevation(z_bottom, z_top)
```

这三样在当前 openEMS/CSXCAD 的 Python 绑定里都不存在（`SetElevation(bottom, top)`
更是把"单侧 elevation + 厚度"写成了区间）。生成的脚本**大概率一跑就 AttributeError**，
而因为本机没装 openEMS，从来没人验证过。

**新行为**

```python
import CSXCAD
CSX = CSXCAD.ContinuousStructure()
metal = CSX.AddMetal('CLK_1')
metal.AddLinPoly([[0,0.1],[20,0.1],[20,-0.1],[0,-0.1]], 'z', 0.0, 0.035)
```

`AddLinPoly(points, norm_dir, elevation, length)`：2D 顶点 + 法向 + 起始 z + 厚度。

### 3.2 参考平面没有建进几何

**旧行为**：端口 z 直接取"相邻下一片铜"，既不检查那层是不是地，也不检查那层有没有几何。
端口可能连到一个**根本不存在的假参考面**；`pcb_em.py` 的文案还写着"最底层铜（参考平面）"。

**新行为**

```
select_reference()：走线层相邻层 → 该层上必须存在 ground-ish 网络（或 --reference-net）
                   → 找不到就 EM_REFERENCE_PLANE_UNRESOLVED
csxcad_script()   ：ref = CSX.AddMetal('REF_GND'); ref.AddBox(ROI 全幅 @ 参考层 z)
plan              ：reference{net,layer,z...} + reference_plane = REFERENCE_PLANE_APPROXIMATION
```

* `--reference-net GND`：显式指定参考网络（名字不必是 GND_*）；
* `--reference-plane-box x0,y0,x1,y1`：显式给平面范围（替代全幅近似）；
* `pcb_em` 现在按**全板**建几何（`net=None`），否则找不到 GND 这个网络。

### 回归测试

| 测试 | 抓什么 |
|---|---|
| `test_csxcad_script_uses_current_api_and_real_plane` | `AddLinPoly` 调用形态、`REF_GND` 真实建出、不得出现 `AddPolygon/SetElevation`、脚本可 `ast.parse` |
| `test_em_script_has_no_dead_api` | 静态全文检查（不需要装 openEMS） |
| `test_reference_plane_unresolved_fails_loudly` | 没有地网且没给 `--reference-net` → 报错 |
| `test_reference_plane_explicit_net` | `--reference-net` 生效 + `is_groundish` 判定 |
| `test_em_script_executes_against_real_csxcad` | **有 openEMS 的机器**上真正 `exec` 生成脚本（本机 skip） |

状态仍是 **EXPERIMENTAL**：本机没有 openEMS，从未真正 exec/求解过。
那条 live 用例就是"能不能升级"的开关。

---

## 4. cache 依赖完整性

**旧行为**：`build_key(model_files=...)` 已经支持，但 `_run_single()` **没传**。
于是 `vendor.lib` 内容改了、deck/source/solver 都没变 → 命中旧结果。

**新行为**

```python
deps = sim_cache.collect_deps(deck_text, cfg, base_dir=workdir)
key, parts = sim_cache.build_key(..., model_files=deps)
```

覆盖：deck 里的 `.include/.inc/.lib`、`model_map.json` 里引用的 `.lib`、
`behavioral_models`、`ibis` 映射及其 `.ibs`、`override` 里的 `.sNp`、`stackup`。
`cache_meta.json` 里逐条记 `{path, sha256}`。

**另外**：命中时 `materialize()` 把 `raw/csv/metrics/stdout/stderr` 还原回 run 目录，
不再只写一个 `metrics.json`（"cache hit" 现在与一次正常 run 的产物等价）。

**回归测试**：`test_cache_key_includes_included_model_files`（改内容不改 deck → key 变）、
`test_cache_collects_deps_from_config_and_override`、`test_cache_hit_materializes_files`、
`test_sim_exec_cache_second_run_skips_ngspice`（端到端 HIT + manifest `cache.hit`）。

---

## 5. de-embedding 频率网格

**旧行为**

```python
if not (abs(f.freq[0] - dut.freq[0]) < 1 and abs(f.freq[-1] - dut.freq[-1]) < 1):
    raise ...
```

`1,2,3,4,5 GHz` 与 `1,1.2,1.8,3.7,5 GHz` 会被判为一致，skrf 拿错点继续算。

**新行为**

```python
same = len(f.freq) == len(dut.freq) and np.allclose(f.freq, dut.freq, rtol=1e-6, atol=0.5)
```

不一致 → `DEEMBED_FREQ_GRID_MISMATCH`（旧码名 `DEEMBED_FREQ_MISMATCH` 已废弃）。
显式 `--interpolate-fixtures`（或 `apply(..., interpolate=True)`）才用
`skrf.Network.resample` 重采，并在 manifest 记
`frequency_grid{fixtures_before, fixtures_after, resampled_roles, method, policy}`。

**回归测试**：`test_deembed_rejects_same_endpoints_different_grid`（首尾相同中间不同必须报错；
插值后 npoints 对齐且 manifest 有记录）、`test_deembed_rejects_port_and_freq_mismatch`。

---

## 6. 兼容性

* 无破坏性 API 变更；`tests/` 从 210 → **220 passed, 1 skipped**。
* 行为收紧（有意为之）：
  1. `IBIS_REFERENCE_UNRESOLVED`（strict）、`EM_REFERENCE_PLANE_UNRESOLVED`、
     `DEEMBED_FREQ_GRID_MISMATCH` 三种新失败；
  2. `sim_file.sh em` 现在要求能确定参考面（真实工程里 GND 必须在几何里）。
* 等级调整：`IBIS clamp` 从 `TESTED` 重新取证后仍标 `TESTED`（不是 CROSS_CHECKED）；
  `RLGC` / `De-embedding` / `Cache` / `openEMS` 只更新证据与 note，等级不变。
  `VALIDATION_STATUS.md` 由 `scripts/validation.py --write` 重生成。

## 7. 这一轮的方法论教训

1. **测试替身必须独立于实现**。上一轮的 fixture 与转换错误互相抵消，
   制造了"0.7 µA 误差"这种看起来很强的假证据。
   现在 fixture 有单独的合规性断言（符号、覆盖范围、单调性）。
2. **量纲要有一条"不变量"断言**。`√(L·C) == td` 与 `√(L/C) == Z0` 必须同时成立，
   只测后者对共同的比例错误完全不敏感。
3. **容差是最容易被用来掩盖 bug 的东西**。DC 分压用 `rel=2e-3` 时，
   R∥L 的 0.15% 误差正好躲过；收紧到 `1e-5` 并加"必须小于 1"的方向断言才抓得住。
4. **没跑过的生成代码 = 未验证**。openEMS 脚本一直没人 exec，
   所以才能同时写错三处 API 而不被发现。现在至少有静态检查 + 一条 skip 的 live 用例。
