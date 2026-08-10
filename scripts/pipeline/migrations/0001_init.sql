-- Migration 0001: schema v1（设计文档 §7 全量表）
--
-- 生命周期分类（设计 §2.2）在各表注释中标注：
--   [原始]  不可变，append-only
--   [事实]  规范化事实，允许因来源修订 upsert，但须可追溯（data_revisions / raw_object_id）
--   [派生]  可按当前口径删除后重算
--   [决策]  不可覆盖，只追加新版本或冲正记录
--   [配置]  种子/参数，可 upsert
--   [运行]  运行与审计日志，append-only
--
-- 通用约定：
--   - 时间戳一律 UTC TEXT（ISO8601），需要时另存来源时区字段（*_tz）；
--   - trade_date / 各日期为市场本地日期 TEXT（YYYY-MM-DD）；
--   - 关键决策值（卡片价区相关、执行价/数量/费用、汇率、财务金额/股数）存 TEXT 定点十进制字符串；
--     行情 OHLCV、指标等中间/展示值允许 REAL（§9.5 软约束）；
--   - JSON 列一律 TEXT，写库前按对应 JSON Schema 校验（§7）。

-- [运行] 每次流水线各阶段的运行记录（版本与审计字段见设计 §2.3）
CREATE TABLE pipeline_runs (
    run_id          TEXT NOT NULL,
    stage           TEXT NOT NULL,          -- 阶段名；同一阶段可安全重跑（覆盖同行）
    as_of           TEXT,                   -- 本次计算的数据截止时间（UTC）
    data_cutoff     TEXT,                   -- 入库数据截止（市场本地日期或 UTC）
    adapter_version TEXT,
    config_hash     TEXT,
    rule_version    TEXT,
    card_version_id TEXT,                   -- 涉及卡片时记录
    app_version     TEXT,
    git_commit      TEXT,
    status          TEXT NOT NULL,          -- running/success/degraded/failed
    error           TEXT,
    started_at      TEXT NOT NULL,          -- UTC
    finished_at     TEXT,
    PRIMARY KEY (run_id, stage)
);

-- [原始] 不可变来源文件索引（data/raw/ 下落盘文件 + 请求元数据 + 校验和）
CREATE TABLE raw_objects (
    raw_object_id       TEXT PRIMARY KEY,
    run_id              TEXT,
    source              TEXT NOT NULL,      -- stock_finance_data / yahoo_finance / ...
    data_type           TEXT NOT NULL,      -- price / announcement / fx / stock_actions / ...
    symbol              TEXT,
    request_params_json TEXT,               -- JSON：请求参数
    file_path           TEXT NOT NULL,      -- data/raw/{source}/{data_type}/{date}/{run_id}/...
    content_hash        TEXT,               -- 相同 hash 不重复解析（§8.3）
    fetch_status        TEXT NOT NULL,      -- ok / error
    ingested_at         TEXT NOT NULL       -- UTC
);
CREATE INDEX idx_raw_objects_hash ON raw_objects(content_hash);

-- [原始] 规范化事实发生修订时的前后值记录（§9.5：第一版允许降级为日志式记录）
CREATE TABLE data_revisions (
    revision_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name      TEXT NOT NULL,          -- 被修订的规范化事实表
    record_key_json TEXT NOT NULL,          -- JSON：主键定位
    field_name      TEXT,                   -- 为空表示整行级重建
    old_value       TEXT,
    new_value       TEXT,
    source          TEXT,
    reason          TEXT,
    run_id          TEXT,
    created_at      TEXT NOT NULL           -- UTC
);

-- [配置] 股票池（种子：config/watchlist.yaml，可 upsert）
CREATE TABLE watchlist (
    symbol          TEXT PRIMARY KEY,
    market          TEXT NOT NULL,          -- CN / HK
    name            TEXT NOT NULL,
    aliases_json    TEXT,                   -- JSON：新闻匹配用别名
    benchmark_code  TEXT NOT NULL,          -- 000300.SH / ^HSI，可按股票覆盖（§3.5）
    currency        TEXT NOT NULL,          -- 交易币种
    timezone        TEXT NOT NULL,          -- 市场本地时区
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,          -- UTC
    updated_at      TEXT NOT NULL           -- UTC
);

-- [事实] 交易日历（种子：config/calendar_{market}_{year}.yaml 展开为逐日行；§3.5）
CREATE TABLE trading_calendar (
    market          TEXT NOT NULL,
    trade_date      TEXT NOT NULL,          -- 市场本地日期
    is_open         INTEGER NOT NULL,       -- 是否开市
    is_full_day     INTEGER NOT NULL,       -- 是否完整交易日（半日市为 0）
    session_open    TEXT,                   -- 开市日开/闭市时间（市场本地）
    session_close   TEXT,
    status          TEXT NOT NULL,          -- trading / half_day / weekend / holiday
    status_detail   TEXT,                   -- 节假日名称等
    timezone        TEXT NOT NULL,          -- 来源时区
    source          TEXT NOT NULL,          -- 种子文件名
    updated_at      TEXT NOT NULL,          -- UTC
    PRIMARY KEY (market, trade_date)
);

-- [事实] 规范化不复权日线 + 复权因子（§3.2；来源修订须写 data_revisions）
CREATE TABLE daily_bars (
    symbol          TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    market          TEXT NOT NULL,
    open_raw        REAL,
    high_raw        REAL,
    low_raw         REAL,
    close_raw       REAL,
    volume_raw      REAL,                   -- 股
    amount_raw      REAL,                   -- 可为空（stock_finance_data 无 amount，见 probe 记录）
    currency        TEXT,
    price_adj_factor REAL,                  -- 前向累积因子，origin 日归一 1.0（§3.3）
    share_factor    REAL,                   -- 只反映拆股/送转等股数变化
    trading_status  TEXT,                   -- normal / suspended
    source          TEXT NOT NULL,
    raw_object_id   TEXT REFERENCES raw_objects(raw_object_id),
    updated_at      TEXT NOT NULL,          -- UTC
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX idx_daily_bars_date ON daily_bars(trade_date);

-- [事实] 公司行为事实（除权除息、拆并股、送转等；§5.4b 冻结/换算的输入）
CREATE TABLE corporate_actions (
    ca_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    ex_date         TEXT NOT NULL,          -- 除权除息生效日（市场本地）
    action_type     TEXT NOT NULL,          -- cash_dividend / split / bonus_share / ...
    cash_per_share  TEXT,                   -- 每股现金分红，定点十进制字符串
    split_ratio     TEXT,                   -- 股份倍率，定点十进制字符串
    details_json    TEXT,                   -- JSON：来源组合等（§3.7 details 约定）
    source          TEXT NOT NULL,
    available_at    TEXT,                   -- UTC
    raw_object_id   TEXT REFERENCES raw_objects(raw_object_id),
    created_at      TEXT NOT NULL,          -- UTC
    UNIQUE (symbol, ex_date, action_type)
);

-- [事实] 复权因子版本（方向、归一化日、来源、生成算法；§3.3）
CREATE TABLE adjustment_factor_versions (
    version_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    factor_origin_date TEXT NOT NULL,       -- 归一化日，该日因子 = 1.0
    direction       TEXT NOT NULL DEFAULT 'forward_cumulative',
    algorithm       TEXT NOT NULL,          -- 生成算法说明（如 forward/none 平台段检测）
    source          TEXT NOT NULL,
    run_id          TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL           -- UTC
);
CREATE INDEX idx_afv_symbol ON adjustment_factor_versions(symbol, created_at);

-- [派生] 完成周的复权技术周线（逐日复权后聚合，只写完成周；§3.4，可重算）
CREATE TABLE weekly_bars (
    symbol          TEXT NOT NULL,
    week_end_date   TEXT NOT NULL,          -- 该周最后一个交易日（由 trading_calendar 判定）
    week_start_date TEXT NOT NULL,          -- 该周第一个交易日
    open_adj        REAL,
    high_adj        REAL,
    low_adj         REAL,
    close_adj       REAL,
    volume_adj      REAL,                   -- 调整后成交量求和
    amount_raw      REAL,                   -- 成交额求和，不做股份因子调整
    trading_days    INTEGER NOT NULL,
    run_id          TEXT,
    PRIMARY KEY (symbol, week_end_date)
);

-- [事实] 基准指数日线（交叉校验日历 + 超额收益对照；§3.5）
CREATE TABLE index_bars (
    index_code      TEXT NOT NULL,          -- 000300.SH / ^HSI
    trade_date      TEXT NOT NULL,
    open            REAL,
    high            REAL,
    low             REAL,
    close           REAL,
    volume          REAL,
    currency        TEXT,
    source          TEXT NOT NULL,
    available_at    TEXT,                   -- UTC
    raw_object_id   TEXT REFERENCES raw_objects(raw_object_id),
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (index_code, trade_date)
);

-- [事实] 公告/新闻事件（只存事实字段，不混 LLM 评价；§3.6）
CREATE TABLE events (
    event_id            TEXT PRIMARY KEY,
    event_type          TEXT NOT NULL,      -- announcement / news
    event_at            TEXT,               -- 事件实际发生时间（UTC），未知为空
    published_at        TEXT,               -- 来源正式发布时间（UTC）
    published_tz        TEXT,               -- 来源时区
    available_at        TEXT,               -- 系统允许参与计算的最早时间（UTC）
    title               TEXT,
    summary             TEXT,
    canonical_url       TEXT,
    source              TEXT NOT NULL,
    source_external_id  TEXT,               -- 优先用于去重
    content_hash        TEXT,               -- 其次 URL / 内容哈希去重
    raw_object_id       TEXT REFERENCES raw_objects(raw_object_id),
    ingested_at         TEXT NOT NULL       -- UTC
);
CREATE INDEX idx_events_published ON events(published_at);
CREATE INDEX idx_events_external ON events(source, source_external_id);

-- [事实] 事件-股票关联（一条事件可关联多只股票）
CREATE TABLE event_symbols (
    event_id        TEXT NOT NULL REFERENCES events(event_id),
    symbol          TEXT NOT NULL,
    PRIMARY KEY (event_id, symbol)
);
CREATE INDEX idx_event_symbols_symbol ON event_symbols(symbol);

-- [决策] 版本化 LLM 消息评价 + 事件研究结果（§5.5；原始事件表不保存评价列）
CREATE TABLE event_assessments (
    event_id            TEXT NOT NULL REFERENCES events(event_id),
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
    PRIMARY KEY (event_id, assessment_version)
);

-- [事实] 财报头（修订新增 revision，不覆盖旧版本；§3.7）
CREATE TABLE financial_reports (
    report_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    period_end      TEXT NOT NULL,          -- 报告期截止日
    period_type     TEXT NOT NULL,          -- annual / interim / quarterly
    fiscal_year     INTEGER NOT NULL,
    published_at    TEXT,                   -- 正式披露时间（UTC）
    published_tz    TEXT,                   -- 来源时区
    available_at    TEXT NOT NULL,          -- 取披露时间，不取报告期截止日（§2.1）
    revision        INTEGER NOT NULL DEFAULT 1,
    currency        TEXT,                   -- 财务币种
    unit            TEXT,                   -- 金额单位
    is_cumulative   INTEGER NOT NULL,       -- 累计值（季报/中报）或单期
    raw_object_id   TEXT REFERENCES raw_objects(raw_object_id),
    ingested_at     TEXT NOT NULL,          -- UTC
    UNIQUE (symbol, period_end, period_type, is_cumulative, revision)
);
CREATE INDEX idx_financial_reports_symbol ON financial_reports(symbol, period_end);

-- [事实] 财务事实（金额/股数为关键决策值，存 TEXT 定点十进制；§3.7、§7）
CREATE TABLE financial_facts (
    report_id           INTEGER PRIMARY KEY REFERENCES financial_reports(report_id),
    revenue             TEXT,               -- 营收
    net_profit_attr     TEXT,               -- 归母净利（TTM 与 PE 口径）
    eps_basic           TEXT,
    eps_diluted         TEXT,
    shares_issued_end   TEXT,               -- 期末已发行股数（PE 默认口径）
    shares_float_end    TEXT,               -- 期末流通股数
    share_count_type    TEXT,               -- issued / float；来源只有流通股数时必须标注（§3.7）
    updated_at          TEXT NOT NULL       -- UTC
);

-- [事实] 股本变动事件（增发/回购注销/送转/转股；§3.7 三来源优先级记入 details_json）
CREATE TABLE share_capital_events (
    sce_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                  TEXT NOT NULL,
    effective_at            TEXT NOT NULL,  -- 生效日（市场本地）
    available_at            TEXT NOT NULL,  -- UTC
    event_type              TEXT NOT NULL,  -- issuance / buyback_cancel / bonus_share / conversion
    share_change            TEXT,           -- 股数变化，定点十进制字符串
    shares_issued_after     TEXT,           -- 变动后已发行股数
    share_count_type        TEXT,           -- issued / float
    details_json            TEXT,           -- JSON：来源组合标注（§3.7）
    source                  TEXT NOT NULL,
    raw_object_id           TEXT REFERENCES raw_objects(raw_object_id),
    created_at              TEXT NOT NULL   -- UTC
);
CREATE INDEX idx_sce_symbol ON share_capital_events(symbol, effective_at);

-- [事实] 财务币种→交易币种日汇率（换算方向在 adapter 内统一；汇率为关键决策值存 TEXT）
CREATE TABLE fx_rates (
    from_currency   TEXT NOT NULL,          -- 财务币种
    to_currency     TEXT NOT NULL,          -- 交易币种
    rate_date       TEXT NOT NULL,
    rate            TEXT NOT NULL,          -- 定点十进制字符串
    source          TEXT NOT NULL,
    available_at    TEXT,                   -- UTC
    raw_object_id   TEXT REFERENCES raw_objects(raw_object_id),
    updated_at      TEXT NOT NULL,          -- UTC
    PRIMARY KEY (from_currency, to_currency, rate_date)
);

-- [事实] 分析师预测历史快照（每次抓取全量保存；历史查询取 snapshot_at <= as_of 最新快照）
CREATE TABLE forecasts (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    snapshot_at     TEXT NOT NULL,          -- UTC，抓取快照时间
    source          TEXT NOT NULL,
    payload_json    TEXT NOT NULL,          -- JSON：当次预测全量
    raw_object_id   TEXT REFERENCES raw_objects(raw_object_id),
    ingested_at     TEXT NOT NULL           -- UTC
);
CREATE INDEX idx_forecasts_symbol ON forecasts(symbol, snapshot_at);

-- [派生] 日线指标（当前口径，可重算；主键 (symbol, trade_date)；§4.3）
-- 实际执行与已发布报告引用的指标值另存输入快照，不依赖本表重算结果。
CREATE TABLE indicators_daily (
    symbol          TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    ma5             REAL,
    ma10            REAL,
    ma20            REAL,
    ma60            REAL,
    ma120           REAL,
    ma250           REAL,
    dif             REAL,
    dea             REAL,
    macd_hist       REAL,                   -- 2*(DIF-DEA)
    rsi6            REAL,
    rsi12           REAL,
    rsi24           REAL,
    boll_mid        REAL,
    boll_upper      REAL,
    boll_lower      REAL,
    boll_bandwidth  REAL,
    vol_ma5         REAL,
    vol_ma10        REAL,
    vol_mean20      REAL,
    vol_std20       REAL,
    vol_mean60      REAL,
    vol_std60       REAL,
    amt_mean20      REAL,                   -- 成交额同参数均值/标准差（可得时）
    amt_std20       REAL,
    amt_mean60      REAL,
    amt_std60       REAL,
    kdj_k           REAL,
    kdj_d           REAL,
    kdj_j           REAL,
    pct_chg         REAL,
    amplitude       REAL,
    pe_ttm          REAL,                   -- 市值口径 PE(TTM)
    pe_status       TEXT,                   -- 空值原因码（TTM<=0 / 缺股本 / 缺汇率等）
    run_id          TEXT,
    rule_version    TEXT,
    config_hash     TEXT,
    computed_at     TEXT NOT NULL,          -- UTC
    PRIMARY KEY (symbol, trade_date)
);

-- [派生] 周线指标（只用完成周；主键 (symbol, week_end_date)）
CREATE TABLE indicators_weekly (
    symbol          TEXT NOT NULL,
    week_end_date   TEXT NOT NULL,
    ma5             REAL,
    ma10            REAL,
    ma20            REAL,
    ma60            REAL,
    dif             REAL,
    dea             REAL,
    macd_hist       REAL,
    rsi6            REAL,
    rsi12           REAL,
    rsi24           REAL,
    boll_mid        REAL,
    boll_upper      REAL,
    boll_lower      REAL,
    boll_bandwidth  REAL,
    vol_ma5         REAL,
    vol_ma10        REAL,
    vol_mean20      REAL,
    vol_std20       REAL,
    kdj_k           REAL,
    kdj_d           REAL,
    kdj_j           REAL,
    pct_chg         REAL,
    amplitude       REAL,
    run_id          TEXT,
    rule_version    TEXT,
    config_hash     TEXT,
    computed_at     TEXT NOT NULL,          -- UTC
    PRIMARY KEY (symbol, week_end_date)
);

-- [派生] 周线锚点（恐慌低点 / 下跌起点；§5.2，随 as_of 重算，fallback 变化生成新 anchor_id）
CREATE TABLE weekly_anchors (
    anchor_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    as_of           TEXT NOT NULL,          -- 识别时点（市场本地日期）
    anchor_type     TEXT NOT NULL,          -- panic_low / decline_start
    trade_date      TEXT NOT NULL,          -- 锚点交易日
    adjusted_price  REAL NOT NULL,          -- 识别时复权价（技术比较用）
    raw_price       REAL NOT NULL,          -- 当日不复权价（排期卡价区比较用，§3.4）
    is_fallback     INTEGER NOT NULL DEFAULT 0,
    run_id          TEXT,
    created_at      TEXT NOT NULL           -- UTC
);
CREATE INDEX idx_weekly_anchors ON weekly_anchors(symbol, as_of);

-- [派生] 确定性信号事实（§5.1；当前口径可重算；历史决策依据由快照冻结）
CREATE TABLE signal_facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    observed_on     TEXT NOT NULL,          -- 观测日/观测周（市场本地日期）
    signal          TEXT NOT NULL,          -- 信号类型（panic / dry_up / no_new_low_3w / divergence / duration / falsify / tier / right_side ...）
    state           TEXT NOT NULL,          -- active / inactive / 状态机状态等
    anchor_id       INTEGER,                -- 关联 weekly_anchors；同一 anchor_id 下统计活跃信号数
    triggered       INTEGER NOT NULL DEFAULT 0,
    active_until    TEXT,                   -- 活跃截止（市场本地日期）
    details_json    TEXT,                   -- JSON：日期、原值、阈值、锚点、原因码（按 signal 类型的 Schema）
    run_id          TEXT,
    rule_version    TEXT,
    config_hash     TEXT,
    created_at      TEXT NOT NULL,          -- UTC
    UNIQUE (symbol, signal, observed_on)
);
CREATE INDEX idx_signal_facts_symbol ON signal_facts(symbol, observed_on);

-- [决策] 排期卡不可变版本（§5.6；LLM/Skill 只产 draft，人工确认激活；
--  激活新版本时关闭旧版 effective_to，旧版本不修改）
CREATE TABLE strategy_card_versions (
    card_version_id         TEXT PRIMARY KEY,
    symbol                  TEXT NOT NULL,
    status                  TEXT NOT NULL,  -- draft / active / superseded / rejected
    schema_version          TEXT NOT NULL,
    created_at              TEXT NOT NULL,  -- UTC
    effective_from          TEXT,           -- 市场本地日期
    effective_to            TEXT,
    supersedes_id           TEXT REFERENCES strategy_card_versions(card_version_id),
    currency                TEXT,
    price_basis             TEXT,           -- 价区口径（第一版为不复权绝对价位）
    earnings_scenarios_json TEXT,           -- JSON：EPS 三情景（价区为关键决策值，JSON 内存定点字符串）
    valuation_scenarios_json TEXT,          -- JSON：PE 刻度/情景（须标注 3 年样本区间，§3.2）
    price_tiers_json        TEXT,           -- JSON：三档价区（关键决策值：定点十进制字符串）
    invalidation_json       TEXT,           -- JSON：证伪线
    swing_box_json          TEXT,           -- JSON：波段箱体
    right_side_trigger_json TEXT,           -- JSON：右侧触发位/止损位
    next_review_at          TEXT,           -- 复核到期日（到期生成提醒，不自动延后）
    input_snapshot_json     TEXT,           -- JSON：输入快照/换算来源（§5.4b 机械换算明细）
    run_id                  TEXT
);
-- 同一股票同一时刻最多一个 active 版本（硬门槛）
CREATE UNIQUE INDEX uq_card_active ON strategy_card_versions(symbol) WHERE status = 'active';

-- [决策] 执行记录（append-only；错录用冲正记录修复，不更新/删除原记录；§5.7）
CREATE TABLE executions (
    execution_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key         TEXT NOT NULL UNIQUE,
    symbol                  TEXT NOT NULL,
    executed_at             TEXT NOT NULL,  -- UTC
    action_type             TEXT NOT NULL,  -- buy / sell / reversal 等
    tier                    TEXT,           -- 档位
    price                   TEXT,           -- 关键决策值：定点十进制字符串
    quantity                TEXT,           -- 定点十进制字符串
    fees                    TEXT,           -- 定点十进制字符串
    card_version_id         TEXT REFERENCES strategy_card_versions(card_version_id),
    signal_snapshot_json    TEXT,           -- JSON：执行时信号/指标快照（冻结，不随重算变化）
    reverses_execution_id   INTEGER REFERENCES executions(execution_id),
    created_at              TEXT NOT NULL   -- UTC
);
CREATE INDEX idx_executions_symbol ON executions(symbol, executed_at);

-- [输出] 报告运行记录（已发布报告不被重算覆盖；修订 = 新 revision 行 + 新文件，§9.5）
CREATE TABLE report_runs (
    report_run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type         TEXT NOT NULL,      -- single / daily / weekly
    symbol              TEXT,               -- 全池日报为空
    as_of               TEXT NOT NULL,      -- 数据截止时间（UTC）
    trade_date          TEXT NOT NULL,      -- 报告对应交易日（市场本地）
    revision            INTEGER NOT NULL DEFAULT 1,
    card_version_id     TEXT REFERENCES strategy_card_versions(card_version_id),
    rule_version        TEXT,
    config_hash         TEXT,
    input_snapshot_json TEXT,               -- JSON：报告输入快照（决策点可追溯，§9.3）
    status              TEXT NOT NULL,      -- complete / incomplete / degraded / failed
    file_path           TEXT,               -- reports/{symbol}/{trade_date}.md 等
    run_id              TEXT,
    created_at          TEXT NOT NULL       -- UTC
);
CREATE INDEX idx_report_runs ON report_runs(report_type, symbol, trade_date);
