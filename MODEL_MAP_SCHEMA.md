# MODEL_MAP_SCHEMA — `models/model_map.json`

外部 SPICE 模型映射表。用于把原理图里的 `U` / `IC` / `X`（以及任何带 ≥2 脚的
未知前缀器件）映射到真实的 `.SUBCKT` / `.MODEL`，而不是像 v2.1 那样一律排除。

> **本 skill 不内置任何厂商模型。** `models/external/` 只是给用户自己放 `.lib`
> 的位置；受许可限制的模型必须由使用者自行取得并放入。

---

## 顶层结构

```json
{
  "version": 2,
  "parts": { "<key>": { ...entry... } },
  "generics": [ { "match": { ... }, ...entry... } ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `version` | 否 | 目前为 2 |
| `parts` | 是 | key 是任意标识（通常用型号或自取的名字），仅用于报错定位 |
| `generics` | 否 | 按"引脚前缀 / 标题关键字"兜底，优先级最低 |

---

## 匹配优先级

```
entry.match.designator   （精确，大小写不敏感）
  > entry.match.mpn      （精确，或 key 本身命中）
    > entry.match.device （精确）
      > entry.match.aliases（精确）
        > generics
```

注意：`match.mpn` 里写 `"TPS82130"` 是**精确匹配**；`behavioral.json` 里才支持
通配符（`"AD9226*"`）。model_map 故意不做模糊匹配——型号张冠李戴的代价太大。

```json
"match": {
  "designator": ["U20"],
  "mpn": ["TPS82130"],
  "device": ["TPS82130"],
  "aliases": ["TPS82130SIL"]
}
```

---

## 条目字段

| 字段 | 类型 | 适用 | 说明 |
|---|---|---|---|
| `type` | `"subckt"` \| `"model"` | 必填 | subckt → 生成 `X` 实例；model → 生成器件实例 + `.MODEL` |
| `file` | string | subckt 必填 | 相对 **model_map 所在目录** 的路径；`external/xxx.lib` |
| `subckt` | string | subckt | `.SUBCKT` 名，缺省用 key |
| `lib_section` | string | 可选 | 有值时生成 `.lib "file" section`，否则 `.include "file"` |
| `pin_mapping_auto` | — | — | 不是配置项；`pins` 缺省时才启用"按引脚名匹配" |
| `pins` | `{原理图引脚号: subckt 引脚名}` | 强烈建议 | 不给则必须引脚名能一一对应 |
| `subckt_order` | `[subckt 引脚名...]` | 可选 | 与文件里的 `.SUBCKT` 顺序比对；不一致时以**文件**为准并告警 |
| `nc_pins` | `[subckt 引脚名...]` | 可选 | 明确声明"就该开路"的 subckt 引脚，避免 mapping 不完整报错 |
| `roles` | `{subckt 引脚名: role}` | 可选 | 也可以写原理图引脚号作 key |
| `card` | string | `type = model` 时二选一 | 内联 `.MODEL` 文本 |
| `model` | string | model | `.MODEL` 名，缺省用 key |
| `device_letter` | `Q`/`M`/`D`/`J`... | **`type = model` 必填** | 决定实例化成什么器件（首字母决定 ngspice 解析） |

### roles 词表

`power_input` `power_output` `signal_input` `signal_output`
`digital_input` `digital_output` `analog_input` `analog_output`
`open_drain_output` `ground` `passive`

只有 `power_output` 会影响仿真逻辑：它把该节点登记为"已驱动"，
从而阻止 auto 模式再并一个理想电压源（这正是 v2.1 毁掉 regulator 仿真的原因）。

---

## 示例：buck 转换器

```json
{
  "version": 2,
  "parts": {
    "TPS82130": {
      "match": {"mpn": ["TPS82130"], "aliases": []},
      "type": "subckt",
      "file": "external/tps82130.lib",
      "subckt": "TPS82130",
      "pins": {
        "1": "EN", "2": "VIN", "3": "GND", "4": "VOUT",
        "5": "VOUT", "6": "FB", "7": "PG", "8": "SSRT"
      },
      "subckt_order": ["VIN", "EN", "GND", "VOUT", "FB", "PG", "SSRT"],
      "nc_pins": ["PG"],
      "roles": {
        "VIN": "power_input",
        "VOUT": "power_output",
        "GND": "ground",
        "EN": "digital_input",
        "PG": "open_drain_output"
      }
    }
  }
}
```

要点：

- 引脚 4 和 5 都映射到 `VOUT`——**允许**，但两个引脚必须落在同一网络（多焊盘
  散热型封装很常见），否则 `MODEL_MAPPING_ERROR`；
- `subckt_order` 与文件里的一致时照抄；文件里若是 7 个引脚而这里写 8 个 → 报错；
- `nc_pins: ["PG"]` 表示 PG 故意不接，生成实例时该位置用 `__NC_` 占位节点，
  **不会**让后面的引脚整体移位。

## 示例：`.MODEL` 类型（三极管阵列）

```json
"MMBT3904_ARRAY": {
  "match": {"mpn": ["MMDT3904"]},
  "type": "model",
  "file": "external/on_semi.lib",
  "lib_section": "TT",
  "model": "MMDT3904",
  "device_letter": "Q",
  "pins": {"1": "C", "2": "B", "3": "E"}
}
```

`device_letter` 缺失 → `MODEL_MAPPING_ERROR`（提示补上）。猜首字母等于猜器件类型，
不猜。

---

## `.include` / `.lib` 的生成规则

- 同一个（文件, section）只写一次；
- 路径用正斜杠，相对 deck 目录优先，跨盘/上级目录时退回绝对路径；
- 含空格一律加双引号（实测 `".include "dir with space/my lib.lib"` 可用）；
- `lib_section` 存在 → `.lib "file" TT`，要求文件里用 `.LIB TT ... .ENDL TT`
  包裹（否则 ngspice 报 `section definition TT not found`，实测）。

---

## 错误码

| 码 | 触发 |
|---|---|
| `MODEL_MAP_MISSING` | `--models` 指向的文件不存在 |
| `MODEL_FILE_MISSING` | 条目里的 `file` 不存在 |
| `MODEL_MAPPING_ERROR` | 缺 pin 映射 / 未知引脚 / 多引脚映射到不同网络 / 文件里找不到 `.SUBCKT` / `subckt_order` 集合不一致 / model 类型缺 `device_letter` |
| `ELEMENT_NAME_MISMATCH` | （override 侧）`element_name` 首字母与器件类型不符 |
