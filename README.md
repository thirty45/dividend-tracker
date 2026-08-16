# 红利股票跟踪 8.16（云端版）

每天自动抓取一次红利相关股票的关键指标，生成可在手机 / 电脑浏览器查看的网页。

## 功能

- **股票列表表格**：每行一只股票，展示代码、名称、现价、涨跌幅、**股息率TTM**、**市盈率TTM**、**市净率**、**总市值**、**ROE**、行业、来源标签，支持搜索和点击表头排序。
- **个股详情**：点击任意股票进入，展示指标卡片 + **K线图**（可开关均线 MA5/10/20/60、BOLL、DMI），以及股息率 / 市盈率 / ROE 历史走势。
- **板块市值排名**：按行业汇总总市值、成分股数、平均股息率、平均市盈率、平均ROE，点击行业可筛选股票。
- **每日自动更新**：GitHub Actions 每天 15:35（北京时间）自动抓数并部署，手机外网随时可看，无需电脑开机。

## 股票池来源

1. **红利类型基金近5年（2020年至今）前十大持仓**（天天基金/东方财富 F10 数据），凡被持有过的股票全部纳入；
2. **红利指数成分股**：中证红利、红利低波、深证红利、上证红利、红利低波100（新浪财经最新成分）；
3. **高股息筛选**：近12个月已实施现金分红计算的股息率TTM ≥ 3% 且总市值 ≥ 100 亿元的股票；
4. 东财“红利股 / 红利破净股”概念板块成分（接口偶尔限流，失败会自动跳过）。

## 指标口径

| 指标 | 来源与口径 |
|---|---|
| 股息率TTM | 近12个月已实施现金分红合计 ÷ 最新收盘价（东方财富分红数据中心） |
| 市盈率TTM / 市净率 / 总市值 | 东方财富估值数据中心（PE_TTM / PB_MRQ / 总市值） |
| ROE | 最新报告期加权净资产收益率（东方财富业绩报表） |
| K线 | 东方财富前复权日K线（MA/BOLL/DMI 由网页端计算） |
| 板块市值排名 | 按估值数据中的行业板块汇总（涨跌幅来自东财行业板块行情） |

## 部署步骤（约10分钟）

1. **注册 GitHub**：https://github.com/signup ，登录。
2. **新建仓库**：点右上角 + → New repository，仓库名随意（如 `dividend-tracker`），选择 **Public**（GitHub Pages 免费版要求公开仓库；数据是公开行情，无隐私问题），不要勾选任何初始化文件。
3. **本机推送**（已装 git 的话在项目目录执行）：
   ```
   git init
   git add .
   git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```
   推送时按提示输入 GitHub 用户名和 Personal Access Token（Settings → Developer settings → Tokens → 生成一个勾选 repo 权限的 token，作为密码输入）。
4. **开启 GitHub Pages**：仓库页面 → Settings → Pages → Source 选择 **GitHub Actions**（保存）。
5. 等第一次 Actions 运行（仓库 → Actions → daily-update → Run workflow 可手动触发一次），完成后访问：
   `https://<你的用户名>.github.io/<仓库名>/`

## 日常使用

- 每天 15:35 自动更新，直接刷新网页即可看到最新数据。
- 想看更多历史K线：页面图表下方有滑块，可缩放。
- 手动触发更新：仓库 → Actions → daily-update → Run workflow。
- 想更新股票池（基金持仓变化/新增高股息股）：本机执行 `python pipeline/build_watchlist.py`，把 `data/meta/watchlist.json` 和 `data/meta/fund_holdings.json` 提交推送即可（也可在 Actions 里手动跑，以后可加）。

## 本地运行（可选）

需要 Python 3.10+，无需安装第三方库（全部用标准库）：

```
python pipeline/build_watchlist.py   # 构建股票池（一次性，约10分钟）
python pipeline/update_daily.py      # 抓当天数据
```

本地预览网页：在项目根目录执行 `python -m http.server 8000`，浏览器打开 `http://localhost:8000`。

## 说明

- 数据来自公开免费接口（东方财富、新浪财经、天天基金），仅供学习参考，不构成投资建议。
- 周末/节假日无新交易日数据时会自动跳过，不产生无效更新。
- 若某只股票长期停牌或已退市，对应指标显示“—”。
