"""factor_watch 行业因子快照测试（§2.1 点时语义 / §3.7 stale 降级口径）。

锁定：
- 时间对齐：GLOBAL 因子对 A 股 as_of 取 T-1（trade_date < as_of），
  CN 因子同日可用（trade_date <= as_of）；
- stale：最新读数距 as_of 超过 5 个 CN 交易日 → status='stale'；
- change_20d/60d 按因子自身读数序列计算，样本不足为 None（§2.5）；
- 映射解析：symbol 覆盖替换行业映射；无映射 → factors=[] + note，不报错；
- 配置校验：code 不在 macro_factors.yaml 因子清单 → IndustryFactorsError。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from scripts.pipeline import db
from scripts.signals import factor_watch as fw

AS_OF = "2026-08-28"        # 周五，CN 交易日


def _ins_factor(c, code, name, market, trade_date, close, unit="元/吨"):
    c.execute(
        """
        INSERT INTO macro_factors (factor_type, code, name, market, trade_date,
                                   close, change_pct, unit, source, ingested_at)
        VALUES ('commodity', ?, ?, ?, ?, ?, NULL, ?, 'test', '2026-08-28')
        """,
        (code, name, market, trade_date, str(close), unit),
    )


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    db.seed_calendar(c, db.CONFIG_DIR / "calendar_cn_2026.yaml")
    # CN 因子：as_of 当日有读数
    _ins_factor(c, "CU0", "沪铜", "CN", "2026-08-28", "85000")
    _ins_factor(c, "CU0", "沪铜", "CN", "2026-08-27", "84000")
    # GLOBAL 因子：T-1（08-27）与当日（08-28）都有，对齐必须取 T-1
    _ins_factor(c, "OIL", "布伦特原油", "GLOBAL", "2026-08-28", "90.00",
                "美元/桶")
    _ins_factor(c, "OIL", "布伦特原油", "GLOBAL", "2026-08-27", "85.00",
                "美元/桶")
    # stale 因子：最新读数 08-10，距 as_of 10 个 CN 交易日
    _ins_factor(c, "I0", "铁矿石", "CN", "2026-08-10", "800")
    # 长序列因子：64 个连续日历日读数，close = 100+i（测 change_20d/60d）
    d0 = date(2026, 6, 25)
    for i in range(64):
        _ins_factor(c, "AU0", "沪金", "CN", (d0 + timedelta(days=i)).isoformat(),
                    str(100 + i), "元/克")
    # 短序列因子：3 个读数（change_20d 样本不足）
    for i, d in enumerate(("2026-08-26", "2026-08-27", "2026-08-28")):
        _ins_factor(c, "USDCNY", "美元兑人民币", "CN", d, f"71{i}",
                    "CNY/100USD")
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------- 时间对齐

def test_cn_factor_same_day_available(conn):
    r = fw.latest_factor_close(conn, "CU0", AS_OF, "CN")
    assert r["trade_date"] == "2026-08-28" and r["close"] == "85000"
    assert r["status"] == "ok"


def test_global_factor_t_minus_1(conn):
    r = fw.latest_factor_close(conn, "OIL", AS_OF, "GLOBAL")
    # 08-28 当日报价在 A 股收盘时不存在 → 取 T-1
    assert r["trade_date"] == "2026-08-27" and r["close"] == "85.00"


def test_missing_factor_returns_none(conn):
    assert fw.latest_factor_close(conn, "NOPE", AS_OF, "CN") is None


def test_stale_after_5_trading_days(conn):
    r = fw.latest_factor_close(conn, "I0", AS_OF, "CN")
    assert r["trade_date"] == "2026-08-10" and r["status"] == "stale"


# ---------------------------------------------------------------- 变动窗口

def test_change_windows(conn):
    # AU0 序列 close=100+i（i=0..63）：cur=163，20 个读数前=143，60 个读数前=103
    snap = fw.snapshot_for_symbol(
        conn, "X.SH", AS_OF, None,
        {"industries": {}, "symbols": {"X.SH": [
            {"code": "AU0", "direction": "positive", "note": "金"}]}})
    f = snap["factors"][0]
    assert f["close"] == "163" and f["trade_date"] == "2026-08-27"
    assert f["change_20d"] == pytest.approx(163 / 143 - 1)
    assert f["change_60d"] == pytest.approx(163 / 103 - 1)


def test_change_insufficient_history_is_none(conn):
    snap = fw.snapshot_for_symbol(
        conn, "X.SH", AS_OF, None,
        {"industries": {}, "symbols": {"X.SH": [
            {"code": "USDCNY", "direction": None, "note": None}]}})
    f = snap["factors"][0]
    assert f["close"] == "712"
    assert f["change_20d"] is None and f["change_60d"] is None


# ---------------------------------------------------------------- 映射解析

def test_symbol_override_replaces_industry(conn):
    mapping = {
        "industries": {"BK1479": [{"code": "OIL", "direction": "negative",
                                   "note": "航油"}]},
        "symbols": {"600029.SH": [{"code": "CU0", "direction": "positive",
                                   "note": "覆盖"}]},
    }
    snap = fw.snapshot_for_symbol(conn, "600029.SH", AS_OF, "BK1479", mapping)
    assert [f["code"] for f in snap["factors"]] == ["CU0"]   # 替换，不合并
    snap = fw.snapshot_for_symbol(conn, "OTHER.SH", AS_OF, "BK1479", mapping)
    assert [f["code"] for f in snap["factors"]] == ["OIL"]
    assert snap["factors"][0]["trade_date"] == "2026-08-27"  # GLOBAL T-1


def test_no_mapping_returns_note(conn):
    snap = fw.snapshot_for_symbol(conn, "X.SH", AS_OF, None,
                                  {"industries": {}, "symbols": {}})
    assert snap["factors"] == [] and "无因子映射" in snap["note"]


# ---------------------------------------------------------------- 配置校验

def test_load_real_config_ok():
    mapping, h = fw.load_industry_factors()
    assert h and "BK1479" in mapping["industries"]
    assert "601899.SH" in mapping["symbols"]


def test_load_rejects_unknown_code(tmp_path):
    ind = tmp_path / "ind.yaml"
    ind.write_text(
        "schema_version: 1\n"
        "industries:\n  BK0001:\n    - {code: FAKE, direction: positive, note: x}\n",
        encoding="utf-8")
    with pytest.raises(fw.IndustryFactorsError, match="不在"):
        fw.load_industry_factors(path=ind)


# ---------------------------------------------------------------- 参数化/性能（1.4/1.5/1.6）

def test_latest_close_sql_parameterized_both_markets(conn):
    """1.4：GLOBAL 与 CN 两个分支都参数化取数，且结果正确（无 f-string 注入操作符）。"""
    cn = fw.latest_factor_close(conn, "CU0", AS_OF, "CN")
    assert cn["trade_date"] == "2026-08-28" and cn["close"] == "85000"
    gl = fw.latest_factor_close(conn, "OIL", AS_OF, "GLOBAL")
    assert gl["trade_date"] == "2026-08-27" and gl["close"] == "85.00"


def test_snapshot_market_lookup_is_batched(conn):
    """1.5：market 解析单次 IN+GROUP BY 批量查询，不随因子数线性增长。"""
    sqls: list[str] = []
    conn.set_trace_callback(lambda s: sqls.append(s))
    try:
        snap = fw.snapshot_for_symbol(
            conn, "X.SH", AS_OF, None,
            {"industries": {}, "symbols": {"X.SH": [
                {"code": "CU0", "direction": None, "note": None},
                {"code": "OIL", "direction": None, "note": None},
                {"code": "I0", "direction": None, "note": None},
                {"code": "AU0", "direction": None, "note": None}]}})
    finally:
        conn.set_trace_callback(None)
    # 4 个因子：market 查询只发生一次（GROUP BY code 批量）
    batch = [s for s in sqls if "GROUP BY code" in s and "macro_factors" in s]
    assert len(batch) == 1
    assert "SELECT market FROM macro_factors" not in " ".join(sqls)
    assert {f["code"]: f["market"] for f in snap["factors"]} == {
        "CU0": "CN", "OIL": "GLOBAL", "I0": "CN", "AU0": "CN"}


def test_load_industry_factors_caches_by_mtime(tmp_path):
    """1.6：同一 mtime 返回同一对象（cache hit）；文件改动后重新加载（cache miss）。"""
    macro = tmp_path / "macro.yaml"
    macro.write_text("factors:\n  - {code: AU0}\n", encoding="utf-8")
    ind = tmp_path / "ind.yaml"
    ind.write_text(
        "schema_version: 1\n"
        "industries:\n  BK1:\n    - {code: AU0, direction: positive}\n",
        encoding="utf-8")
    m1, h1 = fw.load_industry_factors(path=ind, macro_path=macro)
    m2, h2 = fw.load_industry_factors(path=ind, macro_path=macro)
    assert m1 is m2 and h1 == h2  # cache hit：同一对象
    ind.write_text(ind.read_text(encoding="utf-8") + "# touch\n", encoding="utf-8")
    m3, h3 = fw.load_industry_factors(path=ind, macro_path=macro)
    assert m3 is not m1          # mtime 变化 → cache miss 重新加载
    assert m3["industries"]["BK1"][0]["code"] == "AU0"
