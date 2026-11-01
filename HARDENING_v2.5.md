# HARDENING_v2.5 — de-embedding / RLGC / 缓存 / IBIS clamp 使用说明

四个模块的共同点：**都能算，但都有明确的适用边界**。用错场景得到的是"看着合理的错数"，
所以每个都写了判据与拒绝条件。

---

## 1. IBIS clamp（`ibis_to_spice.py`）

### 什么时候生效

IBIS 文件的 `[Model]` 里有 `[POWER Clamp]` / `[GND Clamp]` 段，且 `ibis_map.json`
里该引脚给出了 `model`。生成形如：

```
B_IBIS_U5_X45_PCLAMP DQ VCC I = (0.00333333)*uramp(V(DQ)-(3.3)) + (0.015)*uramp(V(DQ)-(3.6)) + ...
B_IBIS_U5_X45_NCLAMP DQ 0   I = -0.095 + (0.0461111)*uramp(V(DQ)-(-6)) + ...
```

### 数学约定（写错就全错）

```
expr = −I_ibis(V)                         # 因为 B 源的 I 是"从 n+ 流向 n−"
I(v) = I(v0) + Σ_k (s_k − s_{k−1})·uramp(v − v_start_k)
```
- `s_k` 是第 k 段的斜率，**系数必须是斜率增量**；
- 折点必须是**线段起点**；
- 分段线性表非单调时（真实 IBIS 常见）这两点尤其重要。

### 电源参考怎么定

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
- `DEEMBED_FREQ_MISMATCH`：频率轴首尾不一致（**不会**自动插值，先重采到同一网格）；
- `DEEMBED_FIXTURE_MISSING`：方法需要的 role 没给全。

### 必须知道的局限

`OpenShort` 先做 `Y −= Yopen`，剩下的纯串联网络 Y 矩阵**数学上奇异**（det=0），
再对它取 `.z` 等于对奇异矩阵求逆 → 条件数爆炸。也就是说：

- `Open` 在合成数据上可证明精确（Y 相加/相减是精确的）；
- `OpenShort` 的精度**完全取决于实测假件里残留的寄生**，不能指望解析验证。

要更高精度的夹具去除，用 TRL / 2x-thru（`IEEEP370*`，skrf 里已有，本 skill 未封装）。

---

## 3. RLGC 有损线（`rlgc.py`）

```bash
python scripts/rlgc.py --stackup stackup.json --layer L1 \
  --width 0.2 --length 30 --nseg 20 --n1 IN --n2 OUT --out tl.cir
```

模型：

```
R(f) = Rdc + Rac·√f          Rdc = ρ·l/(w·t)      （趋肤用 √f 近似）
L, C  = 由 Z0 与 td 反推      （Z0 来自叠层的解析式）
G(f) = 2πf·C·tanδ            （介质损耗）
```

切成 `nseg` 段，每段：串联 `R + L`，并联 `R(1/G) + C`。

### 这条要记住

**并联电导用 `R`，不要用 `G`。** ngspice 的 `G` 是四端 VCCS（`G1 n+ n- nc+ nc- value`），
两端写法会直接报错。`rlgc.emit()` 已有测试约束（元素首字母只能是 R/L/C）。

### 精度边界

- `√f` 是趋肤效应的**工程近似**，不是 Wheeler 或 Hammerstad 的宽带修正；
- `G` 用 1 GHz 的常数，不随频率变化（宽带 S 参数拟合请走 vector fitting）；
- `Z0` 本身来自 Hammerstad-Jensen / IPC-2141 解析式（±10% 量级）。

所以 RLGC 阶梯适合"看损耗趋势 / 眼图张开度量级"，不适合阻抗控制验收。

---

## 4. 缓存与增量重跑（`sim_cache.py`）

```bash
bash sim_file.sh board.epro2 tran "10p 120n" "" ./out --config sim.json --cache ./cache
```

或在 config 里：`{"cache": {"dir": "./cache"}}`。`--no-cache` 强制不吃。

### key 由什么决定

```
sha256( deck + config + 求解器版本 + 各模型文件哈希 + 源工程哈希 )
```

**任何一项变了就 miss** —— 宁可重算，也不拿上一次的结果凑数。这就是
`incremental_plan()` 里 `_INC_KEY_FIELDS` 的语义。

### 命中判据

`cache_meta.json` 存在。它是 `store()` 的**最后一步**，相当于"这条记录完整落盘"的标记。
（早期版本用"目录里有 raw 文件"判断，导致 `op` 这种不产 raw 的分析永远 miss。）

### 不要用它做什么

- 不要跨求解器版本复用（key 里已含版本，但仍建议清缓存升级）；
- 不要在改了 `.include` 的外部模型但没更新文件路径时指望它发现（key 里存的是文件哈希，
  所以改了文件就会 miss —— 这条是对的）；
- 缓存里只有 raw/metrics/manifest，**没有** deck 的语义解释，别把缓存当归档。

---

## 5. 验证等级怎么自查

```bash
python scripts/validation.py          # 校验，等级谎报会返回非 0
python scripts/validation.py --write  # 重生成 VALIDATION_STATUS.md
```

等级定义见 `VALIDATION_STATUS.md` 顶部或 `validation.py` 的 docstring。
`CORRELATED` 要求 `source=instrument` + `sample=OUT_OF_SAMPLE` + 仪器名 + 数据文件路径，
少一条都上不去。
