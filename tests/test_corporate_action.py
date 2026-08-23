"""D2.4 公司行为处置测试（设计 §5.4b、§9.1）。

覆盖：
- 10 送 10：即时冻结 + 换算 draft 值正确（价区 ×0.5、EPS ÷2、PE 情景不变），
  旧卡保持 active（未确认前持续冻结）；
- 小额现金分红快速通道（影响 <2%）：减法换算 + 自动激活（supersedes 链、
  旧版 effective_to 关闭、card_conversion 事实行），不冻结；
- 大额分红（≥2%）降级三段式：冻结 + 减法 draft；
- 除权日前无行情可算影响比例 → 不猜，降级三段式（§2.5）；
- 换算后 executions 不回溯（原成交价与 card_version_id 原样保留）；
- 撤销冻结 rescind 后监测恢复。
"""

from __future__ import annotations

import json

import pytest

from scripts.pipeline import db
from scripts.signals import cards as card_mod
from scripts.signals import corporate_action as ca_mod
from scripts.signals import daily_watch as dw

SYM = "TEST.SH"


def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    return conn


def add_bar(conn, day, close, symbol=SYM):
    conn.execute(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
            low_raw, close_raw, volume_raw, price_adj_factor, share_factor,
            source, updated_at)
        VALUES (?, ?, 'CN', ?, ?, ?, ?, 100.0, 1.0, 1.0, 'test', ?)
        """,
        (symbol, day, close, close, close, close, db.utc_now()),
    )


CARD_JSONS = {
    "earnings": {"eps": {"bear": "3.20", "base": "3.70", "bull": "4.20"}},
    "valuation": {"pe": {"bear": "12", "base": "15", "bull": "18"},
                  "sample_window": "2023-01~2026-01"},
    "tiers": {"tiers": [{"tier": 1, "zone_low": "55.00", "zone_high": "58.00"},
                        {"tier": 2, "zone_low": "48.00", "zone_high": "52.00"}]},
    "invalidation": {"line": "47.00", "note": "证伪线"},
    "box": {"box_low": "48.00", "box_high": "65.00", "buy_zone_low": "52.00",
            "buy_zone_high": "56.00", "sell_zone_low": "62.00",
            "sell_zone_high": "65.00", "box_invalidation": "46.00"},
    "trigger": {"trigger_level": "60.00", "stop_level": "56.00"},
}


def add_card(conn, symbol=SYM, card_id="cv1", eff_from="2026-01-05", jsons=None):
    j = jsons or CARD_JSONS
    conn.execute(
        """
        INSERT INTO strategy_card_versions (card_version_id, symbol, status,
            schema_version, created_at, effective_from, effective_to, supersedes_id,
            currency, price_basis, earnings_scenarios_json,
            valuation_scenarios_json, price_tiers_json, invalidation_json,
            swing_box_json, right_side_trigger_json, next_review_at,
            input_snapshot_json, run_id)
        VALUES (?, ?, 'active', 'card_v1', ?, ?, NULL, NULL, 'CNY', 'raw',
                ?, ?, ?, ?, ?, ?, '2026-09-01', NULL, 'test')
        """,
        (card_id, symbol, db.utc_now(), eff_from,
         json.dumps(j["earnings"], sort_keys=True),
         json.dumps(j["valuation"], sort_keys=True),
         json.dumps(j["tiers"], sort_keys=True),
         json.dumps(j["invalidation"], sort_keys=True),
         json.dumps(j["box"], sort_keys=True),
         json.dumps(j["trigger"], sort_keys=True)),
    )


def add_ca(conn, ex_date, action_type, *, cash=None, ratio=None, symbol=SYM):
    cur = conn.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, cash_per_share,
            split_ratio, source, created_at)
        VALUES (?, ?, ?, ?, ?, 'test', ?)
        """,
        (symbol, ex_date, action_type, cash, ratio, db.utc_now()),
    )
    return cur.lastrowid


def get_card(conn, card_id):
    return conn.execute(
        "SELECT * FROM strategy_card_versions WHERE card_version_id = ?",
        (card_id,),
    ).fetchone()


# ---------------------------------------------------------------- 10 送 10 三段式

def test_split_freeze_and_draft(tmp_path):
    conn = make_conn(tmp_path)
    add_card(conn)
    add_bar(conn, "2026-02-27", 57.72)
    add_bar(conn, "2026-03-02", 28.80)  # 除权日现价腰斩（非基本面跳变）
    ca_id = add_ca(conn, "2026-03-02", "bonus_share", ratio="2.0")
    with conn:
        res = ca_mod.process_pending(conn, SYM, as_of="2026-03-02")

    assert res.pending == 1 and len(res.frozen_drafts) == 1
    assert res.fastlane_activated == []

    # 第一步：冻结（signal_facts 记 suspended_corporate_action）
    susp = ca_mod.unresolved_suspensions(conn, SYM)
    assert len(susp) == 1 and susp[0]["ca_id"] == ca_id
    row = conn.execute(
        "SELECT state, details_json FROM signal_facts WHERE symbol = ? "
        "AND signal = 'suspended_corporate_action'", (SYM,)).fetchone()
    det = json.loads(row["details_json"])
    assert row["state"] == "active"
    assert (det["ca_id"], det["ex_date"], det["split_ratio"]) == (
        ca_id, "2026-03-02", "2.0")

    # 第二步：换算 draft（旧卡保持 active，draft 未生效）
    draft_id = res.frozen_drafts[0]["draft_card_version_id"]
    draft = get_card(conn, draft_id)
    assert draft["status"] == "draft" and draft["effective_from"] is None
    assert draft["supersedes_id"] == "cv1"
    old = get_card(conn, "cv1")
    assert old["status"] == "active" and old["effective_to"] is None

    tiers = json.loads(draft["price_tiers_json"])["tiers"]
    assert [(t["zone_low"], t["zone_high"]) for t in tiers] == [
        ("27.5000", "29.0000"), ("24.0000", "26.0000")]  # 价区 ×0.5
    assert json.loads(draft["invalidation_json"])["line"] == "23.5000"
    box = json.loads(draft["swing_box_json"])
    assert box["box_high"] == "32.5000" and box["box_invalidation"] == "23.0000"
    rst = json.loads(draft["right_side_trigger_json"])
    assert rst["trigger_level"] == "30.0000" and rst["stop_level"] == "28.0000"
    eps = json.loads(draft["earnings_scenarios_json"])["eps"]
    assert eps == {"bear": "1.6000", "base": "1.8500", "bull": "2.1000"}  # EPS ÷2
    pe = json.loads(draft["valuation_scenarios_json"])["pe"]
    assert pe == {"bear": "12", "base": "15", "bull": "18"}  # PE 情景不变

    # 换算明细入 input_snapshot_json（来源版本 / 事件 / 倍率）
    conv = json.loads(draft["input_snapshot_json"])["conversion"]
    assert (conv["ca_id"], conv["source_card_version_id"], conv["op"]) == (
        ca_id, "cv1", "multiply")
    assert conv["factor"] == "0.5000"

    # 幂等：重跑不再处理同一事件
    with conn:
        res2 = ca_mod.process_pending(conn, SYM, as_of="2026-03-02")
    assert res2.pending == 0
    conn.close()


# ---------------------------------------------------------------- 小额分红快速通道

def test_small_dividend_fastlane_auto_activate(tmp_path):
    conn = make_conn(tmp_path)
    add_card(conn)
    add_bar(conn, "2026-02-27", 57.72)
    ca_id = add_ca(conn, "2026-03-02", "cash_dividend", cash="1.00")
    exec_id = conn.execute(
        """
        INSERT INTO executions (idempotency_key, symbol, executed_at, action_type,
            tier, price, quantity, card_version_id, created_at)
        VALUES ('k1', ?, '2026-02-10T08:00:00+00:00', 'buy', '1', '56.50',
                '1000', 'cv1', ?)
        """,
        (SYM, db.utc_now()),
    ).lastrowid
    with conn:
        res = ca_mod.process_pending(conn, SYM, as_of="2026-03-02")

    # 1.00 / 57.72 ≈ 1.73% < 2% → 快速通道自动激活，不冻结
    assert len(res.fastlane_activated) == 1 and res.frozen_drafts == []
    assert ca_mod.unresolved_suspensions(conn, SYM) == []
    fl = res.fastlane_activated[0]
    assert fl["impact_pct"] == pytest.approx(1 / 57.72)

    new_id = fl["new_card_version_id"]
    new = get_card(conn, new_id)
    assert new["status"] == "active" and new["effective_from"] == "2026-03-02"
    assert new["supersedes_id"] == "cv1"
    old = get_card(conn, "cv1")
    assert old["status"] == "superseded" and old["effective_to"] == "2026-03-02"

    # 减法换算：价区 −1.00，EPS/PE 不动
    tiers = json.loads(new["price_tiers_json"])["tiers"]
    assert [(t["zone_low"], t["zone_high"]) for t in tiers] == [
        ("54.0000", "57.0000"), ("47.0000", "51.0000")]
    assert json.loads(new["invalidation_json"])["line"] == "46.0000"
    assert json.loads(new["right_side_trigger_json"])["trigger_level"] == "59.0000"
    assert json.loads(new["earnings_scenarios_json"]) == CARD_JSONS["earnings"]
    conv = json.loads(new["input_snapshot_json"])["conversion"]
    assert conv["channel"] == "dividend_fastlane"
    assert conv["cash_per_share"] == "1.00" and conv["ca_id"] == ca_id

    # card_conversion 事实行（日报据此标注"分红自动换算"）
    row = conn.execute(
        "SELECT state, details_json FROM signal_facts WHERE symbol = ? "
        "AND signal = 'card_conversion'", (SYM,)).fetchone()
    assert row["state"] == "auto_activated"
    assert json.loads(row["details_json"])["new_card_version_id"] == new_id

    # executions 不回溯：原成交价与原 card_version_id 原样保留（§5.4b）
    exe = conn.execute("SELECT * FROM executions WHERE execution_id = ?",
                       (exec_id,)).fetchone()
    assert exe["price"] == "56.50" and exe["card_version_id"] == "cv1"
    assert exe["quantity"] == "1000"
    conn.close()


# ---------------------------------------------------------------- 大额分红三段式

def test_large_dividend_goes_three_step(tmp_path):
    conn = make_conn(tmp_path)
    add_card(conn)
    add_bar(conn, "2026-02-27", 57.72)
    add_ca(conn, "2026-03-02", "cash_dividend", cash="2.00")  # 3.46% ≥ 2%
    with conn:
        res = ca_mod.process_pending(conn, SYM, as_of="2026-03-02")
    assert res.fastlane_activated == [] and len(res.frozen_drafts) == 1
    assert len(ca_mod.unresolved_suspensions(conn, SYM)) == 1
    draft = get_card(conn, res.frozen_drafts[0]["draft_card_version_id"])
    assert draft["status"] == "draft"
    tiers = json.loads(draft["price_tiers_json"])["tiers"]
    assert (tiers[0]["zone_low"], tiers[0]["zone_high"]) == ("53.0000", "56.0000")
    # EPS 不动（现金分红不改股本）
    assert json.loads(draft["earnings_scenarios_json"]) == CARD_JSONS["earnings"]
    conn.close()


def test_no_prev_bar_downgrades_to_three_step(tmp_path):
    """除权日前无行情可算影响比例 → 不猜，冻结 + draft（§2.5）。"""
    conn = make_conn(tmp_path)
    add_card(conn)
    add_ca(conn, "2026-03-02", "cash_dividend", cash="1.00")
    with conn:
        res = ca_mod.process_pending(conn, SYM, as_of="2026-03-02")
    assert res.fastlane_activated == [] and len(res.frozen_drafts) == 1
    assert any("无行情" in n for n in res.notes)
    conn.close()


# ---------------------------------------------------------------- 冻结 → 撤销 → 恢复

def test_rescind_suspension_restores_monitoring(tmp_path):
    conn = make_conn(tmp_path)
    add_card(conn)
    add_bar(conn, "2026-02-27", 57.72)
    add_bar(conn, "2026-03-02", 55.00)
    ca_id = add_ca(conn, "2026-03-02", "bonus_share", ratio="2.0")
    with conn:
        ca_mod.process_pending(conn, SYM, as_of="2026-03-02")
    assert len(ca_mod.unresolved_suspensions(conn, SYM)) == 1

    with conn:
        ca_mod.rescind_suspension(conn, SYM, ca_id, "2026-03-03",
                                  "事件公告撤销", "test_run", "hash")
    assert ca_mod.unresolved_suspensions(conn, SYM) == []
    # 撤销后 daily_watch 恢复卡片触发输出（03-02 现价 55 在第一档 [55,58]）
    with conn:
        dw.run_daily_watch(conn, SYM)
    rows = conn.execute(
        "SELECT observed_on, state FROM signal_facts WHERE symbol = ? "
        "AND signal = 'tier_triggered' ORDER BY observed_on", (SYM,)).fetchall()
    assert [(r["observed_on"], r["state"]) for r in rows] == [
        ("2026-02-27", "triggered"), ("2026-03-02", "triggered")]
    conn.close()


# ---------------------------------------------------------------- 换算护栏拒绝（§5.4b 无下限保护的兜底）

def test_dividend_exceeding_stop_level_rejected(tmp_path):
    """分红额 > stop_level：换算会产生 ≤0 价格 → 拒绝，冻结待人工，不写新版本。"""
    conn = make_conn(tmp_path)
    add_card(conn)
    add_bar(conn, "2026-02-27", 57.72)
    add_ca(conn, "2026-03-02", "cash_dividend", cash="60.00")  # > stop_level 56
    with conn:
        res = ca_mod.process_pending(conn, SYM, as_of="2026-03-02")
    assert res.fastlane_activated == [] and res.frozen_drafts == []
    assert any("换算被拒" in n for n in res.notes)
    # 不写库：无新卡片版本，旧卡仍 active
    rows = conn.execute(
        "SELECT card_version_id, status FROM strategy_card_versions").fetchall()
    assert [(r["card_version_id"], r["status"]) for r in rows] == [("cv1", "active")]
    # 已冻结待人工处理
    assert len(ca_mod.unresolved_suspensions(conn, SYM)) == 1
    conn.close()


def test_fastlane_rejected_conversion_keeps_old_card_active(tmp_path):
    """快速通道换算被护栏拒绝：旧卡不得提前关闭（先换算后关旧版），冻结待人工。"""
    conn = make_conn(tmp_path)
    low = {k: v for k, v in CARD_JSONS.items()}
    low["tiers"] = {"tiers": [{"tier": 1, "zone_low": "1.20", "zone_high": "1.80"}]}
    low["invalidation"] = {"line": "0.90"}
    low["box"] = {"box_low": "1.20", "box_high": "1.80"}
    low["trigger"] = {"trigger_level": "1.50", "stop_level": "1.00"}
    add_card(conn, jsons=low)
    add_bar(conn, "2026-02-27", 57.72)  # 分红 1.00 → 影响 1.73% < 2% 走快速通道
    add_ca(conn, "2026-03-02", "cash_dividend", cash="1.00")  # 1.00 ≥ stop → 换算为 0
    with conn:
        res = ca_mod.process_pending(conn, SYM, as_of="2026-03-02")
    assert res.fastlane_activated == [] and res.frozen_drafts == []
    assert any("换算被拒" in n for n in res.notes)
    old = get_card(conn, "cv1")
    assert old["status"] == "active" and old["effective_to"] is None  # 旧卡未被关闭
    assert conn.execute(
        "SELECT COUNT(*) FROM strategy_card_versions").fetchone()[0] == 1
    assert len(ca_mod.unresolved_suspensions(conn, SYM)) == 1
    conn.close()


def test_pending_detection_scope(tmp_path):
    """检测口径：除权日 ≥ 卡片 effective_from 且未处理的事件才 pending。"""
    conn = make_conn(tmp_path)
    add_card(conn, eff_from="2026-02-01")
    add_ca(conn, "2026-01-15", "cash_dividend", cash="1.00")  # 卡片生效前
    add_ca(conn, "2026-03-02", "cash_dividend", cash="1.00")  # 生效后
    pending = ca_mod.pending_actions(conn, SYM, "2026-03-10")
    assert [r["ex_date"] for r in pending] == ["2026-03-02"]
    # 无 active 卡片 → 无 pending
    conn.execute("UPDATE strategy_card_versions SET status = 'rejected'")
    assert ca_mod.pending_actions(conn, SYM, "2026-03-10") == []
    conn.close()
