"""D2.5 执行记录测试（设计 §5.7、§8.3）。

锁定：
- add 必须关联当前 active 卡片（无卡拒绝，§2.5/§5.7）；
- signal_snapshot_json 冻结当时 signal_facts 快照（不随重算变化）；
- idempotency_key 去重：显式 key 重复拒绝、缺省派生 key 同参数重试拒绝；
- reverse 新增冲正记录（reverses_execution_id），不改原记录，重复冲正拒绝；
- list 展示冲正链。
"""

from __future__ import annotations

import json

import pytest

from scripts.pipeline import db
from scripts.pipeline import execution as ex
from scripts.pipeline import card as card_cli

SYM = "TEST.SH"


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    now = db.utc_now()
    c.execute(
        """
        INSERT INTO watchlist (symbol, market, name, aliases_json, benchmark_code,
                               currency, timezone, active, created_at, updated_at)
        VALUES (?, 'CN', '测试', '[]', '000300.SH', 'CNY', 'Asia/Shanghai', 1, ?, ?)
        """,
        (SYM, now, now),
    )
    c.execute(
        """
        INSERT INTO strategy_card_versions (card_version_id, symbol, status,
            schema_version, created_at, effective_from, currency, price_basis,
            price_tiers_json, run_id)
        VALUES ('cv1', ?, 'active', 'card_v1', ?, '2026-01-05', 'CNY', 'raw',
                '{"tiers": [{"tier": 1, "zone_low": "55.00", "zone_high": "58.00"}]}',
                'test')
        """,
        (SYM, now),
    )
    c.execute(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
            low_raw, close_raw, volume_raw, price_adj_factor, share_factor,
            source, updated_at)
        VALUES (?, '2026-08-07', 'CN', 57.0, 58.0, 56.5, 57.72, 10000, 1.0, 1.0,
                'test', ?)
        """,
        (SYM, now),
    )
    c.execute(
        """
        INSERT INTO signal_facts (symbol, observed_on, signal, state, triggered,
            details_json, created_at)
        VALUES (?, '2026-08-07', 'tier_triggered', 'triggered', 1,
                '{"close_raw": "57.72"}', ?)
        """,
        (SYM, now),
    )
    c.commit()
    yield c
    c.close()


def _no_card_conn(tmp_path):
    c = db.connect(tmp_path / "nocard.db")
    db.migrate(c)
    return c


# ---------------------------------------------------------------- add

def test_add_ok_freezes_snapshot(conn):
    res = ex.add_execution(conn, SYM, action_type="buy", price="57.72",
                           quantity="100", tier="1", fees="5.00",
                           executed_at="2026-08-07T15:00:00+00:00")
    row = conn.execute("SELECT * FROM executions WHERE execution_id = ?",
                       (res["execution_id"],)).fetchone()
    assert row["card_version_id"] == "cv1"
    assert row["action_type"] == "buy" and row["tier"] == "1"
    assert row["price"] == "57.72" and row["quantity"] == "100" and row["fees"] == "5.00"
    assert row["reverses_execution_id"] is None
    snap = json.loads(row["signal_snapshot_json"])
    assert snap["card_version_id"] == "cv1"
    assert snap["as_of"] == "2026-08-07"
    assert snap["signal_facts"]["tier_triggered"]["state"] == "triggered"
    # 快照冻结：信号表后来变化不影响已存快照（§2.3）
    conn.execute("DELETE FROM signal_facts")
    conn.commit()
    snap2 = json.loads(conn.execute(
        "SELECT signal_snapshot_json FROM executions WHERE execution_id = ?",
        (res["execution_id"],)).fetchone()[0])
    assert snap2["signal_facts"]["tier_triggered"]["state"] == "triggered"


def test_add_rejects_without_active_card(tmp_path):
    conn = _no_card_conn(tmp_path)
    with pytest.raises(ex.ExecutionCLIError, match="无 active 卡片"):
        ex.add_execution(conn, SYM, action_type="buy", price="57.72", quantity="100",
                         executed_at="2026-08-07T15:00:00+00:00")
    assert conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    conn.close()


def test_add_rejects_bad_action_and_decimal(conn):
    with pytest.raises(ex.ExecutionCLIError, match="action_type"):
        ex.add_execution(conn, SYM, action_type="reversal", price="57.72",
                         quantity="100")
    with pytest.raises(ex.ExecutionCLIError, match="十进制"):
        ex.add_execution(conn, SYM, action_type="buy", price="五十七", quantity="100")


def test_idempotency_key_dedup(conn):
    kwargs = dict(action_type="buy", price="57.72", quantity="100",
                  executed_at="2026-08-07T15:00:00+00:00")
    r1 = ex.add_execution(conn, SYM, idempotency_key="order-001", **kwargs)
    assert r1["idempotency_key"] == "order-001"
    with pytest.raises(ex.ExecutionCLIError, match="idempotency_key 重复"):
        ex.add_execution(conn, SYM, idempotency_key="order-001", **kwargs)
    # 缺省派生 key：同参数重试同样拒绝（§8.3）
    r2 = ex.add_execution(conn, SYM, **kwargs)
    assert r2["idempotency_key"].startswith("auto_")
    with pytest.raises(ex.ExecutionCLIError, match="idempotency_key 重复"):
        ex.add_execution(conn, SYM, **kwargs)
    assert conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 2


# ---------------------------------------------------------------- reverse

def test_reverse_creates_reversal_keeps_original(conn):
    r = ex.add_execution(conn, SYM, action_type="buy", price="57.72", quantity="100",
                         tier="1", executed_at="2026-08-07T15:00:00+00:00")
    before = dict(conn.execute("SELECT * FROM executions WHERE execution_id = ?",
                               (r["execution_id"],)).fetchone())
    rev = ex.reverse_execution(conn, r["execution_id"], "录错价格")
    rev_row = conn.execute("SELECT * FROM executions WHERE execution_id = ?",
                           (rev["execution_id"],)).fetchone()
    assert rev_row["action_type"] == "reversal"
    assert rev_row["reverses_execution_id"] == r["execution_id"]
    assert rev_row["price"] == "57.72" and rev_row["quantity"] == "100"
    assert rev_row["card_version_id"] == "cv1"
    snap = json.loads(rev_row["signal_snapshot_json"])
    assert snap["reversal_of"] == r["execution_id"] and snap["reason"] == "录错价格"
    # 原记录不变（§5.7 冲正不删改）
    after = dict(conn.execute("SELECT * FROM executions WHERE execution_id = ?",
                              (r["execution_id"],)).fetchone())
    assert before == after
    # 重复冲正拒绝（幂等）
    with pytest.raises(ex.ExecutionCLIError, match="已被冲正"):
        ex.reverse_execution(conn, r["execution_id"])
    with pytest.raises(ex.ExecutionCLIError, match="不存在"):
        ex.reverse_execution(conn, 9999)


def test_list_shows_reversal_chain(conn, capsys):
    r = ex.add_execution(conn, SYM, action_type="buy", price="57.72", quantity="100",
                         executed_at="2026-08-07T15:00:00+00:00")
    ex.reverse_execution(conn, r["execution_id"])
    ex.cmd_list(conn, SYM)
    out = capsys.readouterr().out
    assert f"#{r['execution_id']}" in out and "已被 #" in out
    assert "reversal" in out and f"冲正 #{r['execution_id']}" in out


# ---------------------------------------------------------------- CLI 退出码

def test_cli_exit_codes(conn, tmp_path):
    db_path = str(conn.execute("PRAGMA database_list").fetchone()[2])
    assert ex.main(["--db", db_path, "add", SYM, "--action-type", "buy",
                    "--price", "57.72", "--quantity", "100",
                    "--executed-at", "2026-08-07T15:00:00+00:00"]) == 0
    # 重复（派生 key 相同）→ 退出码 2
    assert ex.main(["--db", db_path, "add", SYM, "--action-type", "buy",
                    "--price", "57.72", "--quantity", "100",
                    "--executed-at", "2026-08-07T15:00:00+00:00"]) == 2
    assert ex.main(["--db", db_path, "reverse", "1"]) == 0
    assert ex.main(["--db", db_path, "reverse", "1"]) == 2
    assert ex.main(["--db", db_path, "list", SYM]) == 0
