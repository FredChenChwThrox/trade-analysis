"""D1.5 复权因子 golden tests（设计 §9.5 硬门槛：复权公式 golden tests）。

手工小数据集（2026-07-06~07-31，20 个交易日）：
- 真实价序列 p_i = 100 + 0.5*i（单调，便于断言）；
- 2026-07-15（周三）现金分红 ≈1%：raw ×0.99；
- 2026-07-22（周三）10 送 10：raw ×0.5，volume ×2；
- 来源前复权锚定最新价：fwd_i = 0.495 * p_i（连续），f 平台 = 0.495 / 0.5 / 1.0。

锁定：平台段检测结果、归一化 origin=1.0、除权前后复权序列收益率连续、
share_factor 方向（历史量放大到当前股本口径）、因子变化检测三态、
probe01 真实数据平台切换日 = 实测 5 个除权日、末段因子 = 1.0。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts.pipeline import adjust, db
from scripts.pipeline.adjust import (
    apply_adjustment,
    check_factor_change,
    compute_share_factors,
    compute_source_factor,
    detect_factor_change,
    detect_plateaus,
    load_forward_closes,
    normalize_factors,
    plateau_series,
)

# ------------------------------------------------------------- 手工数据集

def _weekdays(start: str, end: str) -> list[str]:
    d, e = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = _weekdays("2026-07-06", "2026-07-31")  # 20 个交易日，4 个完整周
DIV_EX = "2026-07-15"    # 现金分红除权（周中）
SPLIT_EX = "2026-07-22"  # 10 送 10 除权（周中）


def true_price(d: str) -> float:
    return 100.0 + 0.5 * DAYS.index(d)


def build_series():
    """raw/fwd 收盘价（2 位小数，模拟来源舍入噪声）与 raw 成交量。"""
    raw, fwd, vol = {}, {}, {}
    for d in DAYS:
        p = true_price(d)
        m = 1.0 if d < DIV_EX else (0.99 if d < SPLIT_EX else 0.99 * 0.5)
        raw[d] = round(p * m, 2)
        fwd[d] = round(0.495 * p, 2)
        vol[d] = 1000.0 if d < SPLIT_EX else 2000.0
    return raw, fwd, vol


def add_calendar(conn: sqlite3.Connection) -> None:
    now = db.utc_now()
    for d in _weekdays("2026-07-01", "2026-08-31"):
        conn.execute(
            """
            INSERT INTO trading_calendar (market, trade_date, is_open, is_full_day,
                                          session_open, session_close, status,
                                          status_detail, timezone, source, updated_at)
            VALUES ('CN', ?, 1, 1, NULL, NULL, 'trading', NULL, 'Asia/Shanghai', 'test', ?)
            """,
            (d, now),
        )
    conn.commit()


def add_daily_bars(conn: sqlite3.Connection, symbol: str = "TEST.SH") -> None:
    raw, _, vol = build_series()
    now = db.utc_now()
    for d in DAYS:
        p = true_price(d)
        m = raw[d] / p  # 段内倍率（含 2 位小数舍入）
        conn.execute(
            """
            INSERT INTO daily_bars (symbol, trade_date, market,
                open_raw, high_raw, low_raw, close_raw, volume_raw, amount_raw,
                currency, price_adj_factor, share_factor, trading_status,
                source, raw_object_id, updated_at)
            VALUES (?, ?, 'CN', ?, ?, ?, ?, ?, NULL, 'CNY', 1.0, 1.0,
                    'normal', 'test', NULL, ?)
            """,
            (symbol, d, round((p - 0.2) * m, 2), round((p + 0.7) * m, 2),
             round((p - 0.6) * m, 2), raw[d], vol[d], now),
        )
    conn.commit()


def add_corporate_actions(conn: sqlite3.Connection, symbol: str = "TEST.SH") -> None:
    now = db.utc_now()
    conn.execute(
        "INSERT INTO corporate_actions (symbol, ex_date, action_type, cash_per_share,"
        " source, created_at) VALUES (?, ?, 'cash_dividend', '1.0', 'test', ?)",
        (symbol, DIV_EX, now))
    conn.execute(
        "INSERT INTO corporate_actions (symbol, ex_date, action_type, split_ratio,"
        " source, created_at) VALUES (?, ?, 'bonus_share', '2.0', 'test', ?)",
        (symbol, SPLIT_EX, now))
    conn.commit()


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "market.db")
    db.migrate(c)
    add_calendar(c)
    add_daily_bars(c)
    add_corporate_actions(c)
    yield c
    c.close()


# ------------------------------------------------------------- 平台段检测（纯函数）

def test_plateau_detection_synthetic():
    raw, fwd, _ = build_series()
    f = compute_source_factor(raw, fwd)
    plateaus = detect_plateaus(f)
    assert len(plateaus) == 3
    assert [p.start for p in plateaus] == [DAYS[0], DIV_EX, SPLIT_EX]
    assert plateaus[0].end == "2026-07-14"
    assert plateaus[1].end == "2026-07-21"
    assert plateaus[2].end == DAYS[-1]
    assert plateaus[0].factor == pytest.approx(0.495, abs=2e-4)
    assert plateaus[1].factor == pytest.approx(0.5, abs=2e-4)
    assert plateaus[2].factor == pytest.approx(1.0, abs=2e-4)


def test_normalize_origin_is_one():
    raw, fwd, _ = build_series()
    f = compute_source_factor(raw, fwd)
    series = plateau_series(f, detect_plateaus(f))
    internal = normalize_factors(series, DAYS[0])
    assert internal[DAYS[0]] == pytest.approx(1.0)
    # 内部因子单调累积：1.0 → ≈1.0101 → ≈2.0202
    assert internal["2026-07-15"] == pytest.approx(1.0 / 0.99, abs=1e-3)
    assert internal[DAYS[-1]] == pytest.approx(1.0 / 0.495, abs=1e-3)


def test_adjusted_returns_continuous_across_ex_dates():
    """除权前后复权序列收益率连续（= 真实价序列收益率，无机械跳变）。"""
    raw, fwd, _ = build_series()
    f = compute_source_factor(raw, fwd)
    series = plateau_series(f, detect_plateaus(f))
    internal = normalize_factors(series, DAYS[0])
    adj = {d: raw[d] * internal[d] for d in DAYS}
    for prev, cur in zip(DAYS, DAYS[1:]):
        ret_adj = adj[cur] / adj[prev] - 1
        ret_true = true_price(cur) / true_price(prev) - 1
        assert ret_adj == pytest.approx(ret_true, abs=2e-3), f"{prev}->{cur}"


def test_share_factor_direction():
    """10 送 10 之前 share_factor=0.5（历史量 ÷0.5 放大到当前股本口径），之后 1.0。"""
    factors, todos = compute_share_factors(DAYS, [(SPLIT_EX, 2.0)])
    assert factors["2026-07-21"] == pytest.approx(0.5)
    assert factors[SPLIT_EX] == pytest.approx(1.0)
    assert len(todos) == 1 and "TODO" in todos[0]
    # 无送转事件 → 全 1.0，无 TODO
    factors2, todos2 = compute_share_factors(DAYS, [])
    assert all(v == 1.0 for v in factors2.values()) and todos2 == []


# ------------------------------------------------------------- 因子变化检测

def _internal_from_synthetic():
    raw, fwd, _ = build_series()
    f = compute_source_factor(raw, fwd)
    series = plateau_series(f, detect_plateaus(f))
    return raw, normalize_factors(series, DAYS[0])


def test_factor_change_none():
    """重采同样的 forward：r_t 恒等于 f_origin → 不变化。"""
    raw, internal = _internal_from_synthetic()
    _, fwd, _ = build_series()
    f_new = compute_source_factor(raw, {d: fwd[d] for d in DAYS[-5:]})
    res = detect_factor_change(
        {d: internal[d] for d in DAYS[-5:]}, f_new, f_origin_ref=0.495)
    assert not res.changed and res.n_overlap == 5


def test_factor_change_new_dividend():
    """新分红后整段历史前移一个平台（×0.99）：整体位移 → 变化。"""
    raw, internal = _internal_from_synthetic()
    _, fwd, _ = build_series()
    window = DAYS[-5:]
    f_new = {d: fwd[d] * 0.99 / raw[d] for d in window}
    res = detect_factor_change({d: internal[d] for d in window}, f_new,
                               f_origin_ref=0.495)
    assert res.changed and "整体位移" in res.reason


def test_factor_change_ex_date_inside_window():
    """除权落在重叠窗口内：r_t 出现两个平台 → 变化。"""
    raw, internal = _internal_from_synthetic()
    _, fwd, _ = build_series()
    window = DAYS[-5:]
    f_new = {}
    for i, d in enumerate(window):
        scale = 0.99 if i < 2 else 1.0  # 窗口第 3 天为假想的除权生效日
        f_new[d] = fwd[d] * scale / raw[d]
    res = detect_factor_change({d: internal[d] for d in window}, f_new,
                               f_origin_ref=0.495)
    assert res.changed and "窗口内" in res.reason


# ------------------------------------------------------------- DB 级：全量重建

def test_apply_adjustment_full_rebuild(conn):
    _, fwd, _ = build_series()
    with conn:
        res = apply_adjustment(conn, "TEST.SH", forward_closes=fwd, run_id="run_t1")
    assert res.origin_date == DAYS[0]
    assert res.switch_dates == [DIV_EX, SPLIT_EX]
    assert res.bars_updated == len(DAYS)
    assert res.warnings == []  # 平台切换日与 corporate_actions 对齐
    # 因子写库：origin=1.0，末段 ≈2.0202，送转前 share_factor=0.5
    rows = {r["trade_date"]: r for r in conn.execute(
        "SELECT * FROM daily_bars WHERE symbol='TEST.SH'")}
    assert rows[DAYS[0]]["price_adj_factor"] == pytest.approx(1.0)
    assert rows[DAYS[-1]]["price_adj_factor"] == pytest.approx(1.0 / 0.495, abs=1e-3)
    assert rows["2026-07-21"]["share_factor"] == pytest.approx(0.5)
    assert rows[SPLIT_EX]["share_factor"] == pytest.approx(1.0)
    # OHLC 原始值不动
    assert rows[DIV_EX]["close_raw"] == pytest.approx(round(true_price(DIV_EX) * 0.99, 2))
    # 版本记录
    v = conn.execute("SELECT * FROM adjustment_factor_versions WHERE symbol='TEST.SH'"
                     ).fetchone()
    assert v["factor_origin_date"] == DAYS[0]
    assert v["algorithm"] == adjust.ALGORITHM
    notes = json.loads(v["notes"])
    assert notes["source_factor_at_origin"] == pytest.approx(0.495, abs=2e-4)
    assert notes["switch_dates"] == [DIV_EX, SPLIT_EX]
    assert len(notes["share_todos"]) == 1  # 送转 TODO 提示
    # 周线同事务重建（4 个完成周）
    assert res.weekly is not None and res.weekly.weeks_written == 4
    n_week = conn.execute(
        "SELECT COUNT(*) c FROM weekly_bars WHERE symbol='TEST.SH'").fetchone()["c"]
    assert n_week == 4
    # pipeline_runs 留痕
    run = conn.execute(
        "SELECT * FROM pipeline_runs WHERE run_id='run_t1' AND stage='adjust'").fetchone()
    assert run is not None and run["status"] == "success"


def test_check_factor_change_against_db(conn):
    """库内因子 + 新采重叠窗口：先重建，再检测（不变化 / 变化两态）。"""
    _, fwd, _ = build_series()
    with conn:
        apply_adjustment(conn, "TEST.SH", forward_closes=fwd, run_id="run_t2")
    res = check_factor_change(conn, "TEST.SH", _write_fwd_csv(fwd, DAYS[-5:], conn))
    assert not res.changed


def _write_fwd_csv(fwd, dates, conn) -> Path:
    import tempfile
    path = Path(tempfile.mkstemp(suffix=".csv")[1])
    lines = ["open,high,low,close,volume,thscode,time,thsname_cn,thsname_en,currency"]
    for d in dates:
        c = fwd[d]
        lines.append(f"{c},{c},{c},{c},1000,TEST.SH,{d.replace('-', '')},X,NA,CNY")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ------------------------------------------------------------- probe01 真实数据

PROBE_DIR = (Path(__file__).resolve().parents[1] / "data" / "raw"
             / "stock_finance_data" / "price" / "2026-08-09" / "run_probe01")
EXPECTED_EX = ["2023-10-23", "2024-06-25", "2025-06-17", "2025-10-17", "2026-07-22"]


def _biz_day_distance(a: str, b: str) -> int:
    """两个日期间的工作日数差（容差判定用，不需要精确日历）。"""
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    if da > db:
        da, db = db, da
    n, d = 0, da
    while d < db:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


@pytest.mark.skipif(not (PROBE_DIR / "603605.SH.csv").exists(),
                    reason="probe01 原始数据不存在")
def test_probe01_plateau_switch_dates():
    """真实数据：检出平台切换日 = probe 实测 5 个除权日（±1 交易日），末段因子=1.0。"""
    raw = load_forward_closes(PROBE_DIR / "603605.SH.csv")
    fwd = load_forward_closes(PROBE_DIR / "603605.SH_forward_3y.csv")
    assert len(raw) == 726 and len(fwd) == 726
    plateaus = detect_plateaus(compute_source_factor(raw, fwd))
    switches = [p.start for p in plateaus[1:]]
    assert len(plateaus) == 6, f"plateaus={plateaus}"
    for sw, ex in zip(switches, EXPECTED_EX):
        assert _biz_day_distance(sw, ex) <= 1, f"{sw} vs {ex}"
    assert plateaus[-1].factor == pytest.approx(1.0, abs=1e-3)
    # 首段因子与 probe 记录一致（0.9446）
    assert plateaus[0].factor == pytest.approx(0.9446, abs=1e-3)
