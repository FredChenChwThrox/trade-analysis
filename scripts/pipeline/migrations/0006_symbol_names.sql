-- 0006: 股票名称目录（展示层用）。
-- 背景：event_symbols 可能关联 watchlist 池外股票（打标通道写入的涉事公司，
-- 如电报事件的广发证券/国科微/莲花控股），watchlist 无其名称，人审页筛选
-- 下拉只显示代码。本表为 symbol→name 权威缓存：由
-- scripts/collect/symbol_names_collect.py 经东财 push2delay clist（f12 代码/
-- f14 名称，与 industry_collect 同源同域）全市场回填，独立手触发。
-- UI 名称查询口径：watchlist ∪ symbol_names（watchlist 优先）。
CREATE TABLE symbol_names (
    symbol      TEXT PRIMARY KEY,   -- 600000.SH 口径，与 watchlist.symbol 一致
    name        TEXT NOT NULL,      -- 来源官方简称，原样落库不加工
    source      TEXT NOT NULL,      -- 采集源标识（eastmoney_em）
    ingested_at TEXT NOT NULL
);
