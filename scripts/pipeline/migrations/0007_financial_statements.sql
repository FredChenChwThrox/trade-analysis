-- 0007: 基本面深度分析数据层——资产负债表/现金流量表事实表 + 源算指标快照表。
-- 背景：基本面分析 skill（skills/fundamental-analysis-skill）需要债务/现金流/ROE 维度；
-- 数据源 akshare（sina stock_financial_report_sina 全历史 + THS stock_financial_abstract），
-- 采集走 akshare_collect --sources balance_sheet,cash_flow,fin_abstract（手触发，不进 daily）。
--
-- 设计要点：
-- 1. 表头复用 financial_reports（revision/published_at/available_at 语义不变）；
--    2023 年以前无表头的期次由 adapter 新建（published_at=NULL，available_at 降级=
--    入库时间，同 D1.3 口径）——远古期次仅服务长期趋势分析，不进信号链；
--    PIT 方向安全（available_at=入库时间 → 历史 as-of 查询看不到这些行）。
-- 2. 事实表挂 report_id（与 financial_facts 同模式）；内容变化走 data_revisions
--    记录 + 原地更新（报表更正罕见，header revision 语义留给利润表）。
-- 3. financial_indicator_snapshots 存 THS 源算指标（forecasts 风格 payload），
--    只作交叉核对与兜底，不作规范化数字来源（规范化派生指标由 Python 在
--    fundamental_inputs 导出层计算，公式单测锁定）。

CREATE TABLE balance_sheet_facts (
    report_id            INTEGER PRIMARY KEY REFERENCES financial_reports(report_id),
    total_assets         TEXT,          -- 资产总计（元）
    total_liabilities    TEXT,          -- 负债合计（元）
    total_equity_attr    TEXT,          -- 归属于母公司股东权益合计（元，ROE 分母口径）
    monetary_fund        TEXT,          -- 货币资金
    short_term_borrowing TEXT,          -- 短期借款
    long_term_borrowing  TEXT,          -- 长期借款
    bonds_payable        TEXT,          -- 应付债券
    noncurrent_liab_1y   TEXT,          -- 一年内到期的非流动负债
    inventory            TEXT,          -- 存货
    accounts_receivable  TEXT,          -- 应收账款（不含应收票据）
    accounts_payable     TEXT,          -- 应付账款（不含应付票据）
    goodwill             TEXT,          -- 商誉
    updated_at           TEXT NOT NULL  -- UTC
);

CREATE TABLE cash_flow_facts (
    report_id         INTEGER PRIMARY KEY REFERENCES financial_reports(report_id),
    ocf               TEXT,             -- 经营活动产生的现金流量净额（元）
    capex             TEXT,             -- 购建固定资产、无形资产和其他长期资产所支付的现金
    icf               TEXT,             -- 投资活动产生的现金流量净额
    financing_cf      TEXT,             -- 筹资活动产生的现金流量净额
    net_cash_increase TEXT,             -- 现金及现金等价物净增加额
    updated_at        TEXT NOT NULL     -- UTC
    -- FCF = ocf − capex 为派生指标，由导出层 Python 计算，不落库
);

CREATE TABLE financial_indicator_snapshots (
    snapshot_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    period_end    TEXT NOT NULL,        -- 报告期截止日
    source        TEXT NOT NULL,        -- 'akshare'（THS 摘要）
    payload_json  TEXT NOT NULL,        -- {选项: {指标: 值}} 全量保留，金额单位元
    raw_object_id TEXT REFERENCES raw_objects(raw_object_id),
    ingested_at   TEXT NOT NULL,
    UNIQUE (symbol, period_end, source)
);
CREATE INDEX idx_fin_indicator_snapshots_symbol
    ON financial_indicator_snapshots(symbol, period_end);
