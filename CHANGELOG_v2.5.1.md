# CHANGELOG v2.5.1

`easyeda-ngspice-sim` 2.5.0 → 2.5.1 —— **Correctness Hotfix**。

只修会改变物理结果的错误，不新增分析类型（没有 eye diagram、没有新求解器、没有 DDR）。

逐条对照见 **`CORRECTNESS_FIXES_v2.5.1.md`**（旧行为 / 根因 / 新行为 / 回归测试 / 影响版本）。

---

## 1. 三个 P0

| # | 问题 | 结果 |
|---|---|---|
| 1 | IBIS `[POWER Clamp]` 自变量用错（`V(pin)` 而非 `Vcc−Vpin`）+ 全局取负 | 按 IBIS 4.0 规范重写；direction/幅值实测对齐（<1e-6 A）；fixture 重做 |
| 2 | RLGC 的 L/C 少 10³，且 R 与 L 并联 | 30 mm 段恢复 L=11.12 nH / C=2.854 pF、√(L·C)=178.16 ps；拓扑改 R─L 串联 |
| 3 | openEMS 生成代码用了不存在的 API，参考面没建进几何 | 改 `CSXCAD.ContinuousStructure` + `AddMetal.AddLinPoly`；参考面真实建出并标近似 |

## 2. 两个 P1

| # | 问题 | 结果 |
|---|---|---|
| 4 | cache key 未含外部 `.MODEL/.SUBCKT/.LIB` 内容 | `collect_deps()` 收 deck 的 `.include/.lib` + model_map/IBIS/Touchstone/override；命中时把 raw/csv 还原回 run 目录 |
| 5 | de-embedding 只比频率首尾 | 逐点 `allclose`，不一致 → `DEEMBED_FREQ_GRID_MISMATCH`；`--interpolate-fixtures` 才重采并记 manifest |

## 3. 自查额外发现

* `_pwl_current()` 仍在返回 `−I_table`（v2.5 的"统一取负"残留），与 #1 纠缠在一起；
* `[Voltage Range]` 的键名被拼成 `voltagetrange`，**整行被静默丢弃**；
* `ibis_model.resolve()` 的 spec 里 `power_clamp/gnd_clamp` 字段在 2.5.0 才补上，
  但参考电压关键字一直没有；
* 系数精度 6 → 9 位（近阈值处误差 20 nA → 0.02 nA）；
* 新增 `IBIS_REFERENCE_UNRESOLVED` / `EM_REFERENCE_PLANE_UNRESOLVED` /
  `DEEMBED_FREQ_GRID_MISMATCH` 三种显式失败。

## 4. 关键数字

| 项 | v2.5.0 | v2.5.1 |
|---|---|---|
| 30 mm 走线 L_total | 0.01112 nH | **11.12 nH** |
| 30 mm 走线 C_total | 0.002854 pF | **2.854 pF** |
| √(L·C) vs td | 0.178 ps vs 178.16 ps | **178.16 ps vs 178.16 ps** |
| 阶梯直流 V(OUT) | 1.00000 V（电感旁路了铜阻） | **0.99852 V** = 50/(Rdc+50) |
| clamp DC 与表的最大误差 | 0.7 µA（**但语义是错的**） | **< 5e-5 A 且方向正确** |
| 过冲 A/B（0.2 A / 50 Ω） | 10.0 → 8.11 V（方向错的巧合） | 10.0 → 8.11 V（方向已验证） |
| openEMS 铜生成 | `AddPolygon`/`SetElevation`（不存在） | `AddLinPoly(points,'z',elev,thick)` |
| EM 参考面 | 不建（端口连到假面） | `REF_GND` 实体 + `REFERENCE_PLANE_APPROXIMATION` |
| cache 命中后产物 | 只有 metrics.json | raw/csv/metrics/stdout/stderr 全部还原 |

## 5. 测试

* 210 → **220 passed, 1 skipped**（~75 s）。
* 新增 12 条，全部针对"旧实现必然失败"：
  * `test_clamp_pwl_uses_ibis_vcc_relative_voltage`
  * `test_ibis_fixture_is_standards_compliant`
  * `test_ibis_reference_precedence`
  * `test_rlgc_absolute_values_and_delay`
  * `test_rlgc_r_is_in_series_with_l`
  * `test_rlgc_is_honestly_labelled_constant_at_fref`
  * `test_csxcad_script_uses_current_api_and_real_plane`
  * `test_em_script_has_no_dead_api`
  * `test_reference_plane_unresolved_fails_loudly`
  * `test_reference_plane_explicit_net`
  * `test_cache_key_includes_included_model_files`
  * `test_cache_collects_deps_from_config_and_override`
  * `test_cache_hit_materializes_files`
  * `test_deembed_rejects_same_endpoints_different_grid`
  * （skip 的）`test_em_script_executes_against_real_csxcad`

## 6. 验证等级

`VALIDATION_STATUS.md` 由脚本重生成，分布保持
`CROSS_CHECKED 15 / TESTED 16 / EXPERIMENTAL 1`，`CORRELATED`、`QUALIFIED` 仍为 0。

* `IBIS clamp`：重新取证后仍标 **`TESTED`**（不是 `CROSS_CHECKED`）。
  理由是证据只来自我们自己的合规 fixture，没有厂商 IBIS 文件做独立对照。
* `RLGC` / `De-embedding` / `Cache` / `openEMS`：等级不变，只更新证据与 note。
  openEMS 仍是 `EXPERIMENTAL`：本机没有 openEMS，生成脚本从未被 exec 过；
  `test_em_script_executes_against_real_csxcad` 是升级的前置条件。

## 7. 破坏性变更（有意收紧）

1. `sim_file.sh em` 现在要求能确定参考面：真实工程里 GND 必须在几何中
   （`pcb_em` 改为按全板建几何来满足这一点）。
2. `DEEMBED_FREQ_MISMATCH` 改名为 `DEEMBED_FREQ_GRID_MISMATCH`，并且判据严格得多。
3. `rlgc` 生成的网表拓扑与元件数变了（每段 3 个节点、4 个元件），
   外部脚本若按行数解析需同步。
4. `IBIS clamp` 生成的 B 源表达式形式变了（自变量与符号），
   若有 golden 网表比对需要重录。

## 8. 本轮方法论收获（写进文档，避免重复）

1. **测试替身必须独立于实现**。上一轮 fixture 与转换错误互相抵消，
   造出"0.7 µA 误差"这种看起来很强的假证据。
2. **量纲要有一条不变量断言**。`√(L·C) == td` 与 `√(L/C) == Z0` 必须同时成立；
   只测后者对共同比例错误完全不敏感。
3. **容差最容易掩盖 bug**。DC 分压 `rel=2e-3` 时，R∥L 的 0.15% 差异正好躲过。
4. **没跑过的生成代码 = 未验证**。openEMS 脚本同时错三处 API 都没人发现，
   就是因为本机没有 openEMS、也从没 exec 过。
