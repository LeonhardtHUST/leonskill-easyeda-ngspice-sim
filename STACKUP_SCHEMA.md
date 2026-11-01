# STACKUP_SCHEMA — `stackup.json`

叠层是 Z0/TD 的唯一依据。**缺了就报 `STACKUP_INCOMPLETE`，绝不默认 FR4=4.5 继续。**

```json
{
  "layers": [
    {"name": "Top",    "type": "signal", "copper_um": 35},
    {"name": "Inner1", "type": "plane",  "copper_um": 35},
    {"name": "Inner2", "type": "plane",  "copper_um": 35},
    {"name": "Bottom", "type": "signal", "copper_um": 35}
  ],
  "dielectrics": [
    {"between": ["Top", "Inner1"],    "thickness_mm": 0.18, "er": 4.2,
     "loss_tangent": 0.02, "material": "FR4"},
    {"between": ["Inner1", "Inner2"], "thickness_mm": 1.2,  "er": 4.2},
    {"between": ["Inner2", "Bottom"], "thickness_mm": 0.18, "er": 4.2}
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `layers[].name` | 是 | 必须与 `PCB_GEOMETRY.json` 里的 `layer.name` 一致 |
| `layers[].type` | 否 | `signal` / `plane`（内层默认按带状线处理，不看这个字段） |
| `layers[].copper_um` | **是** | 缺 → `STACKUP_INCOMPLETE`（微带线 Z0 对铜厚敏感） |
| `dielectrics[].between` | 是 | 两个层名，顺序无所谓 |
| `dielectrics[].thickness_mm` | **是** | 介质厚度 |
| `dielectrics[].er` | **是** | 相对介电常数 |

## 只补缺（推荐用法）

PCB 文件里 `LAYER_PHYS` 常常只有介质层有数据（实测 `thickness=0.394, permittivity=3.3`，
铜厚是 0/null）。这时用户只需要写铜厚：

```json
{"layers": [{"name": "Top Layer", "copper_um": 35},
            {"name": "Bottom Layer", "copper_um": 35}]}
```

`Stackup.from_file(path, base=pcb_derived)` 会把 PCB 抽到的 εr/厚度填进缺失项，
用户不用重抄。**但绝不会用默认值填补**——真缺 εr 就还是 `STACKUP_INCOMPLETE`。

## 模型选择

| 层位置 | 模型 | 公式 |
|---|---|---|
| 顶层 / 底层 | microstrip | Hammerstad-Jensen（含有限铜厚修正） |
| 内层（两侧都有介质） | stripline | IPC-2141 `Z0 = 60/√εr · ln(4b/(0.67πw(0.8+t/w)))` |

`td_ps_per_mm = 3.33564 × √εeff`。输出必带 `"model_fidelity": "ANALYTIC_ESTIMATE"`，
误差量级 ±10%（常规 w/h、εr 范围内）。

## 单位换算

`stackup.json` 一律用 **mm / µm**（`copper_um` 是微米）。PCB 来的 mil 由
`pcb_geometry` 换算，`Stackup` 只看到 mm。

## 精度自检

| 场景 | 结果 |
|---|---|
| w=0.2mm, h=0.18mm, εr=4.2, 铜 35µm | Z0 = 59.7Ω, εeff = 3.13, td = 5.90 ps/mm |
| 45.08 mm × 6.0 ps/mm（需求文档对照） | 270.48 ps（工具算出 270.5 ps，浮点级一致） |
| 1.6mm 通孔 d=0.3mm | L = 1.30 nH, C = 0.45 pF, R = 1.17 mΩ |
