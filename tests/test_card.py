"""D2.5 卡片版本管理测试（设计 §5.6、§2.4、§5.4b 第三步）。

锁定：
- create-draft：jsonschema + 语义校验拒绝坏 JSON（结构错/区间反向/非定点字符串）；
- activate：同一事务关闭旧 active（effective_to 排他端点），任一时刻最多一个 active；
  公司行为换算 draft 确认激活后冻结自动视为已解（§5.4b 第三步）；
- reject：draft→rejected 不改历史 JSON 字段；非 draft/active 拒绝报错；
- Markdown 渲染：版本文件 + current.md 关键字段齐全（由库记录渲染，§2.4）。
"""

from __future__ import annotations

import json

import pytest

from scripts.pipeline import db
from scripts.pipeline import card as card_cli
from scripts.signals import cards as card_mod
from scripts.signals import corporate_action as ca_mod

SYM = "TEST.SH"

CARD_INPUT = {
    "currency": "CNY",
    "price_basis": "raw",
    "next_review_at": "2026-09-01",
    "earnings": {"eps": {"bear": "3.20", "base": "3.70", "bull": "4.20"}},
    "valuation": {"pe": {"bear": "12", "base": "15", "bull": "18"},
                  "sample_window": "2023-08~2026-08"},
    "price_tiers": {"tiers": [{"tier": 1, "zone_low": "55.00", "zone_high": "58.00"},
                              {"tier": 2, "zone_low": "48.00", "zone_high": "52.00"},
                              {"tier": 3, "zone_low": "42.00", "zone_high": "46.00"}]},
    "invalidation": {"line": "47.00", "note": "证伪线"},
    "swing_box": {"box_low": "48.00", "box_high": "65.00", "buy_zone_low": "52.00",
                  "buy_zone_high": "56.00", "sell_zone_low": "62.00",
                  "sell_zone_high": "65.00", "box_invalidation": "46.00"},
    "right_side_trigger": {"trigger_level": "60.00", "stop_level": "56.00"},
    "input_snapshot": {"demo": True, "note": "测试卡"},
}


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
    c.commit()
    yield c
    c.close()


def _write_json(tmp_path, doc, name="card.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _make_draft(conn, tmp_path, doc=None):
    return card_cli.create_draft(conn, SYM, _write_json(tmp_path, doc or CARD_INPUT))


# ---------------------------------------------------------------- create-draft 校验

def test_create_draft_ok(conn, tmp_path):
    card_id = _make_draft(conn, tmp_path)
    row = card_cli.get_version(conn, card_id)
    assert row["status"] == "draft"
    assert row["effective_from"] is None  # draft 从未生效（§5.6）
    assert json.loads(row["price_tiers_json"])["tiers"][0]["zone_low"] == "55.00"
    assert json.loads(row["input_snapshot_json"])["demo"] is True


def test_create_draft_rejects_schema_violation(conn, tmp_path):
    bad = dict(CARD_INPUT)
    bad["price_tiers"] = {"tiers": [{"tier": 1, "zone_low": 55.0,  # 数值非定点字符串
                                     "zone_high": "58.00"}]}
    with pytest.raises(card_cli.CardCLIError, match="校验失败"):
        _make_draft(conn, tmp_path, bad)
    assert conn.execute("SELECT COUNT(*) FROM strategy_card_versions").fetchone()[0] == 0


def test_create_draft_rejects_reversed_zone(conn, tmp_path):
    bad = json.loads(json.dumps(CARD_INPUT))
    bad["price_tiers"]["tiers"][0]["zone_low"] = "60.00"  # zone_low > zone_high
    with pytest.raises(card_cli.CardCLIError, match="zone_low > zone_high"):
        _make_draft(conn, tmp_path, bad)


def test_create_draft_rejects_bad_decimal(conn, tmp_path):
    bad = json.loads(json.dumps(CARD_INPUT))
    bad["invalidation"]["line"] = "47.00元"  # 非定点十进制
    with pytest.raises(card_cli.CardCLIError, match="校验失败"):
        _make_draft(conn, tmp_path, bad)


def test_create_draft_unknown_symbol(conn, tmp_path):
    with pytest.raises(card_cli.CardCLIError, match="不在 watchlist"):
        card_cli.create_draft(conn, "NOPE.SH", _write_json(tmp_path, CARD_INPUT))


def test_create_draft_anchor_metric_accepted(conn, tmp_path):
    doc = json.loads(json.dumps(CARD_INPUT))
    doc["valuation"]["anchor"] = {"metric": "price_band", "note": "强周期 PE 失真"}
    card_id = _make_draft(conn, tmp_path, doc)
    row = conn.execute("SELECT valuation_scenarios_json FROM strategy_card_versions "
                       "WHERE card_version_id = ?", (card_id,)).fetchone()
    assert json.loads(row[0])["anchor"]["metric"] == "price_band"


def test_create_draft_rejects_bad_anchor_metric(conn, tmp_path):
    doc = json.loads(json.dumps(CARD_INPUT))
    doc["valuation"]["anchor"] = {"metric": "ev_ebitda"}
    with pytest.raises(card_cli.CardCLIError, match="anchor.metric"):
        _make_draft(conn, tmp_path, doc)


def test_create_draft_missing_anchor_warns_not_rejects(conn, tmp_path, capsys):
    _make_draft(conn, tmp_path)  # CARD_INPUT 无 anchor：warning 但不拒绝
    assert "valuation.anchor 缺失" in capsys.readouterr().out


# ---------------------------------------------------------------- activate

def test_activate_single_active(conn, tmp_path):
    c1 = _make_draft(conn, tmp_path)
    res1 = card_cli.activate(conn, c1, "2026-06-01", cards_root=tmp_path / "cards")
    assert res1["superseded"] is None
    assert card_cli.get_version(conn, c1)["status"] == "active"

    c2 = _make_draft(conn, tmp_path)
    res2 = card_cli.activate(conn, c2, "2026-07-01", cards_root=tmp_path / "cards")
    assert res2["superseded"] == c1
    old = card_cli.get_version(conn, c1)
    assert old["status"] == "superseded"
    assert old["effective_to"] == "2026-07-01"  # 排他端点（cards.py 语义）
    # 任一时刻最多一个 active
    assert conn.execute(
        "SELECT COUNT(*) FROM strategy_card_versions WHERE symbol = ? AND status = 'active'",
        (SYM,)).fetchone()[0] == 1
    # 生效区间语义：2026-06-30 旧卡生效，2026-07-01 新卡生效
    versions = card_mod.load_card_versions(conn, SYM)
    assert card_mod.card_for_day(versions, "2026-06-30").card_version_id == c1
    assert card_mod.card_for_day(versions, "2026-07-01").card_version_id == c2


def test_activate_rejects_non_draft(conn, tmp_path):
    c1 = _make_draft(conn, tmp_path)
    card_cli.activate(conn, c1, "2026-06-01", cards_root=tmp_path / "cards")
    with pytest.raises(card_cli.CardCLIError, match="已是 active"):
        card_cli.activate(conn, c1, "2026-06-02", cards_root=tmp_path / "cards")


def test_activate_rejects_historical_backfill(conn, tmp_path):
    c1 = _make_draft(conn, tmp_path)
    card_cli.activate(conn, c1, "2026-06-01", cards_root=tmp_path / "cards")
    c2 = _make_draft(conn, tmp_path)
    with pytest.raises(card_cli.CardCLIError, match="早于当前 active 卡生效日"):
        card_cli.activate(conn, c2, "2026-05-01", cards_root=tmp_path / "cards")


# ---------------------------------------------------------------- 换算 draft 确认激活（§5.4b 第三步衔接）

def test_activate_conversion_draft_resolves_suspension(conn, tmp_path):
    c1 = _make_draft(conn, tmp_path)
    card_cli.activate(conn, c1, "2026-01-05", cards_root=tmp_path / "cards")
    conn.execute(
        """
        INSERT INTO daily_bars (symbol, trade_date, market, open_raw, high_raw,
            low_raw, close_raw, volume_raw, price_adj_factor, share_factor,
            source, updated_at)
        VALUES (?, ?, 'CN', 57.72, 57.72, 57.72, 57.72, 100.0, 1.0, 1.0, 'test', ?)
        """,
        (SYM, "2026-02-27", db.utc_now()),
    )
    cur = conn.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, cash_per_share,
            split_ratio, source, created_at)
        VALUES (?, '2026-03-02', 'bonus_share', NULL, '2.0', 'test', ?)
        """,
        (SYM, db.utc_now()),
    )
    ca_id = cur.lastrowid
    conn.commit()
    with conn:
        res = ca_mod.process_pending(conn, SYM, as_of="2026-03-02")
    draft_id = res.frozen_drafts[0]["draft_card_version_id"]
    assert len(ca_mod.unresolved_suspensions(conn, SYM)) == 1  # 冻结中

    # D2.5 CLI 确认激活：effective_from 缺省取 conversion.ex_date
    act = card_cli.activate(conn, draft_id, cards_root=tmp_path / "cards")
    assert act["effective_from"] == "2026-03-02"
    assert act["superseded"] == c1
    # 冻结视为已解（ca_id 被 active 版本 conversion 吸收）
    assert ca_mod.unresolved_suspensions(conn, SYM) == []
    # 旧版 effective_to 排他关闭
    assert card_cli.get_version(conn, c1)["effective_to"] == "2026-03-02"


# ---------------------------------------------------------------- reject

def test_reject_draft_keeps_history(conn, tmp_path):
    c1 = _make_draft(conn, tmp_path)
    before = dict(card_cli.get_version(conn, c1))
    card_cli.reject(conn, c1, "参数过时")
    after = card_cli.get_version(conn, c1)
    assert after["status"] == "rejected"
    # 历史 JSON 字段一律不动
    for col in ("price_tiers_json", "invalidation_json", "swing_box_json",
                "right_side_trigger_json", "earnings_scenarios_json",
                "valuation_scenarios_json", "created_at"):
        assert after[col] == before[col]
    with pytest.raises(card_cli.CardCLIError, match="只有 draft/active 可拒绝"):
        card_cli.reject(conn, c1)  # rejected 不可再操作


def test_reject_active_closes_effective_range(conn, tmp_path):
    c1 = _make_draft(conn, tmp_path)
    card_cli.activate(conn, c1, "2026-06-01", cards_root=tmp_path / "cards")
    res = card_cli.reject(conn, c1, "演示结束", cards_root=tmp_path / "cards")
    assert res["was"] == "active"
    row = card_cli.get_version(conn, c1)
    assert row["status"] == "rejected"
    assert row["effective_to"] is not None  # 生效区间关闭
    # 不再参与生效区间计算；current.md 视图随废止删除（§2.4/§5.6）
    assert card_mod.load_card_versions(conn, SYM) == []
    assert not (tmp_path / "cards" / SYM / "current.md").exists()
    assert conn.execute(
        "SELECT COUNT(*) FROM strategy_card_versions WHERE status = 'active'"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------- Markdown 渲染（§2.4）

def test_markdown_rendered_on_activate(conn, tmp_path):
    c1 = _make_draft(conn, tmp_path)
    cards_root = tmp_path / "cards"
    res = card_cli.activate(conn, c1, "2026-06-01", cards_root=cards_root)

    version_md = (cards_root / SYM / f"2026-06-01_{c1}.md").read_text(encoding="utf-8")
    current_md = (cards_root / SYM / "current.md").read_text(encoding="utf-8")
    assert res["version_path"].endswith(f"2026-06-01_{c1}.md")
    for text in (version_md, current_md):
        assert c1 in text
        assert "status: active" in text
        assert "[2026-06-01, 开口)" in text          # 生效区间（排他端点）
        assert "| T1 | 55.00 | 58.00 |" in text      # 三档价区
        assert "line: 47.00" in text                  # 证伪线
        assert "箱体下沿: 48.00" in text              # 箱体
        assert "触发位: 60.00" in text                # 右侧
        assert "base: 3.70" in text                   # EPS 情景
        assert "刻度样本区间: 2023-08~2026-08" in text
        assert "demo: true" in text                   # demo 标注
        assert "不手工回写数据库" in text              # §2.4 注记
    assert "当前 active 视图" in current_md

    # 换卡后 current.md 刷新为新版
    c2 = _make_draft(conn, tmp_path)
    card_cli.activate(conn, c2, "2026-07-01", cards_root=cards_root)
    current_md2 = (cards_root / SYM / "current.md").read_text(encoding="utf-8")
    assert c2 in current_md2 and f"2026-07-01_{c2}.md" in current_md2


# ---------------------------------------------------------------- list / show

def test_list_and_show(conn, tmp_path, capsys):
    c1 = _make_draft(conn, tmp_path)
    card_cli.cmd_list(conn, SYM)
    out = capsys.readouterr().out
    assert c1 in out and "status=draft" in out
    card_cli.cmd_show(conn, c1)
    out = capsys.readouterr().out
    assert f"排期卡 {SYM}" in out and c1 in out
    with pytest.raises(card_cli.CardCLIError, match="不存在"):
        card_cli.cmd_show(conn, "nope")
