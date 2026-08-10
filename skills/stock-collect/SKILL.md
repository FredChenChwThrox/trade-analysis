---
name: stock-collect
description: 股票分析系统的数据采集 Skill。调用 kimi-datasource 插件（MCP 工具）获取行情、财报、公告、预期等原始数据，原样落盘到 data/raw/ 供 Python adapter 入库。只搬运，不加工：不计算指标、不评价消息、不生成规范化财务数字（设计 §3.1）。当需要为某只股票采集/补采数据时使用。
---

# 数据采集 Skill

## 职责边界

- 只做三件事：调数据源 → 拿到原始响应 → 落盘 `data/raw/`。
- **禁止**：计算指标、修改/汇总数据、评价消息、生成财务数字。
- 落盘后由 Python adapter 完成校验与入库，本 Skill 不入库。

## 落盘约定

```
data/raw/{source}/{data_type}/{YYYY-MM-DD}/{run_id}/{ticker}.csv
```

- `source`：`stock_finance_data` / `yahoo_finance`
- `data_type`：`price` / `financials` / `announcement` / `forecast` / `fx` / `stock_actions` / `index`
- `run_id`：每次采集生成一个（如 `run_YYYYMMDD_HHMMSS`）
- 同一次采集的多个文件共用同一 `run_id`；同时写一份 `_meta.json` 记录请求参数、抓取时间、来源。

## 数据源路由

- A 股行情/财报/公告/预期：`stock_finance_data`（ticker 形如 `600223.SH`；行情单次最多 3 只、区间最长 3 年，`adjust=none` 采不复权价）。
- 港股行情：`yahoo_finance`（ticker 形如 `0700.HK`，单次区间最长 2 年，长区间需分段）。
- 港股/外汇/公司行为：`yahoo_finance` 的 `get_historical_stock_prices`（FX 如 `CNYHKD=X`）、`get_stock_actions`。
- 指数：A 股 `000300.SH` 走 `stock_finance_data`；恒生 `^HSI` 走 `yahoo_finance`。

## 采集模式

1. **全量初始化**：日线 3 年（超过单次上限时分段，段间重叠 5 个交易日）；财报最近 3 个年报 + 最近 8 个季报；公告 1 年。
2. **增量**：查库内 `max(trade_date)`（由调用方告知），采其后数据，**向前重叠最近 5 个交易日**（用于发现来源修订与新除权，设计 §3.3）。

## 增量采集流程（每日）

1. 从调用方获得：ticker 列表、每只的库内最大日期、本次 `run_id`。
2. 按路由调对应 MCP 工具，`file_path` 指向落盘路径。
3. 写 `_meta.json`。
4. 返回：落盘文件清单 + 每只股票的返回行数与日期范围（供 adapter 核对）。

## 失败处理

- 单次请求失败可重试一次；仍失败则记录到 `_meta.json` 的 `errors` 并跳过该股，**不得**用旧文件冒充新数据（设计 §2.5）。
