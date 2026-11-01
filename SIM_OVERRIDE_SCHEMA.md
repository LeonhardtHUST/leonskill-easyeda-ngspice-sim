# SIM_OVERRIDE_SCHEMA — `sim_override.json`

PCB 寄生层。**目标是"不改 EasyEDA 原理图就能加 PCB 模型"**：走线、via、ESR/ESL、
ferrite、连接器，以及最关键的——把原理图上的一个网络拆成 PCB 上的多段。

---

## 顶层结构

```json
{
  "version": 2,
  "params": {"DUTY": 0.5},
  "ground_aliases": {"0": ["GND"]},
  "segments": [...],
  "parasitics": [...],
  "sources": [...],
  "component_parasitics": [...],
  "disable": ["U20"],
  "replace": {...},
  "disconnect": [...],
  "short": [...]
}
```

| 段 | 作用 |
|---|---|
| `params` | 输出 `.param NAME=VALUE`，供波形表达式或扫参使用 |
| `ground_aliases` | 一般不写在这里（放 sim_config），写在这里也会生效 |
| `segments` | 显式网段切分 + 给虚拟节点起名 |
| `parasitics` | 走线 / via / 电容 / ferrite / 连接器 / 传输线 |
| `sources` | 注入独立电流源 / 电压源 |
| `component_parasitics` | 替换某个元件为带寄生的等价模型（ESR/ESL/DCR） |
| `disable` | 元件整只移除 |
| `replace` | 元件换成理想 regulator / 源 / 电阻 / 短接 / 开路 |
| `disconnect` | 单引脚开路 |
| `short` | 两个节点合并（0Ω ground link、star point） |

---

## 1. 端点引用语法

| 写法 | 含义 |
|---|---|
| `U20.5` | 元件 U20 的第 5 脚（**端点**，可能触发网段切分） |
| `NET-CLOCK` | 原理图里已存在的网络 |
| `$CLK_ADC` | 虚拟节点（override 自己新建的），deck 中写作 `__CLK_ADC` |
| `0` / `GND` / `AGND` | 地节点 |

不存在的名字 → `OVERRIDE_ENDPOINT_MISSING`（strict 下致命）。
**新建节点请用 `$` 前缀**，这样"名字打错"和"我有意新建"就能区分开。

---

## 2. `segments` — 显式网段切分

原理图只有一个 `NET-CLOCK`，PCB 上却是 `XG1 → TL1 → 分支 → TL2/TL3`。
把引脚"摘下来"挂到独立节点，串联元件才真正落在信号路径上。

```json
"segments": [
  {
    "net": "NET-CLOCK",
    "endpoints": {
      "C1.2":  "$CLK_SRC",
      "DA1.7": "$CLK_ADC",
      "U5.89": "$CLK_FPGA"
    }
  }
]
```

效果：`C1` 的 2 脚不再是 `NET-CLOCK` 而是 `__CLK_SRC`，ADC/FPGA 引脚各得独立节点，
原 `NET-CLOCK` 若无剩余引脚则自动消失。

`net` 只用于校验（端点当前不在该网络时给告警，仍然按配置切分）。

写 `segments` 的额外好处：节点名可读，可以直接被 `metrics` 引用（`$CLK_ADC`）。

不写 `segments` 也能工作：`parasitics` 里引用 `comp.pin` 时按下面的规则自动切分
并生成 `__<NET>_<DES>_<PIN>`。

---

## 3. 自动切分规则

| 元件另一端的形态 | 判定 | 切分 |
|---|---|---|
| 另一个 `comp.pin`（同网络） | 串联插入 | ✅ |
| `$虚拟节点` | 分支端点 | ✅ |
| 另一个真实网络 | 网络间串联 | ✅ |
| 地（`0`/`AGND`/ground-like） | 对地并联 | ❌ |
| `"mode": "shunt"` | 显式并联 | ❌ |

所以去耦电容 `{"from": "AVDD", "to": "AGND"}` 不会误摘引脚；
而源端阻尼电阻 `{"from": "C1.2", "to": "$CLK_TX"}` 会正确地把 `C1.2` 摘下来。

---

## 4. `parasitics` — 寄生元件

### 通用字段

| 字段 | 说明 |
|---|---|
| `name` | 记录用名字（报告/manifest 里显示） |
| `type` | 见下表 |
| `from` / `to` | 端点引用 |
| `element_name` | 强制指定**第一个**生成元件的名字（便于扫参） |
| `element_names` | 依次指定生成元件的名字（串联链用） |
| `source` | `datasheet` / `engineering_estimate`；缺失会告警 |
| `mode` | `"series"` 强制切分 / `"shunt"` 强制不切分 |

> `element_name(s)` 的首字母必须与器件类型一致（`R`/`L`/`C`/`T`），否则
> `ELEMENT_NAME_MISMATCH`——把电阻命名成 `L_xxx` 会被 ngspice 当电感解析。

### 支持的类型

#### `transmission_line`（等价写法 `tline`）

```json
{
  "name": "TL_MAIN", "type": "transmission_line",
  "from": "$CLK_TX", "to": "$CLK_BRANCH",
  "z0": 52, "length_mm": 45.08, "velocity_ps_per_mm": 6.0,
  "ref": "0", "source": "engineering_estimate"
}
```

- 生成 `T1 $from 0 $to 0 Z0=52 TD=2.7048e-10`
- `td` 可直接给（`"td": "270p"`），也可由 `length_mm × velocity_ps_per_mm` 推导；
  报告会同时打印
  ```
  length = 45.08 mm
  velocity = 6.0 ps/mm
  TD = 270.5 ps
  ```
- `ref` / `ref2` 是返回路径节点（默认 `0`）
- 校验：`z0 > 0`、`td > 0`、`velocity_ps_per_mm ≤ 20`（防止把 m/s 混进来），
  违反 → `TL_PARAM_INVALID`

#### `series_rl` / `via` / `via_rl`

```json
{"name": "VIA_CLK_1", "type": "series_rl", "from": "$CLK_ADC", "to": "AGND",
 "r": "8m", "l": "0.8n", "element_names": ["R_VIA", "L_VIA"]}
```

生成顺序固定 **R → L**（串联顺序不影响结果，但影响 `element_names` 的对应关系）。

#### `capacitor` / `shunt_cap`

```json
{"name": "HF", "type": "capacitor", "from": "AVDD", "to": "AGND",
 "c": "100n", "esr": "20m", "esl": "0.7n",
 "element_names": ["L_HF", "R_HF", "C_HF"]}
```

生成 `net — L_ESL — R_ESR — C — gnd`（顺序与需求文档一致）。

#### `resistor` / `inductor` / `ferrite` / `connector`

```json
{"name": "FB1", "type": "ferrite", "from": "FRONT_RAIL", "to": "LED_RAIL",
 "rdc": "50m", "l": "1u", "rp": "2k"}
```

ferrite = `R_dc` 串联 + (`L` ∥ `R_p`) 并联块。

---

## 5. `sources` — 注入独立源

```json
"sources": [
  {"type": "current_source", "name": "I_FPGA", "from": "VCCINT", "to": "AGND",
   "waveform": "PULSE(120m 300m 10u 2n 2n 5u 10u)"},

  {"type": "voltage_source", "name": "V_BIAS", "from": "$CLK_BIAS", "to": "0",
   "waveform": {"type": "dc", "value": "1.65"}},

  {"type": "current_source", "name": "I_AC", "from": "AVDD", "to": "AGND",
   "waveform": {"type": "dc", "value": "0"}, "ac": 1}
]
```

`waveform` 可以是字符串（原样落 deck，支持 `{DUTY*10u}` 这类 `.param` 表达式），
也可以是结构体：

| `type` | 参数 |
|---|---|
| `pulse` | `i_idle`/`i_active`（或 `v_low`/`v_high`）、`delay`、`rise`、`fall`、`on`（=`width`）、`period` |
| `pwl` | `points: [[t, v], ...]` |
| `sin` | `offset` `ampl` `freq` `delay` `theta` `phase` |
| `dc` | `value` |

`ac` 字段会追加 `AC <值>`，PDN 阻抗分析用它注入 1 A 激励。

---

## 6. `component_parasitics` — 元件级寄生

```json
"component_parasitics": [
  {"component": "C25", "model": {"C": "100n", "ESR": "20m", "ESL": "0.7n"},
   "source": "datasheet"},
  {"component": "L3",  "model": {"L": "10u", "DCR": "30m"}, "source": "datasheet"},
  {"component": "FB1", "model": {"type": "ferrite", "RDC": "50m", "L": "1u", "RP": "2k"}},
  {"component": "J1",  "model": {"type": "connector", "R": "20m", "L": "2n"}}
]
```

- 只支持 **2 脚元件**；
- 生效后该元件不再走默认生成路径（原来的理想 C/L 被替换）；
- `model.C` / `model.L` 省略时回退到元件自身的 Value。

---

## 7. 启用 / 替换 / 短接 / 断开

```json
"disable": ["U20", "C99"],

"replace": {
  "DA8": {"type": "ideal_regulator", "vout": 2.5},
  "U22": {"type": "ideal_source", "v": 5, "node": "VOUT_NET"},
  "R7":  {"type": "resistor", "r": "10k"},
  "L2":  {"type": "short"},
  "U30": {"type": "open"}
},

"disconnect": ["U20.5", {"endpoint": "U21.7"}],

"short": [{"from": "AGND", "to": "0"}]
```

`replace` 的 `type`：

| type | 效果 |
|---|---|
| `ideal_regulator` / `ideal_source` / `voltage_source` | 在输出节点上挂 `V ... DC <vout>`（A/B 对比 LDO 有无真实模型） |
| `resistor` | 挂一个电阻到地 |
| `short` | 1 mΩ 短接到地 |
| `open` / `none` | 直接移除，不留替代 |

目标节点确定顺序：`node` 字段 → `pin` 字段 → 引脚名含 `VOUT`/`OUT`/`VO` 的唯一候选。
都不行 → `REPLACE_TARGET_UNKNOWN`。

---

## 8. 错误码

| 码 | 触发 |
|---|---|
| `OVERRIDE_FILE_MISSING` | `--override` 文件不存在 |
| `OVERRIDE_ENDPOINT_MISSING` | 端点/网络名不存在、引脚未连线、`disable`/`replace` 指向不存在的元件 |
| `TL_PARAM_INVALID` | 传输线参数非法（含速度单位明显错误） |
| `PARAM_INVALID` | 数值/波形参数非法 |
| `PARASITIC_TYPE_UNKNOWN` | 未知 `type` |
| `PARASITIC_COMPONENT_UNSUPPORTED` | `component_parasitics` 指向非 2 脚元件 / 悬空 |
| `ELEMENT_NAME_MISMATCH` | `element_name` 首字母与器件类型不符 |
| `REPLACE_TARGET_UNKNOWN` | `replace` 无法确定输出节点 |
| `REPLACE_UNSUPPORTED` | 未知 `replace.type` |
