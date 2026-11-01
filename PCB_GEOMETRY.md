# PCB_GEOMETRY — PCB 几何提取与 `PCB_GEOMETRY.json`

```bash
pcb_geometry.py Main.epro2 PCB_GEOMETRY.json --net NET-CLOCK [--unit mil|mm]
```

输入两种：**EasyEDA Pro `.epro2`**（读 `docType=PCB` 文档），或**已解析的 PCB IR**
（`{"tracks":[...],"vias":[...],"pads":[...],"layers":[...],"unit":"mil"}`）。

## 1. 实测格式事实（3 个真实工程验证）

| 图元 | 字段 |
|---|---|
| `LINE` | `netName, layerId, startX/Y, endX/Y, width` |
| `VIA` | `netName, centerX/Y, holeDiameter, viaDiameter, viaType, unusedInnerLayers` |
| `LAYER` | id = `["LAYER", n]`，payload `layerType/layerName` |
| `LAYER_PHYS` | `material, thickness, permittivity, lossTangent`（**常为 null/0**） |
| `COMPONENT` | `x, y, angle, layerId` |
| `ATTR` | `parentId` + `key=Designator / Footprint` |
| `PAD_NET` | id = `["PAD_NET", compId, padNum, padId]`，payload `padNet` |
| `POUR/POURED` | 铺铜（本轮不用来做网格，只用于判断平面层） |

**单位**：坐标与线宽是 **mil**。`CANVAS.unit` 是"界面显示单位"，可能是 `mm`——
**不能**拿它当坐标单位（实测 target_test 项目 `unit=mm` 但坐标是 mil）。
默认 `mil`，可用 `--unit` 覆盖；转换系数写在输出的 `units.scale_mm_per_unit`。

焊盘绝对坐标 = `COMPONENT.(x,y)` + `rotate(PAD.(centerX,centerY), angle)`。
实测某颗 0805 的 pad2 与一条走线端点对齐到 **0.02 mil**，可以直接用作端点锚定。

## 2. 输出

```json
{
  "version": 1,
  "units": {"internal": "mil", "emitted": "mm", "scale_mm_per_unit": 0.0254},
  "board": {"extent_mm": {"x": [9.1, 59.4], "y": [-57.2, -5.6]}},
  "layers": [{"id": 1, "name": "Top Layer", "type": "TOP"},
             {"id": 2, "name": "Bottom Layer", "type": "BOTTOM"}],
  "nets": {
    "NET-CLOCK": {
      "segments": [
        {"from": "XG1.4", "to": "__AUTO_NET_CLOCK_B1", "layer": "Top Layer",
         "length_mm": 21.95, "width_mm": 0.2, "via_count": 0}
      ],
      "terminals": {"XG1.4": {"x_mm": 12.3, "y_mm": -8.1, "layer": "Top Layer",
                              "kind": "pad"}},
      "vias": [],
      "total_length_mm": 21.95
    }
  },
  "warnings": []
}
```

节点命名（需求文档 §2，**不用 `$`**）：

| 类型 | 名字 |
|---|---|
| 焊盘 | `DES.PIN` |
| 分支（T 型接头） | `__AUTO_<NET>_B<n>` |
| 悬空端点 | `__AUTO_<NET>_E<n>` |
| 过孔（每层各一） | `__AUTO_<NET>_V<n>` / `__AUTO_<NET>_V<n>_<LAYER>` |

## 3. 拓扑算法

1. **节点吸附**：坐标量化到 0.1 mil（`SNAP_MM = 0.0025`），key = (x, y, layer)。
2. **T 型接头**：一段走线的端点若落在另一段中间（同层、容差内），把后者**打断**。
   实测路由器不保证在分支点分段，这一步不能省。
3. **过孔不合并**：同坐标的上下层节点保持独立，过孔本身作为"待插入的 R/L"记录在
   `vias[]`（`nodes` 给出两端节点名）。合并会让过孔寄生无处安放。
4. **端子判定**：有焊盘 → `DES.PIN`；度 1 → `E<n>`；度 ≥3 → `B<n>`；过孔节点单列。
5. **路径压缩**：只在"显著节点"（焊盘/端点/分支/过孔）之间生成 segment，
   中间的 degree-2 节点全部合并；长度按实际折线累加。

## 4. 输出里的告警（都会出现在 manifest）

- `N 个走线端点没落在焊盘上`（测试点 / 铺铜连接 / 未完成布线）
- `不同网络名被压到同一节点`（ground_aliases 或命名冲突）

## 5. 已验证

| 项目 | 结果 |
|---|---|
| target_test（USB-C 小板，15 网络） | 层识别 2 层（LAYER 表里有 68 条定义，按"被引用"过滤）；+5V 4 段合计 31.2303 mm = 原始 14 条走线之和 |
| VBUS | 2 段 + 1 个换层过孔建模 + 18 个缝合孔正确跳过 |
| DN | 4 分支 T 型接头 + 2 个过孔，全部端点落在焊盘或分支上 |
