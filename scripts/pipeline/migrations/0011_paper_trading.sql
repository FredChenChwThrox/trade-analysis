-- 0011: 模拟盘（信号决策力实验）——决策流水 + 虚拟仓位。
-- 设计：docs/superpowers/specs/2026-09-05-paper-trading-design.md（v2 评审修订版）。
-- 定位纪律：与 executions 完全隔离；不进信号链/daily 默认链；LLM 不参与。
-- 价格列 TEXT 定点（同 executions 纪律）；ret/pnl REAL 展示级。
-- 反作弊：价格系统取不可自填；窗口 T 盘后→T+1 收盘（超窗 late）；append-only 冲正；
-- 快照冻结。复权因子版本变化会重算 open 仓位收益（结算时点现取，已知偏差声明）。

CREATE TABLE paper_decisions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol               TEXT NOT NULL,
    decision_date        TEXT NOT NULL,   -- 决策日 T（=信号事件日）
    decision_type        TEXT NOT NULL,   -- entry / exit
    signal_source        TEXT NOT NULL,   -- tier_triggered / right_side /
                                          -- falsification_breach / box_entry /
                                          -- deep_exit / timeout / manual / reversal
    signal_snapshot_json TEXT NOT NULL,   -- 冻结：signal/state/details/close_raw/
                                          -- price_adj_factor（决策语境）
    decision             TEXT NOT NULL,   -- follow / skip / counter
    close_used           TEXT,            -- T 日收盘（不复权定点，系统取）
    quantity             INTEGER,         -- follow-entry：notional/close 取整百股
    notional             REAL,            -- 固定名义（config/paper.yaml）
    constraint_tag       TEXT,            -- 'single_position'（结构性 skip）
    late                 INTEGER NOT NULL DEFAULT 0,
    reversed_by          INTEGER,         -- 冲正行 id（冲正模式，append-only）
    note                 TEXT,
    run_id               TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    UNIQUE(symbol, decision_date, decision_type, signal_source)
);

CREATE TABLE paper_positions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol             TEXT NOT NULL,
    entry_decision_id  INTEGER NOT NULL REFERENCES paper_decisions(id),
    entry_date         TEXT NOT NULL,
    entry_close        TEXT NOT NULL,   -- 不复权定点
    quantity           INTEGER NOT NULL,
    notional           REAL NOT NULL,
    deep_exit_line     TEXT NOT NULL,   -- 深度脱离线（entry 时冻结，不复权定点）
    status             TEXT NOT NULL,   -- open / closed
    exit_decision_id   INTEGER REFERENCES paper_decisions(id),
    exit_date          TEXT,            -- 结算日（停牌顺延后）
    exit_close         TEXT,            -- 不复权定点
    exit_source        TEXT,            -- falsification / stopped_out / deep_exit /
                                        --  timeout / manual / reversal
    hold_days          INTEGER,         -- 交易日历日数（含停牌）
    ret                REAL,            -- 结算时按库内现值因子计算（§3.3）
    pnl                REAL,            -- notional × ret
    closed_at          TEXT
);
