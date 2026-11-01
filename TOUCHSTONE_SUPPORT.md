# TOUCHSTONE_SUPPORT — `.sNp` 解析、指标与接入

```bash
# 只看数据（不接电路）
touchstone.py fixture.s2p

# 接进电路：override 里写 sparams（见下）
```

## 1. 解析

| 项 | 支持 |
|---|---|
| 端口数 | `.s1p` `.s2p` … `.sNp`（每点 1+2n² 列，允许折行） |
| 数据格式 | `RI` / `MA` / `DB` |
| 频率单位 | `Hz` / `kHz` / `MHz` / `GHz` |
| 归一化 | `R <z0>`（默认 50） |
| 注释 | `!` 开头；`#` 为选项行 |
| `Y/Z/H/G` 参数 | ❌ 明确报 `TOUCHSTONE_FORMAT_UNSUPPORTED` |

实测一致性：同一器件写成 `Hz + MA` 与 `MHz + DB` 两份文件，解析出的 S21 差
**≤ 5×10⁻⁵ dB**，频率完全一致。

## 2. 指标（`sparam_metrics.py`，不需要 ngspice）

2 端口：`insertion_loss_db`（最小损耗）、`return_loss_db`（最好回损）、
`worst_return_loss_db`、`peak_attenuation_db`、`bandwidth_3db{ f_low, f_high,
bandwidth, note }`、`resonances`（S21 局部极大）、`notches`、`is_reciprocal`。
1 端口：`|Z(f)|`、`z_max_ohm/z_max_f`、`z_min_ohm`、`return_loss_db`。

## 3. 接进 ngspice：adapter 层

**为什么需要 adapter**：ngspice-47 的 `S` 元件在本机实测**不可用**——

```
S1 in 0 out 0 spm
.model spm s file="x.s2p"
→ warning, model type mismatch  /  Unable to find definition of model
```

而 `.sp` 分析只能"量"一个带 RF 端口的电路，不能把 `.sNp` 当二端元件用。
所以提供三条后端，`probe_native()` 自动选择，用户不依赖不稳定语法：

| backend | 做法 |
|---|---|
| `native` | 探测到 `S` 元件可用时直接 `S1 n1 0 n2 0 model` |
| `lumped`（默认） | 从 S 参数提取集总等效（见下） |
| `metrics_only` | 只出指标，不接电路 |

### 集总提取（闭式最小二乘，无迭代）

1 端口 `Z = z0(1+S11)/(1−S11)`，候选拓扑：
`series_rl`、`series_rlc`、`shunt_rl`（R‖L）、`bead3`（Rs + Rp‖Lp，Rp/Lp 对数网格
21×21 + Rs 闭式解）。全部在实部/虚部上解耦，`R = mean(Re Z)`、`L = mean(Im Z/ω)`。

2 端口：先转 Y（`Y11..Y22`），π 网络（`Zs = −1/Y21`，`Yp1 = Y11+Y21`）与
T 网络（`Za = Z11−Z12`，`Zp = Z12`）各拟一次，取 S21 误差小的那个。
实测对 C-L-C 梯形能**精确还原**（L=220nH、C=100pF 分毫不差，S21 误差 0.000 dB）。

### 两道护栏

1. **拟合误差阈值**（2 端口默认 3 dB）：超过就 `SPARAM_FIT_TOO_POOR` 拒绝接入。
2. **低频阻抗检查**（1 端口）：串联 R 型拓扑若在最低频点的 |Z| 明显偏离实测
   （>3× 或 <1/3），判为不可用——否则会给磁珠塞进几十欧的直流电阻，把供电通道堵死
   （实测踩到：AVDD 掉到 0 V）。

## 4. override 里的写法

```json
"sparams": [
  {"name": "FB1", "from": "V_IN", "to": "AVDD",
   "file": "../touchstone/bead.s1p", "backend": "lumped",
   "band": [1e5, 1e8], "max_err_db": 3}
]
```

`band` 强烈建议显式给：只给一个窄带会让低频阻抗无从确定（拟合出错误的直流电阻）。

## 5. 限制

- 集总是**窄带等效**：跨 3 个十倍频程时 3 元件模型误差 20–25%，报告会打印
  `fit rms` 与百分比；宽频器件请分段拟合并拼接。
- 不做法向端口重归一化（假定文件里的 `R <z0>` 与电路一致）。
- 不做多端口（>2）的集总等效；>2 端口只出指标。
