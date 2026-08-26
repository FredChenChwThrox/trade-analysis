# akshare 采集器接入记录

完成日期：2026-08-26

## 实现摘要

新增**可选数据源** akshare 采集器 + adapter，字段对齐现有已验证的 adapter 约定：

- `scripts/collect/akshare_collect.py`：CLI 采集 price/financials/index/telegraph 落盘 raw CSV + `_meta.json`；akshare 为 `pyproject.toml [project.optional-dependencies].akshare`（体积大不进主依赖，未装时 CLI 提示 `uv sync --extra akshare`）。
- `scripts/adapters/akshare.py`：
  - price → 复用 `upsert_daily_bars`（source=akshare）
  - index → 复用 `upsert_index_bars`（source=akshare）
  - financials → **直接转发 `tdx.parse_financials_csv`**（published_at=东财 NOTICE_DATE 披露日，available_at=下一开市交易日 §2.1）
  - telegraph → events + event_symbols（去重/股票匹配）
- `ingest.py` `_ROUTES` 注册 4 条 akshare 路由。

## 字段对齐关键点（实测验证）

| 数据 | 落盘列约定 | 换算 |
|---|---|---|
| price/index | thscode,time,open,high,low,close,volume,amount,currency（kimi 约定） | 成交量手→股 ×100；成交额元直存 |
| financials | code,setcode,period_end,fiscal_year,revenue,net_profit_attr,eps_basic,eps_diluted,currency,unit,is_cumulative,published_at（tdx 约定） | 金额 unit=yuan |
| telegraph | events 字段（published_at UTC 等） | 本地→UTC；哈希去重 |

## 实测（真实 akshare，2026-08-26）

- 财联社电报 20 条 → 17 入库 + 3 无标题行跳过
- 沪深300 全历史 5978 行入库；恒生 3172 行 + 30 行源缺陷跳过（新浪 open=0/close>high）
- 珀莱雅利润表全历史 40 期；2026 中报 published_at=2026-08-24T16:00Z（本地 08-25 00:00），available_at=下一开市日——**补齐 A 股披露时间缺口**（pit_backfill 之外通道）

## 边界决策

- 电报无标题行 / 指数 OHLC 源缺陷行 → **行级跳过**（§2.5），不整批回滚
- 港股财报接口当前不支持（记录 error 不阻塞，A 股财报为主力）

## 测试

`tests/test_akshare_collect.py` 10 项 + `tests/test_adapters_akshare.py` 6 项；**`uv run pytest -q` 386 全绿**。

## 后续建议

- 港股财报接入（`stock_profit_sheet_by_report_em` 对港股代码形态不支持，需换接口）
- 复权因子重建输入（akshare 分红送配明细 → corporate_actions，供 adjust 平台段重建）
- 恒生指数坏行来源可换东财源
