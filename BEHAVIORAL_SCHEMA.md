# BEHAVIORAL_SCHEMA — `models/behavioral.json`

行为级占位模型。用于**没有公开可用晶体管级模型**的器件：FPGA、MCU、ADC 的数字输出、
专用 RF IC。v2.1 只能把它们整只排除（等于从电路里挖掉），v2.2 用三类近似模型顶上。

> 全部标记 `BEHAVIORAL_APPROXIMATION`。它们只表达**端口行为**（电流/电容/边沿），
> 不表达内部电路。报告里绝不会把它们算成"厂商模型"。

---

## 结构

```json
{
  "version": 2,
  "models": {
    "<key>": {
      "match": {"designator": [...], "mpn": [...], "device": [...], "aliases": [...]},
      "type": "dynamic_current_load | capacitive_input | switching_output",
      "pins": {"<role>": ["<引脚名或引脚号>"...]},
      "pins_by": {"<role>": "name | number"},
      "ref": "0",
      "...": "各类型专属参数",
      "not_simulated": ["logic core", "PLL"],
      "note": "给人看的说明"
    }
  }
}
```

## 匹配

`match` 里四个字段都支持**通配符**（`fnmatch`，大小写不敏感），优先级：

```
designator（精确） > aliases > mpn > device
```

例：`"mpn": ["AD9226*"]` 能匹配 `AD9226ARSZ` / `AD9226BCPZ`。

## 引脚定位

`pins` 的 value 支持：

- 引脚**名**（通配符，大小写不敏感）：`"supply": ["VCCINT", "VCC*"]`
- 引脚**号**：`"supply": ["14"]`（名字匹配不到时会再试引脚号）

要用纯引脚号时建议显式写 `"pins_by": {"supply": "number"}`。

找不到 → `BEHAVIORAL_PIN_MISSING`；找到了但没连线 → `BEHAVIORAL_PIN_FLOATING`。

---

## 类型 1 — `dynamic_current_load`

电源脚挂一个 PULSE/PWL 电流汇，表达"这颗片子会拉多少动态电流"。

```json
"FPGA_CORE": {
  "match": {"device": ["EP4CE10*"]},
  "type": "dynamic_current_load",
  "pins": {"supply": ["VCCINT"], "ground": ["GND"]},
  "ref": "0",
  "waveform": {
    "type": "pulse",
    "i_idle": "120m",
    "i_active": "300m",
    "delay": "10u",
    "rise": "2n",
    "fall": "2n",
    "on": "5u",
    "period": "10u"
  },
  "not_simulated": ["logic core", "internal PLL", "IO drive strength"]
}
```

生成：

```
I_BHV_U3_I1 VCCINT 0 PULSE(120m 300m 10u 2n 2n 5u 10u)
```

- `ref` 缺省时取 `pins.ground` 第一个引脚的节点，再缺省才是 `0`
- 多个不同的 supply 网络 → 每个网络一个电流源，电流**平均分摊**（并在报告里注明）
- 波形也可以直接写字符串：`"waveform": "PULSE(0 500m 1u 100n 100n {DUTY*10u} 10u)"`，
  `{DUTY*10u}` 由 override 的 `params` 提供（见 SIM_OVERRIDE_SCHEMA）

---

## 类型 2 — `capacitive_input`

输入脚挂一个电容（可带串联电阻），表达"这颗片的输入电容"。

```json
"ADC_CLK_IN": {
  "match": {"mpn": ["AD9226*"]},
  "type": "capacitive_input",
  "pins": {"input": ["CLK"]},
  "ref": "0",
  "c": "4p",
  "not_simulated": ["ADC sampling core"]
}
```

生成：`C_BHV_DA1_CIN1 __CLK_ADC 0 4p`

可选 `"r": "5"` → 变成 `R + C` 串联（模拟输入串联电阻 / 键合线）。

用于：FPGA clock input、ADC CLK、MCU GPIO、比较器输入。

---

## 类型 3 — `switching_output`

输出脚用 `PULSE 源 + Rout + Cload` 造一个边沿模型。

```json
"OSC_CLOCK_OUT": {
  "match": {"device": ["OSC*"]},
  "type": "switching_output",
  "pins": {"output": ["OUT"]},
  "ref": "0",
  "waveform": {
    "type": "pulse", "v_low": "0", "v_high": "3.3",
    "delay": "2n", "rise": "0.3n", "fall": "0.3n",
    "on": "8.138n", "period": "16.276n"
  },
  "rout": "10",
  "cload": "3p",
  "not_simulated": ["oscillator jitter / phase noise model"]
}
```

生成：

```
V_BHV_XG1_OUT1_SRC __BHV_XG1_OUT1_DRV 0 PULSE(0 3.3 2n 0.3n 0.3n 8.138n 16.276n)
R_BHV_XG1_OUT1_ROUT __BHV_XG1_OUT1_DRV CLK_OSC 10
C_BHV_XG1_OUT1_CLOAD CLK_OSC 0 3p
```

不给 `rout` 时用 1 mΩ 近似理想驱动；`cload` 可省略。

---

## `not_simulated` / `note`

`not_simulated` 里的每一项都会以 `"<项>: not simulated"` 形式出现在报告里，
强制暴露"这个模型到底没算什么"：

```
U3 EP4CE10
simulation level: BEHAVIORAL_APPROXIMATION
models:
  VCCINT = dynamic current load -> 0
logic core: not simulated
internal PLL: not simulated
```

---

## 与厂商模型的关系

`model_resolver` 先跑。**只要 model_map 里能解析出真实模型，就不会用行为级占位**。
想让某个器件强制用行为级模型，把 model_map 里对应条目去掉，或直接在 override 里
`disable` 它再手工加源。

---

## 错误码

| 码 | 触发 |
|---|---|
| `BEHAVIORAL_FILE_MISSING` | `--behavioral` 文件不存在 |
| `BEHAVIORAL_TYPE_UNKNOWN` | `type` 不认识 |
| `BEHAVIORAL_PIN_MISSING` | `pins` 里给的引脚名在原理图上找不到 |
| `BEHAVIORAL_PIN_FLOATING` | 引脚找到了但没接线 |
| `BEHAVIORAL_PARAM_MISSING` | `capacitive_input` 缺 `c` |
| `BEHAVIORAL_WAVE_MISSING` | `dynamic_current_load` 缺 `waveform` |
