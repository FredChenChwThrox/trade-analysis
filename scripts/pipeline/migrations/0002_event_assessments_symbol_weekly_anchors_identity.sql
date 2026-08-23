-- 0002: event_assessments 主键加入 symbol（多 symbol 事件不再互相覆盖）；
--       weekly_anchors 加身份唯一索引（anchor_id 跨重算稳定，配合 identity 复用）。
-- 审计修复批次，详见 docs/execution_log.md。

-- ---------------------------------------------------------------------------
-- event_assessments 重建：PK (event_id, assessment_version) → (event_id, symbol, assessment_version)
-- 回填：symbol 取自 event_symbols（当前数据全部为 1:1 关联，已核实）。
-- 若某 event_id 在 event_symbols 中无对应行，NOT NULL 约束会使迁移失败（宁可失败不猜）。
CREATE TABLE event_assessments_new (
    event_id            TEXT NOT NULL REFERENCES events(event_id),
    symbol              TEXT NOT NULL,
    assessment_version  INTEGER NOT NULL,
    model               TEXT,
    prompt_version      TEXT,
    assessed_at         TEXT NOT NULL,      -- UTC
    event_type          TEXT,
    direction           TEXT,               -- positive / negative / neutral
    materiality         TEXT,               -- 重要性分级
    confidence          REAL,               -- 展示用中间值，允许 REAL
    rationale           TEXT,
    status              TEXT NOT NULL,      -- ok / needs_review / degraded
    event_study_json    TEXT,               -- JSON：T+1/T+5 事件研究结果（§5.5 保守时点）
    run_id              TEXT,
    PRIMARY KEY (event_id, symbol, assessment_version)
);

INSERT INTO event_assessments_new (
    event_id, symbol, assessment_version, model, prompt_version, assessed_at,
    event_type, direction, materiality, confidence, rationale, status,
    event_study_json, run_id
)
SELECT
    a.event_id, es.symbol, a.assessment_version, a.model, a.prompt_version,
    a.assessed_at, a.event_type, a.direction, a.materiality, a.confidence,
    a.rationale, a.status, a.event_study_json, a.run_id
FROM event_assessments a
JOIN event_symbols es ON es.event_id = a.event_id;

DROP TABLE event_assessments;
ALTER TABLE event_assessments_new RENAME TO event_assessments;

-- ---------------------------------------------------------------------------
-- weekly_anchors 身份唯一索引：identity = (symbol, anchor_type, trade_date, is_fallback)
-- 现存数据每轮全删全插、天然无重复，索引可安全建立。
CREATE UNIQUE INDEX uq_weekly_anchors_identity
    ON weekly_anchors(symbol, anchor_type, trade_date, is_fallback);
