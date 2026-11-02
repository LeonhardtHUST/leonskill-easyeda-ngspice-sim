# VALIDATION_STATUS

> 本文件由 `scripts/validation.py` 从 `validation_registry.json` 生成，**请勿手改表格**。
> 修改等级请改注册表并跑 `python -m pytest tests/test_validation.py`。

## 等级定义（唯一权威）

| 状态 | 含义 | 最低证据要求 |
|---|---|---|
| `IMPLEMENTED` | 代码可运行，无测试要求 | 可运行 |
| `TESTED` | 有单元测试（断言我们自己定义的行为） | ≥1 条 unit_test |
| `CROSS_CHECKED` | 与独立信源一致（解析式 / 另一工具 / 原始 CAD 数据 / 闭式真值） | ≥1 条 analytic / cross_tool / golden_corpus |
| `CORRELATED` | 与**仪器实测**（OUT_OF_SAMPLE）或全波提取一致 | ≥1 条 measurement（instrument + OUT_OF_SAMPLE）或 field_solver |
| `QUALIFIED` | TESTED + CROSS_CHECKED + CORRELATED 全满足，且长期回归无回退 | TESTED + CROSS_CHECKED + CORRELATED + regression |
| `EXPERIMENTAL` | 接口存在、结果不可用于决策 | 无（也不会被证据升格） |

纪律：`missing evidence != pass` · `solver available != numerically qualified` · `plot exists != validated`。

## 登记表（33 项）

| 功能 | 等级 | 证据类型 | 证据摘要 |
|---|---|---|---|
| 原理图 IR / 连通性 | `CROSS_CHECKED` | 交叉工具 / 原始 CAD, 单元测试 | 在线 bridge 与离线 .epro2 两个独立前端逐引脚一致（3 个工程）; tests/test_ground.py, tests/test_compat.py |
| 网表生成 R/C/L | `CROSS_CHECKED` | 交叉工具 / 原始 CAD, 单元测试 | 生成的网表在 ngspice 里跑出的工作点与手算/解析值一致; tests/test_compat.py::test_basic_rc_netlist_unchanged |
| 网表生成 BJT/二极管/MOSFET | `CROSS_CHECKED` | 交叉工具 / 原始 CAD, 单元测试 | 9013 共射放大器：V_C=7.35V、增益 155、f_L=288Hz 与理论值一致; tests/test_compat.py::test_bjt_generic_fallback |
| 地映射策略（AGND/DGND 独立） | `TESTED` | 交叉工具 / 原始 CAD, 单元测试 | tests/test_ground.py（7 项）; 真实板 AGND/GND 分离后 ngspice 节点表核对 |
| 外部 .SUBCKT/.MODEL pin mapping | `CROSS_CHECKED` | 交叉工具 / 原始 CAD, 单元测试 | 映射结果与 .SUBCKT 声明的引脚顺序逐项对照；真实 .lib 实跑通过; tests/test_models.py（16 项，含缺失/重复/数量不符） |
| behavioral 占位模型 | `TESTED` | 单元测试, 闭式黄金语料 | tests/test_compat.py::test_behavioral_placeholder_marked; 回归 fixture 的电流负载波形 |
| 离线解析依赖边界（.epro2 → project-analysis） | `TESTED` | 交叉工具 / 原始 CAD, 单元测试 | tests/test_v26_dependency.py（定位优先级、DEPENDENCY_MISSING/PAGE_NOT_FOUND、IR 适配、PCB 产物适配、拒绝 .epro2 直读）; 合成工程经依赖 analyze.py → IR → netlist → ngspice op 全链路跑通（test_sim_file_end_to_end_with_generated_project） |
| PCB 几何提取 | `CROSS_CHECKED` | 交叉工具 / 原始 CAD, 单元测试 | 与原始 PCB LINE 线段逐段比对：+5V 4 段合计 31.2303 mm = 原始 14 条之和; tests/test_v23_units.py::test_pcb_geometry_t_junction_and_length |
| 叠层 / Z0 / TD | `CROSS_CHECKED` | 单元测试, 解析式 | 45.08mm×6ps/mm=270.48ps 与工具输出一致；微带/带状线用标准解析式; tests/test_v23_units.py::test_microstrip_z0_and_td |
| 过孔 R/L/C 估计 | `TESTED` | 单元测试, 解析式 | tests/test_v23_units.py::test_via_parasitics_magnitude; 与 Johnson 经验式的文献量级对照 |
| Touchstone 解析 | `CROSS_CHECKED` | 交叉工具 / 原始 CAD, 单元测试 | 同一器件 MA/Hz 与 DB/MHz 两份文件差 ≤5e-5 dB; tests/test_v23_units.py::test_touchstone_formats_equivalent |
| Touchstone 窄带 lumped 拟合 | `CROSS_CHECKED` | 交叉工具 / 原始 CAD, 单元测试 | 对 C-L-C 梯形精确还原（220nH/100pF 分毫不差，S21 误差 0.000 dB）; tests/test_v23_units.py::test_touchstone_lumped_fit_recovers_lc |
| scikit-rf 网络适配 | `CROSS_CHECKED` | 交叉工具 / 原始 CAD, 单元测试 | renorm 往返复原 ≤1e-9；cascade 与 skrf 直算一致; tests/test_v24_rf.py::test_rf_renormalize_preserves_physics |
| Vector fitting（自适应阶数） | `CROSS_CHECKED` | 交叉工具 / 原始 CAD, 单元测试, 闭式黄金语料 | 黄金语料 7 项 rms ≤0.5 dB（阶数 2–8）; 极点/留数响应与原始 .sNp 逐频点比对 …(+1) |
| Vector fit → SPICE round-trip | `CROSS_CHECKED` | 交叉工具 / 原始 CAD, 单元测试 | ngspice sp 复算：lc_lpf max 0.0051 dB / 相位 0.078°（tol 0.5 dB）; tests/test_v24_roundtrip.py（9 项） |
| Passivity 检查 + 受控 enforcement | `CROSS_CHECKED` | 交叉工具 / 原始 CAD, 单元测试 | 模型 S 的 σmax 与 skrf is_passive 对照；enforcement 恶化会回滚; tests/test_v24_rf.py::test_passivity_enforcement_records_before_after |
| 差分端口 / 混合模 | `CROSS_CHECKED` | 单元测试, 解析式 | 变换可逆 ≤1e-9；平衡对 Scd→−300dB（应为 0），两腿 5% 失配 → −38.3dB; tests/test_v24_rf.py::test_mixed_mode_transform_is_invertible |
| 耦合线解析估计 | `TESTED` | 单元测试, 解析式 | s/h→∞ 时 Zdiff→2·Z0se（误差 <1Ω）；奇模慢于偶模; tests/test_v24_rf.py::test_coupled_microstrip_limits |
| 差分对几何识别 | `TESTED` | 单元测试 | 合成几何：间距/失配识别正确；改名后仍能识别（不看 net 名） |
| IBIS 基础（含 [Diff Pin]） | `TESTED` | 单元测试, 闭式黄金语料 | tests/test_v24_rf.py 与 test_v23_units.py 的 IBIS 用例; 自写 IBIS test double（非厂商数据） |
| IBIS clamp（PWL） | `TESTED` | 单元测试, 解析式 | tests/test_v25_hardening.py::test_clamp_pwl_uses_ibis_vcc_relative_voltage / test_ibis_fixture_is_standards_compliant / test_ibis_reference_precedence; DC 扫描逐点对比 IBIS 表（worst < 5e-5 A），并断言电流方向：V>Vcc 灌回电源轨、V<0 从地灌入 |
| 测量数据导入 | `TESTED` | 单元测试, 闭式黄金语料 | tests/test_v24_rf.py 的 CSV/Touchstone 用例; 合成含噪波形 + 电源轨波形 |
| 仿真 vs 实测相关 | `TESTED` | 单元测试, 闭式黄金语料 | 自相关 RMSE<1e-12；注入 200ps 时延检出误差 <20ps; 合成测量（不是仪器数据） |
| 参数校准 | `TESTED` | 单元测试, 闭式黄金语料 | 可解析电路 F0 ±2kHz / Q ±0.2 复原；无 bounds 即拒绝; 已知参数扰动 |
| 灵敏度 / 可辨识性 | `TESTED` | 单元测试 | 乘积型参数 → 正确报 PARAMETER_IDENTIFIABILITY_LOW |
| De-embedding（Open/Short/OpenShort/…） | `TESTED` | 单元测试, 解析式 | tests/test_v25_hardening.py：Open 反嵌入还原 Y（<1e-6）、OpenShort 接口/清单、频率网格逐点校验; 并联 Y 相加/相减对串联 DUT 精确可逆 |
| RLGC / 有损线 | `TESTED` | 单元测试, 解析式 | √(L_total·C_total) == td_total；30mm 段 L≈11.12nH / C≈2.854pF；Rdc=ρl/(wt); tests/test_v25_hardening.py::test_rlgc_absolute_values_and_delay / test_rlgc_r_is_in_series_with_l / test_rlgc_ladder_dc_division |
| PDN 多端口 | `CROSS_CHECKED` | 交叉工具 / 原始 CAD, 单元测试 | Z11 与手算并联阻抗一致（8.7mΩ vs 8.75mΩ）; tests/test_v23_fixtures.py::test_case_f_multiport_pdn |
| Monte Carlo / corner | `TESTED` | 单元测试 | 同 seed 同结果；统计分位正确；case 上限生效 |
| FFT / 谐波 / 互调 | `CROSS_CHECKED` | 单元测试, 解析式 | 单音幅度归一后读回 1.0（误差 <5%）；1V→+10dBm@50Ω; tests/test_v23_units.py 的 FFT 用例 |
| 求解器抽象 | `TESTED` | 交叉工具 / 原始 CAD, 单元测试 | tests/test_v24_rf.py::test_solver_backends; ngspice probe 与实际版本号一致 |
| openEMS coupon 桥 | `EXPERIMENTAL` | 单元测试, 解析式 | tests/test_v25_hardening.py 验证 polygon 面积/旋转、多层介质、端口层 z、参考面选择与脚本静态检查; 多边形面积 = 长度×线宽；45° 斜线不退化成轴对齐矩形 |
| Cache / incremental rerun | `TESTED` | 单元测试, 解析式 | tests/test_v25_hardening.py：依赖文件哈希纳入 key、命中并还原 raw/csv、增量计划; 改 vendor.lib 内容而 deck 不变 → key 必须变（回归可判定） |

## 自检结果

✅ 全部 33 项等级都被证据支持（CROSS_CHECKED:15, EXPERIMENTAL:1, TESTED:17）

## 什么情况下不能说"已验证"

- 只有 `.raw` 文件、没跟解析值或实测比 → 最多 `TESTED`
- 跟"自己另一份实现"比 → 不算 `CROSS_CHECKED`（要独立信源）
- 拟合残差小 → 不是 `CORRELATED`（那是模型 vs 数据）
- 合成/拟合内数据 → 永远不能升 `CORRELATED`（`sample` 必须 OUT_OF_SAMPLE）
- 依赖本机没装的外部求解器 → `EXPERIMENTAL`

