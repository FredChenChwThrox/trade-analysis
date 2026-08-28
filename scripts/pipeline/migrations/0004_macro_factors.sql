-- 0004: 消息面 r2 Phase 2——宏观因子快照表。
-- 设计依据 docs/superpowers/specs/2026-08-28-message-eval-design-r2.md §3.2。
-- [事实] 因子清单固化在 config/macro_factors.yaml；采集经 akshare_collect
-- --sources macro（sina 系接口）→ adapters/macro_factors.py 入库。
-- flow 层（龙虎榜/大宗）不建新表，统一入 events（scope='flow'，r2 §3.2）。
CREATE TABLE macro_factors (
    factor_type  TEXT NOT NULL,        -- commodity / fx / index_proxy
    code         TEXT NOT NULL,        -- 'AU0' / 'USDCNY' / 'OIL'
    name         TEXT NOT NULL,
    market       TEXT NOT NULL,        -- CN / GLOBAL
    trade_date   TEXT NOT NULL,
    close        TEXT NOT NULL,        -- 定点 TEXT（来源原始值，不换算）
    change_pct   TEXT,                 -- 来源原始值；来源无则 NULL，adapter 不计算
    unit         TEXT,
    source       TEXT NOT NULL,
    raw_object_id TEXT,
    ingested_at  TEXT NOT NULL,
    PRIMARY KEY (factor_type, code, trade_date)
);
CREATE INDEX idx_macro_factors_recent ON macro_factors(code, trade_date DESC);
