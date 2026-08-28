"""event_link 关联层测试（消息面 r2 Phase 3，L2 确定性）。

锁定：scope 关键词初分（macro 先于 policy、"央行降准"归 macro）；主题词后向
复合词排除（"黄金"不命中"黄金周"）；themes/symbol_industry 关联 INSERT OR
IGNORE 不破坏手工关联；resolve_effective 人审 replay。
"""

from __future__ import annotations

import json

from scripts.pipeline import db as pdb
from scripts.signals import event_link


def test_classify_scope_rules():
    assert event_link.classify_scope("flow", "x", None) == "flow"
    assert event_link.classify_scope("announcement", "年报", None) == "company"
    assert event_link.classify_scope("news", "央行宣布降准0.5个百分点", None) == "macro"
    assert event_link.classify_scope("news", "证监会处罚某券商", None) == "policy"
    assert event_link.classify_scope("news", "黄金周消费数据出炉", None) is None


def test_term_hit_word_boundary():
    assert event_link._term_hit("黄金", "金价与黄金现货")
    assert not event_link._term_hit("黄金", "黄金周消费数据")  # 节日复合词不误配
    assert event_link._term_hit("铜", "电解铜库存下降")        # 前向不设挡


def _mkconn(tmp_path):
    conn = pdb.connect(tmp_path / "t.db")
    pdb.migrate(conn)
    now = pdb.utc_now()
    conn.execute(
        "INSERT INTO watchlist (symbol, market, name, aliases_json, benchmark_code,"
        " currency, timezone, active, created_at, updated_at)"
        " VALUES ('601899.SH', 'CN', '紫金矿业', '[]', '000300.SH', 'CNY',"
        " 'Asia/Shanghai', 1, ?, ?)", (now, now))
    conn.execute(
        "UPDATE watchlist SET themes_json = ? WHERE symbol = '601899.SH'",
        ('["铜", "黄金"]',))
    conn.execute(
        "INSERT INTO events (event_id, event_type, published_at, published_tz,"
        " available_at, title, summary, source, content_hash, ingested_at)"
        " VALUES ('evt_t1', 'news', '2026-08-28T02:00:00+00:00', 'Asia/Shanghai',"
        " '2026-08-28T02:00:00+00:00', 'LME铜 库存大降', NULL, 'akshare',"
        " 'h1', ?)", (now,))
    # 手工关联（必须被 INSERT OR IGNORE 保留）
    conn.execute(
        "INSERT INTO event_symbols (event_id, symbol) VALUES ('evt_t1', '600029.SH')")
    conn.commit()
    return conn


def test_link_event_themes_and_industry(tmp_path):
    conn = _mkconn(tmp_path)
    conn.execute(
        "INSERT INTO symbol_industry (symbol, industry_code, industry_name, source,"
        " classification_date, ingested_at) VALUES"
        " ('601899.SH', 'BK1615', '铜', 'akshare_em', '2026-08-28', 'x'),"
        " ('600029.SH', 'BK1479', '航空运输', 'akshare_em', '2026-08-28', 'x')")
    conn.commit()

    added = event_link.link_event(conn, "evt_t1", "LME铜 库存大降", None, "industry")
    conn.commit()
    assert added >= 1
    linked = {r["symbol"] for r in conn.execute(
        "SELECT symbol FROM event_symbols WHERE event_id = 'evt_t1'")}
    assert "600029.SH" in linked          # 手工关联保留
    assert "601899.SH" in linked          # themes 词边界命中（"铜 "尾随空格）

    added2 = event_link.link_event(conn, "evt_t1", "LME铜 库存大降", None, "industry")
    assert added2 == 0                    # INSERT OR IGNORE：重跑不重复
    conn.close()


def test_resolve_effective_review_replay(tmp_path):
    conn = _mkconn(tmp_path)
    now = pdb.utc_now()
    conn.execute(
        "INSERT INTO event_assessments (event_id, symbol, assessment_version,"
        " model, prompt_version, assessed_at, event_type, direction, materiality,"
        " confidence, rationale, status, run_id)"
        " VALUES ('evt_t1', '__event__', 'llm_v1', 'm', 'llm_v1', ?, 'news',"
        " 'negative', 'medium', 0.7, 'r', 'needs_review', 'r')", (now,))
    conn.execute(
        "INSERT INTO event_assessments (event_id, symbol, assessment_version,"
        " model, prompt_version, assessed_at, status, narrative, run_id)"
        " VALUES ('evt_t1', '601899.SH', 'llm_v1', 'm', 'llm_v1', ?,"
        " 'needs_review', '铜价下行影响盈利', 'r')", (now,))
    conn.execute(
        "INSERT INTO event_symbols (event_id, symbol) VALUES ('evt_t1', '601899.SH')")
    for i, (action, payload) in enumerate((
            ("dismiss", None), ("confirm", None),
            ("amend", {"expectation_gap": "低于预期",
                       "falsification": "库存回升"}))):
        conn.execute(
            "INSERT INTO event_human_review (event_id, symbol, action,"
            " payload_json, actor, reviewed_at) VALUES"
            " ('evt_t1', '__event__', ?, ?, 'tester', ?)",
            (action, json.dumps(payload) if payload else None,
             f"2026-08-28T00:00:0{i}+00:00"))
    eff = event_link.resolve_effective(conn, "evt_t1")
    # confirm 撤销 dismiss → ok；amend 覆盖显示值
    assert eff["hidden"] is False
    assert eff["status"] == "ok"
    assert eff["expectation_gap"] == "低于预期"
    assert eff["falsification"] == "库存回升"
    sym = eff["symbols"]["601899.SH"]
    assert sym["narrative"] == "铜价下行影响盈利" and sym["hidden"] is False
    conn.close()
