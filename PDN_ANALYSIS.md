# PDN_ANALYSIS — 多端口阻抗（Z11 / Z21）

```bash
sim_file.sh board.epro2 pdn --config sim.json --ports pdn_ports.json
```

## 原理

ngspice-47 支持 `.sp` S 参数分析 + 带 `portnum`/`z0` 的电压源作为 RF 端口：

```
VPDN1 1V2  0 DC 0 AC 1 portnum=1 z0=50
VPDN2 3V3AD 0 DC 0 AC 1 portnum=2 z0=50
.control
sp dec 20 1k 100meg
write pdn.raw all
.endc
```

输出里直接有 `Z_1_1 / Z_2_1 / …`、`Y_i_j`、`S_i_j`，不需要自己拼 AC 激励矩阵。

> `sp` 的频率语法实测：`dec N f1 f2` / `oct` 可用；**`lin a b n` 返回 0 行**，
> 所以本工具只用 `dec`。

## 端口定义

```json
{"ports": [
  {"name": "FPGA_CORE", "node": "1V2",   "return": "GND"},
  {"name": "ADC_AVDD",  "node": "3V3AD", "return": "AGND", "z0": 50}
]}
```

至少 2 个（ngspice 要求）。`node` 与 `return` 不能相同。

## 关键检查：理想源会把 Z11 压成 0

`sp` 是交流小信号分析，端口源在直流上是短路。如果被测 rail 上直接挂着**理想**电压源，
`Z11 ≡ 0`，结果毫无意义。工具会主动扫 deck 并报：

```
PDN_PORT_SHORTED_BY_IDEAL_SOURCE: 端口 FPGA_CORE（1V2→0）被理想电压源 V1 直接短接，
Z11 会恒为 0；请给该 rail 一个带输出阻抗的真实源（regulator .SUBCKT / series R）
```

strict 下致命。正确做法：用 regulator 的 `.SUBCKT`（带输出阻抗），或用 override 的
`series_rl` 表达 LDO 输出阻抗 + 板级耦合。

## 输出

```json
{
  "ports": [{"name": "FPGA_CORE", "node": "1V2", "return": "0"}],
  "freq_hz": [1000, 1122, …],
  "impedance": {
    "1,1": {"from": "FPGA_CORE", "to": "FPGA_CORE", "|z|_ohm": [...],
            "z_max_ohm": 0.0087, "z_max_f": 1e8, "z_min_ohm": 6e-6, "z_dc_ohm": 0.0087},
    "2,1": {"from": "ADC_AVDD", "to": "FPGA_CORE", "|z|_ohm": [...]}
  },
  "resonances": [{"port": "FPGA_CORE", "f_hz": …, "z_ohm": …}],
  "anti_resonances": [...],
  "note": "simulation result, verify with measurement"
}
```

谐振判定：相邻点比值 > `resonance_min_ratio`（默认 1.5）的局部极大/极小。
**一律附"需实测复核"提示**——PDN 结果对模型（ESR/ESL/平面电容）极敏感。

## 已验证

`wolf_lite_pdn_multi` fixture：两条 rail（1V2 / 3V3AD），各自 `series_rl` 输出阻抗 +
150 mΩ 板级耦合 + 去耦（10µ/4.7µ/100n 带 ESR/ESL）。实测 Z11 ≈ 8.7 mΩ、Z22 ≈ 15 mΩ
（与并联阻抗估算一致），Z21 非零（耦合存在）。

## 限制

- 端口数不限，但每个端口都会让矩阵变大；>6 端口建议分组建模。
- 不做法向"平面腔体"建模（需要 2D 场求解），板的横向阻抗靠 override 的 RLC 表达。
- `.sp` 只给线性小信号结果，不含大信号/开关瞬态——瞬态 droop 请用 `tran` + 电流源。
