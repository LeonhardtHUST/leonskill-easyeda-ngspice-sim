# SOLVER_BACKENDS — 求解器抽象

```python
import solver_backend as sb
b = sb.get("ngspice")          # 默认，也是唯一生产后端
b.probe()        # {name, available, version, status}
b.capabilities() # {analyses:[...], rf_ports, sparam_elem, xspice, notes}
b.run(deck, cwd) # (rc, stdout, stderr)
b.parse(raw)     # parse_raw 的统一结构
```

接口形状参考 Qucs-S 的"前端 + 可换后端"（GPL-2.0，**只借形状，代码自写**）。

## 后端能力对比

| | ngspice-47 | Xyce |
|---|---|---|
| 状态 | `QUALIFIED` | `EXPERIMENTAL` |
| op/tran/ac/dc | ✅ | ✅ |
| `.sp` S 参数分析 + RF 端口 | ✅ | ❌ |
| XSPICE | ✅ | ❌ |
| `S` 元件（Touchstone 直接挂） | ❌（实测 `model type mismatch`） | ❌ |
| 我们的用法 | 全部生产分析 | 仅大规模瞬态/MC 的可选加速 |

## 为什么 Xyce 不阻塞发布

- 没有 `.sp` → PDN/sparam 两类分析在 Xyce 上根本做不了；
- netlist 方言差异（`.control` 块、部分 device）→ 生成器要分叉，风险高于收益；
- 它的价值在"大网络 + 海量 case 的并行"，那是 P1 的优化项，不是正确性项。

所以 v2.4 只把它接成可探测、可切换的**实验后端**，`probe()` 不可用时不报错、不静默回退。

## 切换方式

```json
{"solver": "xyce"}      // 目前仅用于 tran 类分析
```

未知名字直接 `SOLVER_UNKNOWN`（不默默用 ngspice 顶替——那会让结果来源说不清）。
