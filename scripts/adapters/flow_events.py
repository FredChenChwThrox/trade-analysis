"""flow 层采集 CSV → events（scope='flow'，消息面 r2 Phase 2）。

采集：scripts/collect/akshare_collect.py --sources flow（龙虎榜 stock_lhb_detail_em
+ 大宗交易 stock_dzjy_mrmx，仅留 watchlist 行；龙虎榜按"股票×日"合并多上榜原因），
落盘 flow/{date}/{run_id}/{lhb,dzjy}.csv，经 ingest 路由 ("akshare","flow") 进入。

纪律（r2 §3.2/§8.4）：flow 层静默入库——本模块只写事实行，不推送、不进日报、
报告消息面段不展示（报告无任何改动）；scope='flow' 是 Phase 2 唯一填充的
events.scope 值（公告/电报的 scope 分类仍属 Phase 3）。

信源分级：source_tier=3（SOURCE_TIER_FLOW）——交易所公开信息经东财聚合加工
（净买额/折溢价等为计算值），非原文文件；对齐 r2 §4 flow 层 tier 3~5 区间。

幂等：event_id 确定性哈希（lhb 按 股票×日，dzjy 按 股票×日×成交价×成交量），
同内容重跑跳过；available_at=published_at（盘后数据当日可得，§2.1）。
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.adapters.announcements import SOURCE_TIER_FLOW
from scripts.adapters.common import IngestResult, market_tz, utc_now

SOURCE = "akshare"
_KIND_LHB = "lhb"
_KIND_DZJY = "dzjy"
_CST = ZoneInfo("Asia/Shanghai")


def _event_id(kind: str, external: str) -> str:
    return "evt_" + hashlib.sha256(f"{SOURCE}|{kind}|{external}".encode()).hexdigest()[:16]


def _day_utc_iso(day: str) -> str:
    """市场本地日期 00:00 CST → UTC ISO（flow 盘后数据当日可得）。"""
    return datetime.fromisoformat(day).replace(tzinfo=_CST).astimezone(timezone.utc).isoformat()


def _fmt_yi(v: str) -> str:
    try:
        return f"{float(v) / 1e8:.2f} 亿"
    except ValueError:
        return v


def parse_flow_csv(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                   result: IngestResult) -> IngestResult:
    """flow 目录 CSV 入口：按文件 stem 分派到龙虎榜/大宗解析。"""
    stem = path.stem
    if stem.startswith("lhb"):
        return _parse_lhb(conn, path, raw_object_id, result)
    if stem.startswith("dzjy"):
        return _parse_dzjy(conn, path, raw_object_id, result)
    result.skipped += 1
    result.notes.append(f"flow 未知文件 stem: {path.name}（期望 lhb*/dzjy*）")
    return result


def _insert_event(conn: sqlite3.Connection, result: IngestResult, *, kind: str,
                  external_id: str, published_at: str, title: str, summary: str,
                  symbol: str, content_hash: str, raw_object_id: str) -> None:
    event_id = _event_id(kind, external_id)
    if conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone():
        result.skipped += 1
        return
    now = utc_now()
    conn.execute(
        """
        INSERT INTO events (event_id, event_type, event_at, published_at,
            published_tz, available_at, title, summary, canonical_url,
            source, source_external_id, content_hash, raw_object_id, ingested_at,
            source_tier, scope)
        VALUES (?, 'flow', NULL, ?, 'Asia/Shanghai', ?, ?, ?, NULL,
                ?, ?, ?, ?, ?, ?, 'flow')
        """,
        (event_id, published_at, published_at, title, summary, SOURCE,
         external_id, content_hash, raw_object_id, now, SOURCE_TIER_FLOW),
    )
    conn.execute(
        "INSERT OR IGNORE INTO event_symbols (event_id, symbol) VALUES (?, ?)",
        (event_id, symbol))
    result.inserted += 1


def _parse_lhb(conn: sqlite3.Connection, path: Path, raw_object_id: str,
               result: IngestResult) -> IngestResult:
    """lhb.csv（symbol,trade_date,reasons,close,pct_chg,net_buy,net_buy_ratio）
    → events(event_type='flow', scope='flow')，每股每日一行（采集端已合并多上榜原因）。"""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.skipped += 1
        result.notes.append("龙虎榜 CSV 无数据行")
        return result
    for rec in rows:
        symbol = (rec.get("symbol") or "").strip()
        day = (rec.get("trade_date") or "").strip()[:10]
        if not symbol or not day:
            result.skipped += 1
            result.notes.append(f"龙虎榜行缺 symbol/trade_date（{rec}），行级跳过")
            continue
        reasons = (rec.get("reasons") or "").strip() or "上榜"
        net_buy = (rec.get("net_buy") or "").strip()
        ratio = (rec.get("net_buy_ratio") or "").strip()
        pct_chg = (rec.get("pct_chg") or "").strip()
        title = f"龙虎榜上榜（{reasons}）"
        parts = []
        if net_buy:
            direction = "净买入" if not net_buy.startswith("-") else "净卖出"
            parts.append(f"{direction} {_fmt_yi(net_buy)}元")
        if ratio:
            parts.append(f"占成交比 {ratio}%")
        if pct_chg:
            parts.append(f"当日涨跌幅 {pct_chg}%")
        summary = "；".join(parts) or None
        _insert_event(
            conn, result, kind=_KIND_LHB,
            external_id=f"lhb:{symbol}:{day}",
            published_at=_day_utc_iso(day),
            title=title, summary=summary, symbol=symbol,
            content_hash=hashlib.sha256(
                f"lhb|{symbol}|{day}|{reasons}|{net_buy}".encode()).hexdigest(),
            raw_object_id=raw_object_id)
    return result


def _parse_dzjy(conn: sqlite3.Connection, path: Path, raw_object_id: str,
                 result: IngestResult) -> IngestResult:
    """dzjy.csv（symbol,trade_date,close,pct_chg,price,premium_rate,volume,amount,
    buy_branch,sell_branch）→ events(event_type='flow', scope='flow')，每笔一行。"""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        result.skipped += 1
        result.notes.append("大宗 CSV 无数据行")
        return result
    for rec in rows:
        symbol = (rec.get("symbol") or "").strip()
        day = (rec.get("trade_date") or "").strip()[:10]
        price = (rec.get("price") or "").strip()
        volume = (rec.get("volume") or "").strip()
        if not symbol or not day or not price or not volume:
            result.skipped += 1
            result.notes.append(f"大宗行缺必填列（{rec}），行级跳过")
            continue
        premium = (rec.get("premium_rate") or "").strip()
        amount = (rec.get("amount") or "").strip()
        buyer = (rec.get("buy_branch") or "").strip()
        seller = (rec.get("sell_branch") or "").strip()
        pct_chg = (rec.get("pct_chg") or "").strip()
        title = f"大宗交易：成交价 {price}" + (f"（折溢价 {premium}%）" if premium else "")
        parts = [f"成交量 {volume} 股"]
        if amount:
            parts.append(f"成交额 {_fmt_yi(amount)}元")
        if buyer:
            parts.append(f"买方 {buyer}")
        if seller:
            parts.append(f"卖方 {seller}")
        if pct_chg:
            parts.append(f"当日涨跌幅 {pct_chg}%")
        external_id = f"dzjy:{symbol}:{day}:{price}:{volume}"
        _insert_event(
            conn, result, kind=_KIND_DZJY, external_id=external_id,
            published_at=_day_utc_iso(day), title=title,
            summary="；".join(parts), symbol=symbol,
            content_hash=hashlib.sha256(
                f"dzjy|{symbol}|{day}|{price}|{volume}|{amount}".encode()).hexdigest(),
            raw_object_id=raw_object_id)
    return result
