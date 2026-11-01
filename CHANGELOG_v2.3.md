# CHANGELOG v2.3

`easyeda-ngspice-sim` 2.2.0 → 2.3.0

v2.3 的主题：**让 PCB 自己说话**——从真实布线自动提参、用 IBIS/实测 S 参数替代手填，
并且把批量仿真的执行权明确交给外部 ngspice。

---

## 0. 架构边界（本轮最重要的一条）

```
EasyEDA = CAD 数据源    ngspice = 仿真引擎    Skill = 编排层
```

**所有 batch 仿真不再依赖嘉立创仿真页。** 原来的在线链路里 `easyeda-bridge` 无法
自动切到 simulation document，导致每次仿真前都要人工点一下——sweep / Monte Carlo /
A-B 对比就全都失去自动化意义。v2.3 起：

- 提取（schematic IR + PCB geometry + stackup）走 EDA / 文件；
- 构建、求解、扫描、后处理、报告 **全部在外部**完成；
- 嘉立创仿真页保留为**可选可视化前端**（写回能力仍在，但不在主链路）。

新增硬性测试 `HEADLESS_EXTERNAL_SIMULATION`：EDA 停在 PCB 页、不允许切仿真页，
仍须完成 extract → deck → ngspice → metrics → report。

## 1. 新增能力

### P0-A PCB 几何提取（`pcb_geometry.py`）

从 `.epro2` 的 `docType=PCB` 文档提取 trace / layer / width / length / via / pad /
endpoint / branch / net topology，输出 `PCB_GEOMETRY.json`。

实测格式（3 个真实工程验证）：`LINE{netName,layerId,startX..,width}`、
`VIA{netName,centerX..,holeDiameter,viaDiameter,viaType}`、`LAYER`、`LAYER_PHYS`、
`COMPONENT`、`ATTR(Designator/Footprint)`、`PAD_NET`（id 里带 compId+padNum）、
`POUR/POURED`。**坐标与线宽单位是 mil**，与 `CANVAS.unit`（显示单位，可能是 mm）无关。

拓扑处理：
- 端点吸附（0.1 mil）+ **T 型接头自动打断**；
- 焊盘 → `DES.PIN` 端点（实测 C1 pad2 与走线端点对齐到 0.02 mil）；
- 分支点 → `__AUTO_<NET>_B<n>`；悬空端 → `_E<n>`；过孔 → `_V<n>`（每层各一个节点）；
- 长度与原始走线逐段一致（验证：+5V 4 段合计 31.2303 mm = 原始 14 条走线之和）。

### P0-B 叠层与 Z0/TD（`stackup.py` / `trace_extract.py` / `via_extract.py`）

- `stackup.json` 手工定义；也可从 PCB 的 `LAYER_PHYS` **部分提取**（实测该字段只有
  介质层有数据，铜厚常为 0/null → 判 `STACKUP_INCOMPLETE`）。
- 支持"只补缺"：用户 stackup 只写铜厚，εr/介质厚度沿用 PCB 里的值。
- 微带（Hammerstad-Jensen，含铜厚修正）/ 带状线（IPC-2141）解析式，标
  `ANALYTIC_ESTIMATE`。
- 过孔 R/L/C 经验式（L(nH)=0.2·h·[ln(4h/d)+1]，C(pF)=0.0555·εr·T·D1/(D2−D1)），
  标 `ENGINEERING_ESTIMATE`，反焊盘按"焊盘+间隙"假设并写进 `assumptions`。

### P0-C 自动生成 override（`pcb_to_override.py`）

```bash
pcb_to_override.py Main.epro2 --net NET-CLOCK --stackup stackup.json --mode tline
```

输出可直接被 v2.2 的 override 消费：T 线（z0/td 来自叠层）、via 的 series_rl、
`__AUTO_` 虚拟节点、`derivation` 里带完整推导链。

护栏：
- 铺铜缝合孔（两端没有走线）**不建模**，并说明原因（否则会造成悬空节点 → 奇异矩阵）；
- 端点悬空的 T 线（开路短桩）逐个告警；
- 缺叠层 → 直接 `STACKUP_INCOMPLETE`，不会默认 FR4。

### P0-D IBIS（`ibis_parser.py` / `ibis_model.py` / `ibis_to_spice.py`）

解析 `[Component]/[Pin]/[Model]/[Pullup]/[Pulldown]/[POWER|GND Clamp]/[Ramp]/
[Rising|Falling Waveform]/C_comp/C_pkg/R_pkg/L_pkg`，转成行为级等效：

- 输入：`node — L_pkg — R_pkg — C_comp — gnd`
- 输出：由 IV 曲线反推 Rout（Vout=Vcc/2 处），由 Ramp（20–80%）或波形（10–90%）
  折算 0–100% 边沿，生成 `V_PULSE + R_out + C_comp`
- 一律标 `IBIS_DERIVED_BEHAVIORAL_MODEL`，manifest 记 IBIS 文件 sha256
- 层次规则：引脚级 `C_pin/R_pin/L_pin` 优先于 `[Package]`

### P0-E Touchstone（`touchstone.py` / `sparam_element.py` / `sparam_metrics.py`）

- 解析 `.s1p/.s2p/.sNp`，RI/MA/DB 三种格式，Hz/kHz/MHz/GHz 四种频率单位（实测
  MA/Hz 与 DB/MHz 两种写法结果差 ≤ 5×10⁻⁵ dB）。
- 指标：插损、回损、−3 dB 带宽、谐振、陷波、峰值衰减。
- **adapter 层**（因为 ngspice-47 的 `S` 元件实测不可用）：
  1. `native` — 探测到 `S` 元件可用就直接用；
  2. `lumped`（默认）— 从 S 参数提取集总等效：
     1 端口 Z=z0(1+S11)/(1−S11)，候选拓扑 `series_rl / series_rlc / shunt_rl /
     Rs+(Rp‖Lp)`；2 端口 π 与 T 各拟一次取误差小者；
     全部闭式最小二乘（R 与 L、G 与 C 在实部/虚部上解耦），不做迭代优化；
  3. `metrics_only` — 只用数据，不接进电路。
- 拟合质量必须回报（max/RMS dB 误差），超阈值拒绝接入（`SPARAM_FIT_TOO_POOR`）。
- **低频护栏**：串联 R 型拓扑在"器件直流阻抗很低"时会被判为不可用，避免把磁珠的
  供电通道堵死（这是实测踩到的坑）。

### P0-F FFT / 频谱（`fft_analysis.py` / `spectral_metrics.py`）

- 窗函数 rect / hann / blackman-harris，幅度按 `sum(w)` 归一化（单音读出原幅度）；
- 单位纪律：`dBV` 只需电压；`dBFS` 需 full-scale；`dBc` 需基波；**`dBm`/`dBm/Hz`
  必须给阻抗**，否则直接拒绝（任意 SPICE 电压频谱不能标 dBm）；
- 谐波自动匹配（期望频率 / 最近峰 / dBc / 偏移）、互调候选 `|n·f1 ± m·f2|` 与
  实测峰匹配（SDR birdie 定位）。

### P0-G 多端口 PDN（`pdn_multiport.py`）

用 ngspice 的 `.sp` 分析 + `portnum`/`z0` 端口源（实测可用）直接得到 Z 矩阵：
`Z11 / Z22 / Z12 / Z21`、谐振、反谐振。

- 端口定义 `{"ports":[{"name","node","return"}]}`，至少 2 个；
- 会主动检查"rail 被理想电压源直接短接"（Z11 恒为 0 的无意义结果）；
- 输出带 `simulation result, verify with measurement` 提示。

### P0-H Monte Carlo + Corner（`monte_carlo.py`）

- 分布：`gaussian`（sigma_percent，可 clip）、`uniform`（tolerance_percent 或 min/max）、
  `discrete`（values + weights）；
- `random.Random(seed)` → 同 seed 同结果（有测试）；
- 并行 `workers = min(CPU, 8)`，可禁用；每个 case 独立目录；
- 统计：mean/std/p5/p50/p95/min/max；判据失败率 `failure_rate`；
- Corner：`{"VIN":[...],"TEMP":[...],"LOAD":[...]}` 笛卡尔积，`TEMP` 走 `.temp`；
- `max_cases` 硬上限（MC 默认 1000）。

## 2. P1 能力

| 能力 | 实现 |
|---|---|
| 两次仿真对比 | `compare_runs.py`：metric/before/after/delta/better-worse 表（方向白名单可解释） |
| 判据 | `PASS / FAIL / INCONCLUSIVE`，缺指标判 INCONCLUSIVE 而不是 PASS |
| 置信度 | 固定加减分表 + 逐条规则打印（`compare_runs.confidence`） |
| 模型溯源 | manifest 记录 file/sha256/model type/来源；IBIS 同理 |
| 温度限制 | manifest 记录 ngspice TEMP；vendor 模型忽略温度时提示 `TEMPERATURE_MODEL_LIMITATION` |
| 工作区 | `sim_workspace.py`：source/models/configs/generated/runs/plots |
| 报告 | `report.py`：`report.md`（模型保真度/拓扑/扫描/指标/判据/局限）+ 可选 matplotlib |
| 线缆/连接器 | override 的 `connector` / `series_rl`（Z0+delay 用 T 线表达） |
| 耦合传输线 | schema 保留，`status: EXPERIMENTAL`，不阻塞发布 |

## 3. 兼容性

v2.2 的 75 项测试全部继续通过（现在共 118 项）。旧命令、旧参数、旧产物路径不变。
新功能全部 opt-in：不给 `--pcb/--stackup/--ibis/--touchstone/--ports` 时，
行为与 v2.2 完全一致。

## 4. 已知限制（必须知道）

1. **仍不是 field solver**：Z0/TD 是解析式，误差量级 ±10%；via R/L 是经验式。
2. **单位靠约定 + 护栏**：EasyEDA Pro PCB 内部坐标是 mil（实测），但 `CANVAS.unit`
   会写 mm；工具默认 mil，可用 `--unit` 覆盖，且对"板子尺寸明显不合理"不静默。
3. **S 参数集总拟合是窄带等效**：跨 3 个十倍频程时 3 元件模型误差 20–25%，
   拟合质量会打印出来；宽频器件建议分段或用厂商 RLC 模型。
4. **过孔只建模走线路径上的**：铺铜缝合孔不进拓扑（会报数量并说明）。
5. **IBIS 只覆盖 CMOS 输入/输出/双向**：差分、SerDes、多状态模型未实现。
6. **PDN 端口靠理想源会失效**：rail 上直接挂理想电压源时 Z11 恒为 0，工具会拦。
7. 不做 3D 全波、FEM/FDTD、天线、完整 IBIS 7.x、眼图、DDR timing（按需求文档 §38）。

## 5. 本轮踩到并写进代码/文档的坑

| 坑 | 现象 | 对策 |
|---|---|---|
| 元素 id 有时是 JSON 字符串 | `int('["LAYER",1]')` 崩 | `_rid_parts()` 统一处理 |
| 长度双重缩放 | 板长 3mm 级、+5V 总长 0.79mm 而非 31.23mm | 节点 key 已含单位换算，长度不再乘 scale |
| 走线遍历方向 | from/to 随机颠倒、字段串成 `__AUTO_NODE_x_y` | 显式记录遍历真实首尾 |
| 过孔合并节点 | 换层后 R/L 无处安放 | 每层一个节点，过孔变串联元件 |
| 缝合孔 | 一对悬空节点 → 奇异矩阵 | 只保留两端都有走线的过孔 |
| `LAYER` 表恒有 60+ 条 | 2 层板被算成 68 层 | 只保留"被走线/焊盘引用"的铜层 |
| ngspice `S` 元件 | `model type mismatch` | adapter + capability probe |
| `.sp` 分析的 lin 语法 | `sp lin a b n` 返回 0 行 | 只用 `dec`/`oct` |
| 理想源短接 rail | Z11 ≡ 0 | 主动检查并报 `PDN_PORT_SHORTED_BY_IDEAL_SOURCE` |
| 串联 R 型磁珠拟合 | 直流通道被 62Ω 堵死，AVDD 掉到 0V | 低频阻抗护栏 + 三元件拓扑 |
| 同元件两脚之间插元件 | 两个引脚都被"摘下来"，两端悬空 | 判为"替换该器件"，不切分 |
