# CHANGELOG v2.5

`easyeda-ngspice-sim` 2.4.0 → 2.5.0

主题：**Validation & Field Hardening** —— 修正"可信度元数据被高估"，
并把四块"名字有、实质没有"的东西做成真的（IBIS clamp / EM 几何 / de-embedding / 有损线 / 缓存）。

本轮**没有**新增物理模型种类，主要是在补 v2.4 欠下的账。

---

## 0. 起因：一份云端审核报告

审核指出三件事，全部接受：

1. `VALIDATION_STATUS.md` 里 `CORRELATED` / `QUALIFIED` 被用在了「与原始 CAD 数据一致」
   和「与 ngspice 复算一致」上，而文档自己定义 `CORRELATED` = 与**实测数据**一致。
   这是**元数据 bug**，不是文案问题。
2. `em_bridge` 的 `AddBox(x1,y1 → x2,y2)` 对斜线、水平线、线宽、多层介质都是错的。
3. v2.5 优先级应为：taxonomy 修正 → EM 几何 → 真测量相关 → de-embedding → clamp/RLGC/cache → eye。

## 1. P0-A 验证等级机器化（本轮最重要）

| 动作 | 结果 |
|---|---|
| 新增 `validation_registry.json` | 32 项功能 × {等级, 证据[], note}，证据类型限定 6 种 |
| 新增 `scripts/validation.py` | 校验"等级是否被证据支持"；`--write` 生成 `VALIDATION_STATUS.md` |
| `VALIDATION_STATUS.md` | **改为脚本生成，禁止手改表格** |
| 新增 `tests/test_validation.py`（8 项） | 注册表自检跑在 pytest 里，等级谎报会直接 fail |

具体降级（全部是"原来写高了"）：

| 功能 | v2.4 声称 | v2.5 实际 | 理由 |
|---|---|---|---|
| 原理图 IR / 连通性 | CORRELATED | `CROSS_CHECKED` | 两个独立前端一致 ≠ 与实物一致 |
| PCB 几何提取 | CORRELATED | `CROSS_CHECKED` | 与 CAD 源文件一致，无实物测量 |
| Vector fit round-trip | CORRELATED | `CROSS_CHECKED` | 与 ngspice 复算一致，属 cross_tool |
| 外部模型 pin mapping | CORRELATED | `CROSS_CHECKED` | 同上 |
| 求解器 ngspice | QUALIFIED | `CROSS_CHECKED` | QUALIFIED 需实测背书，本机没有 |

当前分布：**`CROSS_CHECKED 15 / TESTED 16 / EXPERIMENTAL 1`，`CORRELATED` 与 `QUALIFIED` 各 0 项。**

> 这不是"降级就完事"：`_measurement_ok()` 的判据是
> `source=instrument` + `sample=OUT_OF_SAMPLE` + 必须有 instrument 名与数据文件路径。
> 少任何一条，等级就上不去 —— 从机制上堵住以后再被高估。

## 2. P0-B openEMS 几何真实化

`em_bridge.py` 重写铜与介质的生成方式（详见 `OPENEMS_BRIDGE.md`）：

- 新增 `segment_polygon(a, b, width)`：中心线沿法向 ±w/2 偏移 → 4 顶点旋转矩形；
- 新增 `polygon_area(poly)`：鞋带公式，用于**自检**（面积必须 = 长度 × 线宽）；
- 新增 `layer_stack(stackup)`：每层铜的 z 区间 + 每层介质的 z 区间（不再压成单 slab）；
- 铜改用 `CSXCAD.Polygon + AddPoint + SetElevation`（`make_polygon()` 兼容两种 API 写法）；
- 介质逐层 `AddMaterial(epsilon, kappa)` + `AddBox`，`kappa` 由 tanδ 换算；
- 端口 z 改用**各自走线层**的 z，另一侧接**相邻铜层**（参考平面）；
- 新增 `scripts/pcb_em.py`：`.epro2|geometry.json → em/em_coupon.py + em/EM_PLAN.json`；
- `sim_file.sh` 增加 `em` 模式（`--net / --fmax / --margin / --em-out`）。

实测（合成 coupon，2 层板，CLK_N 是 39.6 mm 的 45° 斜线）：

| 量 | v2.4（AddBox） | v2.5（polygon） |
|---|---|---|
| 45° 走线面积 | 795 mm²（28.2×28.2 外包框） | **5.94 mm²** = 39.6 × 0.15 ✓ |
| 水平走线 | 零厚度 | 6 mm² = 30 × 0.2 ✓ |
| 介质层数 | 1（压平） | 3（逐层） |
| 端口 z | 全部顶层 | P1 `0→1.565`（L1 走线）、P2 `1.6→0.035`（L2 走线） |

仍未做：POUR 铺铜、thirds-rule 网格、3D 过孔孔壁。**状态仍为 `EXPERIMENTAL`，从未跑过求解。**

## 3. P0-C 测量 correlation / P0-D de-embedding

- `deembed.py`：封装 `skrf.calibration.deembedding`（Open/Short/OpenShort/ShortOpen/SplitPi/SplitTee），
  带 fixture 端口数/频率轴一致性检查、`DEEMBED_MANIFEST.json`（含每个 fixture 的 sha256）、独立 CLI。
  **修了一个真 bug**：skrf 的构造参数第一个位置是 dummy 不是 DUT，v2.5 之前那条路径会把
  DUT 当成 open 假件，减完直接得 0（测试里表现为 S≡I）。
- 测量相关：无真实仪器数据 → 相关功能保持 `TESTED`，**没有**升级成 `CORRELATED`。
  这是刻意的：`test_validation.py` 会拦住任何没有 `instrument` 证据的 `CORRELATED`。

合成数据上的精确性验证（`tests/test_v25_hardening.py`）：

| 用例 | 结果 |
|---|---|
| `Open` 反嵌入（pad = 并联 C，DUT = 串联 2·Zlead+Zdut） | Y 矩阵回复误差 **< 1e-6 相对** |
| `OpenShort` | 只验接口/manifest/数值有限 —— 因为减完 Y 后矩阵**数学上奇异**，`.z` 条件数爆炸，要求精度的测试是自欺欺人 |

## 4. P1-A IBIS clamp → 真 PWL 电流源

`[POWER Clamp]` / `[GND Clamp]` 从"解析了但没用"变成真的进网表：

- `ibis_parser` 补 `mode_owner`；`ibis_model.resolve` 的 spec 补 `power_clamp/gnd_clamp`
  （v2.4 里这两个字段压根没传出来，等于 clamp 一直没生效）；
- `ibis_to_spice._emit_clamps()`：`B n+ n- I = −I_ibis(V)`，用 `uramp()` 拼分段线性；
- 电源参考优先级：`ref_vcc` 显式 > IBIS `POWER` 引脚所在网络（自动） > 独立理想源（带防自环）；
- 配置项 `clamps: false` 可整体关闭（A/B 对照用）。

**两处数学 bug（都是本轮自己写错又自己测出来的）**：

1. uramp 的折点写成了线段**终点**，应该是**起点** → `−5.9 V` 处电流被钉死在 95 mA；
2. 系数写成了 `slope_k`，而 uramp 是**累加**的，必须写 `slope_k − slope_{k−1}`
   → 4.2 V 处算出 −14 mA（表里 −12 mA）。

修完之后 DC 扫描 −6…+6 V 逐点对比 IBIS 表：**worst |err| ≈ 7e-7 A（0.7 µA）**。
过冲 A/B：0.2 A 注入 50 Ω，无 clamp 峰值 10.0 V，开 clamp 后 < 7.0 V（断言 >30% 抑制）。

## 5. P1-B RLGC / 有损线

`rlgc.py`：由叠层 + 线宽 + 长度推出 R(f)=Rdc+Rac√f、L、C、G=2πfC·tanδ，
切成 N 段 R+L 串联 / R(1/G)+C 并联的阶梯，可直接进 ngspice。

- 自洽性：`√(L/C) == Z0`（相对 1e-6）、`Rdc == ρl/(wt)`、G 随 f 线性、R 随 √f 增；
- **避坑**：并联电导必须落成 `R = 1/G`，不能写 `G`（ngspice 的 `G` 是四端 VCCS，
  两端写法直接报错）。测试用 `ln[0] in "RLC"` 硬性约束；
- 直流行为实测：`V(OUT) = 1V × 50/(Rdc+50)`，ngspice op 复算相对误差 < 0.2%。

## 6. P1-C 缓存 / 增量重跑

`sim_cache.py`：`key = sha256(deck + config + 求解器版本 + 模型文件 + 源工程哈希)`，
按内容寻址存 `metrics.json` / raw / `cache_meta.json`。

- `sim_exec.py --cache <dir>`（或 `config.cache.dir`）opt-in，`--no-cache` 强制不吃；
- 命中判据 = `cache_meta.json` 存在（它是 store 的**最后一步**，相当于完成标记；
  早期版本用"有 raw 文件"判断，导致无 raw 的 `op` 永远 miss，本轮修掉）；
- manifest 增加 `cache: {key, hit, root}`，报告里能看到这次到底跑没跑；
- `incremental_plan()` 对比两轮 key 的构成，报"哪些 study 可复用、哪一项变了"。

实测：同一输入第二次运行打印 `[cache] HIT`，manifest `cache.hit = true`，skip ngspice。

## 7. 兼容性与测试

- v2.4 的 175 项全部继续通过；现在共 **208 项，28.22 s**。
- 新增 `tests/test_v25_hardening.py`（25 项）。
- 破坏性变更**没有**，但有两处行为收紧：
  1. `sim_file.sh` 现在**拒绝未知 mode**（以前会把 `rf`/`si` 这类不存在的模式
     传给 `sim_exec` 后报"deck 生成失败"，看起来像仿真出错）；
  2. `em_bridge.layer_stack()` 要求叠层同时有 `layers` 和 `dielectrics`，
     只给介质会在摆铜层时无法确定 z → `STACKUP_INCOMPLETE`（v2.4 那个测试夹具
     本身就是缺铜层的，已修正）。
- `SKILL_VERSION` → `2.5.0`；manifest 里的版本号测试改为对比 `SKILL_VERSION`，不再硬编码。

## 8. 文档纠错（属于本轮目标）

v2.4 的 `SKILL.md` 里有一批**跑不通的 CLI**：`sim_file.sh … rf` / `si` / `correlate` /
`calibrate`，以及 `--network / --backend / --diff-pair / --measurement / --fit / --solver`
这些从没在 `_opts.sh` 里注册过的参数。v2.5 全部改成
"**Python API**"并附上真实可用代码，同时给 `sim_file.sh` 加了模式白名单硬失败。

这类"文档说有、代码没有"和等级高估是同一个病，所以放在同一轮修。

## 9. 明确未做

- 真实仪器测量相关（本机没有示波器/VNA 数据）→ `CORRELATED` 保持 0 项；
- POUR 铺铜展开、thirds-rule 网格加密、3D 过孔 —— EM 仍是 `EXPERIMENTAL`；
- eye diagram（P2）；
- 频率相关 εr / 宽带介质模型；
- openEMS 实际求解（GPL 边界，永远交给外部进程）。
