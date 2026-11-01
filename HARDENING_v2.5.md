# HARDENING_v2.5 — de-embedding / RLGC / 缓存 / IBIS clamp 使用说明

四个模块的共同点：**都能算，但都有明确的适用边界**。用错场景得到的是"看着合理的错数"，
所以每个都写了判据与拒绝条件。

---

## 1. IBIS clamp（`ibis_to_spice.py`）

> **v2.5.1 修正**：本节在 v2.5.0 里写错了自变量与符号，已按 IBIS 4.0 规范重写。
> 详细对照见 `CORRECTNESS_FIXES_v2.5.1.md` §1。

### 什么时候生效

IBIS 文件的 `[Model]` 里有 `[POWER Clamp]` / `[GND Clamp]` 段，且 `ibis_map.json`
里该引脚给出了 `model`。生成形如：

```
* POWER clamp：自变量 = Vcc − Vpin
B_IBIS_U5_X45_PCLAMP DQ VCC I = 0.095 + (-0.0319231)*uramp((V(VCC)-V(DQ))-(-6.6)) + ...
* GND clamp：自变量 = Vpin − Gnd
B_IBIS_U5_X45_NCLAMP DQ 0   I = -0.095 + (0.0319231)*uramp(V(DQ)-(-6.6)) + ...
```

### 数学约定（写错就全错）

```
自变量：
  [POWER Clamp]  Vtable = V(ref) − V(pin)      ← 规范：Vtable = Vcc − Voutput
  [GND Clamp]    Vtable = V(pin) − V(gnd)
符号：
  表里的符号就是方向（"positive = into the component"），**不要再统一取负**
  POWER 导通区（Vtable<0）为正、GND 导通区（Vtable<0）为负 —— 两者相反
折线：
  I(v) = I(v0) + Σ_k (s_k − s_{k−1})·uramp(v − v_start_k)
```

- 系数必须是**斜率增量**，折点必须是**线段起点**；
- 分段线性表非单调时（真实 IBIS 常见）这两点尤其重要；
- 系数按 9 位有效数字落盘，近阈值处误差 ~0.02 nA。

### 参考电压怎么定

`resolve_references()` 的优先级（IBIS 允许用不同 rail 驱动同一个模型）：

| 侧 | 优先级 |
|---|---|
| POWER | `[POWER Clamp Reference]` → `[Pullup Reference]` → `[Voltage Range]` |
| GND | `[GND Clamp Reference]` → `[Pulldown Reference]` → 0 V |

都没有时用映射里的 `vcc` 兜底并 warning；`strict` 下抛 `IBIS_REFERENCE_UNRESOLVED`，
**拒绝猜**。

### 接到哪个节点

| 优先级 | 条件 | 说明 |
|---|---|---|
| 1 | 映射里写了 `ref_vcc` | 最可靠，直接就是网络名 |
| 2 | IBIS `[Pin]` 里标成 `POWER` 的引脚有网络 | 夹位电流真正流回板上电源 |
| 3 | 都没有 | 造一个只有这颗器件用的理想源，并在 warnings 里说明 |

若 POWER 脚映射到的网络和信号脚相同（映射错误），会放弃自动路径并退回 3，避免 `B n n`。

### 关闭与 A/B

```json
{"designator": "U5", "pin": "45", "ibis_file": "x.ibs", "clamps": false}
```

开着和关着各跑一次，比较 `tran_stats.max` 是验证 clamp 是否真的起作用的最快办法。

### 精度边界

clamp 是**纯二极管夹位**：不含 `[Series Model]`、不含温度系数、不含 clamp 的封装寄生。
用 typ corner（`i_typ`）。它能改善过冲/下冲的量级，不能替代器件级 ESD 模型。
表外的自变量按**端点值保持**（不平推），所以用真实 IBIS 时确认表的覆盖范围是
−Vcc..+2Vcc（规范要求，但厂商文件不一定遵守）。

---

## 2. de-embedding（`deembed.py`）

```bash
python scripts/deembed.py --dut dut.s2p --open open.s2p --short short.s2p \
  --method open_short --out ./de --name board
```

产出 `board_deembedded.s2p` + `DEEMBED_MANIFEST.json`（方法、类名、每个 fixture 的 sha256、频段）。

### 方法矩阵

| method | skrf 类 | fixture role | 前提 |
|---|---|---|---|
| `open` | `Open` | open | 夹具只有并联寄生（pad 电容） |
| `short` | `Short` | short | 夹具只有串联寄生 |
| `open_short` | `OpenShort` | open + short | 串联寄生靠 DUT、并联寄生在外侧 |
| `short_open` | `ShortOpen` | short + open | 顺序相反 |
| `split_pi` / `split_tee` | `SplitPi` / `SplitTee` | thru | 夹具对称可分半 |

### 三条硬约束（会直接报错）

- `DEEMBED_PORT_MISMATCH`：fixture 端口数 ≠ DUT 端口数；
- `DEEMBED_FREQ_GRID_MISMATCH`：频率网格**逐点**不一致（v2.5.1 起不再只看首尾）。
  默认拒绝，先重采到同一网格；显式 `--interpolate-fixtures` 才用
  `skrf.Network.resample` 重采，并把 before/after 网格与 `resampled_roles` 写进 manifest；
- `DEEMBED_FIXTURE_MISSING`：方法需要的 role 没给全。

### 必须知道的局限

`OpenShort` 先做 `Y −= Yopen`，剩下的纯串联网络 Y 矩阵**数学上奇异**（det=0），
再对它取 `.z` 等于对奇异矩阵求逆 → 条件数爆炸。也就是说：

- `Open` 在合成数据上可证明精确（Y 相加/相减是精确的）；
- `OpenShort` 的精度**完全取决于实测假件里残留的寄生**，不能指望解析验证。

要更高精度的夹具去除，用 TRL / 2x-thru（`IEEEP370*`，skrf 里已有，本 skill 未封装）。

---

## 3. RLGC 有损线（`rlgc.py`）

> **v2.5.1 修正**：v2.5.0 的 L/C 少了 10³，且每段把 R 与 L 并联（直流时电感旁路铜阻）。
> 见 `CORRECTNESS_FIXES_v2.5.1.md` §2。

```bash
python scripts/rlgc.py --stackup stackup.json --layer L1 \
  --width 0.2 --length 30 --nseg 20 --n1 IN --n2 OUT --out tl.cir
```

模型（**常数元件阶梯**，不是频率相关网络）：

```
L = Z0·td / ℓ        （ℓ 用**米**！用毫米会得到 H/mm，再乘长度就少 10³）
C = td / (Z0·ℓ)
R = Rdc = ρ·ℓ/(w·t)  （低频/直流正确；Rac·√f 未实现）
G = 2πf_ref·C·tanδ   （取 f_ref 处的常数）
```

每段的拓扑（**串联**）：

```
cur ── R ── mid ── L ── next
                        ├── Rg(=1/G) ── 0
                        └── C ── 0
```

30 mm / 0.2 mm 走线的参考量级：`td = 178 ps`、`L = 11.12 nH`、`C = 2.854 pF`、
`√(L·C) = 178 ps`（必须等于 td）。

### 这两条要记住

1. **并联电导用 `R`，不要用 `G`。** ngspice 的 `G` 是四端 VCCS
   （`G1 n+ n- nc+ nc- value`），两端写法会直接报错。
2. **串联支路必须 R 与 L 分开两个节点。** `R cur next` + `L cur next` 是 R∥L，
   直流时电感把铜阻彻底短路；DC 回归的容差要收到 `1e-5` 才抓得住。

### 精度边界

- 标 `RLGC_CONSTANT_AT_FREF`：R/G 不随频率变化，`at_frequency()` 给的 R(f)/G(f) 只是
  报告值，**不进网表**；宽带拟合请走 vector fitting；
- `Z0` 来自 Hammerstad-Jensen / IPC-2141 解析式（±10% 量级）；
- `Rac·√f` 未实现，所以高频损耗会被低估。

所以 RLGC 阶梯适合"看损耗趋势 / 眼图张开度量级"，不适合阻抗控制验收。

---

## 4. 缓存与增量重跑（`sim_cache.py`）

```bash
bash sim_file.sh board.epro2 tran "10p 120n" "" ./out --config sim.json --cache ./cache
```

或在 config 里：`{"cache": {"dir": "./cache"}}`。`--no-cache` 强制不吃。

### key 由什么决定

```
sha256( deck + config + 求解器版本 + **所有外部依赖文件的哈希** + 源工程哈希 )
```

依赖文件由 `sim_cache.collect_deps()` 收集（v2.5.1）：deck 里的
`.include/.inc/.lib`、`model_map.json` 引用的 `.lib`、`behavioral_models`、
`ibis` 及其 `.ibs`、`override` 里的 `.sNp`、`stackup`。逐条的
`{path, sha256}` 记在 `cache_meta.json` 里。

v2.5.0 的 `build_key(model_files=...)` 虽然支持，但 `_run_single()` **没传** ——
于是改了 `vendor.lib` 内容、deck 没变时会命中旧结果。这是最典型的一类
stale cache，现在有专门回归（`test_cache_key_includes_included_model_files`）。

**任何一项变了就 miss** —— 宁可重算，也不拿上一次的结果凑数。

### 命中时会发生什么

`materialize()` 把缓存里的 `raw / csv / metrics / stdout / stderr` 复制回 run 目录，
所以"cache hit"与一次正常 run 的产物等价（v2.5.0 只写了一个 `metrics.json`）。
manifest 里记 `cache: {key, hit, root}`，`load()` 返回的
`materialized_files` 也会进 run 记录。

### 命中判据

`cache_meta.json` 存在。它是 `store()` 的**最后一步**，相当于"这条记录完整落盘"的标记。
（早期版本用"目录里有 raw 文件"判断，导致 `op` 这种不产 raw 的分析永远 miss。）

### 不要用它做什么

- 不要跨求解器版本复用（key 里已含版本，但仍建议清缓存升级）；
- 缓存里只有产物，**没有** deck 的语义解释，别把缓存当归档；
- 不要在缓存目录里手改文件 —— key 不会变，下次命中就拿到你改过的脏数据。

---

## 5. 验证等级怎么自查

```bash
python scripts/validation.py          # 校验，等级谎报会返回非 0
python scripts/validation.py --write  # 重生成 VALIDATION_STATUS.md
```

等级定义见 `VALIDATION_STATUS.md` 顶部或 `validation.py` 的 docstring。
`CORRELATED` 要求 `source=instrument` + `sample=OUT_OF_SAMPLE` + 仪器名 + 数据文件路径，
少一条都上不去。
