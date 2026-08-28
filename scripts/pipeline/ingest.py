"""入库 CLI（D1.3）：raw 文件/目录 → adapter 路由 → 规范化事实入库。

用法：
    uv run python -m scripts.pipeline.ingest <raw_file_or_dir> [...] [--db PATH]

按路径 data/raw/{source}/{data_type}/{date}/{run_id}/{file}.csv 推断 source/data_type
路由到对应 adapter，逐文件打印 IngestResult，结尾打印汇总。
- 相同 content hash 的文件跳过不重复解析（§8.3）；
- price 目录下 *_forward*.csv 为前复权文件，留给 D1.5 复权模块，本 CLI 跳过；
- 校验冲突/错误的文件整批不入库，进程退出码为 1；incomplete（降级）不影响退出码。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.adapters import akshare as ak
from scripts.adapters import stock_finance_data as sfd
from scripts.adapters import tdx
from scripts.adapters import tianyancha as tyc
from scripts.adapters import yahoo_finance as yf
from scripts.adapters.common import IngestResult, ingest_file
from scripts.pipeline.db import DEFAULT_DB_PATH, connect

_ROUTES = {
    # 通达信（默认第一优先源，§3.2 2026-08-21 起新增）
    ("tdx", "kline"): tdx.parse_kline_csv,
    ("tdx", "index"): tdx.parse_index_csv,
    ("tdx", "announcement"): tdx.parse_announcement_csv,
    ("tdx", "quotes"): tdx.parse_quotes_csv,
    ("tdx", "financials"): tdx.parse_financials_csv,        # 2026-08-23 起弥补 kimi 鉴权失效
    # kimi-datasource（fallback；access_token 失效需 /login）
    ("stock_finance_data", "price"): sfd.parse_price_csv,
    ("stock_finance_data", "financials"): sfd.parse_financials_csv,
    ("stock_finance_data", "announcement"): sfd.parse_announcement_csv,
    ("stock_finance_data", "forecast"): sfd.parse_forecast_csv,
    ("stock_finance_data", "index"): sfd.parse_index_csv,
    ("yahoo_finance", "price"): yf.parse_price_csv,
    ("yahoo_finance", "fx"): yf.parse_fx_csv,
    ("yahoo_finance", "stock_actions"): yf.parse_stock_actions_csv,
    ("yahoo_finance", "index"): yf.parse_index_csv,
    ("tianyancha", "announcement"): tyc.parse_announcement_csv,
    # akshare（可选源：字段对齐现有 adapter 约定；财报复用 tdx 解析含披露日）
    ("akshare", "price"): ak.parse_price_csv,
    ("akshare", "financials"): ak.parse_financials_csv,
    ("akshare", "index"): ak.parse_index_csv,
    ("akshare", "telegraph"): ak.parse_telegraph_csv,
    ("akshare", "announcement"): ak.parse_announcement_csv,  # 标准公告线格式（公共引擎）
    ("akshare", "forecast"): ak.parse_forecast_csv,      # 一致预期（转发 sfd 解析）
    ("akshare", "stock_info"): ak.parse_stock_info_csv,  # 股本快照 → share_capital_events
}


def _symbol_from_filename(path: Path) -> str | None:
    stem = path.stem
    for sep in ("_is_", "_bs_", "_cf_"):
        if sep in stem:
            return stem.split(sep)[0]
    return stem or None


def _route(path: Path):
    """从路径推断 (source, data_type)；不在 raw 约定结构内返回 None。"""
    parts = path.parts
    if "raw" not in parts:
        return None
    i = len(parts) - 1 - parts[::-1].index("raw")
    rel = parts[i + 1:]
    if len(rel) < 4:  # {source}/{data_type}/{date}/{run_id}/file.csv
        return None
    return rel[0], rel[1]


def iter_csv_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.csv")))
        elif path.suffix.lower() == ".csv":
            files.append(path)
        else:
            print(f"[skip] 非 CSV 文件/目录不存在: {p}")
    return files


def ingest_paths(conn, paths: list[str]) -> tuple[list[IngestResult], IngestResult]:
    results: list[IngestResult] = []
    total = IngestResult()
    for path in iter_csv_files(paths):
        route = _route(path)
        if route is None or route not in _ROUTES:
            r = IngestResult(file_path=str(path))
            r.errors.append(f"无法按路径推断 source/data_type 路由: {path}")
            results.append(r)
            total.merge(r)
            continue
        source, data_type = route
        if data_type == "price" and "_forward" in path.stem:
            r = IngestResult(source=source, data_type=data_type, file_path=str(path))
            r.skipped = 1
            r.notes.append("前复权文件，留给 D1.5 复权模块，本批不入 daily_bars")
            results.append(r)
            total.merge(r)
            continue
        r = ingest_file(
            conn, path,
            source=source, data_type=data_type,
            symbol=_symbol_from_filename(path),
            parse=_ROUTES[route],
        )
        results.append(r)
        total.merge(r)
    return results, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.pipeline.ingest")
    parser.add_argument("paths", nargs="+", help="raw CSV 文件或目录")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        results, total = ingest_paths(conn, args.paths)
    finally:
        conn.close()
    for r in results:
        print(r.summary())
    print(f"TOTAL [{total.status}] inserted={total.inserted} updated={total.updated} "
          f"skipped={total.skipped} conflicts={total.conflicts} "
          f"errors={len(total.errors)} incomplete={len(total.incomplete_reasons)}")
    return 1 if total.conflicts or total.errors else 0


if __name__ == "__main__":
    sys.exit(main())
