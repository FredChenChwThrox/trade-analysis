-- 0008: daily_bars 换手率列（§5.7 筹码集中度缺口的直接指标）。
-- 背景：sina 日线自带 turnover（小数，0.010478=1.0478%）、东财日线「换手率」为
-- 百分点（1.0478=1.0478%），采集器统一归一为小数后落盘，库内口径单一。
-- 列可空：存量行由回填补齐；港股/无源行保持 NULL（§2.5 不猜）。
-- 更新语义：turnover 属派生快照元数据（volume/流通股本的函数），差异更新不记
-- data_revisions（open/close 等价格事实字段的 revision 语义不变，
-- 见 stock_finance_data.upsert_daily_bars）。
ALTER TABLE daily_bars ADD COLUMN turnover REAL;
