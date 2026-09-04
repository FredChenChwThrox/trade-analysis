"""symbol_names 名称目录采集器：东财全市场 A 股代码→官方简称。

独立手触发（不进 daily；名称近似静态，缺名时跑一次即可）：
    uv run python -m scripts.collect.symbol_names_collect --date 2026-08-29

数据域与 industry_collect 相同（push2delay，本网络实测直连可用），fields 取
f12（代码）+ f14（名称）。落盘 symbol_names/{date}/{run_id}/symbol_names.csv
后全量 upsert 进 symbol_names 表（名称改名以最新一次采集为准）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "data" / "raw" / "akshare"
DEFAULT_DB = ROOT / "data" / "market.db"
BASE = "https://push2delay.eastmoney.com/api/qt/clist/get"
_UT = "bd1d9ddb04089700cf9c27f6f7426281"
# 沪深京 A 股（与东财行情列表口径一致）
_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def _suffix(code: str) -> str:
    """6 位代码 → 交易所后缀（与 industry_collect 同口径）。"""
    if code.startswith(("6", "9")):
        return ".SH"
    if code.startswith(("4", "8")):
        return ".BJ"
    return ".SZ"


def fetch_all(session: requests.Session) -> list[tuple[str, str]]:
    """全市场分页拉取，返回 [(symbol, name)]。"""
    rows: list[dict] = []
    page = 1
    while True:
        r = session.get(BASE, params={
            "pn": page, "pz": 100, "po": 1, "np": 1, "ut": _UT, "fltt": 2,
            "invt": 2, "fid": "f3", "fs": _FS, "fields": "f12,f14",
        }, timeout=15)
        r.raise_for_status()
        data = r.json()["data"]
        rows.extend(data["diff"])
        if len(rows) >= data["total"]:
            break
        page += 1
    return [(f"{m['f12']}{_suffix(str(m['f12']))}", str(m["f14"]))
            for m in rows if m.get("f12") and m.get("f14")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.collect.symbol_names_collect")
    parser.add_argument("--date", required=True, help="落盘目录日期 YYYY-MM-DD")
    parser.add_argument("--run-id", default="run_names")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out-root", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    session = requests.Session()
    session.trust_env = False  # 直连（push2delay 实测可达；系统代理会断连）
    pairs = fetch_all(session)

    out = Path(args.out_root) / "symbol_names" / args.date / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    fp = out / "symbol_names.csv"
    with open(fp, "w", newline="", encoding="utf-8") as f:
        f.write("symbol,name\n")
        for sym, name in sorted(pairs):
            f.write(f"{sym},{name}\n")
    meta = {"run_id": args.run_id, "source": "eastmoney_em", "data_type": "symbol_names",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "purpose": "symbol_names 全市场 A 股名称目录（展示层用，缺名时手触发）",
            "requests": [{"api": "push2delay clist", "rows": len(pairs)}]}
    (out / "_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(args.db)
    try:
        with conn:
            for sym, name in pairs:
                conn.execute(
                    "INSERT INTO symbol_names (symbol, name, source, ingested_at)"
                    " VALUES (?, ?, 'eastmoney_em', ?)"
                    " ON CONFLICT(symbol) DO UPDATE SET"
                    " name=excluded.name, source=excluded.source,"
                    " ingested_at=excluded.ingested_at",
                    (sym, name, now))
    finally:
        conn.close()
    print(f"[symbol_names] {len(pairs)} 只 → {fp}，已 upsert 进 {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
