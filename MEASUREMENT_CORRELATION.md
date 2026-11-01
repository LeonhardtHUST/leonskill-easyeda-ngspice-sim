# MEASUREMENT_CORRELATION — 仿真 vs 实测

```bash
sim_file.sh board.epro2 correlate --simulation run01 --measurement scope.csv
```

## 能导入什么

| 来源 | 格式 | 说明 |
|---|---|---|
| 示波器 | CSV `time, voltage` | 列名任意，用 `mapping` 指定；单位从表头推断（s/ms/µs/ns、V/mV） |
| 电源轨 | CSV `time, rail_voltage, rail_current` | 电流列可选 |
| VNA | `.s1p/.s2p/…` | 走 `rf_network` 解析 |

```python
m = mi.load_csv("scope.csv", mapping={"time": "Time(s)", "voltage": "CH2"})
```

列名猜不到会 `MEAS_COLUMN_NOT_FOUND`（不静默取错列）。时间列非单调只告警、**不排序**
（排序会把数据问题藏起来）。

## 时间域相关（§17）

1. **时间窗**：`t0/t1`，默认取仿真与实测的交集；
2. **时延对齐**：`delay_s` 显式给，或 `auto_delay` 自动估；
   - 阶跃/脉冲类信号用 **50% 穿越差**（实测：两个几乎相同的阶跃做互相关，峰值永远在 lag=0，
     除边沿外完全重合 → 互相关失效）；
   - 非阶跃才退回互相关；
3. **DC 偏移**：默认去掉（`remove_dc`）；
4. **幅度归一**：`normalize_amplitude` 可选，会记录 `gain_applied`；
5. **重采样**：线性插值到实测的 `dt`，超界返回 NaN（不外公推）。

输出：

```json
{"rmse_v": 0.00284, "nrmse": 0.00086, "peak_error_v": 0.0112,
 "rise_time_sim_s": 8.1e-9, "rise_time_meas_s": 8.4e-9,
 "overshoot_sim_pct": 21.3, "overshoot_meas_pct": 19.8,
 "delay_s": 2.0e-10, "dc_removed": true}
```

**实测校验**（合成数据）：同一份数据自相关 → RMSE < 1e-12；注入 200 ps 时延 → 检出值误差
< 20 ps。

## 频域相关

`correlate_sparam(sim_net, meas_net)`：在重叠频段逐端口对比较 |Sij| 与相位，
输出 rms/max 误差。同样不比较低于 −120 dB 的条目（那是零）。

## 纪律

- `CORRELATION_REPORT.json` 里必须标明 `sample`：
  - `OUT_OF_SAMPLE`：拟合用 A 段、验证用 B 段（可信）
  - `IN_SAMPLE`：同一批数据既拟合又验证（**不能当验证依据**）
- 这是"仿真 vs 实测"的偏差，与"模型 vs 数据"的拟合残差是两件事，报告里分开写。

## 尚未做

- 探头/示波器带宽反卷积（需要探头模型）；
- 抖动分解（RJ/DJ 分离）；
- 多通道同时对齐（目前一次一对）。
