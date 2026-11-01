# RF_NETWORK_ENGINE — scikit-rf 适配层

```python
import rf_network as rn
n = rn.RfNetwork.from_touchstone("filter.s2p")     # 先试 skrf，失败退回 v2.3 解析器
n.to_y(); n.to_z(); n.to_abcd()
n.renormalize(75.0)                                # 端口阻抗变换
n.cascade(other); n.connect(0, 2)
n.is_passive(); n.is_reciprocal()
```

## 为什么用 scikit-rf 而不是自己写

S/Y/Z/ABCD 转换、renormalization、级联、connect、de-embedding 都是**已经写对了十年**的
矩阵运算。自己重写只会引入符号约定错误（S 与 Y 的转置、复共轭、z0 归一化）。
scikit-rf 是 **BSD-3-Clause**，可以直接作为运行时依赖（`requirements-rf.txt`）。

## 适配层做了什么

| 事情 | 说明 |
|---|---|
| 依赖探测 | `available()` / `capability_report()`；不可用时**不抛异常**，调用方退回 lumped |
| z0 维度归一 | skrf 的 `z0` 是 `(npoints, nports)`；这里统一成每端口代表值（复数支持） |
| 端口阻抗契约 | `ensure_z0(net, target, strict)`：不一致 → renom 或 `RF_Z0_MISMATCH` |
| 元数据 | 保留 `meta`（原始文件、renorm 前后 z0、级联来源），进 manifest |
| 优雅降级 | skrf 缺失时用 v2.3 的 `touchstone.py` 解析，`backend="numpy_fallback"`，只支持读取与 |S| |

## 端口阻抗契约（需求文档 §6）

不许再假设"Touchstone 的 z0 == 电路 z0"：

```python
net, info = rn.ensure_z0(net, 75.0, strict=True)
# info = {"original_z0":[50+0j], "target_z0":[75+0j], "renormalized": True}
```

- 相同 → 原样返回，`renormalized=False`；
- 不同且有 skrf → 自动 renorm，manifest 记 `original_z0 / target_z0 / renormalized`；
- 不同且没有 skrf → `RF_Z0_MISMATCH`（strict 下致命）。

## 级联与连接

```python
c = a.cascade(b)          # 2 端口级联（skrf `**`）
d = n.connect(0, 2)       # 把内部端口 0 与 2 接起来（如 cascade 中段接地）
```

只有 2 端口能 cascade，其他组合直接 `RF_PORT_COUNT`。

## 已知限制

- 复数 z0 的 renormalization 依赖 skrf 实现，极端 z0（|z0| 很小）会数值退化 → 报
  `RF_RENORM_FAILED` 而不是给个错数；
- 不一致的频率轴不能直接 cascade（skrf 会插值或报错，本层要求先用 `RfNetwork`
  重采到同一网格）；
- `numpy_fallback` 后端只够"看数据"，做不了 renorm/cascade。
