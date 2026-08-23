"""财报披露日回填（解除 D1.3 降级，恢复设计 §2.1 硬门槛：按正式披露时间生效）。

背景：financial_reports 历史入库时 published_at 全 NULL、available_at 填入
库时间（D1.3 降级）。本模块用天眼查"上市信息-上市公告"原始 CSV 回溯真实
披露日并回填：
- published_at = 公告日 00:00 本地（Asia/Shanghai）→ UTC；
- available_at = 下一个开市交易日 00:00 本地 → UTC（复用
  adapters/tianyancha._next_open_available_at 的保守规则，§2.1）；
- 每次改写写 data_revisions（旧值/新值含天眼查 uuid 与来源文件），
  回填前调用方应先导出 financial_reports 备份 CSV。

匹配规则（match_disclosure，纯函数，§2.5 不猜）：
- 按 (period_type, period_end) 生成标题关键词：
    annual(12-31)    → "{fy}年年度报告"
    quarterly(03-31) → "{fy}年第一季度报告" / "{fy}年一季度报告"
    interim(06-30)   → "{fy}年半年度报告"
    quarterly(09-30) → "{fy}年第三季度报告" / "{fy}年三季度报告"
- 只取 stock_code 等于该股 6 位代码的行（A+H 混排的 H 股公告不算）；
- 排除：英文版、修订版/（修订后）、更正版/（更正后）/更正公告、问询/回复、
  业绩说明会等非首披文本；摘要与全文同步披露，全文优先，缺全文时取摘要；
- 同标题多条取**最早**披露日；匹配不上返回 None（保持降级，不猜）。

CLI：
    uv run python -m scripts.pipeline.pit_backfill \
        --raw-dir data/raw/tianyancha/announcement/2026-08-17/pit_backfill \
        [--raw-dir ...] [--symbol 603605.SH ...] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.adapters.common import (
    load_calendar,
    market_tz,
    record_revision,
    sha256_file,
)
from scripts.adapters.tianyancha import _next_open_available_at
from scripts.pipeline.db import DEFAULT_DB_PATH, connect, utc_now

SOURCE = "tianyancha"
DATA_TYPE = "announcement"

# 非首披文本排除词（命中即跳过该行）
_EXCLUDE_WORDS = ("英文", "修订", "更正", "更新", "问询", "回复", "说明会", "审核")
# 摘要与全文同步披露：全文优先，缺全文时可取摘要（同日）
_ABSTRACT_WORD = "摘要"


def title_keywords(period_type: str, period_end: str, fiscal_year: int) -> list[str]:
    """按报告期生成披露公告标题关键词；未知期间组合返回空列表（不匹配）。"""
    mmdd = period_end[5:]
    fy = fiscal_year
    if period_type == "annual" and mmdd == "12-31":
        return [f"{fy}年年度报告"]
    if period_type == "interim" and mmdd == "06-30":
        return [f"{fy}年半年度报告"]
    if period_type == "quarterly" and mmdd == "03-31":
        return [f"{fy}年第一季度报告", f"{fy}年一季度报告"]
    if period_type == "quarterly" and mmdd == "09-30":
        return [f"{fy}年第三季度报告", f"{fy}年三季度报告"]
    return []


@dataclass
class AnnouncementRow:
    title: str
    date: str            # YYYY-MM-DD
    uuid: str | None
    stock_code: str
    source_file: str


@dataclass
class DisclosureMatch:
    disclosure_date: str
    title: str
    uuid: str | None
    source_file: str
    tier: str            # full / abstract


def load_announcements(raw_dirs: list[str | Path], symbol: str) -> list[AnnouncementRow]:
    """读取 <symbol>_p*.csv 公告行：过滤非该股 6 位代码行（A+H 混排 H 股跳过），
    按 uuid（缺省 title|date）去重。"""
    code6 = symbol.split(".")[0]
    seen: set[str] = set()
    rows: list[AnnouncementRow] = []
    for d in raw_dirs:
        for path in sorted(Path(d).glob(f"{symbol}_p*.csv")):
            with open(path, newline="", encoding="utf-8") as f:
                for rec in csv.DictReader(f):
                    title = (rec.get("title") or "").strip()
                    date_s = (rec.get("time") or "").strip()[:10]
                    if not title or not date_s:
                        continue
                    stock_code = (rec.get("stock_code") or "").strip()
                    if stock_code and stock_code != code6:
                        continue  # H 股/其他市场行
                    uuid = (rec.get("uuid") or "").strip() or None
                    key = uuid or f"{title}|{date_s}"
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(AnnouncementRow(
                        title=title, date=date_s, uuid=uuid,
                        stock_code=stock_code, source_file=path.name))
    return rows


def match_disclosure(
    period_type: str,
    period_end: str,
    fiscal_year: int,
    announcements: list[AnnouncementRow],
    *,
    code6: str | None = None,
) -> DisclosureMatch | None:
    """在公告流中找该财报的首披公告。匹配不上返回 None（§2.5 不猜）。

    code6：该股 6 位 A 股代码；传入时跳过 stock_code 不一致的行
    （A+H 混排的 H 股/其他市场公告不算，正常由 load_announcements 预过滤）。
    """
    keywords = title_keywords(period_type, period_end, fiscal_year)
    if not keywords:
        return None
    cands = [
        r for r in announcements
        if (code6 is None or not r.stock_code or r.stock_code == code6)
        and any(k in r.title for k in keywords)
        and not any(w in r.title for w in _EXCLUDE_WORDS)
    ]
    if not cands:
        return None
    full = [r for r in cands if _ABSTRACT_WORD not in r.title]
    tier_rows, tier = (full, "full") if full else (cands, "abstract")
    best = min(tier_rows, key=lambda r: r.date)
    return DisclosureMatch(
        disclosure_date=best.date, title=best.title, uuid=best.uuid,
        source_file=best.source_file, tier=tier)


# ---------------------------------------------------------------- 回填

@dataclass
class BackfillResult:
    run_id: str = ""
    matched: int = 0
    unmatched: list[dict] = field(default_factory=list)   # 保持降级的财报清单
    details: list[dict] = field(default_factory=list)     # 成功回填明细
    notes: list[str] = field(default_factory=list)


def register_raw_csvs(
    conn: sqlite3.Connection,
    raw_dirs: list[str | Path],
    symbols: list[str],
    *,
    run_id: str,
) -> dict[str, str]:
    """把本批原始 CSV 登记 raw_objects（幂等）。返回 {file_name: raw_object_id}。"""
    out: dict[str, str] = {}
    now = utc_now()
    for d in raw_dirs:
        for symbol in symbols:
            for path in sorted(Path(d).glob(f"{symbol}_p*.csv")):
                content_hash = sha256_file(path)
                raw_object_id = f"raw_tyc_ann_{content_hash[:12]}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO raw_objects (raw_object_id, run_id, source,
                        data_type, symbol, request_params_json, file_path,
                        content_hash, fetch_status, ingested_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?)
                    """,
                    (raw_object_id, run_id, SOURCE, DATA_TYPE, symbol,
                     json.dumps({"api": "上市信息-上市公告", "file": path.name},
                                ensure_ascii=False),
                     str(path), content_hash, now),
                )
                out[path.name] = raw_object_id
    return out


def export_reports_backup(conn: sqlite3.Connection, path: str | Path) -> int:
    """回填前导出 financial_reports 全表备份 CSV（可据此手工回滚）。返回行数。"""
    rows = conn.execute("SELECT * FROM financial_reports ORDER BY symbol, period_end").fetchall()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([d[0] for d in conn.execute("SELECT * FROM financial_reports LIMIT 0").description])
        for r in rows:
            w.writerow([r[k] for k in r.keys()])
    return len(rows)


def run_pit_backfill(
    conn: sqlite3.Connection,
    *,
    symbols: list[str],
    raw_dirs: list[str | Path],
    run_id: str,
    backup_path: str | Path | None = None,
    dry_run: bool = False,
) -> BackfillResult:
    """对指定股票回填 financial_reports.published_at/available_at（调用方负责事务）。

    匹配成功的行：published_at=披露日 00:00+08→UTC、available_at=下一开市日
    00:00+08→UTC，并逐行写 data_revisions（new_value 含 uuid/title/来源文件）。
    匹配不上的行保持原值不动，列入 unmatched 清单（§2.5 不猜）。
    """
    res = BackfillResult(run_id=run_id)
    if backup_path is not None:
        n = export_reports_backup(conn, backup_path)
        res.notes.append(f"financial_reports 备份 {n} 行 → {backup_path}")

    calendar = load_calendar(conn, "CN")
    if not calendar:
        raise ValueError("trading_calendar 缺失（market=CN），先 seed 日历再回填")
    raw_ids = {} if dry_run else register_raw_csvs(
        conn, raw_dirs, symbols, run_id=run_id)
    tz = market_tz("CN")

    for symbol in symbols:
        announcements = load_announcements(raw_dirs, symbol)
        if not announcements:
            res.notes.append(f"{symbol} 无公告原始文件")
        reports = conn.execute(
            """
            SELECT report_id, symbol, period_end, period_type, fiscal_year,
                   published_at, available_at
            FROM financial_reports WHERE symbol = ? ORDER BY period_end
            """,
            (symbol,),
        ).fetchall()
        for rep in reports:
            m = match_disclosure(
                rep["period_type"], rep["period_end"], rep["fiscal_year"],
                announcements, code6=symbol.split(".")[0])
            if m is None:
                res.unmatched.append({
                    "symbol": symbol, "period_end": rep["period_end"],
                    "period_type": rep["period_type"],
                    "fiscal_year": rep["fiscal_year"],
                })
                continue
            pub_local = datetime.combine(
                datetime.fromisoformat(m.disclosure_date).date(),
                datetime.min.time(), tzinfo=tz)
            published_at = pub_local.astimezone(timezone.utc).isoformat()
            available_at = _next_open_available_at(calendar, m.disclosure_date)
            res.matched += 1
            res.details.append({
                "symbol": symbol, "period_end": rep["period_end"],
                "period_type": rep["period_type"],
                "disclosure_date": m.disclosure_date, "tier": m.tier,
                "title": m.title, "uuid": m.uuid,
            })
            if dry_run:
                continue
            record_revision(
                conn,
                table_name="financial_reports",
                record_key={"report_id": rep["report_id"], "symbol": symbol,
                            "period_end": rep["period_end"],
                            "period_type": rep["period_type"]},
                old_value={"published_at": rep["published_at"],
                           "available_at": rep["available_at"]},
                new_value={
                    "published_at": published_at, "available_at": available_at,
                    "published_tz": "Asia/Shanghai",
                    "tianyancha_uuid": m.uuid, "title": m.title,
                    "tier": m.tier,
                    "source_file": m.source_file,
                    "raw_object_id": raw_ids.get(m.source_file),
                },
                source=f"{SOURCE} 上市信息-上市公告",
                reason="PIT 回填：财报按正式披露时间生效（解除 D1.3 降级）",
                run_id=run_id,
            )
            conn.execute(
                """
                UPDATE financial_reports
                SET published_at = ?, published_tz = 'Asia/Shanghai',
                    available_at = ?
                WHERE report_id = ?
                """,
                (published_at, available_at, rep["report_id"]),
            )
    return res


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.pipeline.pit_backfill")
    parser.add_argument("--raw-dir", action="append", required=True,
                        dest="raw_dirs", help="公告 CSV 目录（可多次）")
    parser.add_argument("--symbol", action="append", dest="symbols",
                        help="只处理指定股票（默认 watchlist 全部）")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--backup", default=None, help="financial_reports 备份 CSV 路径")
    parser.add_argument("--dry-run", action="store_true", help="只匹配不写库")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    run_id = args.run_id or (
        "pit_backfill_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    conn = connect(args.db)
    try:
        symbols = args.symbols or [
            r["symbol"] for r in conn.execute(
                "SELECT symbol FROM watchlist WHERE market = 'CN' AND active = 1 "
                "ORDER BY symbol")
        ]
        if args.dry_run:
            res = run_pit_backfill(
                conn, symbols=symbols, raw_dirs=args.raw_dirs,
                run_id=run_id, dry_run=True)
        else:
            with conn:
                res = run_pit_backfill(
                    conn, symbols=symbols, raw_dirs=args.raw_dirs,
                    run_id=run_id, backup_path=args.backup)
        print(f"run_id={res.run_id} matched={res.matched} unmatched={len(res.unmatched)}")
        for n in res.notes:
            print(f"  NOTE: {n}")
        for u in res.unmatched:
            print(f"  未匹配: {u['symbol']} {u['period_end']} {u['period_type']}")
        return 0 if not res.unmatched else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
