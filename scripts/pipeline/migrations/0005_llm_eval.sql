-- 0005: 消息面 r2 Phase 3——LLM 评价链。
-- 设计依据 docs/superpowers/specs/2026-08-28-message-eval-design-r2.md §3.3。
-- 三件事：symbol_industry 新表；event_assessments 重建（修 0002 遗留
-- assessment_version INTEGER 亲和 + 扩 r2 研判字段）；event_human_review 新表。

-- ---------------------------------------------------------------------------
-- symbol_industry [事实]：全市场行业归属（东财细分行业 BK 码，一次性全量 + 季度刷新，
-- 独立手触发不进 daily）。关联层 ② 的 JOIN 侧。
CREATE TABLE symbol_industry (
    symbol          TEXT NOT NULL,
    industry_code   TEXT NOT NULL,    -- 东财板块码 BKxxxx
    industry_name   TEXT NOT NULL,
    source          TEXT NOT NULL,    -- 'akshare_em'
    classification_date TEXT NOT NULL,
    raw_object_id   TEXT,
    ingested_at     TEXT NOT NULL,
    PRIMARY KEY (symbol, source, classification_date)
);
CREATE INDEX idx_symbol_industry_lookup ON symbol_industry(symbol, source);

-- ---------------------------------------------------------------------------
-- event_assessments 重建 [决策]：
-- ① assessment_version INTEGER 亲和 → TEXT NOT NULL（修 0002 遗留，'event_study_v1'/'llm_v1'）；
-- ② 扩 r2 研判字段：target/half_life（四道筛子 3/4）、expectation_gap（人补）、
--    action_hint（仅提示不触发）、falsification（人写/LLM 建议稿）、narrative（逐股叙事）。
-- 回填：原列全量平移，新列 NULL（历史 event_study_v1 行不冒充研判）。
CREATE TABLE event_assessments_new (
    event_id            TEXT NOT NULL REFERENCES events(event_id),
    symbol              TEXT NOT NULL,          -- '__event__' 为事件级行
    assessment_version  TEXT NOT NULL,          -- 'event_study_v1' / 'llm_v1'
    model               TEXT,
    prompt_version      TEXT,
    assessed_at         TEXT NOT NULL,
    event_type          TEXT,
    direction           TEXT,                   -- positive / negative / neutral
    materiality         TEXT,                   -- low / medium / high / critical
    confidence          REAL,
    rationale           TEXT,                   -- ≤300 字，禁预测语言
    target              TEXT,                   -- eps / pe / sentiment（四道筛子 3）
    half_life           TEXT,                   -- day / week / month / quarter（4）
    expectation_gap     TEXT,                   -- 预期差描述（LLM 初判可空，人补）
    action_hint         TEXT,                   -- none / swing / schedule / redraw_anchor
    falsification       TEXT,                   -- 证伪条件（人定稿，LLM 可给建议）
    narrative           TEXT,                   -- 逐股叙事（仅 symbol 行，≤150 字）
    status              TEXT NOT NULL,          -- ok / needs_review / degraded / suspended
    event_study_json    TEXT,
    run_id              TEXT,
    PRIMARY KEY (event_id, symbol, assessment_version)
);

INSERT INTO event_assessments_new (
    event_id, symbol, assessment_version, model, prompt_version, assessed_at,
    event_type, direction, materiality, confidence, rationale, status,
    event_study_json, run_id
)
SELECT
    event_id, symbol, CAST(assessment_version AS TEXT), model, prompt_version,
    assessed_at, event_type, direction, materiality, confidence, rationale,
    status, event_study_json, run_id
FROM event_assessments;

DROP TABLE event_assessments;
ALTER TABLE event_assessments_new RENAME TO event_assessments;

-- ---------------------------------------------------------------------------
-- event_human_review [决策]：人工对机器标签的确认/否决/补写，不改写原始行。
-- 主键含 reviewed_at 支持多次操作；effective_status 由查询层解析：
-- 未撤销 dismiss → 排除；upgrade_materiality → 覆盖 materiality 显示；
-- confirm → ok；amend → payload 覆盖 expectation_gap/falsification/target/half_life
-- 显示值；否则取 event_assessments.status。
CREATE TABLE event_human_review (
    event_id     TEXT NOT NULL REFERENCES events(event_id),
    symbol       TEXT NOT NULL,          -- '__event__' 为事件级
    action       TEXT NOT NULL,          -- confirm / dismiss / upgrade_materiality / note / amend
    payload_json TEXT,                   -- amend/upgrade/note 的补充值
    actor        TEXT NOT NULL,
    reviewed_at  TEXT NOT NULL,
    run_id       TEXT,
    PRIMARY KEY (event_id, symbol, reviewed_at)
);
