"""指标计算入口（D1.7，设计 §4.2、§4.3）。

对指定股票全量重算 indicators_daily 与 indicators_weekly：
- 日线：daily_bars 复权后 OHLC + 调整后成交量 → core.compute_indicators；
  pe_ttm/pe_status 由 valuation.compute_pe_series 按点时口径补（不复权市值口径）。
- 周线：weekly_bars（已是逐日复权聚合的完成周）→ 同参数 compute_indicators，
  周期单位为周；MA120/250 等窗口不足自然为 NaN（不写入周表无对应列的指标）。
- 全量重算：DELETE + 重插，同事务（§4.3：约 3 年日线适合全量重算）。
- run 记录：pipeline_runs 写 run_id/config_hash（indicators.yaml 内容哈希）/
  rule_version/pandas 版本（§4.2）。

CLI：
    uv run python -m scripts.indicators.compute <symbol> [--db PATH]
"""

from __future__ import annotations

import argparse
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from scripts.adapters.common import sha256_file
from scripts.indicators import core, valuation
from scripts.pipeline.db import CONFIG_DIR, DEFAULT_DB_PATH, connect, utc_now

INDICATORS_CONFIG = CONFIG_DIR / "indicators.yaml"

DAILY_INDICATOR_COLS = [
    "ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
    "dif", "dea", "macd_hist",
    "rsi6", "rsi12", "rsi24",
    "boll_mid", "boll_upper", "boll_lower", "boll_bandwidth",
    "vol_ma5", "vol_ma10",
    "vol_mean20", "vol_std20", "vol_mean60", "vol_std60",
    "amt_mean20", "amt_std20", "amt_mean60", "amt_std60",
    "kdj_k", "kdj_d", "kdj_j",
    "pct_chg", "amplitude",
]

WEEKLY_INDICATOR_COLS = [
    "ma5", "ma10", "ma20", "ma60",
    "dif", "dea", "macd_hist",
    "rsi6", "rsi12", "rsi24",
    "boll_mid", "boll_upper", "boll_lower", "boll_bandwidth",
    "vol_ma5", "vol_ma10", "vol_mean20", "vol_std20",
    "kdj_k", "kdj_d", "kdj_j",
    "pct_chg", "amplitude",
]


@dataclass
class ComputeResult:
    symbol: str = ""
    run_id: str = ""
    config_hash: str = ""
    rule_version: str = core.RULE_VERSION
    daily_rows: int = 0
    weekly_rows: int = 0
    pe_ok: int = 0
    pe_null: int = 0
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [
            f"{self.symbol} run_id={self.run_id} rule_version={self.rule_version}",
            f"config_hash={self.config_hash[:12]}…",
            f"indicators_daily {self.daily_rows} 行，indicators_weekly {self.weekly_rows} 行，"
            f"pe_ttm 非空 {self.pe_ok} / 空 {self.pe_null}",
        ]
        lines.extend(f"  NOTE: {n}" for n in self.notes)
        return "\n".join(lines)


def load_params() -> tuple[dict, str]:
    """读取 config/indicators.yaml defaults 与内容哈希（§4.2）。"""
    doc = yaml.safe_load(INDICATORS_CONFIG.read_text(encoding="utf-8"))
    return doc["defaults"], sha256_file(INDICATORS_CONFIG)


def _daily_frame(conn: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT trade_date, open_raw, high_raw, low_raw, close_raw,
               volume_raw, amount_raw, price_adj_factor, share_factor
        FROM daily_bars WHERE symbol = ? ORDER BY trade_date
        """,
        (symbol,),
    ).fetchall()
    if not rows:
        raise ValueError(f"{symbol} 无 daily_bars，先入库行情与复权因子")
    df = pd.DataFrame([dict(r) for r in rows]).set_index("trade_date")
    f = df["price_adj_factor"].fillna(1.0)
    sf = df["share_factor"].fillna(1.0)
    frame = pd.DataFrame({
        "open": df["open_raw"] * f,
        "high": df["high_raw"] * f,
        "low": df["low_raw"] * f,
        "close": df["close_raw"] * f,
        "volume": df["volume_raw"] / sf,
        "amount": df["amount_raw"],
    }, index=df.index)
    return frame


def _weekly_frame(conn: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT week_end_date, open_adj, high_adj, low_adj, close_adj, volume_adj
        FROM weekly_bars WHERE symbol = ? ORDER BY week_end_date
        """,
        (symbol,),
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows]).set_index("week_end_date")
    return pd.DataFrame({
        "open": df["open_adj"], "high": df["high_adj"], "low": df["low_adj"],
        "close": df["close_adj"], "volume": df["volume_adj"],
    }, index=df.index)


def _num(v) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def recompute_indicators(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    run_id: str | None = None,
    assume_visible_reports: bool = True,
) -> ComputeResult:
    """全量重算该股日线/周线指标（调用方负责事务/提交）。

    assume_visible_reports：当前财报 available_at 为入库时间降级（D1.3 记录，
    严格点时将导致全序列财报不可见），照常使用并在 pe_status 标注
    ";degraded_available_at"（§2.1 降级场景）。
    """
    started_at = utc_now()
    now_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = run_id or f"indicators_{symbol}_{now_compact}"
    params, config_hash = load_params()
    res = ComputeResult(symbol=symbol, run_id=run_id, config_hash=config_hash)

    watch = conn.execute(
        "SELECT timezone, currency FROM watchlist WHERE symbol = ?", (symbol,),
    ).fetchone()
    if watch is None:
        raise ValueError(f"{symbol} 不在 watchlist")

    # ---- 日线
    frame = _daily_frame(conn, symbol)
    ind = core.compute_indicators(frame, params)
    close_raw = conn.execute(
        "SELECT trade_date, close_raw FROM daily_bars WHERE symbol = ?", (symbol,),
    ).fetchall()
    close_raw_map = {r["trade_date"]: r["close_raw"] for r in close_raw}
    pe = valuation.compute_pe_series(
        conn, symbol, list(frame.index), close_raw_map,
        watch["timezone"], watch["currency"],
        assume_visible=assume_visible_reports,
    )
    if assume_visible_reports:
        res.notes.append(
            "财报 available_at 为入库时间降级（D1.3），TTM 照常使用全部报告，"
            "pe_status 标注 degraded_available_at")

    now = utc_now()
    conn.execute("DELETE FROM indicators_daily WHERE symbol = ?", (symbol,))
    for d in frame.index:
        pe_val, pe_status = pe[d]
        if pe_val is None:
            res.pe_null += 1
        else:
            res.pe_ok += 1
        conn.execute(
            f"""
            INSERT INTO indicators_daily (symbol, trade_date,
                {", ".join(DAILY_INDICATOR_COLS)},
                pe_ttm, pe_status, run_id, rule_version, config_hash, computed_at)
            VALUES (?, ?, {", ".join("?" * len(DAILY_INDICATOR_COLS))}, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, d, *[_num(ind[c].loc[d]) for c in DAILY_INDICATOR_COLS],
             pe_val, pe_status, run_id, core.RULE_VERSION, config_hash, now),
        )
        res.daily_rows += 1

    # ---- 周线（weekly_bars 已是复权聚合的完成周，周期单位为周）
    wframe = _weekly_frame(conn, symbol)
    conn.execute("DELETE FROM indicators_weekly WHERE symbol = ?", (symbol,))
    if wframe.empty:
        res.notes.append("无 weekly_bars，周线指标未生成")
    else:
        wind = core.compute_indicators(wframe, params)
        for w in wframe.index:
            conn.execute(
                f"""
                INSERT INTO indicators_weekly (symbol, week_end_date,
                    {", ".join(WEEKLY_INDICATOR_COLS)},
                    run_id, rule_version, config_hash, computed_at)
                VALUES (?, ?, {", ".join("?" * len(WEEKLY_INDICATOR_COLS))}, ?, ?, ?, ?)
                """,
                (symbol, w, *[_num(wind[c].loc[w]) for c in WEEKLY_INDICATOR_COLS],
                 run_id, core.RULE_VERSION, config_hash, now),
            )
            res.weekly_rows += 1

    # ---- run 记录（§2.3、§4.2）
    conn.execute(
        """
        INSERT OR REPLACE INTO pipeline_runs (run_id, stage, as_of, data_cutoff,
            adapter_version, config_hash, rule_version, app_version,
            status, error, started_at, finished_at)
        VALUES (?, 'indicators', ?, ?, NULL, ?, ?, ?, 'success', NULL, ?, ?)
        """,
        (run_id, utc_now(), frame.index[-1], config_hash, core.RULE_VERSION,
         f"pandas {pd.__version__}", started_at, utc_now()),
    )
    return res


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.indicators.compute")
    parser.add_argument("symbol")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        with conn:  # DELETE + 重插 + run 记录同一事务（§4.3）
            res = recompute_indicators(conn, args.symbol)
        print(res)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
