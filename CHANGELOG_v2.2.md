# CHANGELOG v2.2

`easyeda-ngspice-sim` 2.1.0 → 2.2.0

本轮范围：**只做会直接影响 SDR 板（buck/LDO 电源树、ADC PDN、61.44 MHz clock 分支、
Front LCD PWM 线束、PCB 寄生、参数扫描与量化）的功能**。没有扩展成 SI/PI/EM 平台。

---

## 一句话总结

v2.1 能跑"小电路"，v2.2 能跑"有电源树和 PCB 寄生的板子"，并且**在拓扑不可信时
选择明确失败，而不是产出一条看着正常的错误波形**。

---

## 破坏性变更（必须知道）

| 变更 | 影响 | 迁移方式 |
|---|---|---|
| **AGND/DGND/PGND/CHASSIS/EARTH 不再自动并到节点 0** | 以前 AGND 直接变成 0，现在保持独立节点 | 需要合并时在 config 写 `ground_aliases`；报告会逐条打印 `Ground mapping` |
| **`U`/`IC`/`X` 以及"未知前缀 + ≥2 脚"的器件不再默认静默排除** | 以前一律"无仿真模型，已排除"，现在会尝试 model_map → behavioral → 才排除 | 老工程若依赖"IC 被排除"，请配 `disable` 或看 `excluded` 清单 |
| **子电路实例名加 `X` 前缀**（`U20` → `XU20`） | 之前直接叫 `U20` 会被 ngspice 当成 URC 传输线（实测报 `can't find model`） | 无（这是修 bug），但引用实例名的地方要改成 `XU20` |
| `0Ω` 电阻统一换成 `1 mΩ` | ngspice 对 0Ω 只 clamp 到 1e-12，1µΩ 量级还会 `timestep too small` 挂死 | 无，报告注释里会写明 |
| 默认 `power_policy = auto` 但会打显著 warning | 电源网标自动补源仍在，但不再"悄悄发生" | 复杂板卡建议改 `explicit` |
| 报告/产物多了 `netlist_report.json`、`metrics.json`、`simulation_manifest.json`、`sweep_summary.*` | 只增不减 | 无 |

---

## 新增能力（按需求文档章节）

### P0-A 地映射策略（§1）
- 默认只把 `GND` / `GROUND` / `0` 映射到 SPICE 节点 0；
  `AGND` / `DGND` / `PGND` / `CHASSIS` / `EARTH` / `VSS` / `GND1` 一律保持独立。
- 合并必须显式声明：`{"ground_aliases": {"0": ["GND", "AGND", "DGND"]}}`。
- 报告强制打印：
  ```
  Ground mapping:
    GND  -> 0
    AGND -> AGND   [kept isolated]
  ```
- 冲突映射（同一网络指两个节点）→ `GROUND_MAPPING_CONFLICT`。
- 未合并的 ground-like 网络会给出提示，**不静默**。

### P0-B 外部 SPICE 模型映射（§2–§3、§5）
- 新增 `models/model_map.json`（默认空表）+ `models/external/`（用户自放厂商模型）。
- 匹配优先级：`designator` > 精确 `MPN` > 精确 `DeviceName` > `alias` > `generic`。
- 支持 `.SUBCKT` / `.MODEL` / `.LIB file section`；`.include` 自动去重、路径加引号、
  可用相对 deck 路径（Windows 含空格实测通过）。
- **pin mapping 严格校验**：缺失 / 重复（映射到不同网络）/ subckt pin 数不符 / 未知
  pin 名 → `MODEL_MAPPING_ERROR`，只有 `--allow-partial-model` 才降级为警告。
- 不给 `pins` 时允许**按引脚名自动匹配**（名字对得上才算，绝不按几何或顺序猜）。
- `roles` 支持 `power_input / power_output / signal_input / signal_output /
  digital_input / digital_output / analog_input / analog_output / open_drain_output /
  ground / passive`。
- 报告输出 provenance：
  ```
  U20 TPS82130
  model source: external/tps82130.lib
  model type: SUBCKT
  subckt: TPS82130
  pin mapping: VERIFIED
  fallback: no
  ```

### P0-C 自动理想电源冲突（§4）
- `--power-policy auto`：保留 v2.1 行为，但输出显著 warning，且**电源网的节点已由
  某个 subckt 的 `power_output` 引脚驱动时不再补源**（v2.1 会并一个理想源，直接毁掉
  regulator 仿真）。
- `--power-policy explicit`：只认真实电压源 + config 的 `external_supplies`，
  不再按网名猜电压；孤立电源轨在 strict 下报 `ISOLATED_RAIL`。
- 同一节点被两个理想源驱动 → strict 下 `DUPLICATE_VOLTAGE_SOURCE`。

### P0-D 行为级占位模型（§6–§7）
- 新增 `models/behavioral.json`，三类模型：
  `dynamic_current_load` / `capacitive_input` / `switching_output`。
- 支持 PULSE / PWL / SIN / DC 波形；`{DUTY*10u}` 这类 `.param` 表达式可直接写在
  波形字符串里。
- 一律标记 `BEHAVIORAL_APPROXIMATION`，报告单独成段并带 `not_simulated` 声明，
  绝不与厂商模型混为一谈。

### P0-E PCB override + 网段切分（§8–§9、§16、§17）
- 新增 `sim_override.json`：`segments` / `parasitics` / `sources` / `device` /
  `component_parasitics` / `short` / `disconnect` / `disable` / `replace` / `params`。
- **端点级网段切分**：用 `component.pin` 作为端点，自动把该引脚从原网络摘下挂到
  虚拟节点（`$CLK_ADC`），串联元件才真正落在信号路径上。
- 对地并联（去耦电容 / via 到地）**不会**误触发切分，靠"另一端是不是地"判定。
- `$虚拟节点` 在 deck 中写作 `__NAME`（ngspice 把 `$` 当行内注释符，实测
  `V1 $SRC 0 1` 直接报 "Not enough parameters for v source"），报告保留 `$` 写法。
- `disable` / `replace(ideal_regulator|ideal_source|resistor|short|open)` /
  `short`（节点合并）/ `disconnect`（引脚开路，IC 用 `__NC_` 占位）。

### P0-F 传输线与 RLC 寄生（§10–§11）
- 无损传输线 `T n1 0 n2 0 Z0=.. TD=..`；`td` 可直接给，也可由
  `length_mm × velocity_ps_per_mm` 推导，报告同时显示推导过程：
  ```
  length = 45.08 mm
  velocity = 6.0 ps/mm
  TD = 270.5 ps
  ```
- 单位护栏：`velocity_ps_per_mm > 20` 直接拒绝（防止把 m/s 或 in/ps 填进来造成
  三个数量级的错误）。
- 元件级寄生：电容 ESR/ESL（`net — L — R — C — gnd`）、电感 DCR、ferrite
  （R_dc + L∥R_p）、via、connector。所有寄生参数要求 `source` 字段
  （`datasheet` / `engineering_estimate`），缺失会告警。

### P1-A 参数扫描（§12–§13）
- 一维 / 二维笛卡尔积，`max_cases`（默认 200）硬上限。
- 不依赖 `.step`：Python 侧改值 → 独立跑 ngspice → 收集 raw。
- 参数可以是**元件名**（配合 `element_name` / `element_names`）或 **`.param` 名**。
- `r=0` 会被替换成 `1m`（见"破坏性变更"）。
- 每个 case 产物：`runs/<case-id>/{deck.cir, stdout.txt, stderr.txt, *.raw, *.csv, metrics.json}`；
  汇总 `sweep_summary.csv` / `sweep_summary.json`。

### P1-B 定量指标（§15）
- transient：`max/min/peak_to_peak/mean/rms/overshoot/undershoot/rise_time/fall_time`，
  并区分 `signal_kind = edge | rail`（电源轨只报 `ripple_pct`/`deviation_pct`，
  不再产出"过冲 120%"这种荒谬数字）。
- clock：`threshold_crossings`（按"同一条边沿"聚类，能报 `ringing` /
  `max_crossings_in_edge`）、`time_delta`（`NODE@阈值` → skew）。
- AC/PDN：`impedance` → `max_impedance` / `peak_frequency` / 任意频点
  `z_at_<freq>`（log-log 插值）。
- PWM：`fft_harmonics`（基波 + 谐波 + 相对 dB）。

### P1-C Simulation manifest 与 fidelity（§19、§28）
- 每次仿真产出 `simulation_manifest.json`：源工程/hash/skill 版本/ngspice 版本/
  分析设置/模型清单（含 pin mapping 状态）/行为级模型/被排除器件/地映射/电源/
  override/警告/错误/每个 case 的目录。
- Level 0–3 逐块展示，措辞统一，禁止"已成功仿真整个 PCB"。

### §18 Strict validation
`--strict` 下以下情况**直接失败**（退出码 3，不产出波形）：

- 未映射且位于有效电流路径的 IC（`UNMAPPED_IC`）
- subckt pin mapping 不完整（`MODEL_MAPPING_ERROR`）
- 模型文件不存在 / `.SUBCKT` 名不存在（`MODEL_FILE_MISSING` / `MODEL_MAPPING_ERROR`）
- `power_output` 被理想源重复驱动（`POWER_OUTPUT_CONFLICT`）
- 同一节点多个独立电压源（`DUPLICATE_VOLTAGE_SOURCE`）
- `explicit` 模式下孤立电源轨（`ISOLATED_RAIL`）
- override 端点不存在（`OVERRIDE_ENDPOINT_MISSING`）
- 传输线参数非法（`TL_PARAM_INVALID`）
- `element_name` 首字母与器件类型不符（`ELEMENT_NAME_MISMATCH`）

（AGND/DGND 已默认不合并，所以"隐式合并"这条不再需要靠 strict 兜底。）

---

## 兼容性

保持不变的：

- `sim_file.sh <epro2> <mode> [arg] [page] [workdir]`
- `sim_from_sch.sh <工程> [页名] <mode> [arg] [workdir]`
- `sim_pipeline.sh`（老仿真页入口）
- `netlist_gen.py <ir> <out> [--keep-open]`（不传新参数 = 旧行为，除地映射与 IC 策略）
- `build_deck.py <raw> <out> <op|tran|ac|dc|custom>`
- R/C/L、BJT、二极管、MOSFET（含泛化兜底）、电压源、SVG 线路图、仿真页写回、
  中文工程/文件名、Windows ngspice-47
- v2.1 的 `+12V → VPWR_12V` / `-15V → VPWR_NEG_15V` 命名

新增入口（不破坏旧的）：

```bash
sim_file.sh board.epro2 tran "10p 120n" "" ./out \
  --config sim.json --models model_map.json --behavioral behavioral.json \
  --override sim_override.json --power-policy explicit --strict
```

---

## 已知限制

- 传输线只支持**无损** `T` 线；`LTRA` / lossy 未做（本轮不必要）。
- 传输线参考节点只支持单一返回路径（默认 0），没有差分/多导体耦合。
- 行为级模型是**拓扑级近似**，不做晶体管级还原，也不建模抖动/相位噪声。
- 不使用 `.step`；扫描由 Python 编排，因此不支持扫描"任意表达式"。
- `ac` 阻抗假设 1 A 激励（或用 `amplitude` 换算），没有自动注入 AC 探针。
- 大 raw（>50 MB）解析仍是纯 Python，约 1 万点/秒量级；扫描 case 多时注意磁盘。
- IBIS、Touchstone、场求解器等按需求文档 §20 明确不在本轮范围。
