-- 0010: 自算筹码分布快照表（换手率衰减模型，chip_v1）。
-- 设计：docs/superpowers/specs/2026-09-04-chip-distribution-design.md（v2，评审修订版）。
-- 定位纪律：模型估算观察项，不进信号链、不进 daily 默认链（§2.5）。
-- 口径：复权域计算、输出折回不复权（§5.4）；现金分红在前复权口径下表现为
-- 历史成本平移，不还原真实股东成本（设计 §2.3/§7 偏差声明）。
-- 幂等：DELETE+重插+pipeline_runs 同事务（§6 派生表惯例）；--all 单一全局 run_id。
CREATE TABLE chip_distribution (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    trade_date          TEXT NOT NULL,
    winner_ratio        REAL,            -- 获利比例 [0,1]
    avg_cost            REAL,            -- 平均成本（不复权口径，已折回）
    cost_5              REAL,            -- 90% 成本区间下沿（不复权）
    cost_95             REAL,            -- 90% 成本区间上沿（不复权）
    concentration_90    REAL,            -- 90% 集中度 = (cost95-cost5)/(cost95+cost5)
    estimation_status   TEXT NOT NULL,   -- burn_in / mature / insufficient_data
    turnover_used       REAL,            -- 当日换手率快照（审计：结果对得上输入）
    amount_used         REAL,            -- 当日成交额快照（对照核 vwap 峰输入审计）
    source              TEXT NOT NULL,   -- 'self_computed'
    params_json         TEXT NOT NULL,   -- {A,k_cap,peak_mode,dist_shape,n_bins,
                                         --  burn_in_days,price_pad,window}
    run_id              TEXT NOT NULL,
    rule_version        TEXT NOT NULL,   -- chip_v1_close_tri（形状/峰值编码进版本串）
    config_hash         TEXT NOT NULL,
    computed_at         TEXT NOT NULL,
    UNIQUE(symbol, trade_date)
);
