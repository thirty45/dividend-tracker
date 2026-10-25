# 红利池_MA60_等权 回测

## 展示闸门（display_policy）

| 字段 | 值 |
|------|-----|
| `deployable` | **false** |
| `absolute_ann_return` | `forbidden_for_public` |
| `excess_vs_000922` | `forbidden_for_public` |

允许对外/站点引用的过程指标：最大回撤、持仓只数、调仓次数、`fill_stats`、偏差清单。  
**禁止**把绝对年化、相对 000922（价格指数）超额写成可实盘/可部署结论。

原因：幸存者偏差未修 + 主基准仍为价指；全收益官方码 **H00922** 在本环境无可用行情。

## 入口

```bash
python -m pipeline.backtest                  # 全量 watchlist
python -m pipeline.backtest --limit 50       # 调试子集
python -m pipeline.backtest --skip-download  # 已有 data/kline_raw
python -m pipeline.backtest --skip-fill-missing  # 不尝试东财补缺 K
```

结果目录：`data/backtest/红利池_MA60_等权/`（`summary.json` 内部 / `summary.public.json` 站点 / `equity.csv` / `chart.html`）。

## 站点入口

Pages 顶栏「回测」→ `#/backtest`，只读 `summary.public.json`（过程指标 + 偏差清单）。
不嵌入 `chart.html`，不展示绝对年化与相对 000922 超额。

## 口径摘要

- 选股：当前 watchlist ∩ 不复权月末收盘 ≥ MA60（与 `update_daily._month_end_series` 一致）；**不含**首页 year_low/premium。
- 调仓：自然月末收盘后算，下一交易日开盘；无开盘则收盘（`fill=close`）。
- 成本：单边万三，滑点 0。
- 主基准：**000922 价格指数**（`benchmark_type=price_index`，`excess_public=false`）。全收益码 H00922 已探测，无源则不启用。
- 可选：H30269 真源优先；失败时 **005561 仅 proxy**。H20269 为低波全收益（若可得）。
- **pool_sensitivity=untested**（无历史成分）；**幸存者偏差**已标注。

完整调仓明细 `rebalances.json` 默认不入库。
