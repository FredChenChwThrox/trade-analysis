"""库内确定性信号事实的只读装载器（Phase A 时序事件版输入层）。

隔离说明：仍不 import scripts/pipeline|signals/*，自持 SQL 直查共享库
（与 data.py 读 daily_bars 同一原则）。所有表均为既有点时口径产物，
无未来函数由生产端保证（§5.1/§5.3），本层只消费。

口径要点：
- 衰竭计数：§5.3 "同一 anchor_id 下当前完成周仍 active 的不同信号数"；
  逐周逐锚分组 SUM(state='active')。
- 最近完成周：以 weekly_bars 已验证完成周日期集合为准（不自行推导周末）。
- 机械证伪线换算：HFQ 域定价 stop_adj = raw × f(锚点交易日)——后复权
  序列历史值稳定，故该止损位在曲线上永不因后续除权漂移（§5.4 反向折回）。
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_right
from dataclasses import dataclass

_EXHAUSTION_SIGNALS = ("panic", "dry_up", "no_new_low_3w", "divergence", "duration")


@dataclass(frozen=True)
class WeeklyExhaustionCount:
    week_end: str
    anchor_id: int
    n_active: int


def load_exhaustion_counts(conn: sqlite3.Connection,
                           symbol: str) -> dict[str, tuple[int, int]]:
    """返回 {week_end_date: (anchor_id, 最大同锚 active 数)}（§5.5 口径）。

    同一周多锚并存时取 active 数最大组；并列取 anchor_id 小者（稳定序）。
    """
    rows = conn.execute(
        """
        SELECT sf.observed_on AS week_end, sf.anchor_id,
               SUM(CASE WHEN sf.state = 'active' THEN 1 ELSE 0 END) AS n_active
        FROM signal_facts sf
        WHERE sf.symbol = ? AND sf.signal IN (?, ?, ?, ?, ?)
          AND sf.anchor_id IS NOT NULL
        GROUP BY sf.observed_on, sf.anchor_id
        """,
        (symbol, *_EXHAUSTION_SIGNALS),
    ).fetchall()
    out: dict[str, tuple[int, int]] = {}
    for r in rows:
        week = r["week_end"]
        n = r["n_active"] or 0
        aid = r["anchor_id"]
        cur = out.get(week)
        # n 主序、同 n 取 anchor_id 小者（稳定序）
        if cur is None or (n, -aid) > (cur[1], -cur[0]):
            out[week] = (aid, n)
    return dict(sorted(out.items()))


def load_completed_weeks(conn: sqlite3.Connection, symbol: str) -> list[str]:
    """该股已验证完成周的 week_end 升序列表（无周线则空——不猜）。"""
    rows = conn.execute(
        "SELECT week_end_date FROM weekly_bars WHERE symbol = ?"
        " ORDER BY week_end_date", (symbol,),
    ).fetchall()
    return [r["week_end_date"] for r in rows]


def latest_week_before(weeks: list[str], date: str) -> str | None:
    """≤date 的最近完成周；无则 None。"""
    idx = bisect_right(weeks, date)
    return weeks[idx - 1] if idx else None


@dataclass(frozen=True)
class DeclineStartAnchor:
    anchor_id: int
    trade_date: str          # 锚点交易日（市场本地）
    raw_price: float         # 当日不复权收盘（§3.4 锚点双记）
    adj_price: float         # 当日后复权价（识别时）
    stop_adj: float | None   # 机械证伪线（HFQ 域），=None 表示缺因子不设线


def load_decline_starts(conn: sqlite3.Connection, symbol: str,
                        stop_pct: float) -> list[DeclineStartAnchor]:
    """全部 decline_start 锚点 → 机械证伪线序列（HFQ 域，按 trade_date 升序）。

    stop_adj = raw_price × price_adj_factor(锚点交易日)。当日因子缺失时不猜，
    该锚不参与止损（入场仍允许，出场回退到下一锚上线）。
    """
    rows = conn.execute(
        """
        SELECT wa.anchor_id, wa.trade_date, wa.raw_price, wa.adjusted_price,
               db.price_adj_factor
        FROM weekly_anchors wa
        LEFT JOIN daily_bars db
               ON db.symbol = wa.symbol AND db.trade_date = wa.trade_date
        WHERE wa.symbol = ? AND wa.anchor_type = 'decline_start'
          AND wa.is_fallback = 0
        ORDER BY wa.trade_date
        """,
        (symbol,),
    ).fetchall()
    out: list[DeclineStartAnchor] = []
    for r in rows:
        raw = float(r["raw_price"])
        factor = r["price_adj_factor"]
        # HFQ 域定价：锚点日因子一次性落位，历史点不随后续除权漂移
        stop = None if factor is None else round(float(r["adjusted_price"]) * (1 - stop_pct), 6)
        out.append(DeclineStartAnchor(
            anchor_id=r["anchor_id"], trade_date=r["trade_date"],
            raw_price=raw, adj_price=float(r["adjusted_price"]),
            stop_adj=stop,
        ))
    return out


def anchor_for_date(anchors: list[DeclineStartAnchor],
                    week_end: str) -> DeclineStartAnchor | None:
    """≤week_end 最近一次识别的 decline_start 锚（事件按识别时序生效）。"""
    prior = [a for a in anchors if a.trade_date <= week_end]
    return prior[-1] if prior else None
