# 红利池_MA60_等权 回测

## 入口

```bash
python -m pipeline.backtest                  # 全量 watchlist
python -m pipeline.backtest --limit 50       # 调试子集
python -m pipeline.backtest --skip-download  # 已有 data/kline_raw 时
```

结果目录：`data/backtest/红利池_MA60_等权/`（`summary.json` / `equity.csv` / `chart.html`）。

## 口径摘要

- 选股：当前 watchlist ∩ 不复权月末收盘 ≥ MA60（与 `update_daily._month_end_series` 一致）；**不含**首页 year_low/premium 筛选。
- 调仓：自然月末收盘后算，下一交易日开盘调；无开盘则收盘（`fill=close`）。
- 成本：单边万三，滑点 0。
- 基准：000922（腾讯 `sh000922`）；H30269 可选（东财不可用时用跟踪基金 005561 累计净值代理）。
- **幸存者偏差**：历史全程使用「当前」watchlist。

完整调仓明细 `rebalances.json` 默认不入库（见 `.gitignore`）。
