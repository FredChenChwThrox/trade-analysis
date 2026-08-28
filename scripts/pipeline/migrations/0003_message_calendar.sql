-- 0003: 消息面 r2 Phase 1（日历层 + 信源分级 + watchlist 行业/主题扩展）。
-- 设计依据 docs/superpowers/specs/2026-08-28-message-eval-design-r2.md §3.1；
-- 执行说明 docs/superpowers/specs/2026-08-28-message-eval-r2-phase1-handoff.md §3.1。

-- ---------------------------------------------------------------------------
-- event_calendar：已知时点事件表（L0 日历层，事实/配置混合）。
-- kind: report_disclosure / unlock / macro_release / fomc / card_review（card_review
-- 为派生项，查询时 union strategy_card_versions，不落本表）。
-- source: 'akshare'（批量拉取+人工核对）/ 'manual'（config/event_calendar.yaml 种子）。
CREATE TABLE event_calendar (
    cal_id             TEXT PRIMARY KEY,
    kind               TEXT NOT NULL,     -- report_disclosure / unlock / macro_release / fomc / card_review
    symbol             TEXT,              -- 宏观类为 NULL
    scheduled_date     TEXT NOT NULL,     -- 市场本地日期
    source             TEXT NOT NULL,     -- 'akshare' / 'manual' / 'derived'
    remind_before_days INTEGER NOT NULL DEFAULT 3,
    note               TEXT,
    raw_object_id      TEXT,
    ingested_at        TEXT NOT NULL
);
CREATE INDEX idx_event_calendar_date ON event_calendar(scheduled_date);

-- ---------------------------------------------------------------------------
-- events 扩列：scope 五档分层（Phase 1 只建列不填充，telegraph/公告的 scope
-- 分类属 Phase 2/3）；source_tier 信源分级（r2 §2.1：公告=1、财联社电报=4，
-- 其余历史路径 NULL=未分级，语义见 docs/database_schema.md §6）。
ALTER TABLE events ADD COLUMN scope TEXT;
ALTER TABLE events ADD COLUMN source_tier INTEGER;

-- ---------------------------------------------------------------------------
-- watchlist 扩列：行业码（东财 BK 码，无可靠来源留 NULL 待人工补，§2.5 不猜）
-- 与主题词（JSON 数组，供 Phase 3 词边界关联用）。
ALTER TABLE watchlist ADD COLUMN industry_code TEXT;
ALTER TABLE watchlist ADD COLUMN themes_json TEXT;
