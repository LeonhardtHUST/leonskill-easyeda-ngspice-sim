# MONTE_CARLO — 容差统计与 Corner

```bash
sim_file.sh board.epro2 montecarlo --config mc.json --cases 500 --seed 1234
sim_file.sh board.epro2 corner      --config corner.json
```

## 配置

```json
{
  "analysis": {"mode": "tran", "args": "10p 120n"},
  "metrics": [{"name": "avdd", "metric": "tran_stats", "node": "AVDD", "t0": 2e-5}],
  "montecarlo": {
    "cases": 200,
    "seed": 1234,
    "workers": 8,
    "max_cases": 1000,
    "parameters": {
      "R_CLK_DAMP": {"nominal": "22",  "distribution": "uniform",  "tolerance_percent": 20},
      "C1":         {"nominal": "220p","distribution": "gaussian", "sigma_percent": 5,
                     "clip_sigma": 3},
      "R_TERM_ADC": {"nominal": "100", "distribution": "discrete",
                     "values": ["91", "100", "110"], "weights": [1, 3, 1]},
      "R99":        {"nominal": "1k",  "distribution": "uniform",  "min": "950", "max": "1100"}
    },
    "criteria": [
      {"metric": "adc.overshoot_pct", "op": "<", "value": 25.0},
      {"metric": "adc_cross.total",   "op": "<=", "value": 8}
    ]
  }
}
```

| 分布 | 参数 |
|---|---|
| `gaussian` | `sigma_percent`（必填）、可选 `clip_sigma`、`positive` |
| `uniform` | `tolerance_percent`（±%）或 `min`/`max` |
| `discrete` | `values` + 可选 `weights` |

参数名 = **deck 里的元件名**（`netlist_gen` 用位号做元件名，所以 `C1`/`R_CLK_DAMP`
直接可用）或 `.param` 名。改不到就 `SWEEP_PARAM_NOT_FOUND`（不猜）。

## 可复现

`random.Random(seed)` 生成，case 顺序固定 → **同 seed 同结果**（有单元测试）。
并行只影响完成顺序，`records` 最后按 index 排序，因此统计稳定。

## 输出

```
runs/case-0001/{deck.cir, stdout.txt, stderr.txt, tran.raw, tran.csv, metrics.json}
montecarlo_cases.csv       每 case 一行（参数 + 扁平化指标）
montecarlo_summary.json    统计 + 每 case 判据结论
```

统计：`mean / std / p5 / p50 / p95 / min / max`；
失败率：`failure_rate = FAIL 的 case 数 / 总 case 数`（未给 criteria 时为 null）。

判据的 `metric` 用**扁平名**（`<指标名>.<字段>`，与 `sweep_summary.csv` 列名一致）。
缺失的指标不计为 PASS，而是把整批判为 `INCONCLUSIVE`。

## Corner（确定性笛卡尔积）

```json
{"corner": {"axes": {"VIN": [11.5, 13.8, 14.5],
                     "TEMP": [0, 25, 60],
                     "LOAD": ["idle", "nominal", "peak"]},
            "max_cases": 27}}
```

- `TEMP` 走 `.temp`（有就替换，没有就插在 `.end` 前）；
- 其它轴走元件名 / `.param`；
- `LOAD: ["idle","nominal","peak"]` 这种符号值需要你在 override 里用
  `.param` + `{LOAD}` 表达，或直接给数值。

## 性能

默认 `workers = min(CPU, 8)`，可设 1 关闭并行。每个 ngspice 子进程都有 `timeout`
（默认 600 s）。进度按 `[done/total]` 打印。

## 限制

- 不做拉丁超立方 / 敏感性分析（Sobol 等）。
- 分布假设来自用户输入，工具不替你判断"这个 sigma 是否现实"。
- 温度只在 ngspice 支持的范围内生效；厂商 `.SUBCKT` 若忽略温度，manifest 会提示
  `TEMPERATURE_MODEL_LIMITATION`。
