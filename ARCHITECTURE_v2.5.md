# ARCHITECTURE v2.5

v2.5 不新增物理模型种类，做的是**把已有能力标定清楚 + 把四块空壳填实**。

```
┌──────────────────────────────────────────────────────────────────────────┐
│  边界（v2.2 起不变）                                                     │
│  EasyEDA = CAD 数据源      ngspice = 仿真引擎      本 skill = 编排层      │
│  任何 batch 仿真不需要打开 EDA、不需要切仿真页                            │
└──────────────────────────────────────────────────────────────────────────┘
```

## 1. 新增/改动的层次

```
                      ┌──────────────────────────────┐
   validation_registry.json ──► validation.py ──► VALIDATION_STATUS.md
                      │  (32 项 × 等级 × 证据)         ▲
                      │                                │ 机器校验，谎报就 fail
                      └──────────► tests/test_validation.py
                                                   
   数据 ──► 拓扑 ──► 模型 ──► 寄生 ──► deck ──► 求解 ──► 指标 ──► manifest
                     │         │        │        │                    │
              ibis clamp   rlgc 阶梯   sim_cache（内容哈希）      cache{key,hit}
                     │         │
                 pcb_em ──► em_coupon.py（EXPERIMENTAL，外部求解）

   测量 ──► deembed ──► 干净 .sNp ──► rf_network ──► vector_fit ──► ngspice
```

## 2. 验证等级的机制（v2.5 的核心）

问题不是"表格写错了"，而是"表格可以随手改"。所以：

| 层 | 内容 | 谁能改 |
|---|---|---|
| 数据 | `validation_registry.json`：32 项，每项 `{status, evidence[], note}` | 人（提交时） |
| 规则 | `validation.EVIDENCE_POWER`：等级 ↔ 允许的证据类型 | 人（改规则要慎重） |
| 校验 | `check_entry()` / `check_all()`，跑在 pytest 里 | 机器 |
| 渲染 | `render_markdown()` → `VALIDATION_STATUS.md` | 脚本（`--write`） |

`_measurement_ok()` 是唯一的 `CORRELATED` 入口，判据：

```python
source == "instrument" and sample == "OUT_OF_SAMPLE" and instrument and file
```

合成数据、拟合内数据、与另一工具一致 —— 一律不算。这就是为什么当前
`CORRELATED 0 / QUALIFIED 0`：本机没有仪器数据。

## 3. IBIS clamp 的数据流

```
.ibs ─[ibis_parser]─► models[..].power_clamp / gnd_clamp
                           │
     ibis_model.resolve ◄──┘   （v2.4 这里漏传了两个字段 → clamp 一直没生效）
              │
     ibis_to_spice._emit_clamps
              │  expr = −I_ibis(V)，用 uramp 拼 PWL
              ▼
     B n+ n−  I = ...        n+ = 信号节点
                             n− = ref_vcc / IBIS POWER 脚网络 / 独立理想源
```

关键不变量：
- 系数是斜率**增量**，折点是线段**起点**（否则 I-V 曲线整体错位，v2.5 两个 bug 都在这里）；
- `B n n` 自环禁止（POWER 脚误映射到信号网时自动退回独立源）。

## 4. EM 几何（v2.5 重写）

```
pcb_geometry ─► geo{nets:{terminals, segments{layer,width_mm}}}
stackup      ─► layers[copper_um] + dielectrics[thickness, er, tanδ]
                     │
        pcb_em._pick_ports：端口 z = 走线层 z → 相邻铜层 z
                     │
        em_bridge.segment_polygon：中心线 ± w/2 法向偏移 → 4 顶点
        em_bridge.layer_stack   ：每层铜 z 区间 + 每层介质 z 区间
                     │
                em_coupon.py（CSXCAD.Polygon + AddPoint + SetElevation）
                     │
                [外部 openEMS 求解] ─► .sNp ─► rf_network
```

不带求解器、不带网格加密；`EM_PLAN.json` 里的 `limitations` 是给人和 Agent 都看的。

## 5. de-embedding / RLGC / cache 的位置

| 模块 | 在链路中的位置 | 不做什么 |
|---|---|---|
| `deembed.py` | 测量之后、`rf_network` 之前 | 不做网络运算（全交给 skrf），不自动重采频率轴 |
| `rlgc.py` | `trace_extract` 的有损替代 | 不做宽带拟合（那是 vector fitting 的活） |
| `sim_cache.py` | `sim_exec` 前后 | 不跨求解器版本复用，不缓存 deck 语义 |

## 6. 失败策略（延续 v2.2 的"宁可失败"）

| 情况 | 行为 |
|---|---|
| 叠层缺铜层/介质厚度 | `STACKUP_INCOMPLETE`，不默认 FR4 |
| 未知 mode | `sim_file.sh` 立刻退出并列出支持的模式 |
| fixture 端口/频率不一致 | `DEEMBED_*_MISMATCH`，不插值 |
| passivity enforcement 修坏模型 | 回滚 + `rejected`（v2.4 起） |
| 验证等级缺证据 | `validation.py` 返回非 0，pytest fail |

## 7. 依赖分层（不变）

```
requirements-core.txt   numpy / scipy
requirements-rf.txt     scikit-rf        （没装 → RF 功能明确报 RF_BACKEND_UNAVAILABLE）
requirements-em.txt     openEMS/CSXCAD   （只提示，永不自动调用）
```

v2.5 新增模块只依赖 core（`rlgc`/`sim_cache`/`validation` 全是纯标准库 + math），
`deembed` 依赖 RF 层，`em_bridge` 只生成脚本、零依赖。
