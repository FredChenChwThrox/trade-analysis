"""5 只新观察股票全量初始化采集（run_init）。

按 skills/stock-collect 约定只搬运不落库：
- 行情 stock_finance_data get_price：adjust=none 3 年 + adjust=forward 3 年
- 财报 stock_finance_data get_financial_statements：7 期利润表（3 年报 + 4 季报）
- 一致预期 stock_finance_data get_forecast：每只 1 次
- 股本 yahoo_finance get_stock_info：每只 1 次（sharesOutstanding）
每项失败重试一次，仍失败记录到 _meta.json errors 并继续，不中断整批。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta

from scripts.collect.mcp_client import McpClient

# 仓库根目录（数据落盘根）：环境变量 TRADE_ROOT 指定，缺省取当前工作目录
ROOT = os.environ.get("TRADE_ROOT", os.getcwd())
DATE = "2026-08-10"
RUN_ID = "run_init"
START, END = "2023-08-10", "2026-08-08"
PERIODS = ["20231231", "20241231", "20251231", "20250331", "20250630", "20250930", "20260331"]

STOCKS = [
    ("603288.SH", "603288.SS", "海天味业"),
    ("601318.SH", "601318.SS", "中国平安"),
    ("002747.SZ", "002747.SZ", "埃斯顿"),
    ("601899.SH", "601899.SS", "紫金矿业"),
    ("600029.SH", "600029.SS", "南方航空"),
]

# data_type -> (source, 目录)
DIRS = {
    "price": ("stock_finance_data", f"{ROOT}/data/raw/stock_finance_data/price/{DATE}/{RUN_ID}"),
    "financials": ("stock_finance_data", f"{ROOT}/data/raw/stock_finance_data/financials/{DATE}/{RUN_ID}"),
    "forecast": ("stock_finance_data", f"{ROOT}/data/raw/stock_finance_data/forecast/{DATE}/{RUN_ID}"),
    "stock_info": ("yahoo_finance", f"{ROOT}/data/raw/yahoo_finance/stock_info/{DATE}/{RUN_ID}"),
}

records: dict[str, list[dict]] = {k: [] for k in DIRS}


def call_with_retry(client: McpClient, source: str, api: str, params: dict) -> tuple[bool, str]:
    """调 call_data_source_tool，失败重试一次。返回 (ok, note)。"""
    args = {"data_source_name": source, "api_name": api, "params": params}
    last_err = ""
    for attempt in (1, 2):
        try:
            res = client.call_tool("call_data_source_tool", args)
            if res.get("isError"):
                last_err = res["content"][0]["text"][:500]
                continue
            text = res["content"][0]["text"]
            # 数据源在 text JSON 里报告 EMPTY_DATA 等业务错误
            if "EMPTY_DATA" in text or '"error"' in text[:200].lower():
                last_err = text[:500]
                continue
            file_path = params.get("file_path", "")
            if file_path and (not os.path.exists(file_path) or os.path.getsize(file_path) == 0):
                last_err = f"file missing or empty: {file_path}; resp={text[:300]}"
                continue
            return True, text[:300].replace("\n", " ")
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        if attempt == 1:
            time.sleep(2)
    return False, last_err


def main() -> int:
    for _, d in DIRS.values():
        os.makedirs(d, exist_ok=True)
    with McpClient() as client:
        for ticker, yahoo_ticker, name in STOCKS:
            # 1) 行情 none + forward
            for adjust, suffix in (("none", ""), ("forward", "_forward_3y")):
                fp = f"{DIRS['price'][1]}/{ticker}{suffix}.csv"
                ok, note = call_with_retry(client, "stock_finance_data", "stock_finance_data_get_price", {
                    "ticker": ticker, "start_date": START, "end_date": END,
                    "interval": "D", "adjust": adjust, "format": "json", "file_path": fp,
                })
                records["price"].append({
                    "api": "stock_finance_data_get_price",
                    "params": {"ticker": ticker, "start_date": START, "end_date": END,
                               "interval": "D", "adjust": adjust, "format": "json"},
                    "file": os.path.basename(fp), "status": "ok" if ok else "error",
                    "note": note if ok else None, "error": None if ok else note,
                })
                print(f"[price {adjust:7s}] {ticker} {name}: {'ok' if ok else 'ERROR ' + note[:120]}")
            # 2) 财报 7 期
            for period in PERIODS:
                fp = f"{DIRS['financials'][1]}/{ticker}_is_{period}.csv"
                ok, note = call_with_retry(client, "stock_finance_data", "stock_finance_data_get_financial_statements", {
                    "ticker": ticker, "statement": "is", "financial_parameter": period,
                    "format": "json", "file_path": fp,
                })
                records["financials"].append({
                    "api": "stock_finance_data_get_financial_statements",
                    "params": {"ticker": ticker, "statement": "is",
                               "financial_parameter": period, "format": "json"},
                    "file": os.path.basename(fp), "status": "ok" if ok else "error",
                    "note": note if ok else None, "error": None if ok else note,
                })
                print(f"[fin {period}] {ticker} {name}: {'ok' if ok else 'ERROR ' + note[:120]}")
            # 3) 一致预期
            fp = f"{DIRS['forecast'][1]}/{ticker}.csv"
            ok, note = call_with_retry(client, "stock_finance_data", "stock_finance_data_get_forecast", {
                "ticker": ticker, "format": "json", "file_path": fp,
            })
            records["forecast"].append({
                "api": "stock_finance_data_get_forecast",
                "params": {"ticker": ticker, "format": "json"},
                "file": os.path.basename(fp), "status": "ok" if ok else "error",
                "note": note if ok else None, "error": None if ok else note,
            })
            print(f"[forecast] {ticker} {name}: {'ok' if ok else 'ERROR ' + note[:120]}")
            # 4) 股本
            fp = f"{DIRS['stock_info'][1]}/stock_info_{yahoo_ticker}.csv"
            ok, note = call_with_retry(client, "yahoo_finance", "get_stock_info", {
                "ticker": yahoo_ticker, "file_path": fp,
            })
            records["stock_info"].append({
                "api": "get_stock_info",
                "params": {"ticker": yahoo_ticker},
                "file": os.path.basename(fp), "status": "ok" if ok else "error",
                "note": note if ok else None, "error": None if ok else note,
            })
            print(f"[stock_info] {yahoo_ticker} {name}: {'ok' if ok else 'ERROR ' + note[:120]}")

    fetched_at = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    for data_type, (source, d) in DIRS.items():
        meta = {
            "run_id": RUN_ID,
            "source": source,
            "data_type": data_type,
            "fetched_at": fetched_at,
            "purpose": "5 只新观察股票全量初始化（海天味业/中国平安/埃斯顿/紫金矿业/南方航空），同步观察无排期卡",
            "requests": records[data_type],
        }
        with open(f"{d}/_meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        errs = [r for r in records[data_type] if r["status"] != "ok"]
        print(f"== {data_type}: {len(records[data_type])} requests, {len(errs)} errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
