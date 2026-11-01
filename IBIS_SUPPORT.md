# IBIS_SUPPORT — IBIS 解析与行为级转换

```bash
sim_file.sh board.epro2 tran "10p 120n" "" ./out --ibis ibis_map.json
```

## 支持范围（刻意不做全标准）

| 段 | 状态 |
|---|---|
| `[IBIS Ver]` `[Component]` `[Manufacturer]` `[Package]` `[Pin]` | ✅ |
| `[Model]`（`Model_type` / `C_comp` / `C_pkg` / `R_pkg` / `L_pkg` / `Vinl` / `Vinh` / `Vmeas`） | ✅ |
| `[Pullup]` `[Pulldown]` `[POWER Clamp]` `[GND Clamp]`（V-I 表） | ✅ |
| `[Ramp]`（`dv/dt_r` `dv/dt_f`） | ✅ |
| `[Rising Waveform]` `[Falling Waveform]` | ✅（只取 typ，min/max 列一并解析） |
| `[Diff Pin]` / 差分模型 / 多状态 / 预加重 | ❌ 未实现（会明确报"不支持"而不是猜） |

数值后缀（`1.2pF` / `3.3V` / `50ohm` / `1nH` / `-8.0mA`）统一走工程值解析。

## `ibis_map.json`

```json
{
  "parts": [
    {"designator": "U5", "pin": "89", "ibis_file": "EP4CE10.ibs",
     "model": "3V3_LVCMOS_IN", "vcc": 3.3, "corner": "typ"},
    {"designator": "U5", "pins": ["45", "46", "47"], "ibis_file": "EP4CE10.ibs"},
    {"mpn": "EP4CE10E22C8N", "pin": "89", "ibis_file": "EP4CE10.ibs"},
    {"component": "FIXTURE_FPGA", "pin": "89", "ibis_file": "x.ibs"}
  ]
}
```

匹配顺序：`designator`（精确）→ `component`（对 designator/MPN/DeviceName）→
`mpn` → `device`。`model` 省略时用 `[Pin]` 里该脚自己的 model 名。

## 转换结果

| 模型类型 | 生成 |
|---|---|
| Input | `node — L_pkg — R_pkg — C_comp — gnd` |
| Output / I/O | `V_PULSE(0..vcc, tr/tf)` + `R_out` + `C_comp` |
| Open drain | 按 Output 处理，`v_high = vcc` |

- `R_out` 由 IV 曲线在 `Vout = vcc/2` 处反推（`R = (vcc/2)/|I|`）。实测某 3.3V
  LVCMOS 的 ±33mA@1.65V → 50Ω。
- `tr/tf`：`[Ramp]` 给的是 **20–80%** 斜率 → ×1.8 折算成 SPICE PULSE 的 0–100%；
  没有 Ramp 时用波形 10–90% → ×1.25。`edge_basis` 会写进报告，不隐瞒口径。
- 引脚级 `R_pin/L_pin/C_pin` **优先于** `[Package]`（IBIS 的层次规则）。

## 标记与溯源

所有产物标 `IBIS_DERIVED_BEHAVIORAL_MODEL`，manifest 记录：

```json
{"designator": "U5", "pin": "89", "model": "3V3_LVCMOS_IN",
 "ibis_file": "fixture_lvcmos.ibs", "ibis_sha256": "…", "c_total_pf": 4.6,
 "rout_ohm": null, "level": "IBIS_DERIVED_BEHAVIORAL_MODEL"}
```

## 限制

- 输出模型的 `V_PULSE` 只是**默认占位激励**；真实时钟/数据波形请用 override 的
  `sources` 覆盖，或把这颗器件当负载看。
- 不做 C_comp 的电压依赖（`[C_comp]` 只能给常数）。
- 夹位二极管（clamp）目前只解析不建模；有强过冲钳位需求时请用行为级 override。
