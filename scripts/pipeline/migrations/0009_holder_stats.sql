-- 0009: 股东户数序列（筹码集中度间接指标，§5.7）。
-- 数据源：akshare stock_zh_a_gdhs_detail_em（东财 datacenter，仅 A 股，实测
-- 上市起全历史）。事件驱动披露（财报配套 + 少数公司自愿月度），
-- 手触发采集（akshare_collect --sources gdhs），不进 daily 默认 sources。
-- 点时口径：stat_date=统计截止日（事实归属日）；announced_at=公告日期
-- （PIT 可见日；倒挂即披露滞后如实保留，不做插值不猜，§2.5）。
-- upsert 语义：同 (symbol, stat_date) 重采，内容一致幂等跳过、变化原地更新
-- （快照风格，同 macro_factors；无 revision 链）。
CREATE TABLE holder_stats (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                 TEXT NOT NULL,
    stat_date              TEXT NOT NULL,   -- 股东户数统计截止日 YYYY-MM-DD
    holder_count           INTEGER NOT NULL,-- 股东户数-本次（户）
    holder_count_prev      INTEGER,         -- 股东户数-上次（户）
    holder_count_delta     INTEGER,         -- 增减（户）
    holder_count_delta_pct REAL,            -- 增减比例（小数，-0.4279=-42.79%；采集器归一）
    avg_hold_value         REAL,            -- 户均持股市值（元）
    avg_hold_shares        REAL,            -- 户均持股数量（股）
    total_share            REAL,            -- 总股本（股）
    share_change           REAL,            -- 股本变动（股）
    announced_at           TEXT,            -- 公告日期 YYYY-MM-DD
    source                 TEXT NOT NULL,   -- 'akshare'
    raw_object_id          TEXT NOT NULL,
    ingested_at            TEXT NOT NULL,
    UNIQUE(symbol, stat_date)
);
