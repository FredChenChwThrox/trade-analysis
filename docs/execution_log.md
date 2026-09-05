# 执行日志

> 记录实现过程的执行情况：完成项、偏差、决定。格式：日期 + 条目。

## 2026-08-09

- 设计定稿：`docs/system_design.md`（实现基线 v2，含四轮 review 修补：复权方案、交易日历、点时语义、公司行为处置 5.4b、硬门槛/软约束 9.5 等）。
- 排期卡 skill 已入库：`skills/fred-valuation-card-skill/`（数据源适配 iFinD→kimi-datasource 待 D3.3 做）。
- 创建 `docs/`：移入设计文档；新增 `docs/implementation_plan.md`（四阶段任务拆解）与本日志。
- 环境决定：uv 管理 Python 环境（uv 0.11.21 已装）；包下载失败走系统代理。
- 下一步：D0 骨架 → D1.1 插件实测（珀莱雅 603605.SH）。
- D0 完成：uv 项目（pandas/pyyaml/jsonschema/pytest，直装无需代理）、目录结构、`config/indicators.yaml`、`config/signals.yaml`、`config/watchlist.yaml`、A 股 2026 日历种子（上交所公告 45 号官方来源）、港股日历种子（标记 incomplete_todo）、`skills/stock-collect/SKILL.md`。
- D1.1 完成：插件实测珀莱雅 3 年日线（726 行）。关键发现：**amount 缺失**（允许空）；复权因子需 forward÷none 反推，存在 2 位小数舍入噪声，**必须平台段检测**（阈值 0.1%）；3 年共 5 次小额分红除权。详见 `docs/probe_20260809_stock_finance_data.md`。
- D1.2 + D0.4 完成：`scripts/pipeline/migrations/0001_init.sql`（§7 全量 25 张业务表 + `schema_migrations`，含生命周期分类注释）、`scripts/pipeline/db.py`（连接助手开外键/WAL；CLI `migrate`/`seed`；migration 单事务执行、已应用跳过）。种子入库：watchlist 珀莱雅 603605.SH；CN 2026 日历展开 365 天（春节/周末/交易日验证通过）；HK 种子 `incomplete_todo` 按约定跳过并打印提示。`uv run pytest tests/test_db.py -v` 7 项全过。
- 偏差说明：① 关键决策值（卡片价区 JSON 内部、执行价/数量/费用、汇率、财务金额/股数）按 §9.5 存 TEXT 定点字符串；行情 OHLCV、指标值等中间值用 REAL。② 新增辅助表 `weekly_anchors`（§5.2 锚点需要持久化 anchor_id 供 signal_facts 引用与 fallback 变更追溯，设计未列该表）。③ `strategy_card_versions` 用部分唯一索引（`WHERE status='active'`）实现"同时刻最多一个 active"。④ `report_runs` revision 按 §9.5 软约束降级为"新行 + 新文件"，未做结构化 diff。
- 下一步：D1.3 adapters（`adapters/stock_finance_data.py` 行情/财报/公告/预期、`adapters/yahoo_finance.py` 港股/FX/stock_actions、指数行情；重复 content hash 不重复解析）。
- D1.3 + D1.4 完成：
  - `scripts/adapters/common.py`：`ingest_file`（raw_objects 登记 + content hash 去重 + 登记/解析/写入同事务，校验失败整批回滚）、`IngestResult`（插入/更新/跳过/冲突 + incomplete 原因）、`record_revision`（data_revisions 前后值，§9.5 降级）、市场/时区/定点字符串工具。
  - `scripts/adapters/stock_finance_data.py`：price→daily_bars（OHLC 校验 `low<=open/close<=high`、非负、交易日必须在 trading_calendar；失败整批拒绝）、financials→financial_reports/facts（更正新增 revision 不覆盖）、announcement→events/event_symbols、forecast→forecasts（每次抓取一批快照）、index→index_bars。
  - `scripts/adapters/yahoo_finance.py`：price→daily_bars（UTC 时间戳转市场本地交易日）、fx→fx_rates（方向统一为财务币种→交易币种，反向对取倒数）、stock_actions→corporate_actions、index→index_bars（000300.SS→000300.SH 别名归一）。
  - `scripts/pipeline/calendar_check.py`：`check_symbol_day`（trading_with_bars/suspended/source_missing/non_trading_day/incomplete 五态，停牌 vs 缺数用基准指数有无 bar 区分）、`cross_check_index_calendar`（指数有 bar 日历休市或反之 → 冲突），CLI `day` / `cross-check`。
  - `scripts/pipeline/ingest.py`：CLI 按 `data/raw/{source}/{data_type}/...` 路由；`*_forward*.csv` 跳过留给 D1.5；冲突/错误退出码 1。
  - 真实入库（`data/market.db`）：603605.SH 日线 726 行（2023-08-09~2026-08-07）、0700.HK 24 行（2026-07-07~08-07）、fx_rates 24 行（CNY→HKD）、corporate_actions 20 行（19 次分红 + 2014-05-15 1拆5）、index_bars 9 行、financial_reports/facts 各 2 行（FY2025 年报 + 2026Q1）、forecasts 1 快照。重跑同文件全部按 content hash 跳过（幂等验证通过）。门禁 CLI：08-07 trading_with_bars、08-08 non_trading_day、0700.HK incomplete（HK 日历缺失）；CN 指数交叉校验 07-07~07-17 ok（11 天）。
- run_probe02 样本字段与预期差异（adapter 已按可得字段设计）：
  1. `get_stock_announcement` 持续 EMPTY_DATA（603605.SH 近 3 个月/年内、600519.SH 对照均空）→ 公告 adapter 列名按接口文档推断，未经真实样本验证，events 表暂无真实数据；错误记录于 `data/raw/stock_finance_data/announcement/2026-08-09/run_probe02/_meta.json`。
  2. `get_price` 对指数（000300.SH/399300.SZ/000001.SH）持续 EMPTY_DATA → 指数样本改用 yahoo_finance `000300.SS` 兜底，adapter 做代码别名归一；stock_finance_data 指数 adapter 已实现但未经真实样本验证。
  3. 利润表 CSV 无披露时间列 → `available_at` 降级取入库时间并记 incomplete（§2.1 降级，后续可用公告披露日修正）；无股本列 → `shares_issued_end/shares_float_end` 为 NULL（PE 所需股本待 D1.5 用 get_stock_info/get_stock_actions 补齐）。
  4. 利润表 CSV `time` 列为空 → period_end 从文件名 `_is_YYYYMMDD` 推断。
  5. yahoo 指数样本末行（2026-08-06T16:00Z）OHLC 全空、volume 非空（来源残缺 bar）→ adapter 对 OHLC 全缺失行行级跳过并记 note，部分缺失仍整批拒绝。
  6. yahoo Date 列为 UTC 时间戳：港股/指数 T16:00Z、FX T23:00Z，均按市场本地时区换算取日期（本地交易日 = UTC 日 +1）。
  7. 一致预期 CSV 第 2 行为估值比率（PE/PB/PS 等）而非预测值 → forecasts 快照原样全量保存，不下钻字段。
- 决定：① 补种 CN 2023–2025 交易日历（上交所公告 51号/47号/38号官方来源，`config/calendar_cn_{2023,2024,2025}.yaml`），否则 3 年行情无法过日历校验；HK 日历仍 incomplete_todo，0700.HK 入库按 §2.5 记 incomplete 但不拒绝（分析输出侧由门禁拦截）。② OHLC 全缺失行（实时末根残缺 bar）行级跳过不算冲突；部分缺失/违反 OHLC 关系/非交易日有 bar 一律整批拒绝。③ 规范化事实内容变化 → 更新 + data_revisions 前后值（§9.5 降级）。④ `tests/test_db.py::test_calendar_seed_idempotent` 的 CN 行数断言更新为 2023–2026 四年 1461 天。
- `uv run pytest -v` 34 项全绿（test_db 7 + test_adapters 16 + test_calendar_check 11）。
- 下一步：D1.5 复权因子（`pipeline/adjust.py`：probe01 已备 adjust=none 3 年 + forward 3 年/重叠窗口样本；forward÷none 平台段检测，阈值 0.1%，origin 归一；重叠窗口发现因子变化全量重建）。
- D1.5 + D1.6 完成：
  - `scripts/pipeline/adjust.py`：来源因子 f_t = forward ÷ none（不复权侧取库内 `daily_bars.close_raw`）；**平台段检测**以段内中位数为参考、0.1% 相对容差开新段，段因子取中位数（免疫 ±0.0001 舍入噪声）；归一化 `price_adj_factor_t = f_t / f_origin`（origin=库内最早交易日，归一 1.0）；`share_factor_t = Π(1/ratio)`（当时股本÷最新股本，历史量 ÷factor 放大到当前股本口径），无送转全 1.0，有送转输出 TODO 人工核对；平台切换日与 `corporate_actions` 交叉印证（偏差>2 交易日记警告不阻断）；因子版本（算法/origin/来源/平台段明细/forward 文件 hash）写 `adjustment_factor_versions`；因子列更新 + 版本 + 周线重建同一事务（§2.2 第 3 类），OHLC 不动。CLI `--check-only` 做重叠窗口因子变化检测：r_t = f_new/internal 恒等于 f_origin 为不变，整体位移>0.1% 或窗口内出现两个平台 → 判定变化（退出码 3）触发全量重建。
  - `scripts/pipeline/weekly.py`：逐日复权（每日自己的因子）后聚合，开=周首日/收=周末日/高低=逐日极值/量=Σ raw÷share_factor/额=Σ raw 不调整；只写完成周（ISO 周最后一个开市日由 trading_calendar 判定、已过且有 bar），进行时周与周末日无 bar 的周跳过记 note；整 symbol 删除后重算。
  - 真实跑通 603605.SH（forward_3y probe01）：检出 6 段平台，切换日 **精确命中** probe 实测 5 个除权日（2023-10-23、2024-06-25、2025-06-17、2025-10-17、2026-07-22，零偏差）；因子写库 726 行（6 个取值 1.0~1.0586，origin=1.0）；`weekly_bars` 154 行（2023-08-11~2026-08-07，最后一周为最近完成周）；2026-07-22 周中除权周复权序列连续（日线 59.62→59.94→61.92→61.69→59.55，周高 61.93 无扭曲）；`--check-only` 对 28 日重叠窗口判定因子一致（位移 0.0000%）。
  - 偏差/决定：① corporate_actions 无 603605.SH 记录（yahoo stock_actions 只采了 0700.HK），5 个平台切换日未交叉印证，记 note 不阻断——后续可用 get_stock_actions 补采 A 股分红事件回填；② 0700.HK 周线因 HK 日历 incomplete_todo 未生成（CLI 退出码 2，§2.5 不猜）；③ forward CSV 不入 raw_objects（ingest CLI 仍跳过），文件路径+sha256 记入因子版本 notes 做溯源；④ share_factor 方向定为"当时股本÷最新股本"（历史值<1），与 price_adj_factor 的 origin 锚定方向不同，已在模块 docstring 说明。
- `uv run pytest -v` 50 项全绿（test_db 7 + test_adapters 16 + test_calendar_check 11 + test_adjust 10 + test_weekly 6）。
- 下一步：D1.7 指标计算（`scripts/indicators/`：MA/MACD/RSI/BOLL/KDJ/量额，复权口径，config/indicators.yaml 参数化，golden tests）。
- D1.7 完成：
  - `scripts/indicators/core.py`：MA（5/10/20/60/120/250，完整窗口不足 NaN）、MACD（EMA adjust=False，柱=2×(DIF−DEA)）、RSI（Wilder RMA：首值=前 window 个 delta 简单平均，其后 (avg×(n−1)+x)/n 递推；全涨=100/全跌=0/走平=50）、BOLL+带宽（ddof=0）、KDJ（初始 K/D=50，零振幅 HHV=LLV 沿用前值）、量能（vol_ma5/10、vol_mean/std 20/60，std 统一 ddof=0；成交额同参数，603605.SH 来源无 amount → amt_* 全 NULL）、pct_chg/amplitude（百分比存储，复权 OHLC）、`historical_mean`（shift(1) 排除当前 bar，供信号层用）。rule_version=`indicators_v1`。
  - `scripts/indicators/valuation.py`：TTM 归母净利按 §3.7（年报直取 / 中报=上年年报+本年累计−去年同期累计，组成项缺失→空；点时过滤 available_at<=as_of + 最新 revision）；`pe_ttm = close_raw × 当日已生效股本 ÷ TTM`（不复权市值口径，effective_at<=as_of 最新股本事件）；原因码 pe_status（no_share_capital / no_visible_report / ttm_missing_prev_annual / ttm_missing_prev_same_period / ttm_missing_net_profit / ttm_non_positive / fx_missing）；`load_share_snapshot` 解析 yahoo get_stock_info CSV 写 share_capital_events（raw_objects 登记 + content hash 幂等 + 冲突拒绝）。
  - `scripts/indicators/compute.py`：CLI `uv run python -m scripts.indicators.compute <symbol>`；日线（复权 OHLC+调整量）与周线（weekly_bars 同参数，周期单位周）全量重算，DELETE+重插+run 记录同事务；config_hash=indicators.yaml 内容 sha256；pipeline_runs 写 config_hash/rule_version/pandas 版本。
  - 股本来源尝试：yahoo_finance `get_stock_info`（ticker 603605.SS，经插件 MCP stdio 直调）拿到 **sharesOutstanding=395,976,049**（总股本 issued 口径；floatShares=195,352,099 仅入 details），样本落盘 `data/raw/yahoo_finance/stock_info/2026-08-09/run_probe03/`；作为单点事件写入 share_capital_events（event_type=`snapshot_issued`，effective_at=2023-08-09 覆盖整个保留区间，share_count_type=issued，details_json 标注快照假设与待交叉验证）。
  - 真实跑通 603605.SH：indicators_daily 726 行 / indicators_weekly 154 行。抽查 2026-08-07：ma5=62.3105、ma20=61.8230、ma250=71.7380、dif=0.16419、dea=0.20365、macd_hist=−0.07893、boll_mid=61.8230、bw=0.13254、vol_ma5=5,050,260、vol_std20=1,324,243——与 pandas 独立复算逐项一致。**pe_ttm 全部 726 行为空**：最新可见报告为 2026Q1 季报，中报三件套缺 2025Q1 去年同期累计（库内仅 FY2025 年报+2026Q1 两期财报），pe_status=`ttm_missing_prev_same_period;degraded_available_at`——按 §3.7 不补历史空洞；需补采 2025Q1（及更早季度）财报后 TTM 才非空。
  - 偏差/决定：① 财报 available_at 降级为入库时间（D1.3 已知），严格点时将使全序列财报不可见；compute 默认 `assume_visible_reports=True` 照常使用全部报告并在 pe_status 逐行追加 `;degraded_available_at` 标注（严格点时逻辑仍在 valuation.visible_reports 中，供数据补齐后切换）；② 股本快照 available_at 同为入库时间，逐日股本只按 effective_at 取（同类降级，details 已标注）；③ 成交量/成交额标准差统一 ddof=0（设计仅显式规定 BOLL，已在 docstring 说明并锁于 golden tests）；④ pct_chg/amplitude 以百分比存储（1.23=1.23%）；⑤ RSI 走平（增益/损失皆 0）定义为 50；⑥ share_capital_events 新增 event_type=`snapshot_issued`（schema 注释枚举之外的快照型事件，已记录）；⑦ pe_ttm 写入 REAL（展示/中间值，§9.5 软约束允许）。
  - `tests/test_indicators.py` 19 项 golden tests：SMA/EMA 递推、MACD 柱、Wilder RSI 种子+递推+三边界、BOLL ddof=0、KDJ 初始 50+零振幅沿用、窗口不足 NaN、shift(1)、TTM 年报直取/三件套/缺项/点时可见性/最新修订、PE 四种原因码+正常值、集成小样本全量重算（含 pe NULL 路径与幂等重跑）。
- `uv run pytest -v` 69 项全绿（test_db 7 + test_adapters 16 + test_calendar_check 11 + test_adjust 10 + test_weekly 6 + test_indicators 19）。
- 下一步：补采 2025Q1（及 2024 各季度）财报使 TTM/PE 可用；随后 D1.8 信号层（周线锚点、衰竭信号、signal_facts）。
- D1.8 完成（D1 阶段收官）：
  - **财报补采回填**（run_probe04）：kimi-datasource `get_financial_statements` 补采 603605.SH 七个报告期利润表（2024Q1/2024中报/2024Q3/FY2024/2025Q1/2025中报/2025Q3）全部成功（各 1 行），落盘 `data/raw/stock_finance_data/financials/2026-08-09/run_probe04/`（含 _meta.json），ingest 入库 7 行（available_at 仍按入库时间降级标注）。库内财报现 9 期（2024Q1~2026Q1 连续）。重算指标后 **pe_ttm 全部 726 行非空**（三件套齐备：最新 2026Q1 季报 TTM = FY2025 归母 14.98 亿 + 2026Q1 3.67 亿 − 2025Q1 3.90 亿 = 14.74 亿；2026-08-07 PE=15.5043 与独立复算一致）；pe_status 仍带 `;degraded_available_at` 标注（披露日缺失降级未消除）。
  - `scripts/adapters/common.py` 重构：抽出 `register_and_parse`（登记+解析，不做事务管理，供 pipeline 在更大事务内调用），`ingest_file` 改为其单文件事务包装（行为不变，test_adapters 全绿）。
  - `scripts/pipeline/daily.py`（§8.1 步骤 1-5）：CLI `--date D [--raw-dir PATH]` + `status <symbol> [--date D]`。run_id 固定 `daily_{date}`，pipeline_runs 按 (run_id, stage) 覆盖（calendar / symbol:{symbol} / summary 三阶段，§2.3 版本字段 adapter_version/config_hash/rule_version/app_version 全填，git_commit 尽力而为——本项目非 git 仓库为 NULL）。raw-dir 文件按 watchlist 归组：**个股文件随该股事务入库**，forward 文件走因子变化检查（变化→apply_adjustment 全量重建含周线；一致→note），指数/fx 等其他文件单文件事务先行入库；单股"入库→门禁→因子→周线→指标"一个事务，BatchRejected/异常回滚标记 failed 不影响他股。门禁 non_trading_day/incomplete/source_missing 跳过计算不产出结果（§2.5）。
  - `tests/test_daily.py` 6 项：完整 pipeline 跑通、非交易日跳过（CLI 退出码 0）、幂等重跑全表快照一致（raw_objects 不膨胀、run_id 覆盖）、单股入库冲突回滚隔离（他股不受影响、summary=failed）、HK 日历缺失 incomplete、status 命令输出。
  - 真实跑通：`--date 2026-08-07` → 603605.SH ok，指标重算 daily=726/weekly=154，pe_ttm 非空 726；重跑 exit=0 幂等；`--date 2026-08-08` → non_trading_day 跳过 exit=0；`status 603605.SH` 最近 5 日 gate 全 trading_with_bars、pe_ttm 15.50~16.33、daily_run=success（08-07）。
  - 偏差/决定：① 每日 run 不含新数据时也对 watchlist 全量重算周线/指标（3 年数据量小，§4.3 允许；换取重跑语义简单确定）；② raw-dir 中非 watchlist 文件（指数/fx）在个股事务外单文件入库（它们不是单股原子单元的一部分）；③ 因子变化触发重建时 `adjustment_factor_versions` 追加新行（版本历史 append-only，重跑会重复追加——仅在实际变化时发生）；④ pe_status 的 `;degraded_available_at` 标注在披露日来源补齐前持续存在（D1.3 已知降级）。
- `uv run pytest -v` 75 项全绿（69 + test_daily 6）。
- 下一步：D2.1 信号层（周线锚点 `signals/anchors.py`、衰竭信号 `signals/exhaustion.py`，阈值边界单测）。
- D2.1 完成：
  - `scripts/signals/common.py`：RULE_VERSION=`signals_v1`、五项信号名常量、`load_params`（signals.yaml defaults + 内容 sha256 作 config_hash）、`WeekBar` 数据结构。
  - `scripts/signals/anchors.py`：逐 as_of（每个完成周）独立识别锚点，只用该周及之前数据。恐慌低点 = 最近一次有效恐慌型信号（与 exhaustion.panic_condition 同一判定）所在周内最低复权价交易日（平值取最早交易日）；无恐慌信号 → fallback = 过去 26 完成周（含当前周）最低复权收盘价所在周（平值取最近周，设计未规定，锁定）。下跌起点 = 恐慌低点周向前 26 完成周内最高复权收盘价所在周（平值取离恐慌低点最近周），锚点日 = 该周周末日、价格 = 周复权收盘。锚点身份 =(anchor_type, trade_date, is_fallback)，变化时追加新 anchor_id 不覆盖旧行；同身份在周序列上必然连续，id 回填到后续 as_of。
  - `scripts/signals/exhaustion.py`：五项信号（纯函数 + 逐周编排）。恐慌型（量≥前 20 周均量×2 + 长下影/大阳线，均量 shift(1) 不含当前周）、干涸型（≤下跌起点后前 4 个完成周均量 50%，基数不含当前周、不足 4 周不判定）、三周不创新低（确认周触发，再次跌破失效，确认前跌破该锚点永不触发）、周线底背离（左右各 2 周严格 pivot low，只在 pivot+2 确认周判定与记录、不回填，26 周窗内最近两已确认 pivot 比较收盘 + RSI12 或 MACD 柱）、持续时间（距下跌起点 ≥8 完成周，triggered 锁在 elapsed==8 那一周）。活跃期：恐慌/底背离确认周起 4 完成周（span=触发周+3，同锚点内）；干涸型条件满足才活跃；nnl/持续时间按 §5.3。episode 结束：同一恐慌锚点连续段内首个收盘 > 下跌起点收盘的周起，该锚点全部信号 inactive（reason=episode_ended）。`count_active_signals` 实现"同一 anchor_id 下当前完成周活跃信号数"（供档位触发）。
  - `scripts/signals/weekly_signals.py`：CLI `uv run python -m scripts.signals.weekly_signals <symbol>`。读 weekly_bars + daily_bars（周内定位/ raw 价）+ indicators_weekly（RSI12/MACD 柱）；weekly_anchors 与 signal_facts（五项）DELETE+重插同事务；pipeline_runs 阶段 `weekly_signals` 记 config_hash/rule_version；active_until 已知完成周取实际周末日、未来周日历 ISO 周外推（仅展示用）。输出当前周锚点明细（§5.2 ⚠️ 供人工核对）+ 五项信号状态 + 历史触发统计 + 锚点变更段。
  - `tests/test_signals_weekly.py` 16 项：六个阈值边界三态（放量 2 倍/下影实体比/下影振幅比/大阳线实体 60%/涨幅 5%/缩量 50%）、干涸基数不足不判定、nnl 四状态、持续 8 周边界、锚点 fallback/下跌起点/平值规则、底背离只在确认周触发不回填（构造双 pivot 序列锁定）、episode 结束 + 恐慌 4 周活跃到期、活跃计数、截断重算前 N 周与全量一致（无未来函数）、幂等重跑。
  - 真实跑通 603605.SH：154 完成周 → weekly_anchors 21 行（10 段锚点变更）、signal_facts 770 行。历史触发：panic 3 次（2024-09-27、2025-04-30、2026-06-05 周，均为大阳线形态）、dry_up 8 周、divergence 5 次、no_new_low_3w 3 次、duration 1 次。当前周 2026-08-07：恐慌低点锚点 2026-06-01（复权 63.5057 / 不复权 61.25，非 fallback），下跌起点 2026-02-06（复权收盘 78.2286 / 不复权 75.45）；活跃信号仅 duration 1 项（elapsed=25 周，< min_active 2）；dry_up 不满足（当前量 2525 万 > 基数均量 1615 万 ×50%≈808 万）；episode 未结束（周收盘 61.10 < 78.23）。2026-02-20 周因春节休市不在完成周序列，干涸基数正确取之后 4 个完成周。
  - 偏差/决定：① fallback 锚点平值规则设计未规定 → 锁定取离当前最近周；② 周内最低复权价交易日平值取最早交易日；③ 底背离 pivot 采用严格小于（窗口内平值不构成 pivot）；④ duration 的 triggered 只在 elapsed==8 周置 1（锚点形成时 elapsed 已 >8 的 episode 直接 active 不记触发）；⑤ dry_up 的 triggered 在条件满足的每个周置 1（统计口径=触发周数）；⑥ active_until 对 nnl/duration 这类开放活跃信号存 NULL（失效时间由后续周状态行体现）；⑦ 信号重算读库存 indicators_weekly（全量序列口径），无未来函数由"每周只用 ≤ 该周数据"的编排保证并由截断测试锁定；⑧ signals CLI 独立运行，未挂入 daily pipeline（待 D2 后续阶段随日报接入）。
- `uv run pytest -v` 91 项全绿（75 + test_signals_weekly 16）。
- 下一步：D2.2 日频监测（daily_watch）、D2.3 右侧确认状态机（right_side）、D2.4 公司行为处置（corporate_action）。
- D2.2 + D2.3 + D2.4 完成：
  - `scripts/signals/cards.py`：排期卡读取与机械换算公共层。锁定卡片生效区间语义 `[effective_from, effective_to)`（排他端点，同日至多一卡生效，§5.1）；卡片 JSON 字段第一版 schema（price_tiers/invalidation/swing_box/right_side_trigger/earnings/valuation，价格类十进制字符串）；`convert_card_fields` 机械换算（multiply=×1/倍率 价格类+EPS，subtract=−每股分红 只动价格类，PE 刻度不动，结果量化 4 位小数 ROUND_HALF_UP）；`handled_ca_ids` 扫描 input_snapshot_json.conversion。
  - `scripts/signals/daily_watch.py`（§5.4）：CLI `uv run python -m scripts.signals.daily_watch <symbol>`。逐交易日（无未来函数）对当日生效卡计算——档位临近（距最近边界 ≤3%，分母=边界价，恰好 3% 算临近；价区内不计临近）、档位触发（收盘进区；第二三档需同 anchor_id 当前完成周活跃衰竭信号 ≥2，完成周取 ≤当日最近 week_end_date，weekly_bars 缺失回退 signal_facts；不足记 pending_signals 不触发）、证伪线有效跌破（收盘 ≤ 线×0.99 恰好 1% 算跌破，连续 2 日确认，holding/recovered/watching 状态，跨版本重置连续计数）、波段箱体位置（box_breached/above_box/sell_zone/buy_zone/below_box/mid_box 六分类，只监测存档边界）、均线口径纪律（ma_comparison：复权 MA20/60 ÷ 当日 price_adj_factor 折回后与不复权现价比，details 同录复权原值/因子/折回值）。无 active 卡片写 daily_watch 行 state=incomplete（reason=no_active_card）不产卡片信号（§2.5）；公司行为冻结期（unresolved_suspensions 非空，除权日起）只写 daily_watch state=suspended 挂起触发（§5.4b 第一步）。DELETE+全量重插，只对各版本实际生效区间计算。
  - `scripts/signals/right_side.py`（§5.4）：CLI `uv run python -m scripts.signals.right_side <symbol>`。状态机 idle→waiting_retest（收盘 ≥ 关键位×1.01 且当日调整量 ≥ 前 20 日调整均量 ×2，shift(1) 不含当日，样本不足 20 日不判定保持原状态）→confirmed（10 交易日内 low ≤ 关键位×1.02 且收盘 ≥ 关键位×0.99）/invalidated（收盘 ≤ 关键位×0.99，≤ 锁定）/expired（10 日无合格回踩）；等待期判定顺序 invalidated>confirmed>expired，terminal 次日回 idle 可开新 episode。量能为调整量（raw÷share_factor，与周线口径一致）。每次转换写 signal_facts（起始/截止日、关键位、全部容差、成交量明细）。逐版本生效区间跑、版本切换重置 idle；冻结日不参与判定不推进窗口。无卡/无触发位保持 idle 记 incomplete。
  - `scripts/signals/corporate_action.py`（§5.4b）：CLI `uv run python -m scripts.signals.corporate_action <symbol>`。检测 pending（除权日 ≥ active 卡 effective_from、未被任何版本 conversion 记录、无未撤销冻结）。快速通道：现金分红且 D/除权日前一日 close_raw < 2% → 减法换算新版本**自动激活**（先关旧版 superseded+effective_to=ex_date 再插新版——uq_card_active 部分唯一索引要求），card_conversion 事实行记 auto_activated；除权日前无行情算不出影响比例不猜、降级三段式。三段式：freeze_card 写 suspended_corporate_action（幂等 INSERT OR IGNORE）+ generate_conversion_draft 机械换算 draft（不激活、effective_from 留空、supersedes_id 指旧版、conversion 明细入 input_snapshot_json）；确认激活留给 D2.5 CLI；rescind_suspension 写 rescinded 行撤销冻结。executions 一律不触碰（不回溯，§5.4b）。
  - `tests/test_signals_daily.py` 15 项 + `tests/test_corporate_action.py` 8 项：证伪线恰好 1%/连续 2 日/只 1 日/未跌破/holding/recovered、档位临近 3% 三态、第一档无信号要求+第二档信号不足 pending 补足触发（集成 count_active_signals）、口径纪律（因子 2.0 构造除权数据，折回值比较与跨尺度直接比结论相反并显式锁定）、箱体六分类、状态机 confirmed/invalidated/expired 三路径+突破量价恰好边界（+1%、2 倍量）+样本不足不判定+集成跑通、无卡 incomplete 两模块、10 送 10 冻结+draft 换算值（价区 ×0.5/EPS ÷2/PE 不变）、小额分红快速通道自动激活+supersedes 链、大额分红降级三段式、无前行降级、executions 不回溯、rescind 恢复监测、pending 检测口径、幂等重跑。
  - 真实跑通（演示卡，已删除）：手工插入 603605.SH 演示 active 卡（card_version_id=`demo_603605_d22`，effective_from=2026-06-01，参考现价 57.72/PE 15.5 设定：T1 [55,58] T2 [48,52] T3 [42,46]、证伪线 47、箱体 48~65 买区 52~56 卖区 62~65 箱证伪 46、右侧触发位 60 止损 56、EPS 3.2/3.7/4.2、PE 12/15/18，input_snapshot 标 demo:true）。daily_watch：监测 49 个交易日写 signal_facts 245 行；2026-08-07 收盘 57.72 → **第一档触发**（区内，无需信号；完成周活跃信号仅 duration 1 项 anchor_id=20）、档位临近 inactive（T2 距 11%）、证伪线 no_breach、箱体 mid_box、**现价低于折回 MA20**（复权 61.823÷因子 1.058587=58.40 > 57.72）。right_side：2026-06-01 收盘 67.13 ≥ 60.60 且量 1874 万 = 前 20 日均量 3.81 倍 → waiting_retest；随后 10 交易日 low 未回踩 ≤61.20 → 2026-06-15 expired；当前 idle。corporate_action：603605.SH 库内无公司行为事件 → 0 件待处理（逻辑由单测覆盖）。演示后删除演示卡与 247 行演示信号事实；重跑两模块均输出 incomplete（no_active_card，exit=2，§2.5 验证通过），库内现仅存 daily_watch/right_side 各 1 行 no_active_card 状态行。
  - 偏差/决定：① 证伪线/档位临近/右侧跌破边界语义锁定为 ≤/≥  inclusive（恰好 1%、3%、2 倍量均算），设计措辞"1% 以上"取含等于，已锁于边界测试；② 右侧状态机 terminal 次日回 idle 允许新 episode（设计未明示，锁定）；③ 右侧量能用调整量（raw÷share_factor）与周线口径一致（设计未指定，锁定）；④ 卡片生效区间锁 [from, to)，fastlane 旧版 effective_to=ex_date；⑤ 等待期判定顺序 invalidated>confirmed>expired（同日撞线时取保守）；⑥ 换算结果量化 4 位小数（1/3 类倍率非有限小数时截断，精确因子入 conversion.factor）；⑦ 现金分红换算不动 EPS/PE（§5.4b 只规定价格类字段减法）；⑧ tier_triggered 的 details 只记录现价所在档（未入区的档不展开）；⑨ 信号 CLI 仍独立运行未挂入 daily pipeline（同 D2.1 决定，待报告层接入）。
- `uv run pytest -v` 114 项全绿（91 + test_signals_daily 15 + test_corporate_action 8）。
- 下一步：D2.5 卡片管理 CLI（draft 确认激活/拒绝、冻结确认后恢复监测）；随后 D3 报告层。
- D2.5 + D2.6 完成（信号模块挂入每日 pipeline，execution_log 偏差⑨消除）：
  - `scripts/pipeline/card.py`（§5.6、§2.4、§5.4b 第三步）：CLI `create-draft <symbol> --json PATH`（jsonschema 结构校验 + 语义校验：区间方向/重复档位/定点十进制字符串模式，失败拒绝入库）、`activate <card_version_id> [--effective-from D]`（同一事务关闭旧 active：status→superseded、effective_to=新版 effective_from 排他端点；effective_from 缺省取换算 draft 的 conversion.ex_date，否则当日；禁止早于当前 active 生效日的历史回填激活；换算 draft 确认后 ca_id 被 active 版本 conversion 吸收，冻结自动视为已解）、`reject`（draft→rejected；对 active 为人工废止：关 effective_to=当日，历史 JSON 字段不动，并刷新 current.md 视图——无 active 时删除）、`list`/`show`。激活时渲染 `cards/{symbol}/{effective_from}_{card_version_id}.md` + 刷新 `current.md`（由库记录渲染，含 demo/conversion 标注与"不手工回写数据库"注记，§2.4）。
  - `scripts/pipeline/execution.py`（§5.7、§8.3）：CLI `add`（必须关联当前 active 卡，无卡拒绝 exit 2；价格/数量/费用定点十进制字符串保留输入精度；idempotency_key 未提供时按 symbol/action/price/qty/tier/fees/executed 日期派生确定性 key，重复 key 拒绝；signal_snapshot_json 冻结当时周报五项+锚点+日频六项+右侧最新 signal_facts 与 weekly_anchors 快照，删表验证不随重算变化）、`reverse`（新增 action_type='reversal' 冲正行，reverses_execution_id 指向原记录，原记录不动，同一记录只能冲正一次）、`list`（冲正链展示）。
  - `scripts/pipeline/report.py`（§6.1-6.4、§9.5）：CLI `uv run python -m scripts.pipeline.report --date D [--symbol S]`。单股报告七段（运行状态/当前定位/决策点/观察点/衰竭信号/指标快照/来源与异常）；衰竭信号段带 anchor_id + 恐慌低点/下跌起点锚点明细（§5.2 ⚠️）；观察点全部带临近度（档位距边界 %、证伪线距离、干涸阈值差值、右侧突破线距离，均取 signal_facts details_json）；无触发明写"今日无决策点"；关键数字逐条标注来源与截止日期；语言纪律扫描词表 BANNED_WORDS（看涨/看跌/建议买入/建议卖出/建议持有/预测涨跌）。全池日报五级确定性排序（数据异常 > 证伪/复核逾期/换算 draft 待确认 > 已确认决策点 > 3% 内观察点 > 普通更新；同级按距边界百分比→symbol，逐条显示排序原因）。report_runs 每份一行（revision/card_version_id/rule_version=report_v1/config_hash=signals.yaml 哈希/input_snapshot_json 含 gate/facts 状态/指标 config_hash）；同日重跑生成 revision 新行 + 同名文件覆盖 + 报告头与日志记 revision 序号（§9.5 降级）。
  - `scripts/pipeline/daily.py` 接入（§8.1 步骤 5、7）：指标重算后同事务依次 weekly_signals → daily_watch → right_side → corporate_action（单模块异常记 notes degraded 不拖垮该股其余阶段；入库/门禁/指标失败仍整体回滚）；全部股票完成后报告阶段 run_reports，异常捕获记 pipeline_runs 阶段 report=degraded 不阻断前面阶段；`run_daily` 新增 `reports_root` 参数（CLI `--reports-root`，默认 reports/），`test_daily.py` 四个调用点随之传临时目录。
  - `tests/test_card.py` 12 项 + `tests/test_execution.py` 8 项 + `tests/test_report.py` 8 项：draft 校验三类拒绝、激活唯一 active/排他端点/非 draft 拒绝/历史回填拒绝、换算 draft 确认激活解冻结、reject 不改历史/废止 active 关区间删视图、Markdown 关键字段齐全、执行快照冻结、幂等键去重（显式+派生）、冲正链与重复冲正拒绝、CLI 退出码、七段结构/无决策点/锚点明细/临近度、五级排序（异常股在触发股前）、语言纪律扫描、report_runs revision、pipeline 集成全链路幂等（信号事实剔除 anchor_id 自增键后逐行一致，report_runs revision 增长为设计行为）。
  - 真实跑通：演示卡 `603605SH_a6a4de99` 经新 CLI create-draft + activate（effective_from=2026-06-01，沿用 D2.2 演示参数，input_snapshot demo:true）→ `daily --date 2026-08-07` 全链路 ok（指标 726/154 行，四信号阶段 ok，报告 complete P3）→ 单股报告（第一档触发决策点、距 T2 上沿 11.0%、干涸阈值差 1717.5 万、右侧距突破线 4.8%、现价低于折回 MA20 58.4014、PE 15.5043 带 degraded_available_at 标注）+ 全池日报；报告 CLI 重跑演示 revision 2（report_runs 4 行）；随后演示卡 reject 保留（status=rejected，生效 [2026-06-01, 2026-08-09)，版本 Markdown 存档于 `cards/603605.SH/2026-06-01_603605SH_a6a4de99.md`，current.md 随废止删除），真实报告语言纪律扫描通过。
  - 偏差/决定：① reject active 视为人工废止（关 effective_to，状态机 draft/active→rejected），设计未明示该路径，供演示卡收场与管理纠错用；② 信号阶段顺序按任务约定 weekly_signals → daily_watch → right_side → corporate_action（同日新除权的冻结下一批次才体现在 daily_watch 输出，603605.SH 库内无公司行为事件，无实际影响）；③ 单信号模块异常降级为 notes + 继续（派生数据可重算），未采用整体回滚（§8.1 原子性主指行情/指标/信号发布一致，已记录）；④ report_runs.config_hash 取 signals.yaml 哈希（决策阈值所在），indicators.yaml 哈希入 input_snapshot_json；⑤ 幂等比对剔除 anchor_id（weekly_anchors DELETE+重插自增键重编号，D2.1 既有语义，details_json 内锚点日期/价格等稳定值逐行一致）；⑥ 非交易日门禁的股票跳过单股报告、列入全池日报"非交易日/跳过"段；⑦ 执行 executed_at 缺省取当前 UTC，卡片关联按 executed 日期取当日生效 active 版本。
- `uv run pytest -v` 142 项全绿（114 + test_card 12 + test_execution 8 + test_report 8）。
- 下一步：D3 消息评价与事件研究（LLM 阶段，§9.4 上线顺序第 4 步）、排期卡 skill 数据源适配（D3.3）。
- D3.3 第一步完成（排期卡 skill 适配：底稿导出器 + SKILL.md 改造）：
  - `scripts/pipeline/card_inputs.py`（§5.6、§3.2、§3.4）：CLI `uv run python -m scripts.pipeline.card_inputs <symbol> [--db PATH] [--out-dir PATH]` → 写 `cards/{symbol}/inputs_{最新交易日}.json` 并打印摘要。底稿九段（schema card_inputs_v1）：① meta（标的信息 + 各来源数据截止日期 + 口径注记）② earnings（年报+季报营收/归母净利/EPS 及同比序列、TTM 现值=最新年报+本财年最新累计−上一财年同期累计，§3.7）③ forecasts（最新快照 FY1–FY3 净利/营收/增速 + 裂口对照=券商 FY1 增速−最近季报实际增速）④ valuation_scale（恐慌低点清单：日期/复权价/不复权价/is_fallback/当日 pe_ttm + pe_ttm 分位数 p5/p25/p50/p75/p95 线性插值 + 当前值；sample_window 首末日期强制标注，§3.2）⑤ market_snapshot（现价不复权/当前 PE(TTM)/总股本）⑥ exhaustion_params（当前锚点不复权前低、下跌起点后前 4 周均量基数、×2 放量阈值、0.40/0.50/0.60 缩量阈值，config/signals.yaml 实数）⑦ signal_status（当前完成周五项衰竭信号状态 + 活跃计数 ≥2 口径）⑧ daily_watch（tier/证伪/箱体/均线最近 facts 摘要 + active 卡概要）⑨ config_params（参数回声 + config_hash/rule_version）。纯读取不写库（只读纪律有测试锁定）。
  - `skills/fred-valuation-card-skill/SKILL.md` 适配：第 1 步取数改为先跑 card_inputs 拿底稿（主输入），缺口才回源 kimi-datasource 插件（接口名对照 ifind_get_financial_statements→stock_finance_data_get_financial_statements、ifind_get_price→stock_finance_data_get_price、ifind_get_forecast→stock_finance_data_get_forecast）；行情口径说明（系统存不复权价+复权因子、周线系统聚合、卡片一律不复权口径）；第 3 步新增硬性规则"PE 刻度必须标注 3 年样本区间（§3.2），引用更早历史须声明样本外"；第 5 步指明底稿 exhaustion_params 实数阈值直接采用；第 9 步改双产物（排期卡 Markdown + 严格符合 cards.py schema 的卡片 JSON，供 create-draft 入库）并写明 draft-only 原则（§5.6：skill 只产 draft，activate/reject 必须人工确认）；语气与边界补 draft-only 条目。框架逻辑（估值锚/胜率/衰竭/证伪纪律）一字未改，build_schedule.py 用法保留；references/ 三文件无 iFinD 引用，未改动。
  - `tests/test_card_inputs.py` 12 项：九段结构齐全、分位数线性插值口径（[10,20,30,40,50]→p5=12/p25=20/p50=30/p75=40/p95=48）、percentile 边界、样本区间标注存在且含 §3.2、恐慌低点 pe_ttm 关联=indicators_daily 当日值、TTM=100+25−20=105 与同比序列、裂口 gap_pp=5.0（百分数归一口径）、衰竭阈值=基数 250×(2.0/0.40/0.50/0.60)、活跃计数、导出文件名+只读纪律（导出前后各表行数不变）、未知 symbol 报错、CLI 摘要。
  - 真实跑通 603605.SH → `cards/603605.SH/inputs_2026-08-07.json`：现价 57.72（不复权），PE(TTM) 15.5043，TTM 归母净利 14.74 亿、TTM EPS 3.7228（股本 3.9598 亿）；PE 分位 p5 15.8783 / p25 19.8001 / p50 22.8884 / p75 26.3052 / p95 29.5319（样本 2023-08-09 ~ 2026-08-07，726 个交易日）；恐慌低点 15 个（3 个真恐慌：2024-09-24 PE 22.69 / 2025-04-28 PE 25.03 / 2026-06-01 PE 18.03，其余 fallback 已标注）；当前锚点前低（不复权）61.25，下跌起点 2026-02-06 周，干涸基数均量 1615.29 万（调整量）→ 放量阈值 ≥3230.58 万、缩量阈值 646.12 万~969.17 万（中值 807.64 万）；当前完成周活跃信号仅 duration 1 项（min 2，不满足释放）；一致预期 FY1 16.26 亿（+8.59%）vs 2026Q1 实际 −6.05%，裂口 14.64pp。
  - 偏差/决定：① 底稿九段划分与 card_inputs_v1 schema 为本次锁定（任务只规定内容项）；② FY1 年份锁定=快照年（forecasts 表无年份字段）；③ ths_fore_np_yoy_stock 为百分数口径，归一为分数与盈利同比一致；④ 放量/缩量阈值统一派生自干涸基数（下跌起点后前 4 周均量，与 references/exhaustion-signals.md"下跌初期均量"口径一致），恐慌信号自身 20 周均量基数未单列实数（参数在 config_params 回声）；⑤ 恐慌低点清单含 fallback 锚点并标注 is_fallback，skill 侧自行筛选；⑥ TTM/PE 走 degraded_available_at 口径与 indicators_daily 一致，meta.notes 标注；⑦ 导出日期取最新交易日（非生成日），同日重跑幂等覆盖同名文件。
- `uv run pytest -v` 154 项全绿（142 + test_card_inputs 12）。
- 下一步：D3.3 第二步（用 skill 对 603605.SH 生成正式排期卡 draft + create-draft 入库）；D3 消息评价与事件研究。

## 2026-08-10

- **首张真实排期卡激活**：`603605SH_120ca661`（effective_from=2026-08-10）。三档 61.5–64.1 / 54.7–57.6 / 42.8–46.2，证伪线 42.8；波段箱体 54.7–61（买入区 54–56.5、卖出区 59.5–61、箱体证伪线 54.7，仓位上限 20%）；右侧触发位 61/止损 59.5；胜率区间 35–55%（证据不足按固定比例下限）；next_review=2026-08-31（中报窗口）。`cards/603605.SH/current.md` 已刷新。
- **execution.py 新增 --backfill/--note**：补录系统上线前手工执行时关联当前 active 卡（而非 executed_day 当时卡片），snapshot 只记 backfill 标记。正常路径"executed_day 必须有 active 卡"的校验不变。
- **补录 4 笔波段执行**（executions #1–#4，均标 backfill）：07-03 卖 60.200×1700、07-09 买 57.000×900、07-13 买 55.930×800（分批买入）、07-15 卖 59.500×1800（含 100 股更早底仓）。57.000 高于买入区上沿 56.5 已如实备注。
- 154 测试全绿。下一步：2026-08-10 盘后首个正式 daily（真实卡监测日报）；D3.4 数周并行观察开始。
- **5 只新观察股票初始化完成**（海天味业 603288.SH / 中国平安 601318.SH / 埃斯顿 002747.SZ / 紫金矿业 601899.SH / 南方航空 600029.SH，同步观察无排期卡，卡片相关 incomplete 属 §2.5 预期）：
  - 新增 `scripts/collect/mcp_client.py`（kimi-datasource 插件 MCP stdio 直调客户端，subprocess + 行分隔 JSON-RPC，带超时）与 `scripts/collect/init_collect.py`（批量采集 + 失败重试一次 + _meta.json 错误记录不中断）。
  - watchlist 6 只 seed 入库；采集落盘 `data/raw/{stock_finance_data,yahoo_finance}/{price,financials,forecast,stock_info}/2026-08-10/run_init/`（各带 _meta.json）。**偏差**：财报首次采集 statement=income_stmt 被数据源拒绝（PARAMETER_ERROR，合法值为 income_statement 或别名 is），35 期全失败并记录 _meta；改用 `is`（run_probe04 成功取值）重采 35/35 全 ok。行情 10 文件（none+forward 各 5，725 数据行/只，2023-08-10~2026-08-07）、预期 5、股本 5（sharesOutstanding：海天 55.60 亿/平安 106.60 亿/埃斯顿 8.71 亿/紫金 206.02 亿/南航 134.77 亿，细节含 A/H 口径假设标注同 603605.SH 做法）。
  - ingest 3665 行（daily_bars 5×725 + financial_reports/facts 35 + forecasts 5），forward 文件按约定跳过；35 期财报 available_at 取入库时间降级（D1.3 已知）。
  - adjust 逐股全量重建（origin=2023-08-10，周线同事务重建 154 完成周/只）。除权平台切换日：**603288.SH 5 个**（2024-06-19、2025-06-05、2025-09-24、2026-02-06、2026-06-15）；**601318.SH 6 个**（2023-10-25、2024-07-26、2024-10-18、2025-06-30、2025-10-24、2026-06-10）；**002747.SZ 1 个**（2024-06-07）；**601899.SH 6 个**（2023-12-25、2024-06-11、2024-08-09、2025-06-13、2025-09-30、2026-06-26）；**600029.SH 0 个**（全区间 f=1.0）。corporate_actions 均无记录，切换日未交叉印证（同 603605.SH 既有 note，不阻断）。
  - 股本快照：load_share_snapshot 逐股写 share_capital_events（event_type=snapshot_issued，effective_at=2023-08-10 覆盖保留区间，source=yahoo_finance get_stock_info）。
  - compute 逐股：indicators_daily 725 / indicators_weekly 154，**pe_ttm 五只全部 725 行非空**（TTM 三件套齐备：FY2025 年报 + 2026Q1 − 2025Q1）；pe_status 带 `;degraded_available_at` 标注。weekly_signals 逐股：signal_facts 772 行/只；当前完成周 2026-08-07 仅 600029.SH 有 2 项活跃（divergence+duration，满足 min_active=2），其余 4 只 0 项。
  - `daily --date 2026-08-07` 全池 6 只 ok：5 只新股 daily_watch/right_side 均 incomplete（no_active_card，§2.5 预期），corporate_action ok；603605.SH 卡片 2026-08-10 才生效故 card_not_effective_at_as_of。全池日报 `reports/daily/2026-08-07.md` 全为 P5 普通更新（无数据异常/决策点），单股报告状态 degraded（no_active_card）。
  - `uv run pytest -q` 154 项全绿。
- **吸筹形态状态机上线（§5.4c 新增）**：用户提供《如何看出主力吸筹》方法整理（三阶段框架），评估后作为确定性观察信号落地。
  - `scripts/signals/accumulation.py`：日线级状态机 idle → watching（放量破位：跌幅 ≥5% + 调整量 ≥ 前 20 日均量 ×2.0 shift(1) + 收盘创 60 日新低）→ consolidating（破位满 10 交易日起三条件：窗口振幅 ≤15% + 窗口均量 ≤ 破位基数 ×0.8 + MA5/10/20 粘合 ≤5%，箱体取窗口收盘价上下界）→ confirmed（放量 ×1.5 阳线收盘破箱体上沿）/ failed（跌破箱体下沿 box_broken / 破位后 120 日未确认 expired_no_consolidation / 横盘超 120 日 expired_consolidation），terminal 次日回 idle。试盘（振幅 ≥3% + 上影 ≥ 振幅 50% + 量 ×1.5）只在 consolidating 内计数不转换状态。每日一行写 signal_facts（signal="accumulation"），DELETE+重插幂等，pipeline_runs 阶段 accumulation。价复权、量 ÷share_factor，与指标 §4.1 口径一致；逐日无未来函数。
  - 配套：`config/signals.yaml` 新增 accumulation 节（⚠️ 默认值待人工核对数周，同 §5.2 纪律）；daily.py 信号链插入 right_side 之后（单模块异常 degraded 不拖垮其余）；report.py 单股报告观察点段新增"吸筹形态"一行（带 ⚠️ 参数待核对 + 日 K 代理无分时/盘口 + 仅观察点不进卡片触发标注）；设计文档新增 §5.4c（原 §5.5 消息评价编号不变）。
  - `tests/test_accumulation.py` 7 项：破位边界（恰好 -5%/恰好 2 倍量/非新低/样本不足）、试盘边界、全路径 idle→watching→consolidating→confirmed→次日回 idle（箱体 [93.9, 94.2] 收盘价定界、试盘计数不转换状态）、box_broken 失效、缺 MA 不确认直至 expired、重算幂等 + run 记录。
  - 珀莱雅实测：726 行事实，历史转换 2 次——2024-07-16 放量破位（-7.13%、2.29 倍量、60 日新低）→ 破位后持续阴跌（still_falling）→ 2025-01-13 expired_no_consolidation（符合原方法"破位后继续下跌=真出货"判据）；当前 idle。**参数敏感性发现**：近期 2026-06-22（-6.48%、量比 1.42）与 2026-07-17（-5.80%、量比 1.58）两次大跌仅量比未达 2.0 阈值未触发，即 54.7–61 波段区前的下跌未进入 watching——vol_multiple=2.0 是否偏严留待人工核对期评估（默认值未擅自调整）。
  - `uv run pytest -q` 161 项全绿（154 + 7）。
- **报告指标快照新增折回展示（§5.4 口径纪律）**：用户反馈中国平安 BOLL"对不上"——核实为复权口径展示问题（601318.SH 当前因子 1.161792，BOLL 中轨 61.7394 = 前 20 日复权收盘均值，手工复算分毫不差；不复权现价 53.38 看似偏离实为口径差）。report.py 指标快照段 MA6 条与 BOLL 三轨各加一行折回值（复权 ÷ indicators_daily 当日 price_adj_factor，因子取自 daily_bars 同日行，缺失则不显示折回行），复权原值保留。161 项测试全绿；601318.SH 报告实测：MA20 折回 53.1415、BOLL 折回 中 53.1415 / 上 56.7573 / 下 49.5257，现价 53.38 站中轨上方，口径自洽。
- **交接文档沉淀 + git 接管**：新增 `docs/handoff.md`（其他智能体接收入口：环境/目录结构/常用命令/关键约定/当前状态与已知缺口/常见任务指引，读文档顺序 system_design → implementation_plan → execution_log → handoff）与根目录 `AGENTS.md`（精简版硬性约定：口径纪律/不猜/无未来函数/LLM 边界/文档纪律/每日例行）。git init（main 分支）+ `.gitignore`（.venv/__pycache__/.pytest_cache/.workbuddy/.DS_Store/data/market.db——库为派生二进制，可由 data/raw 重跑管线重建；raw CSV 仅 980K 入库管理保证可重建性）；首次提交 7ba4a66，180 文件。
- **数据库设计文档**：新增 `docs/database_schema.md`——以 migrations/0001_init.sql 为准的逐表逐字段说明：通用约定（生命周期六类/UTC 与市场本地日期/定点十进制 TEXT/复权口径）、25 表速查清单（生命周期/主键/写入方）、按 9 组分组的逐表字段表（运行审计、配置日历、行情与公司行为、事件消息、财务股本预测汇率、指标、信号、决策执行、输出）、signal_facts 的 signal 取值与写入模块对照、关键引用关系图与日常查询入口。handoff.md 与 AGENTS.md 已加链接。

## 2026-08-10（续）

- **Web UI 第一期完成（docs/tasks/ 00–11，TDD 全流程）**：
  - 初始化：`config/ui.yaml`（app/defaults/price_display/charts 配色）、`scripts/ui/{__init__,db,config,parse_args,app}.py`、`pyproject.toml` 增 flask>=3.0.0。`app.py` 用 `create_app(db_path, ui_config)` 工厂（测试注入临时库）+ `main()` CLI；`GET /health` 返回库状态；`TRADE_DB_PATH` 环境变量可覆盖库路径。
  - 查询层：`scripts/ui/queries.py` 全部只读查询（股票列表/单股 bars 与指标/信号/卡片/执行/运行/仪表板）；参数化 SQL + 排序白名单；`compute_tier_state` 档位计算；指标 `unadjusted/adjusted_back` 对价格刻度字段（MA/BOLL）按当日因子折回（§5.1）；周线不复权 = 按 weekly_bars 周边界聚合 daily_bars 原始 OHLC；`get_multi_indicators`/`get_compare`/`get_dashboard(_alerts/_run_stats)`。
  - 布局与页面：`base.html` + navbar/filter_bar/footer partials + 404/500；`common.js`（formatDate/formatNumber/fetchJSON/renderStatusBadge/initStockSearch 等 11 个工具）；8 个页面（`/` `/stocks` `/stock/{symbol}` `/indicators` `/signals` `/compare` `/cards` `/runs`）均服务端渲染骨架 + 页面 JS 拉 API 渲染，URL query 全状态同步（刷新/分享不丢失），ECharts 主副图联动缩放，60s 自动刷新（首页/运行页）。`/reports/<path>` 只读服务报告 Markdown（限定 .md、越界 404）。
  - 价格口径纪律落库到 UI：股票列表/单股页标注复权因子与口径说明；不复权模式指标自动折回可与卡片价区直接对比。
  - 数据质量虚拟码：`pe_status` 筛选支持 ok（空或 `ok%`）/degraded（含 degraded）/missing（其他原因码），兼容真实库 `ok;degraded_available_at` 标注。
  - 测试：`tests/conftest.py`（`ui_db_path`/`ui_conn`/`client` fixtures）+ `tests/ui_seed.py`（确定性合成数据：7 股、因子 1.0/2.0 两档、30 交易日、信号/卡片/执行/运行/报告）+ `tests/test_ui_app.py`4 + `test_ui_queries.py`50 + `test_ui_layout.py`10 + `test_ui_api.py`37。**`uv run pytest -q` 262 项全绿**（161 原 + 101 UI）。
  - 性能实测（真实库）：/api/stocks 50 条 ~8ms、3 年日线 ~5ms、6×6 指标 ~6ms、6 股对比 ~3ms、首页 ~5ms，远低于验收线。
  - 启动：`uv run python -m scripts.ui.app` → http://127.0.0.1:5000/health。
  - 偏差/决定：① 指标行返回键用 `date`、bar 行用 `trade_date`；② compare 的 close/volume/amount 走 bars 口径（支持复权与周线聚合），其余指标走折回口径；③ 生效区间筛选只针对有 effective_from 的卡片；④ "今日"口径 = 全库最新 trade_date（非自然日）；⑤ `page_size` 上限钳制 200 而非报错；⑥ 各页面进度记录见 `docs/tasks/progress/task-00..11-done.md`。

## 2026-08-10（下午）

- **UI 第一期入库 + 交互审查修复**：另一智能体按 `docs/ui_design_phase1.md` 实现只读 Web UI（`scripts/ui/`，Flask + Tailwind/ECharts CDN，9 页面 + 15 API，`config/ui.yaml`，101 项 pytest）。本次交互审查后修复 6 项：
  1. **P0 单股页主图崩溃**：stock.js `closeByDate` 声明在 execMarkers 块内、块外引用 → ReferenceError 全图白屏（两只股票默认打开即坏）。声明提到块外（执行/信号散点共用）。
  2. **P0 口径错误**：完全复权模式下卡片价区/证伪线/箱体/执行标记仍按不复权原价叠加（珀莱雅偏 6%、平安 16%）。改为其仅在非 fully_adjusted 模式渲染，PRICE_NOTE 补说明（§5.1 口径纪律）。
  3. **P1 跨页动线断**：列表页"对比选中"逗号拼接 URL，compare/indicators 页 getAll 不拆 → readURL 改 flatMap(split(',')) 兼容两种形式。
  4. **P1 runs 页报告详情取错表**：拿 report_run_id 撞 pipeline_runs。后端 report_filters/list_report_runs 新增 report_run_id 过滤，前端报告行改查 /api/reports。
  5. **setOption 合并残留**：stock/compare 三处改 notMerge + getInstanceByDom 复用实例（取消勾选/移除股票后旧 series 不再残留）。
  6. **全局筛选条死控件**：事件只在列表页绑定，其余 7 页为死控件且与 stock.js 快捷按钮串扰。base.html 改条件 include（show_filter_bar），仅 /stocks 传入。
  - 附带：runs 页两个表格补详情列缺失的表头。
  - 验证：262 项 pytest 全绿（161 + UI 101）；node --check 4 个改动 JS 通过；重启服务后实测 9 页面全 200、/api/reports?report_run_id=1 返回正确记录、首页无筛选条/列表页有、对比 API 逗号参数正常。遗留未修（打磨级）：执行记录买卖颜色不一致、首页"今日"标签实为最新交易日、信号页 timeline 固定 200 条窗口、默认日期 90 日历日 vs 设计 90 交易日、pe_status 徽章口径不一致、JS 无测试。
  - gitignore 增加 .opencode/（工具状态）；docs/tasks/（UI 任务分解文档）随 UI 一并入库。

## 2026-08-10（晚，盘后首个正式 daily）

- **EOD 发布延迟**：stock_finance_data 当日日线约 20:00 后才发布（15:55–17:00 每 2 分钟探测 30 次均无；close_summary 盘后 EMPTY；指数 get_price EMPTY 改走 yahoo 000300.SS 且当日 bar 已有）。20:07 单次探测成功，重采 6 只 none+forward（窗口 2026-08-01..08-10）。run_id=run_20260810_1555，_meta.json 已记录。
- **发现管线缺陷（因子检查误报，已绕过未修代码）**：daily 先入库新 bar（price_adj_factor 占位 1.0）再做 check_factor_change，占位行进入重叠窗口比对 → 有历史分红（内部因子≠1.0）的 5 只每日必误报"平台段位移"触发全量重建，而窗口 forward 文件（6 行）不含 origin 日 → apply_adjustment 抛 ValueError 整股回滚（600029.SH 因子恒 1.0 不受影响）。且若绕过误报判"一致"，新 bar 会滞留占位因子 1.0 与邻日脱节——"新 bar 因子赋值"在一致路径上无实现，属设计缺口（§3.3 待补：一致路径应给新 bar 赋当前平台段因子；变化路径应要求全量 forward 而非窗口文件）。
- **本次处置（不改代码）**：窗口 forward 文件（含 08-10）+ 新采 segA（origin→08-04）按日期去重合并为 `{ticker}_forward_3y.csv`（字符串级拼接，无数值改动；>3 年上限 1095 天故分段，skill 已有分段约定）。daily 取 sorted[-1] 用 3y 文件 → 误报触发全量重建（version_id 7–11），因子/周线/指标/信号全部正确重算，6 只全 ok。报告 revision=2（首跑 revision=1 为 5 只 failed 的降级版，§9.5 重跑行为）。注意：该误报意味着分红股每个交易日都会全量重建一次（结果正确、版本号日增），是否修代码待人工决定。
- **结果**：daily_2026-08-10 汇总 ok=6。603605.SH 报告 complete（卡片 603605SH_120ca661 今日生效），其余 5 只 degraded(no_active_card) 属预期。沪深300 入 index_bars（yahoo，08-10 收 4702.02）。日报 reports/daily/2026-08-10.md：P4 一条（珀莱雅距 T2 上沿 57.60 还差 2.5%），无 P1/P2/P3。

## 2026-08-10（UI 返工）

- **起因**：用户反馈第一版 UI "交互根本用不起来"——6 只股票的池子却要搜索/下拉选股；"信号时间轴"直接倒 signal_facts 原始表（state=holding_active 等机器字段）。拍板方向：单股页为核心，其余页面合并为一个"数据"入口。
- **导航重构（navbar.html + app.py）**：导航 = watchlist 全部股票页签（中文名，`request.path` 判定当前高亮，移动端横向滚动）+ 最右"数据"入口；股票列表由 context processor 服务端注入（`nav_stocks`，复用 queries.get_watchlist）。旧导航项（首页/股票列表/指标分析/信号时间轴/多股对比/卡片列表/运行状态）全部删除。
- **首页 `/` = 股票启动台**（重写 index.html，服务端渲染，删除 index.js）：6 张大卡片 = 名称/symbol + 最新收盘（不复权，千分位）+ 涨跌幅（红涨绿跌）+ 档位/箱体位置一句话（有卡"T2 价区内"/"档外·距 T2 上沿 2.5% · 箱体内"，无卡"无卡片"）+ 最近 5 日触发信号数；最新 pipeline_runs 有 failed/degraded 时顶部警示 banner。新增 `queries.list_run_alerts` 与模板过滤器 `fmt`（千分位 + 缺失"—"）；`list_stocks` 新增 `box_state` 字段（`compute_box_state`，与 daily_watch.box_position_state 同口径纯函数）。
- **`/stocks` 302 到 `/`**；stocks.html / stocks.js / partials/filter_bar.html / index.js 删除，base.html 的 show_filter_bar 条件 include 一并移除。
- **单股页重设计（stock.html + stock.js）**：首屏 = 头部（名称/卡片链接）+ 8 个关键数字卡片（现价/涨跌幅/PE(TTM) 带 pe_status 角标/当前档位/箱体位置/右侧状态/吸筹形态/活跃衰竭信号 n/5，全部由新 API 填充，null 显示"—"）+ 主图 + 副图（默认成交量 + MACD，空槽位隐藏、下拉可加）+ 信号摘要 + 事件流 + 卡片与执行（位置下移）。已修口径纪律保留：卡片/执行标记仅非 fully_adjusted 叠加、setOption notMerge=true、指标折回逻辑不动。默认改为 MA5/20/60、execMarkers=true（URL `exec=0` 可关）。执行配色统一 A 股惯例买红卖绿（图表散点原卖红买绿已纠正，表格改中文买入/卖出）。
- **新 API `GET /api/stocks/{symbol}/overview`**（queries.get_stock_overview，参数 event_limit/event_offset）：关键数字 + 信号中文摘要（11 类信号固定顺序：档位临近/档位触发/证伪线/箱体位置/右侧确认/吸筹形态 + 五项衰竭，中文名与状态中文映射，detail 从 details_json 提取关键数字——干涸型"当前周量 vs 阈值"、档位"距边界 %"、证伪线"连续跌破 n/2 日"、吸筹"破位日/箱体/试盘次数"；无卡股票省略卡片相关项，无数据给占位不编造）+ 事件流（triggered=1 或 state 转换的行，时间倒序，每条中文一句话 + fact_id 供详情弹窗）。衰竭计数 = 同 anchor 最近完成周 state=active 数/5。compute_tier_state 档外分支补 nearest_tier/nearest_side。
- **`/data` 数据入口页**：信号查询/指标查看/多股对比/卡片版本/运行状态 5 个入口块（原页面保留可用，仅退出导航）。
- **测试**：test_ui_layout.py 导航断言重写（股票页签 + /data + 旧标签消失）；test_ui_api.py 中 /stocks 改 302 断言、路由清单换 /data、JS 清单删 index/stocks、首页断言改启动台元素；新增 7 项（data 页、overview 有卡/无卡/事件流/分页/404、导航高亮）。**`uv run pytest -q` 269 项全绿**（262 + 7）。
- **实测**（真实库临时端口）：/ /stock/603605.SH /stock/601318.SH /data /signals /indicators /compare /cards /runs 均 200，/stocks 302→/，旧导航标签消失；603605 overview 返回"档外·距 T2 上沿 2.5%/箱体内/活跃衰竭 1/5"与卡片一致；601318 卡片相关字段全 null。
- **遗留**：① 事件流文本对 details_json 结构各异的信号做了常见键提取，未见过的结构回退"转为{状态中文}"（不编造）；② ma_comparison 状态映射 above/below/mixed→均线上方/下方/交叉为新增中文文案；③ 单股页 JS 无自动化测试（沿用既有惯例）；④ right_side 无数据行时显示"—"（2026-08-10 跑批后 603605 无 right_side 行，符合不猜原则）；⑤ 601318 等股 accumulation 无行同样显示"—"。

## 2026-08-10（UI 三联图定稿落地）

- **起因**：用户评审并定稿交互原型（`docs/prototype/stock_page_template.html` + `generate.py` + 样例 `stock_603605.html`），要求按原型 1:1 改造真实单股页 `/stock/<symbol>`。
- **图表改为单 ECharts 实例三 grid 联动**（stock.js 整体重写）：grid0 蜡烛图（红涨绿跌 [open,close,low,high]）+ MA5/20/60（legend 可开关）；grid1 成交量柱（对前收涨红跌绿，÷10000 万单位）+ 均量20 黄线；grid2 MACD（DIF/DEA + 柱正红负绿）。`axisPointer.link xAxisIndex:'all'`；dataZoom inside+slider 均绑 [0,1,2]；y 轴右侧、boundaryGap、仅底部 grid 显日期；自定义 tooltip（蜡烛开/收/低/高、量带"万"）。卡片标记改原型样式：三档 yAxis 横带 markArea（极浅底色 + insideLeft 9px 小标签）+ 证伪/箱体上下沿/右侧触发细 markLine（insideStartTop 小字）；执行买红卖绿带"买/卖+价格"小字；信号 pin 无文字悬停见名（SIGNAL_NAMES 中文映射）。口径纪律保留：fully_adjusted 模式不叠加卡片/执行/信号标记；日/周切换保留（周线同结构）；setOption notMerge=true。加载全部历史（start=2000-01-01），dataZoom 初始定位最近约 120 根。
- **控制区精简**：仅口径（不复权/完全复权）+ 周期（日线/周线）两个切换 + 一行口径说明；删除 chartType/panel 选择器/日期输入/marker 复选框/quick 按钮（MA 开关交给 legend）；URL 同步只剩 granularity/price。
- **导航改股票下拉框**（navbar.html）：select 列全部 watchlist（按 market、symbol 排序，app.py context processor 内排序），当前股票 selected，onchange 跳 /stock/<symbol>；"数据"入口保留。
- **首屏 8 张数字卡片改原型结构**（label/value/sub 三行，CSS 入 app.css：.num-card/.up/.down）：PE 副行 pe_status 中文映射 JS 实现（ok 开头→"正常"、含 degraded_available_at→追加"·披露日降级"、含 degraded→"降级"、其他非空→"数据缺失"）；档位 "Tn 内"+价区 / "档外"+"距 Tn 上/下沿 x.x%"；箱体副行箱体区间；右侧副行"触发 x"；衰竭"n/5 活跃"+副行"完成周 <date>"；现价副行数据截止日。
- **事件流面板 → 资讯流面板**：渲染空态占位"资讯源二期接入，标签由 LLM 打标后存档"（真实资讯二期接入，不写死示例数据）；overview API 的 events 字段保留不动。
- **排期卡面板扩充**（数据源 /api/cards/<card_version_id>）：三档价区表 + "反推口径"列（T3 用悲观 EPS、其他档中性 EPS，价区÷EPS 得隐含 PE，纯算术）；情景假设（EPS/PE 三情景、恐慌底刻度序列、样本窗口、体系判断 regime）；交易框架（证伪线+note、波段箱体买/卖区/失效线、右侧触发/止损）；保留"查看完整 JSON"。
- **信号现状面板保持现状**（overview 摘要 + 详情弹窗不动）。
- **顺带修复**：`GET /api/stocks/{s}/indicators` 不传 fields 时 500——`fields_arg` 缺省返回 `[]`，`get_stock_indicators` 只判 `is None`，拼出 `SELECT date, FROM` 语法错误。改为 `if not fields`（空列表等同 None 返回全部列），与"不填 fields 即返回全部列"语义一致（旧 stock.js 同样路径，该 bug 自第一期存在）。
- **测试**：test_ui_layout.py 导航断言改下拉框（option selected）；新增 test_page_stock_prototype_elements（新结构 ID 全在 + 已删控件 ID 全不在 + 资讯流空态文案）。**`uv run pytest -q` 270 项全绿**；`node --check stock.js` 通过。
- **实测**（真实库 test client + 临时端口）：/ /stock/603605.SH /stock/601318.SH /data 均 200；下拉框 selected 正确；指标 API 全历史 727 行含 ma5/20/60、vol_mean20、dif/dea/macd_hist（周线 154 行同）；信号 pins 数据 11 条 triggered；卡片详情 eps/regime/scales 齐备。
- **与原型出入**：① 信号 pin 数据来自 /api/stocks/{s}/signals（limit=2000 覆盖全历史），按 (observed_on, signal) 去重，等价原型 DISTINCT 语义；② 执行记录表保留既有五列（时间/动作/档位/价格/数量），未改原型四列；③ 资讯流为纯空态占位（原型为示例数据，按要求不写死）；④ grid/高度/tooltip 文案照抄原型。

## 2026-08-10（中国平安估值排期卡 draft）

- **流程**：fred-valuation-card-skill 全流程。底稿 `cards/601318.SH/inputs_2026-08-10.json`（行情 08-10 / 财报 2026Q1 / 一致预期快照 08-10）→ 盈利底稿 → 估值刻度 → build_schedule.py → 胜率打分 → 双产物：`cards/601318.SH/中国平安估值排期卡_draft_2026-08-10.md` + `draft_2026-08-10.json` → `create-draft` 入库 **601318SH_6c1eba32**（draft-only，激活待人工）。
- **口径发现（重要）**：系统 EPS/PE 为市值口径（A 股价 × A 股股本 106.6 亿 ÷ 集团归母净利），TTM EPS 12.4562；平安 A+H 结构使该口径与总股本披露口径（年报 EPS 7.68）差约 1.7 倍。卡内强制标注，全部刻度同口径计算、相对比较有效。
- **关键判断**：① 裂口 17.28pp 未收敛（券商 FY1 +9.89% vs Q1 实际 -7.38%），情景以实际趋势为准：EPS 中性 11.60 / 悲观 10.40 / 极悲 9.30；② 体系"下移后企稳"（2023 刻度 3.96→3.03，2024-09 回升 3.54），PE 4.2/3.4/3.0；③ 三档 37.90–39.40 / 33.60–35.40 / 27.90–30.10，证伪线 27.90；④ 现价 53.32 高于 T1 上沿 35.2%，不触发；⑤ 胜率区间 T1 35–50/T2 50–65/T3 55–70，Kelly 上限 0%/6.7%/13.1%——**T1 Kelly=0，裂口收敛前即使到档也不建仓**；⑥ 波段箱体 47–56（现价已在卖区下沿附近，非开仓点），右侧触发 56.00/止损 53.50；⑦ P/EV 与股息率锚数据缺口已声明，待中报后人工补充。
- **验证**：`uv run pytest -q` 270 全绿（纯数据变更无回归）。

## 2026-08-10（平安卡激活 + 当日重跑）

- 人工确认激活 601318SH_6c1eba32（effective_from=2026-08-10，next_review 2026-08-31）；生成卡片 MD `cards/601318.SH/2026-08-10_601318SH_6c1eba32.md` 并刷新 current.md。
- 同日重跑 daily（无新 raw，revision=3，§9.5）：601318.SH 报告 degraded→**complete**，档位/箱体/证伪/右侧信号全部落库（现价距 T1 上沿 35.3%、mid_box、右侧距突破线 5.7%、吸筹 watching 破位日 2026-06-18）；今日无决策点；汇总 ok=6。
- UI 实测：/api/stocks/601318.SH/overview 已返回卡片定位（档外·距 T1 上沿 35.3%/箱体内）。

## 2026-08-10（海天味业估值排期卡 draft）

- fred-valuation-card-skill 全流程，底稿 `cards/603288.SH/inputs_2026-08-10.json` → 双产物（卡 MD + `draft_2026-08-10.json`）→ `create-draft` 入库 **603288SH_e7e77c60**（draft-only，激活待人工）。
- **关键判断**：① 裂口 0.91pp 已收敛（FY1 +11.87% vs Q1 实际 +10.96%），中性情景参考一致预期：EPS 1.43/1.29/1.14；② 体系"缓慢降档尾段"——5 次探底稳定 26–28，2026-06-22 日内 31.60 折算 PE≈24.1 刺穿底部带下沿（周线未破，样本外标注）：PE 29.5/26.5/24.0；③ 三档 36.40–37.90 / 32.50–34.20 / 27.40–29.50，证伪线 27.40；④ **现价 36.78 落在第一档价区内**（T1 触发成立）；⑤ 胜率 60–70/70–80/75–85，Kelly 0%/12.4%/18.3%——T1 保守修复目标下赔率 0.52 非正期望，建仓需接受 p75 修复假设并小仓；⑥ 波段箱体 31.60–39.00（现价近卖区下沿），右侧触发 39.00/止损 37.00；⑦ 衰竭锚 35.28 已被 31.60 击穿（周线未确认），卡内标注实际前低。
- `uv run pytest -q` 270 全绿。

## 2026-08-10（海天卡激活 + 当日重跑）

- 人工确认激活 603288SH_e7e77c60（effective_from=2026-08-10，next_review 2026-08-31）。
- 同日重跑 daily（revision=4）：603288.SH 报告转 **complete**，产出**决策点 [档位触发 T1]**——收盘 36.78 进入 T1 价区 [36.40, 37.90]（第一档无附加信号要求）；箱体 mid_box；汇总 ok=6。

## 2026-08-10（南方航空估值排期卡 draft）

- fred-valuation-card-skill 流程，底稿 `cards/600029.SH/inputs_2026-08-10.json` + 参照《航空业基本面与消息面分析_20260729》→ 双产物 → `create-draft` 入库 **600029SH_a9c860a3**（draft-only，激活待人工）。
- **强周期锚切换**：航空 2023–2024 连亏、2025 微利（+8.57 亿，三大航唯一）、2026 油价冲击，PE 失真不作锚；按细则改用**价格底带 + PB**（PB 2.53@5.18 外部口径，底稿无净资产已声明缺口，中报后补）。EPS×PE 矩阵负盈利失效，三档手工锚定，Kelly 手工算。
- **关键判断**：① 系统裂口 280.7pp 与 Q1 同比 -298% 均为负基数伪信号（实际 Q1 +14.81 亿淡季扭亏，大改善）；真实矛盾=Q2 油价冲击（航油 +70%，三大航 Q2 合计亏 122–138 亿）vs 券商 FY1 7.07 亿隐含 H2 打平；② 价格底带 5.18–5.60 维持近 3 年（8 次探底），2026-07-14/15 双底 4.95 刺穿下沿 → 底带下移 4.95–5.60；③ 三档 4.95–5.20 / 4.45–4.75 / 3.95–4.25（对应 PB 2.42–2.54 / 2.17–2.32 / 1.93–2.08），证伪线 3.95；④ **现价 5.15 落在 T1 区内**；⑤ **衰竭信号已 2 项 active（底背离+持续时间，min 2 满足）**，T2/T3 信号条件已成立（底背离 active_until 2026-08-21）；⑥ 胜率 55–65/65–75/70–80，Kelly 4.7%/13.3%/17.0%（修复目标 6.50≈PB 3.2，油价不回落不成立）；⑦ 波段仓不适用（下跌趋势中），右侧触发 5.65/止损 5.40。
- **schema 适配**：swing_box 不适用时给空对象 `{}`（字段仅允许十进制字符串，null/note 均不允许）；`_DEC` 支持负数字符串（bear "-0.59"）。
- `uv run pytest -q` 270 全绿。

## 2026-08-11（南航卡激活）

- 人工确认激活 600029SH_a9c860a3，effective_from=**2026-08-11**（激活时间已过零点，与前两张卡当日生效不同）；生成卡片 MD 并刷新 current.md。
- 08-10 同日重跑（revision=5）：南航卡当日未生效，报告维持 degraded(no_active_card)（§2.5 正确行为，不猜）；今日（08-11）盘后 daily 起卡片信号开始计算。

## 2026-08-11（紫金矿业估值排期卡 draft）

- fred-valuation-card-skill 流程，底稿 `cards/601899.SH/inputs_2026-08-10.json` → 卡 MD + draft JSON → `create-draft` 入库 **601899SH_85cd7f52**（draft-only，激活待人工）。
- **口径发现（重要）**：底稿 PE 刻度（panic_lows 的 pe_ttm 及分位）全部为**静态折算值**——历史低点价格÷最新 TTM EPS 2.9944（现价 35.45÷2.9944=11.84 与 current_pe_ttm 精确一致），非"当时可见 TTM 的 PE"。盈利两年近三倍使刻度被动下移，卡内强制口径说明。
- **关键判断**：① 裂口 −39.8pp 为**有利方向**（Q1 实际 +97.5% 远超券商 FY1 +57.7%），中性情景取券商 FY1 816 亿：EPS 3.96/3.14/2.51；② 旧价格底带 11.38–15.45（2023-08~2025-03 共 16 次探底）对应 TTM≈220 亿时代已失效，锚改用**折算 PE 刻度×未来 EPS**（乐观 8.0=2025-09-04 低点 23.39 折算 7.97 / 中性 5.5≈p50 / 悲观 4.5）；③ 三档 20.91–21.78 / 16.41–17.27 / 11.30–12.20（T3 恰为旧底带下沿，结构自洽），证伪线 11.30；④ 现价 35.45 高于 T1 上沿 62.8%，**远离所有档位，当前不操作**；⑤ 胜率 55–65/70–75/75–85，Kelly 2.8%/14.7%/18.6%（修复目标 31.7=中性×乐观，金价趋势逆转则不成立）；⑥ 波段仓不适用（44.94→24.42 下跌后反弹段），右侧触发 35.80（近 60 日高）/止损 33.50；⑦ 衰竭锚 2025-09-04（23.39）为老锚，其后回落未击穿前低，0 项 active 符合预期；跌破 23.39 则系统重算锚。
- 三档由 build_schedule.py 生成（与手算 Kelly 一致）。
- `uv run pytest -q` 270 全绿。

## 2026-08-11（紫金卡激活）

- 人工确认激活 601899SH_85cd7f52，effective_from=2026-08-11；生成卡片 MD 并刷新 current.md。
- 11:18 盘中跑 daily：6 只全部 incomplete（当日 bar 未发布，基准亦无）→ 报告 degraded P1，§2.5 正确行为。参照昨日经验（EOD 约 20:00 发布），今晚 20:00 后采集增量并重跑验证紫金卡片信号计算。

## 2026-08-11（豫光金铅 600531.SH 入池）

- watchlist 第 7 只：`config/watchlist.yaml` 新增 600531.SH 豫光金铅 → `db seed` 导入（watchlist 7 只）。
- 全量初始化采集（run_init_600531，data/raw/**/2026-08-11/）：行情 none+forward 3 年各 725 行（2023-08-11..2026-08-10，2023-08-10 起会超来源 1095 天上限故起始后移 1 天）；利润表 7 期（3 年报+4 季报）；一致预期快照（FY1 8.64 亿/FY2 9.10 亿/FY3 10.30 亿）；yahoo get_stock_info 股本快照（sharesOutstanding=1,209,262,698；返回文本 ticker 误写 000733.SZ，数据体确认为豫光金铅，meta 已标注）。
- 入库：daily_bars 725 行 + 财报 7 期（available_at 降级为入库时间，来源无披露时间）+ forecast；股本经 `valuation.load_share_snapshot` 写 share_capital_events（effective_at=2023-08-11 区间起点，同前 6 只惯例）。
- 复权因子全量重建（adjust）：11 个平台段（分红型切换，corporate_actions 无记录已 NOTE）；周线重建 154 周。
- daily 2026-08-10（revision=6）：600531.SH 报告 degraded(no_active_card)（§2.5 正确行为，同步观察无卡）；现价 13.90；吸筹状态机 idle；衰竭锚 2025-09-08（9.78 不复权）0 项 active；汇总 ok=7。
- 测试修复：UI 测试 7 处硬编码股票总数 7→8（test_ui_queries×3 / test_ui_api×3 / test_ui_app×1，注释同步 6CN+1HK→7CN+1HK）；`uv run pytest -q` 270 全绿。
- 今晚 20:22 盘后增量 cron 已更新为 7 只（eb027c36，原 fe5f4df6 删除）。

## 2026-08-11（豫光金铅估值排期卡 draft）

- fred-valuation-card-skill 流程，底稿 `cards/600531.SH/inputs_2026-08-10.json` → 卡 MD + draft JSON → `create-draft` 入库 **600531SH_9a009077**（draft-only，激活待人工）。
- **体系判断**：铅锌冶炼+金银回收（不掌握矿山，弹性弱于矿业股），盈利稳定 5.8→8.07→8.60 亿，2026Q1 +20.1% vs 券商 FY1 零增长（+0.4%）；裂口 -19.7pp 为有利方向但券商预期本身保守。股价 2025-09 起 9.78→24.79（2026-01-29 与紫金同日见顶）→ 阴跌 10.58（-57%，07-20）→ 8 月放量反弹至 13.90。**核心不确定**：估值中枢是否因贵金属行情从 p50 9.5 上移至 13+（2025-09-08 回踩 9.78 折算 13.17 远高于旧底带 6.7–9.6），未经完整回调验证，保守取中枢 9.5/上沿 13。
- **关键判断**：① EPS 0.71/0.62/0.50（中性=券商 FY1 8.64 亿）× 折算 PE 13/9.5/8；② 三档 6.48–6.75（=2024 平台+2025-03 探底区）/ 5.60–5.89（=2023-12~2024-03 底带）/ 4.00–4.32（有意深于 3 年最低 4.86 的双杀定价），证伪线 4.00；③ 现价 13.90 高于 T1 上沿 106%，不操作；④ 胜率 50–60/60–70/65–75，**T1 Kelly=0**（胜率下沿 50%+赔率 1.0 非正期望，执行需届时证据推胜率至上沿）；T2/T3 Kelly 10.0%/16.0%；修复目标 9.2；⑤ 波段仓不适用（下跌后反弹段），右侧触发 15.75（7 月平台区）/止损 13.20；⑥ 衰竭锚 2025-09-08（9.78）老锚 0 项 active，跌破 9.78 系统重算。
- `uv run pytest -q` 270 全绿。

## 2026-08-11（豫光金铅卡激活）

- 人工确认激活 600531SH_9a009077，effective_from=2026-08-11；生成卡片 MD 并刷新 current.md。
- 08-10 同日重跑（revision=2）：卡当日未生效，报告维持 degraded(no_active_card)（§2.5 正确行为，同南航模式）；今日（08-11）盘后 daily（cron eb027c36 20:22）起卡片信号开始计算。

## 2026-08-11（埃斯顿估值排期卡 draft）

- fred-valuation-card-skill 流程，底稿 `cards/002747.SZ/inputs_2026-08-10.json` → 卡 MD + draft JSON → `create-draft` 入库 **002747SZ_f2a92708**（draft-only，激活待人工）。
- **锚类型**：题材定价股（人形机器人量产预期），PE 完全失真（TTM 231.7 vs 样本 p50 135；2024 巨亏 -8.10 亿、2025 微利 0.45 亿、TTM 1.30 亿）→ 按细则改用**价格底带 + PS**（营收 40–49 亿稳定：PS 刻度 股灾底 2.1 / 2024 平台 2.5–2.8 / 大平台 3.4–4.8 / 现价 6.2；PB 10.50 参考意义弱）。EPS×PE 矩阵失真未用脚本，三档手工锚定，Kelly 手工算。
- **关键判断**：① 裂口 -307.7pp 为低基数百分比失真（+674.6% vs +366.9% 均虚高），实质=Q1 0.98 亿占券商 FY1 2.10 亿的 47%，节奏超前；② 2026-05 放量主升 21→49.00（07-07 见顶）→ 回落 -43% 至 27.95（07-30）→ 反弹 34.63；核心不确定=题材是否有第二波（决定大平台 19.2–27.2 是回调终点还是下跌中继）；③ 三档 26.0–27.5（主升启动位+大平台上沿，7-30 已逼近）/ 21.0–23.0（大平台中枢）/ 14.5–16.5（2024 困境平台，刻意避开股灾底 11.3–12.1 流动性危机价），证伪线 14.50；④ 现价高于 T1 上沿 25.9%（六只里距 T1 最近）；⑤ 胜率 50–60/60–70/65–75，Kelly 4.1%/11.7%/16.0%（修复目标 45.0=前高下方题材回暖区）；⑥ 波段仓不适用，右侧触发 36.00/止损 31.00；⑦ **衰竭锚滞后**：系统锚 2026-06-29 的 32.93 已被 27.95 击穿，量能基数为 6 月放量期 5.77 亿股，锚切换前衰竭读数参考意义有限，以 27.95 是否守住为准（同海天卡模式标注）。
- `uv run pytest -q` 270 全绿。

## 2026-08-11（埃斯顿卡激活）

- 人工确认激活 002747SZ_f2a92708，effective_from=2026-08-11；生成卡片 MD 并刷新 current.md。**至此 7 只观察股全部持有激活排期卡**。
- 今日（08-11）盘后 daily（cron eb027c36 20:22）起紫金/豫光/埃斯顿三张新卡信号同步开始计算。

## 2026-08-11（消息面二期设计写入 system_design.md）

- §3.6 公告与新闻：来源改为矩阵表（●已备 adapter/◐已设计未实现/○二期新增），新增全市场快讯（财联社，政策/宏观主渠道）与行业新闻（东财行业频道）两个二期来源；新增行业/政策事件→个股关联机制（watchlist 扩展 keywords 人工初筛 + LLM 建议人工确认，与卡片 draft-only 同纪律）；明确二期上线子序 ①公告入库②个股新闻③LLM 评价+资讯流④快讯/行业+scope 关联。
- §5.5：event_assessments Schema 新增 `scope` 字段（company/industry/policy/macro，与 direction 正交），资讯流每条资讯展示 direction×scope 双标签；scope 列落地需配套 migration，随二期第③步实施。
- §9.4：第 4 步指向 §3.6 二期子序；补二期可选扩展=采集资产负债表自算历史 PB（一期 PB/PS 用 forecast 快照单点值）。
- 背景：用户确认消息面四层面（基本面/政策面/行业/公司）当前仅覆盖公司级，政策/行业无来源，故作此二期设计。

## 2026-08-11（卡片锚指标明示 + 单股页基本面区块）

- **锚指标明示（工作流 C）**：① `card.py` 新增 `valuation.anchor` 语义校验——`metric` 枚举（pe_scale/pe_static_scale/pb/ps/price_band/mixed）非法拒绝、缺失仅 warning 不拒绝（兼容存量 draft，`card_input_warnings`）；② skill `card-template.md` 卡首强制「锚定指标」行；③ 单股页卡片面板新增「锚定指标」行：优先 `valuation.anchor`（枚举映射中文），回退 `input_snapshot.anchor_type_note`（4 张新卡），再回退「PE(TTM) 刻度（默认）」（3 张老卡真实情况）；非 PE 锚时「PE 三情景」标注「非锚，仅分位参考」；④ 存量 7 卡不动库（不可变版本纪律）。tests/test_card.py +3 用例。
- **基本面区块（工作流 B）**：`queries.py` 新增 `_fundamentals`（最新年报/季报营收净利+同比，口径同 card_inputs 同季匹配；PB/PS 取自最新 forecasts.payload_json 同花顺快照单点值并标注快照日期；一致预期 FY1–3）接入 `get_stock_overview`；stock.html 数字卡行下新增「基本面」6 格区块；stock.js `renderFundamentals`（缺失「—」+ PB/PS 快照角标提示）。tests/test_ui_queries.py +2 用例（含缺失不猜）。
- 验证：API `/api/stocks/601899.SH/overview` 返回真实 fundamentals（FY2025 营收 3490.79 亿 +14.96%、PB 4.74 快照 08-10 等）；南航卡 API 无结构化 anchor、回退链命中 anchor_type_note；UI 服务已重启加载新代码（bash-ebwcyx82）。
- `uv run pytest -q` **275 全绿**（270 + 新增 5）。

## 2026-08-11（盘后例行 cron eb027c36 + 新 bar 因子继承 bug 修复）

- **采集**：7 只 × 14 行情 CSV（none+forward，窗口 2026-08-01..08-11，各 7 行含当日 EOD）落 `data/raw/stock_finance_data/price/2026-08-11/run_20260811_2022/`；沪深300 改 yahoo period=5d（start/end 区间查询当日 EMPTY_DATA）落 `data/raw/yahoo_finance/index/2026-08-11/run_20260811_2022/`，000300.SH 收 4663.79（-0.81%），经 `scripts.pipeline.ingest` 单独入库（daily --raw-dir 不覆盖指数目录）。
- **bug 修复（adapters/stock_finance_data.py upsert_daily_bars）**：新插入 bar 原一律填 price_adj_factor=1.0 → 有分红历史的股票最近平台段内部因子 ≠1.0（如珀莱雅 ≈1.058），因子变化检查把新 bar 误判「窗口内平台位移（除权）」触发全量重建，而重建用 raw-dir 内 7 天窗口 forward CSV → origin 日（2023-08）不在序列报 ValueError，6 股 failed（仅从未分红的 600029.SH 通过）。修复：新 bar 因子继承上一交易日（无历史落 1.0）；附带消除「检查未触发时新 bar 永远带错因子污染周线/均线」隐患。tests/test_adapters.py +1 回归用例（test_price_ingest_new_bar_inherits_factor）。
- **重跑全 ok**：7/7 ok，报告全 complete（002747/601318/601899/603288/603605/600029 revision=3，600531 revision=2，日报 revision=3，§9.5 同日重跑）。
- **卡片验证**：紫金卡（85cd7f52）与豫光卡（9a009077）生效首日 complete、无档位触发（33.18 远高于 T1 上沿 21.78；豫光 13.62 高于 T1 上沿 6.75）；南航卡 T1 触发（5.05 ∈ [4.95,5.20]）进日报优先级 3；海天跌出 T1 区（36.17 < 36.40，距下沿 0.6% 进优先级 4 观察）；珀莱雅距 T2 上沿 57.60 还差 2.2%（优先级 4）；埃斯顿收 36.12 站上右侧触发位 36.00 但未过突破线 36.36（=触发位×1.01），right_side 未触发，状态机 idle。
- 今日收盘：珀莱雅 58.89 / 海天 36.17 / 平安 52.55 / 埃斯顿 36.12（+4.3%）/ 紫金 33.18（-6.4%）/ 南航 5.05 / 豫光 13.62。
- `uv run pytest -q` **276 全绿**（275 + 新增 1）。

## 2026-08-11（earnings-surge-screener skill 移植接入）

- 来源：用户提供的 `earnings-surge-screener.zip`（A股"业绩兑现痕迹×景气表述"三轨选股工作流：Track A 预告大增×景气关键词 / Track B 早期痕迹 / Track C β错杀 + 通用闸门层 + 阶段二三维前瞻 + 跟踪重检）。
- **移植**到 `skills/earnings-surge-screener/`（SKILL.md + 6 references + 2 scripts）：① 数据源层改写——删除 `scripts/gildata_query.py`（硬编码 `/app/.agents/plugins/...` 不可用于本机），改走 kimi-datasource MCP（`call_data_source_tool`，data_source_name="gildata"，params={query, file_path}）；Wind 选股兜底本环境无对应端点，降级为"换措辞重试语义选股"；② 脚本调用统一 `uv run python`；③ 新增「与本系统的衔接」节：核心候选→人工确认→watchlist→stock-collect 采集→fred-valuation-card-skill 出卡 draft→人工激活；报告存 `reports/screening/`；证据 CSV 落 `data/raw/gildata/`；证伪信号经确认转 cron；执行留痕 execution_log。LLM 只产 draft、卡片激活必须人工两条纪律不变。
- **实调验证（gildata MCP 可得性确认）**：① `gildata_announcement_data`"2026年半年度业绩预告中提到产品供不应求"→15 行公告原文落 `data/raw/gildata/announcement/2026-08-11/ann_supply_short.csv`；② `verify_announcements.py` 正则核验：12 候选→4 合格（铜冠铜箔+486~544%、金安国纪+936~1063%、安达科技扭亏、中晶科技+55~75%），剔除预亏 1 家；③ `gildata_fin_query` 3 家×单主题（珀莱雅/海天/平安 2026-2027 一致预期净利润）一次成功，与 SKILL.md 记载的调用口径一致。
- **parse_gildata_table.py 修 2 个 bug**：① 业绩预告表空表头/重复表头导致 `pd.concat` 抛 InvalidIndexError——表头去重命名（空名补 _colN，重名加 _2/_3）；② 首列去重静默吞掉一致预期表次年以后各行——改全行去重（仍能去掉 gildata 重复 result 块）。
- 本 skill 定位选股研究层（LLM 主导判断流），与确定性管线是上下游关系，产出不经人工确认不进管线。

## 2026-08-12（石油石化板块筛选：earnings-surge-screener 首次实战）

- 用户点名板块场景，走 `skills/earnings-surge-screener` 全流程：Track A 公告景气检索 5 组关键词（价格上涨/供给偏紧/高景气/订单满产/扭亏）落 `data/raw/gildata/announcement/2026-08-12/petro_*.csv`；verify_announcements.py 双因子核验 43 候选 → 19 合格，人工剔除非石化杂音与非经常型（中泰化学扣非仍亏）后石化核心 8 家。
- Track B：B1 Q2 单季拐点仅恒逸石化（+2500.7%，营收 +0.2% 贴线降级）、潜能恒信（+522.9%，油价链背离谨慎）；B3 近 3 月盈利预测上调 >10% **0 家**——本轮是公告驱动而非一致预期驱动。
- Track C 环境闸门：沪深300 YTD -0.6%（非普跌）；科创50 YTD +23.8% vs 申万石油石化 +3.8%，差 20pct < 30pct 阈值，吸血环境不成立，Track C 仅出排雷（石化机械/中化国际预亏、东华能源 YTD -30.9%）。
- 阶段二三维：6 家一致预期（恒力 2026E 118.1 亿 +66.9%、荣盛 76.7 亿 +803.7%、盛虹 41.8 亿 +3023%、恒逸 70.8 亿 +2640%、上石化扭亏 3.7 亿、东华 4.1 亿）；YTD 分化大（恒逸 +65.5% 已兑现 vs 恒力 -18.0%、东华 -30.9% 滞涨）；油价布伦特 08-11 收 87.72，惠誉看 Q4 降至 70、大摩下调 2027 至 75/70，成本中枢下行是炼化链弹性核心来源也是最大反向变量。
- 板块定级四因子 6/8 → **主线候选**：炼化聚酯强、上游油价链弱（石化机械预亏示警 capex 放缓）；报告落 `reports/screening/2026-08-12-石油石化.md`（含证伪信号 4 条与跟踪清单：首选恒力石化，衔接纪律=人工确认→watchlist→stock-collect→出卡 draft，不自动进管线）。

## 2026-08-12（恒力石化加入股票池）

- 承接石油石化板块筛选（`reports/screening/2026-08-12-石油石化.md` 首选标的），人工确认后 600346.SH 恒力石化加入 `config/watchlist.yaml`（aliases: 恒力石化/恒力/Hengli），`uv run python -m scripts.pipeline.db seed` 导入 watchlist 表，全池 8 只。
- 尚未采集行情/财报数据（下一步走 stock-collect 流程），UI 在采集前对该股输出 incomplete（§2.5 不猜）。

## 2026-08-12（恒力石化 600346.SH 采集入库）

- 全量初始化（run_init_600346）：行情 none+forward 各 726 行（2023-08-14..2026-08-12）；利润表 7 期（FY2023 归母 69.05 亿 / FY2024 70.44 亿 / FY2025 70.75 亿 / 2026Q1 39.10 亿 +90.7%；**2026 中报未披露返回空，文件删除按 §2.5 记缺失**）；forecast 快照（FY1 120.78 亿 +70.7%/FY2 139.77 亿/FY3 162.34 亿，PE(LYR) 17.83、PB(MRQ) 1.78）；公告 1 年 121 条（要点：07-07 H1 预增 +136%、07-20 第五期回购+07-21 首次回购、**04-26 子公司被美国财政部列入 SDN 清单**、05-26 权益分派）；yahoo get_stock_info 股本快照 7,039,099,786 股（与 gildata 一致）。
- **adapter 修复（stock_finance_data.py）**：① 公告列名别名——推断列名（time/title/url）与真实样本（reportDate/reportTitle/pdfURL，time 列空）不符致 121 条公告整批回滚，补别名+注释更新（原注释自承"未经真实样本验证"）；② event_symbols 孤儿修复——真实样本 thscode 列空导致公告无个股关联，回退用文件名 stem（`\d{6}\.(SH|SZ|BJ)`）关联；存量 121 条已 SQL 回填。tests/test_adapters.py +1 回归用例（真实列名+关联断言）。
- 入库：daily_bars 726 行 + 财报 7 期（available_at 入库降级）+ forecast + 公告 121 条；股本经 load_share_snapshot 写 share_capital_events（effective_at=2023-08-14，单点假设已标注；恒力有回购需后续 get_stock_actions 交叉验证）。
- 复权因子全量重建：5 个平台段（切换日 2024-05-27/2025-06-19/2025-09-24/2026-06-02，corporate_actions 无记录已 NOTE）；周线重建 153 周。
- daily 2026-08-12：600346.SH ok（指标 daily=726 weekly=153，吸筹 ok，卡片信号 no_active_card 待激活），报告 degraded P5 revision=1；**其余 7 只 incomplete（08-12 增量未采，旧 cron eb027c36 属上个会话未延续）**；现价 17.75，PE(TTM) 13.99 处 p50–p75。
- UI 测试 7 处硬编码股票总数 8→9（test_ui_queries×3 / test_ui_api×3 / test_ui_app×1，注释 7CN+1HK→8CN+1HK）。`uv run pytest -q` **277 全绿**。

## 2026-08-12（恒力石化估值排期卡 draft）

- fred-valuation-card-skill 流程，底稿 `cards/600346.SH/inputs_2026-08-12.json` → 卡 MD + draft JSON → `create-draft` 入库 **600346SH_5df2b631**（draft-only，激活待人工）。
- **锚类型**：静态折算 PE 刻度（pe_static_scale，同紫金/豫光卡）——炼化强周期但三年 69–71 亿盈利平台未亏损、底部 PE 带 9.0–13.0 三轮稳定、底部价位逐轮上移（11.11→13.65→14.22→14.95/15.00 双底），体系未切换；PB(MRQ) 1.77 仅快照参考（无历史 PB 序列）。
- **关键判断**：① 盈利周期反转（Q1 +90.7%、H1 预增 +136% 占券商 FY1 60%），裂口 -19.9pp 有利方向；中性 EPS 1.49=券商 FY1 打 87 折（下半年油价回落库存损失，惠誉 Q4 70/大摩 2027 75-70），悲观 1.21/极悲 0.99；② PE 13/11/9.5（乐观=2025-08 低点当时 PE 体系上沿，悲观=股灾底 9.0–9.7+p5 9.99）；③ 三档 15.7–16.4（=2026-07 探底区上半，07-24 已触及 15.95）/ 12.6–13.3（击穿双底后 2024-11 底下方+2023-12 底带）/ 9.4–10.2（深于 3 年最低 11.11 的双杀定价），证伪线 9.40；④ 现价 17.75 高于 T1 上沿 8.3%（全池距 T1 最近之一），市场定价中性情景×中位 PE 未给反转溢价；⑤ 胜率 50–60/60–70/65–75，**T1 Kelly=0**（赔率 0.5 非正期望，执行需届时证据推胜率上沿）；T2/T3 Kelly 9.4%/15.9%；修复目标 19.4；⑥ 波段仓不适用（26.74→15.00 下跌后反弹段），右侧触发 18.50/止损 16.60；⑦ 衰竭锚 2025-08-20 前低 14.95 仍有效（07-13 低点 15.00 未击穿）但偏老，跌破即系统重算；⑧ 尾部风险：子公司 SDN 清单（04-26 公告）+ 油价站稳 100+ 证伪价差逻辑，均列入复核触发器。
- `uv run pytest -q` **277 全绿**。
- 注意：盘后增量 cron（原 eb027c36）属上个会话未延续，本 session CronList 为空，每日 20:22 增量采集当前无定时任务，待用户确认是否重建（8 只）。

## 2026-08-12（恒力石化卡激活）

- 人工确认激活 600346SH_5df2b631，effective_from=2026-08-12；生成卡片 MD 并刷新 current.md。
- 同日重跑 daily（§9.5，revision=2）：卡片信号当日生效——daily_watch/right_side 由 no_active_card 转 ok，单股报告 complete P5；现价 17.75 高于 T1 上沿 16.40 无档位触发，右侧 17.75 < 触发位 18.50 状态机 idle。**至此 8 只观察股全部持有激活排期卡**。

## 2026-08-12（盘后增量采集：7 只 + 指数，全池 ok=8）

- 增量（run_20260812_daily，窗口 2026-08-01..08-12）：7 只 × none+forward 各 8 行落 `data/raw/stock_finance_data/price/2026-08-12/run_20260812_daily/`；窗口内 7 只 forward==none 逐行一致（无分红事件）；600346.SH 当日已随 init 全量入库不在本批。
- **指数降级处理**：yahoo 000300.SS period=5d 返回 degenerate 行（O=H=L=4716.97 占位，C=4690.92 正确）校验冲突拒收；显式区间 EMPTY_DATA、period=1mo 截断 07-17、重试一次同结果（按 skill 失败处理记录 _meta.errors）。**兜底**：08-12 OHLC 取 gildata 指数日行情（O 4660.47/H 4700.43/L 4657.69/C 4690.92，与 yahoo C 交叉一致；08-11 库内 bar 与 gildata 逐字段一致已验证 Date 标记惯例），手工构造 yahoo 格式行 `000300.SS_fixed.csv` 入库 index_bars（全程 _meta 留痕，未用旧数据冒充）。
- daily 2026-08-12（revision=3）全池 **ok=8 全 complete**：**南航 T1 触发**（收 5.07 ∈ [4.95,5.20]，优先级 3）；海天距 T1 下沿 36.40 还差 0.4%（优先级 4 观察）；珀莱雅距 T2 上沿 57.60 还差 2.2%（优先级 4）；恒力卡生效次日无触发（17.75 高于 T1 上沿 8.3%）；埃斯顿收 34.71（-3.9%，跌回右侧触发位 36.00 下方，状态机 idle）。
- 今日收盘：珀莱雅 58.88 / 海天 36.24 / 平安 52.60 / 埃斯顿 34.71 / 紫金 33.58 / 南航 5.07 / 豫光 13.78 / 恒力 17.75；沪深300 4690.92（+0.58%）。
- cron 仍未重建（本会话 CronList 为空），每日增量当前靠手动触发，待用户确认。

## 2026-08-12（earnings-surge-screener：创新药板块筛选）

- 沿用石油石化同构三轨流程，产出 `reports/screening/2026-08-12-创新药.md`，定级 **结构性主线（5/8）**：命中密度 2（海思科/荣昌生物/石药创新/贝达 ≥4 家独立命中）+ 业绩一致性 1（头部三家为 license-out 一次性首付款驱动，卖方 2027E 一致预期 -12%/-76%/数据冲突；持续放量型仅贝达、翰宇）+ 周期位置 1（政策回暖明确——2026-04 价格形成机制意见 2-3 年价格稳定期、医保初审通过率 92%、商保目录第二年；但 license-out 2025 已爆发 157 笔/1356.55 亿美元 +161%，高基数第二年，阳光诺和预亏示范 BD 节奏波动）+ 行情确认度 1（CS 创新药 YTD +5.3%/创新药30 +9.7% vs 沪深300 -0.6%，温和跑赢非主升；恒生创新药 -4.0%；个股显著强于指数——荣昌 A +66.8%/贝达 +45.2%/翰宇 +44.8%）。
- **核心结论**：中报季创新药高增长含金量分化，产品销售持续放量型（贝达 ★★★ 2026E +95%→2027E +31%；翰宇扣非 +76~92%）> 一次性授权收入型（海思科首付 1.08 亿美元、荣昌艾伯维 RC148、石药创新 AZ 4.2 亿美元——按事件跟踪非业绩趋势，首付款年份 PE 失真）；CRO 传导线早期信号（泰格新签订单 95-105 亿 vs 84.2 亿、美迪西/睿智扭亏但估值抢跑）。
- **方法论发现**：双因子验证脚本关键词库为工业量价型设计，医药公告几乎不命中（30 家合格仅睿智医药/金城医药 2 家医药相关），Track A 以人工阅读公告原文替代；报告内已建议 skill 增补"对外授权/首付款/里程碑/新药获批"关键词组（未改动 skill，待人工决定）。
- 数据落盘：`data/raw/gildata/announcement/2026-08-12/pharma_*.csv`（5 组）、`stock_selection/2026-08-12/pharma_b1/b3`、fin_query 6 个（index_ytd/consensus_1/2/ytd_1/2，其中估值查询两次 PARAMETER_ERROR 实体超限后拆句成功）；核验 `/tmp/pharma_verified.csv`。
- 衔接纪律：跟踪清单（首选贝达、次选翰宇/泽璟、事件跟踪海思科/荣昌 H、观察泰格/美迪西/石药创新）不自动进系统，待人工确认后走 watchlist → stock-collect → 出卡 draft → 激活。

## 2026-08-13（天赐材料 002709.SZ 采集入库）

- 第 10 只观察股（电解液龙头，创新药/石化筛选线外人工指定）。watchlist.yaml 已由人工加好，`db seed` 导入（active=9）。
- 全量初始化（run_init_002709，data/raw/**/2026-08-13/）：行情 none+forward 各 726 行（2023-08-14..2026-08-12，区间 1094 天未超来源 3 年上限，单次采全）；利润表 9 期请求（3 年报 + 最近 8 个季报期，较恒力先例多采 20240930 用于 2025Q3 同比）——FY2023 归母 18.91 亿 / FY2024 4.84 亿 / FY2025 13.62 亿 / 2026Q1 16.54 亿（同比 +1006%，低基数+电解液涨价）；**2026 中报未披露返回全空行，文件删除按 §2.5 记缺失**（仅 07-10 业绩预告）；forecast 快照（FY1 66.74 亿 +390.0% / FY2 78.54 亿 / FY3 91.87 亿，PE(LYR) 59.06、PB(MRQ) 4.22）；公告 1 年 249 条（要点：07-10 半年度业绩预告、08-04 中期利润分配+07-21 控股股东中期分红提议、04-22 2025 年度及特别分红权益分派、03-28 H 股发行上市进展、07-04 南通天赐终止 24.3 万吨项目）；yahoo get_stock_info 股本快照 2,038,561,744 股（与 gildata 总股本 20.39 亿一致，交叉核对件 `data/raw/gildata/fin_query/2026-08-13/share_check_002709.csv`）。
- **股本疑点（已 NOTE）**：天赐转债（127073）2025Q4 集中转股，总股本 2025-09-30 19.14 亿 → 2026-03-30 20.39 亿（+6.5%）；单点快照假设在 2025-11 前高估股本，该段 pe_ttm 存在口径偏差（details_json 假设已标注，同恒力回购情形待 get_stock_actions 细化）。
- 入库：daily_bars 726 行 + 财报 8 期（available_at 入库降级）+ forecast + 公告 249 条（adapter 别名修复后整批成功，event_symbols 经文件名关联 249 行）；股本经 load_share_snapshot 写 share_capital_events（effective_at=2023-08-14，sce_id=9）。
- 复权因子全量重建（version_id=14）：5 个平台段（切换日 2024-04-29 / 2025-05-22 / 2026-01-12 / 2026-04-29，均为小额分红，corporate_actions 无记录）；周线重建 153 周。
- daily 2026-08-12（revision=4）：**全池 ok=9**。002709.SZ ok（指标 daily=726 weekly=153，吸筹 ok，卡片信号 no_active_card 待激活），报告 degraded P5 revision=1；其余 8 只库内已有 08-12 数据故仍 ok。现价 39.46，PE(TTM) 28.06（TTM 归母 28.67 亿），处 p75 上方（p5/p50/p95=10.73/15.92/36.67）。
- 吸筹模块：全历史 726 行 signal_facts 全 idle、0 触发（参数第一版默认值待人工核对期，同珀莱雅/恒力）。
- card_inputs 底稿 `cards/002709.SZ/inputs_2026-08-12.json`；一致预期裂口 FY1 +390% vs 2026Q1 实际 +1006%（-615.7pp，预期显著落后于已兑现业绩）。
- UI 测试硬编码股票总数 9→10（test_ui_queries×3 / test_ui_api×3 / test_ui_app×1，注释 8CN+1HK→9CN+1HK）；另 test_list_stocks_sort_and_pagination 的 pe_ttm 升序 NULL 排前断言由 0700.HK 改为 002709.SZ（NULL 并列按 symbol 字典序，恒力 600346.SH 字典序在 0700.HK 后未触发，002709.SZ 在其前）。`uv run pytest -q` **277 全绿**。

## 2026-08-13（天赐材料估值排期卡 draft）

- fred-valuation-card-skill 流程，底稿 `cards/002709.SZ/inputs_2026-08-12.json` → 卡 MD + draft JSON → `create-draft` 入库 **002709SZ_0e757373**（draft-only，激活待人工）。
- **锚类型**：静态折算 PE 刻度（pe_static_scale，同恒力/紫金/豫光卡）——六氟磷酸锂强周期，2023-2024 盈利崩塌期当时 PE 带 9.2-24.8 失真；体系已切换为周期复苏定价（底部 PE 14.3→21.5 上移），锚设在新体系内。
- **关键判断**：① 盈利强反转但存 Q2 分歧：Q1 16.54 亿（+1006%）、H1 预告 27-30 亿（+908~1020%），Q2 单季隐含环比 -18%~-37%（碳酸锂涨价+六氟 Q2 均价回落，公司 07-10 投关记录自证）；裂口 -615.7pp 有利方向，但 H1 中值仅完成券商 FY1 的 43%（卖方隐含 H2 环比 +35%），中性 EPS 3.00=FY1 打 92 折（61.2 亿），悲观 2.60（H2 环比 -14%），极悲 2.20（六氟回落 2025 水位）；② PE 18/14/10（中性 14=2025-08-11 低点静态 13.7，悲观 10=2024 底部带+p5 10.7 下方，仅极悲启用）；③ 三档 35.0-36.5（7 月底部区上半，08-03 已触及 35.94）/ 28.5-30.0（2025-09-22 大底 29.73±1.5%）/ 21.0-22.5（深于 2025-09 底 -24~29% 双杀定价），证伪线 21.00；④ 现价 39.46 ≈ 中性×PE 13.2，市场按中性情景定价未给复苏溢价，高于 T1 上沿 8.1%；⑤ 胜率 50-60/60-70/65-75，T1 Kelly 2.4%（边际期望注，执行需六氟价格企稳/Q3 排产证据推胜率上沿），T2/T3 Kelly 11.7%/16.0%，修复目标 54.0；⑥ 波段仓适用：箱体 35.5-40.5（约 4 周、下沿两次上沿三次试探），买 35.5-36.5/卖 39.5-40.5/证伪 34.70，上限 15%（与 T1 重叠分开记账）；右侧触发 41.00/止损 37.00，收复 44.34（07-10 预告日高）为更强确认；⑦ 衰竭锚 2025-09-22 前低 29.73 已 episode_ended，跌破 34.75 系统将以 07 月下跌段重算，执行 T2/T3 以届时实数为准；⑧ **吸筹核查（用户指定）**：系统 accumulation 726 日全 idle 0 触发（量比 0.79 未达 2.0），人工观察 8 月缩量横盘有初步卖压衰竭迹象但不构成系统信号，筹码分布无源；⑨ 尾部风险：转债转股摊薄（2025Q4 已 +6.5% 股本）+ 长协锁量不锁价 + 碳酸锂成本，均列入复核触发器。
- draft JSON schema 校验：swing_box 键名按 `scripts/signals/cards.py` 约定（box_invalidation，无 position_cap_pct——仓位上限记 input_snapshot.notes）。
- `uv run pytest -q` **277 全绿**。

## 2026-08-13（晚，盘后例行 daily 2026-08-13，全池 ok=9）

- 增量（run_20260813_daily，窗口 2026-08-05..08-13）：9 只 × none+forward 各 7 行落 `data/raw/stock_finance_data/price/2026-08-13/run_20260813_daily/`；窗口内 forward==none 逐行一致（无除权）；因子继承修复（14a1564）后常规窗口文件即可，无需 08-10 的 forward_3y 合并绕过。
- 指数：yahoo 000300.SS 单行（Date 标记惯例 2026-08-12T16:00Z=08-13）O 4716.97/H 4727.23/L 4661.88/C 4663.95，**gildata 指数日行情逐字段交叉一致**（核对件 data/raw/gildata/fin_query/2026-08-13/index_check_000300.csv），直接入库无 _fixed 兜底；沪深300 收 4663.95（-0.57%）。
- daily 2026-08-13（revision=1）**ok=9**：002709.SZ degraded(no_active_card，draft 待人工激活) 属预期，其余 8 只 complete。
- 决策点/观察：**南航 T1 触发**（收 5.05 ∈ [4.95,5.20]，连续第二日在区内，P3；衰竭信号 divergence+duration 双活跃达 min_active=2）；海天距 T1 下沿 36.40 差 0.5%（P4）；珀莱雅距 T2 上沿 57.60 差 1.6%（P4，自上方回落逼近）。
- 今日收盘：珀莱雅 58.53（-0.59%）/ 海天 36.20（-0.11%）/ 平安 52.80（+0.38%）/ 埃斯顿 34.67（-0.12%）/ 紫金 32.21（-4.08%，周内第二次大跌）/ 南航 5.05（-0.39%）/ 豫光 13.17（-4.43%）/ 恒力 17.16（-3.32%，高于 T1 上沿 4.6% 逼近中）/ 天赐 38.81（-1.65%）。
- 周期任务触发方式仍为手动（cron 未重建），同 08-12 记录待用户确认。

## 2026-08-13（天赐材料卡激活 + 08-13 盘后批衔接）

- 人工确认激活 002709SZ_0e757373，effective_from=2026-08-12；生成卡片 MD 并刷新 current.md。
- 同日重跑 daily 2026-08-12（§9.5）：卡片信号当日生效——daily_watch/right_side 由 no_active_card 转 ok；tier_triggered/tier_proximity/box_position/falsification_breach 出数（收 39.46，mid_box，无档位触发）。
- **发现并行盘后批**：`data/raw/**/2026-08-13/run_20260813_daily/`（collected_at 2026-08-13 20:20+08:00，覆盖 9 只+002709.SZ，窗口 08-05..08-13，forward==none 逐行一致，沪深300 收 4663.95 经 gildata 交叉一致无需 _fixed 兜底）与 daily_2026-08-13 已在激活前由本会话之外的渠道完成（非本 agent、非 cron——CronList 为空）；校验为收盘后采集、全日 bar 有效（非盘中半成品），08-13 bar 已入库（002709.SZ 收 38.81）。
- 因 08-13 批跑在激活前，002709.SZ 当日无卡片信号；激活后重跑 daily 2026-08-13（§9.5）：卡片信号补齐——收 38.81，box_position=mid_box，tier_proximity inactive（距 T1 上沿 36.50 约 6.3% > 3% 阈值），tier_triggered inactive（衰竭信号活跃 0 项），证伪线监测生效（breach_threshold 20.79）。
- `uv run pytest -q` **277 全绿**。至此 9 只 A 股全部持卡生效（002709.SZ 第 9 张激活卡；watchlist 共 10 只含 0700.HK 待港股源）。

## 2026-08-13（牧原股份 002714.SZ 加入 watchlist）

- 生猪养殖龙头，第 11 只观察股（人工指定，未走筛选线）。watchlist.yaml 加行 + `db seed` 导入（active=11，含 fixture 侧 0700.HK）。
- 仅入池未采集：002714.SZ 暂无行情/财报数据，日报该股票 incomplete（§2.5 预期行为），待人工指示后走全量采集 → 出卡 draft 流程。
- UI 测试硬编码股票总数 10→11（test_ui_api×3 / test_ui_queries×3 / test_ui_app×1，注释 9CN+1HK→10CN+1HK）；test_ui_queries.py:341 的 `total == 10` 为 pipeline_runs 计数与股票数无关，未动。`uv run pytest -q` **277 全绿**。

## 2026-08-14（牧原股份 002714.SZ 采集入库）

- 承接 08-13 入池（active=11），走恒力/天赐同构全量初始化（run_init_002714，data/raw/**/2026-08-14/）。实际日期 2026-08-14（周五，盘中采集），最新完整交易日 2026-08-13。
- 行情 none+forward 各 727 行（2023-08-14..2026-08-13，区间未超来源 3 年上限单次采全）；利润表 9 期请求（3 年报 + 8 季报期口径，同天赐含 20240930）——FY2023 归母 **-42.63 亿**（亏损）/ FY2024 178.81 亿 / FY2025 154.87 亿 / 2025Q1 44.91 亿 / 2025H1 105.30 亿 / 2025Q3 累计 147.79 亿 / **2026Q1 -12.15 亿（同比 -127.05%，猪价下行转亏）**；**2026 中报未披露返回全空行，文件删除按 §2.5 记缺失**（07-11 已发业绩预告：归母预亏 57-67 亿，同比 -154%~-164%）。forecast 快照（FY1 57.76 亿 -62.7% / FY2 272.42 亿 / FY3 258.35 亿，PE(LYR) 14.97、PB(MRQ) 2.69）。
- **公告接口故障（NOTE）**：stock_finance_data get_stock_announcement 今日全空——002714.SZ 1 年窗口与近月窗口均 EMPTY_DATA，同刻对昨日已验证的 002709.SZ 1 年窗口同样 EMPTY_DATA，判定来源端临时故障（非个股无公告），按 skill 失败处理记 _meta.json errors 跳过，未用旧文件冒充。要点经 gildata_announcement_data 兜底核实（`data/raw/gildata/announcement/2026-08-14/muyuan_ann_check.csv`，不入库）：07-11 半年度业绩预告、2026 年 1-5 月销售简报（均价 12.57→9.45→9.80 元/公斤，同比 -17%~-36%）、2026-02-06 H 股上市 27,395.14 万股 + 03-10 超额配售 3,627.17 万股、04-15 业绩说明会（2026 出栏指引 7,500-8,100 万头、分红比例由 20% 提至 40%、屠宰板块 2025 首次年度盈利）。**公告事件流待接口恢复后补采入库**。
- yahoo get_stock_info 股本快照 sharesOutstanding **5,462,773,044**（A 股口径），gildata 交叉核对（`data/raw/gildata/fin_query/2026-08-14/share_check_002714.csv`）：总股本 577,299.61 万股 = A 股 546,277.30 万 + **H 股 31,022.31 万**（2026-02 H 股发行所致）。**股本疑点（已 NOTE）**：单点快照为 A 股口径且固定于 2023-08-14 起整个区间，2026-02 后总股本（含 H）实际 +5.7%，PE(TTM) 分母口径偏小于含 H 总股本（与卖方 PE 口径差异约 5.7%）；details_json 单点假设已标注，同天赐转债情形待 get_stock_actions 细化。
- 入库：daily_bars 727 行 + 财报 8 期（available_at 入库降级）+ forecast；股本经 load_share_snapshot 写 share_capital_events（effective_at=2023-08-14，sce_id=10）。
- 复权因子全量重建（version_id=15）：**5 个平台段，切换日 2024-12-30 / 2025-06-26 / 2025-10-16 / 2026-05-27**，f=0.939133→0.959126→0.972070→0.989139→1.0，幅度 1.1%~2.1% 均为现金分红级，**无送转/转增大跳变**（区间内总股本变动仅转债微量转股与 H 股发行，均不改变 A 股价格口径）；corporate_actions 无记录已 NOTE。周线同事务重建 153 周。
- daily 2026-08-13（--raw-dir 本批）：**全池 ok=10**（0700.HK 无港股源仍不在门禁内）。002714.SZ ok（指标 daily=727 weekly=153，吸筹 ok，卡片信号 no_active_card 待出卡），报告 degraded P5（no_active_card §2.5 预期）；现价 40.15，PE(TTM) 22.42（TTM 归母 97.81 亿 = FY2025 154.87 - 2025Q1 44.91 + 2026Q1 -12.15），处 p5–p50（p5/p50/p95=20.03/23.32/28.41）。
- 吸筹模块：全历史 727 行 signal_facts 全 idle、0 触发（近 60 日 43 个交易日同；参数第一版默认值待人工核对期，同珀莱雅/恒力/天赐）。
- card_inputs 底稿 `cards/002714.SZ/inputs_2026-08-13.json`；一致预期裂口 FY1 -62.7% vs 2026Q1 实际 -127.05%（+64.3pp，卖方预期未充分定价 H1 亏损深度）。`uv run pytest -q` **277 全绿**。

## 2026-08-14（牧原股份估值排期卡 draft）

- fred-valuation-card-skill 流程，底稿 `cards/002714.SZ/inputs_2026-08-13.json` → 卡 MD + draft JSON → `create-draft` 入库 **002714SZ_e8fd9ea7**（draft-only，激活待人工）。
- **锚类型**：**price_band 价格底带（首张非 PE 系锚卡）**——2026 猪周期亏损年（H1 预告亏 57-67 亿）PE 失效，TTM 97.81 亿随中报披露将骤降；两轮亏损周期底部 2023-10-20 的 31.44 与 2026-06-25 的 31.81 跨周期验证底带 31-33；PB(MRQ) 2.69 仅快照参考。盈利情景以 2027E 反转年为定价基准。
- **关键判断**：① 盈利轨迹：FY2023 -42.63 亿 → FY2024 +178.81 亿 → FY2025 +154.87 亿 → 2026Q1 -12.15 亿、H1 预亏 57-67 亿（猪价 10.4 元/kg -28%）；② **裂口 +64.3pp 不利方向且性质严重**：券商 FY1 +57.76 亿隐含 H2 盈利 115-125 亿与 H1 预亏直接矛盾，快照滞后于 07-11 预告，按纪律弃用 FY1；FY2 272 亿隐含强反转，中性打 73 折=200 亿（EPS 3.66），悲观 120 亿（2.20），极悲 40 亿（0.73）；③ PE 14/11/9（2024-25 盈利大年低点前瞻 PE 10.5-14 样本内依据）；④ 三档 35.5-37.0（2024-09 底 35.02/2025-03 底 36.11）/ 31.5-33.5（两轮大底 31.44/31.81+06-29 系统低点 33.11，中性×悲观 PE≈32.9）/ 27.0-29.0（深于 3 年最低 -8~14% 双杀+PB 1.8-1.9），证伪线 27.00；⑤ 现价 40.15 ≈ 2027E 中性×PE 11.0，市场已定价中性温和反转未留折扣，高于 T1 上沿 8.5%；⑥ 胜率 50-60/60-70/65-75，Kelly 4.8%/12.1%/15.9%，修复目标 51.2；⑦ **衰竭信号当前 2 项活跃（duration 31 周 + no_new_low_3w holding）meets_min=true——首张出卡时信号门槛已满足的卡**；跌破 33.11 则 no_new_low 失效重计；⑧ 波段仓不适用（-37% 下跌后反弹段），右侧触发 42.50/止损 38.50，更强确认 45.0-45.8；⑨ 吸筹模块 727 日全 idle（量比 0.6-0.8），与衰竭 2 项活跃并存不矛盾（吸筹阈值更苛刻）。
- 脚本机械档位 T3=6.6-7.1 为极端 EPS×PE 笛卡尔积失真，按恒力/天赐先例弃用并结构锚定（matrix_source 已注明）。
- 猪周期位置：猪价谷底 10% 区间震荡 22 个月+，能繁 2026Q2 末 3780 万头环比 -3.2% 逐月加速去化，10-12 个月传导指向 2026 末-2027 供应收缩；核心高频变量=月度销售简报+能繁存栏月报。
- `uv run pytest -q` **277 全绿**。

## 2026-08-14（晚，盘后例行 daily 2026-08-14，全池 ok=10）

- 增量（run_20260814_daily，窗口 2026-08-06..08-14）：9 只 × none+forward 各 7 行；窗口内 forward==none 无除权。指数 yahoo 单行收 4665.88（+0.04%），gildata 交叉一致后入库。
- **首跑 P1 误报（已当日修正）**：牧原股份 002714.SZ 当日新入池（draft 卡 002714SZ_e8fd9ea7 待激活），采集沿用了旧 9 只清单漏采 → 门禁判 suspended（"交易日无 bar+基准有 bar→停牌"）P1。补采窗口（08-14 正常交易，收 39.29 -2.14%）后重跑，revision=2，ok=10。**教训：每日采集清单必须以当日 watchlist 表为准，不复用前日清单**。
- 决策点/观察：**南航 T1 触发连续第三日**（收 5.02 ∈ [4.95,5.20]，P3）；**珀莱雅收 57.18 进入 T2 价区 [54.70,57.60]，但同锚点活跃衰竭信号仅 1 项 < 2 → 档位触发待确认、不触发**（P5 附注）；海天 35.82 距 T1 下沿 36.40 差 1.6%（P4）。
- 天赐材料卡 002709SZ_0e757373 已激活（effective 2026-08-12，人工操作），本日报告 complete。
- 今日收盘：珀莱雅 57.18（-2.31%）/ 海天 35.82（-1.05%）/ 平安 51.75（-1.99%）/ 埃斯顿 34.75（+0.23%）/ 紫金 32.53（+0.99%）/ 南航 5.02（-0.59%）/ 豫光 13.05（-0.91%）/ 恒力 17.13（-0.17%）/ 天赐 39.47（+1.70%）/ 牧原 39.29（-2.14%）。

## 2026-08-14（晚续，牧原股份卡激活）

- 人工指令激活 002714SZ_e8fd9ea7，effective_from=2026-08-14；生成卡片 MD 并刷新 current.md。同日重跑 daily（§9.5，revision=3）：卡片信号当日生效，daily_watch/right_side 转 ok，单股报告 complete P5。现价 39.29 高于 T1 上沿 37.00 6.2% 无档位触发；右侧触发位 42.50（突破线 42.92），证伪线 27.00，next_review 2026-08-31。

## 2026-08-14（晚续 2，公告补采受阻 + 确定性事件研究上线）

- **公告补采失败（来源端故障持续）**：8 只缺公告（603605/603288/601318/002747/601899/600029/600531/002714）1 年窗口补采，stock_finance_data get_stock_announcement 全部 EMPTY_DATA；对照昨日已验证的 002709.SZ 近月窗口同样 EMPTY → 故障持续（与上午 run_init_002714 结论一致）。失败明细记 `data/raw/stock_finance_data/announcement/2026-08-14/run_20260814_ann/_meta.json`，待接口恢复后重采。gildata_announcement_data 为自然语言片段检索，不适合确定性入库。
- **新模块 `scripts/signals/event_study.py`（设计 §5.5 确定性事件研究，无 LLM）**：对 events⋈event_symbols 公告事件算 T+1/T+5 复权收益与 000300.SH 超额，写 `event_assessments`（assessment_version='event_study_v1'、model='deterministic'，评价列全 NULL 不冒充 LLM）。口径：日历=trading_calendar(CN) 权威开市日；基准价=available_at 前最后一个开市日复权收盘；指数开市日缺 bar → degraded 不静默顺延；个股开市日无 bar → suspended；终点 > 数据截止 → pending（到期重跑自动补齐）；degraded/pending 行重跑重算，完整行跳过。CLI `uv run python -m scripts.signals.event_study [--symbol S|--all]`，未接入 daily/report（下轮再议）。
- **000300.SH 指数回补 1 年**（run_20260814_index_backfill，yahoo period=1y，224 行入库）：index_bars 228 行（2025-08-14 起）。**发现数据空洞 2026-07-20..08-07**（yahoo 区间补采仅回 07-17 单行；stock_finance_data 000300.SH/399300.SZ × none/forward 均 EMPTY）——非节假日（对照 trading_calendar），记 _meta known_gap。
- 实跑 370 条公告事件（002709.SZ 249 + 600346.SH 121）：**ok 356、degraded 14**（均为 07-18..08-07 指数空洞：bench_base_missing 12 / bench_missing_t5 2，回补后自动转正）、suspended 0；evt_45cf4804175f0757（002709 08-12 公告）t5=2026-08-19 pending 待到期。首轮曾用有洞日历产出的 370 行已全部 DELETE 按新口径重算。
- 测试 `tests/test_event_study.py` 16 项（时点语义/手算数值/停牌/空洞 degraded/pending 重算/LLM 版本行不受影响/幂等）。`uv run pytest -q` **293 全绿**（277+16）。

## 2026-08-15（公告接口恢复探测：仍故障）

- 重试 8 只补采：603605.SH 1 年窗口 EMPTY_DATA；对照 002709.SZ 近月窗口、603605.SH 近月窗口均 EMPTY → 接口故障第 3 日持续。明细记 `data/raw/stock_finance_data/announcement/2026-08-15/run_20260815_ann/_meta.json`，补采继续挂起。

## 2026-08-15（公告补采改道天眼查：8 只 1 年公告全量入库 + 事件研究重跑）

- **背景**：stock_finance_data get_stock_announcement 故障第 3 日（当日早盘探测仍 EMPTY，见上条），改走天眼查「上市信息-上市公告」接口（open.api.tianyancha.com/services/open/stock/announcement/2.0，探测记录 `docs/probe_20260815_tianyancha.md`）。
- **采集**（`data/raw/tianyancha/announcement/2026-08-15/run_20260815_ann/`，窗口 2025-08-14..2026-08-14，pageSize=20 按 time 倒序，停采=当页末行越过下界，跨界行保留）：603605.SH 9 页 180 行 / 603288.SH 15 页 300 行 / 601318.SH 10 页 200 行 / 002747.SZ 12 页 240 行 / 601899.SH 20 页 400 行 / 600029.SH 13 页 260 行 / 600531.SH 11 页 220 行 / 002714.SZ 17 页 340 行，合计 107 文件 2140 行，零请求失败；_meta.json 记录窗口与逐只页数行数。**A+H 混排**：6 只含 HK.xxxxx 行（002714/603288/601318/002747/601899/600029），600531/603605 纯 A；002747（2026-03）与 002714（2026-02）为窗口内新上市 H 股。
- **新 adapter `scripts/adapters/tianyancha.py`**：ticker 从文件名 stem 正则取；uuid→source_external_id 幂等（event_id=sha256 前 16 位）；published_at=公告日 00:00+08 转 UTC，available_at=下一开市交易日 00:00 本地（复用 common 日历，缺失降级 +1 天记 incomplete）；announcementType→summary、ossUrl→canonical_url。缺 title/time 或 stock_code 指向不同 A 股 → conflict 整批回滚；HK 行行级 skipped+note。ROUTES 注册 `(tianyancha, announcement)`，未接入 daily.py。
- **入库**：inserted=1339 / skipped=801（全部 HK 行）/ conflicts=0，exit 0。SQL 验证：8 只分股计数 240/141/144/220/93/167/154/180 合计 1339，source_external_id 无重复，原 stock_finance_data 370 条未动。
- **事件研究重跑**（`event_study --all`，run event_study_ALL_20260815T061742Z）：公告事件累计 1709 条（370+1339），写入 1354 行（含 pending 重算 15），跳过已完成 355；状态分布 **ok 1230 / degraded 124**（degraded 主因 2026-07-20..08-07 沪深300 指数空洞，回补后自动转正）。
- 测试：`tests/test_adapters.py` 新增 5 项天眼查用例（字段映射/uuid 幂等/缺标题 conflict/错公司 conflict/HK 行跳过）。`uv run pytest -q` **298 全绿**。
- 文档：database_schema.md events 来源加 tianyancha；新建 probe 记录。stock_finance_data 公告接口恢复后不回采重叠窗口（uuid 口径不同会重复入库，届时以 source 区分即可，如需合并再议）。

## 2026-08-15（event_study 接入 daily 管线 + report 消息评价文案修正）

- **daily.py 接入池级事件研究阶段**（§8.1 步骤 6 确定性部分）：逐股循环之后、报告生成之前，`with conn:` 事务内调 `run_event_study(conn, run_id=f"{run_id}_event_study")`（全池）；成功记摘要 notes（写入/pending 重算/跳过计数 + ok/suspended/degraded 分布），异常记 "event_study degraded: ..." 不阻断报告（§2.2 第 3 类派生数据）；_record_stage 记 daily 台账 stage='event_study'（success/degraded），与 run_event_study 自记 run 双记录并存（同 accumulation 模式）。模块 docstring 同步步骤说明，CLI 不变。
- **report.py §7 文案修正**：原「event_assessments 未接入（LLM 阶段不在本批）」过时，改为如实表述——LLM 消息评价（D3）未接入；确定性事件研究 event_study_v1 已接入，附该股库内 event_assessments 状态计数（ok/suspended/degraded 各 N + pending 终点计数，按 symbol ⋈ event_symbols 过滤）。
- **测试 +3**：test_daily.py 新增 test_event_study_stage（预埋事件+行情+基准指数跑 run_daily，断言 event_assessments 落 event_study_v1 行且数值正确——base 08-03 / T+5 08-10 / excess 非空，daily 台账与自记 run 双 pipeline_runs）与 test_event_study_failure_not_blocking_report（monkeypatch 抛 RuntimeError：不阻断报告/汇总，阶段记 degraded，事务回滚无残留）；test_report.py 七段结构测试加新文案断言 + 新增 test_message_section_event_study_counts（库内计数与 pending 终点计数上报告）。`uv run pytest -q` **301 全绿**。
- **真实库端到端**：`daily --date 2026-08-14`（无 --raw-dir，重跑属 §8.3/§9.5 设计行为，report revision=4）：全池 ok=10 exit 0；event_study 阶段 success——公告事件 1709 条，写入 145 行（全部 pending/degraded 重算），跳过已完成 1564 行，分布 ok 21、degraded 124（仍为 07-20..08-07 指数空洞）；pipeline_runs 四阶段 calendar/event_study/report/summary 全 success + 自记 run daily_2026-08-14_event_study。**evt_45cf4804175f0757（002709 08-12 公告）仍 ok + t5 pending（T+5=2026-08-19 未到，预期）**，t1 已落定（ret -1.65%、超额 -1.07%）。报告 §7 新文案生效（如 002714.SZ：库内 240 条，degraded 21 / ok 219，pending 终点 1 条）。
- **event_assessments 全库分布**：ok 1585 / degraded 124（合计 1709 闭合）。system_design.md §8.1 步骤 6 括注更新（LLM 评价仍不含；确定性事件研究已接入 daily）。

## 2026-08-15（全池消息面分析 draft ×10）

- 先生成确定性底稿 `reports/{symbol}/message_brief_2026-08-15.md` ×10（一次性脚本从 market.db 计算，脚本已删）：事件概览/近 90 天公告清单带 T+1/T+5 超额/按类型分组统计（样本<5 标注）/显著异动 top5/缺口声明。抽查 600346.SH、603605.SH 数字与 SQL 直查一致。
- 再逐股产 `reports/{symbol}/消息面分析_20260815_draft.md` ×10（LLM 只消费底稿+卡片，不产数字；均标 draft 待人工核对）。主 agent 抽查 002714.SZ：业绩预告 +2.86%/+9.70%、增持计划 +6.03%/+10.49%、全样本 T+1 均值 -0.70%/胜率 19.2%，SQL 复核全对。
- 共性发现：多只公告首日偏弱 T+5 回摆（牧原/平安同源形态）；degraded 124 条（000300.SH 07-20..08-07 空洞）恰好盖住 7 月下旬密集公告窗口（牧原 7 月简报、恒力第五期回购、豫光增发获准、紫金终止收购 Allied Gold 等），指数回补后重跑 event_study 自动转正。

## 2026-08-16（消费领域全扫描筛选：earnings-surge-screener 三轨+阶段二）

- 用户定范围：全消费扫描、只做研究不入池。按 skill 执行：子行业格局（web+gildata，/tmp/consumer_landscape_20260815.md）→ Track A（8 组检索 148 候选→52 合格→消费仅 2 家入池：益生/拉芳）→ Track C 闸门（吸血环境确认：TMT 成交占比 99%+ 分位、主线 vs 消费 YTD 差 44-57pct）→ Track C 筛选（C1 洽洽/有友；C2×8；C3 排除×6 含五粮液现金流背离；C4×3）→ 板块定级（景气验证中×3：调味品/大众食品、白羽鸡子链、乳制品/软饮料）→ Track B（★★ 圣农/洽洽/中炬/天味 + ★×9）→ 阶段二三维前瞻（23 家总池）。
- 最终报告 `reports/screening/2026-08-16-消费.md`：**S级空缺；第一梯队唯一=圣农发展**（YTD -1.35% 滞涨 × 2027E +35% × 次年 PE 11.5x）；C1=洽洽、有友；跨轨冲突贝泰妮以 C3 为准。最近验证窗口=8 月下旬中报密集期。证据 CSV 落 data/raw/gildata/**/2026-08-1{5,6}/。
- SKILL.md 修正：gildata_announcement_data 实测也卡 ≤3 实体上限（数据源路由节已补注）。
- 遗留：跟踪清单 3 家核心候选的证伪信号转 cron 提醒待用户确认；机构拥挤度核验、石头 OCF 等待补查项见报告第十节。

## 2026-08-16（watchlist 入池 3 只消费候选）

- 圣农发展 002299.SZ / 洽洽食品 002557.SZ / 有友食品 603697.SH 加入 config/watchlist.yaml（来源：reports/screening/2026-08-16-消费.md 第一梯队+C1），代码经 stock_finance_data get_stock_info 核实；`db seed` 后 watchlist=13 只全 active。
- 测试适配：UI 测试硬编码池规模 11 改为动态 `EXPECTED_STOCK_TOTAL`（tests/ui_seed.py，yaml 股数+补种 0700.HK）；排序并列断言最小 symbol 由 002709.SZ 改 002299.SZ（池扩大的如实结果）。`uv run pytest -q` **301 全绿**。
- 下一步（待指令）：stock-collect 采集 3 只历史数据 → fred-valuation-card-skill 出卡 draft → 人工激活后纳入每日管线。

## 2026-08-16（圣农发展 002299.SZ 采集入库）

- 承接入池（watchlist=13），走牧原同构全量初始化（run_init_002299，data/raw/**/2026-08-16/）。周日采集，最新完整交易日 2026-08-14。
- 行情 none+forward 各 726 行（2023-08-16..2026-08-14，单次采全）；利润表 9 期请求（3 年报 + 8 季报期口径）——FY2023 归母 6.64 亿 / FY2024 7.24 亿 / FY2025 13.80 亿 / 2026Q1 2.53 亿（同比 +71.4%）；**2026 中报未披露返回全空行，文件删除按 §2.5 记缺失**（08-12 仅见 7 月销售简报，中报预约披露未到期）。forecast 快照（FY1 12.57 亿 -8.9% / FY2 17.10 亿 / FY3 20.14 亿，PE(LYR) 14.53、PB(MRQ) 1.75）。
- 公告走天眼查（stock_finance_data 公告接口故障持续，勿用）：福建圣农发展股份有限公司 × 7 页 = 140 条（2026-08-12..2025-07-19，首页 stock_code=002299 核对通过，分页至 time<2025-08-16 止），纯 A 股无 H 股混入。要点：月度销售简报逐月、2025 限制性股票激励（10-29 草案 / 12-17 授予 / 2026-01-26 登记完成）、2025 前三季度分红（12-09 实施）、2025 年报+2026Q1 业绩预告（04-13）、回购股份注销（2025-09-25 / 2026-06-30）。
- yahoo get_stock_info 股本快照 sharesOutstanding **1,242,770,322**（implied 同值，纯 A 股）；get_stock_actions 14 行（13 笔现金分红 + 2011-02-23 一笔 2.0 送转），经 ingest 入 corporate_actions 15 行（分红拆股分行）。
- 入库：daily_bars 726 + 财报 8 期（available_at 入库降级）+ forecast 1 + 公告 events 140；股本经 load_share_snapshot 写 share_capital_events（effective_at=2023-08-16，sce_id=12，单点快照假设已标注）。
- 复权因子全量重建（version_id=17）：**6 个平台段，切换日 2024-04-26 / 2025-01-10 / 2025-05-23 / 2025-12-15 / 2026-05-21**，f=0.925527→0.943785→0.957857→0.970551→0.988575→1.0，幅度 1.2%~2.0% 均为现金分红级，**5/5 切换日与 corporate_actions 除权日对齐**（容差 2 交易日）；NOTE：2011-02-24 送转 2.0 倍计入 share_factor（在采集区间之前，区间内 share_factor 恒 1），已提示人工核对。周线同事务重建 154 周（末周 2026-08-14）。
- `uv run pytest -q` **301 全绿**。缺口：2026 中报未披露（§2.5 记缺失，披露后补采 20260630）。

## 2026-08-16（洽洽食品 002557.SZ 采集入库）

- 承接今日入池（3 只消费候选之一），走牧原同构全量初始化（run_init_002557，data/raw/**/2026-08-16/）。实际日期 2026-08-16（周日），最近交易日 2026-08-14。
- 行情 none+forward 各 726 行（2023-08-16..2026-08-14，未超来源 3 年上限单次采全）；利润表 9 期请求（3 年报 + 8 季报期口径，同牧原先例含 20240930）——FY2023 归母 8.03 亿 / FY2024 8.49 亿 / **FY2025 3.18 亿（同比 -62.5%）** / 2025Q1 0.77 亿 / 2025H1 0.89 亿 / 2025Q3 累计 1.68 亿 / 2026Q1 1.68 亿（同比 +117.8%，低基数反转）；**2026 中报未披露返回全空行，文件删除按 §2.5 记缺失**（07-14 已发 2026 年半年度业绩预告，见公告流；洽洽中报惯例 8 月下旬披露）。forecast 快照（FY1 6.58 亿 / FY2 7.77 亿 / FY3 8.82 亿，PE(LYR) 30.13、PB(MRQ) 1.76）。
- 公告走天眼查（stock_finance_data 公告接口故障持续，勿用）：洽洽食品股份有限公司 × 10 页 = 200 条（2026-08-08..2025-06-04，首页 stock_code=002557 核对通过，分页至 time<2025-08-16 止），纯 A 股无 H 股混入。要点：洽洽转债相关披露密集（回售、转股价调整/不下修、按季转股情况——股本缓增来源）、第七/九/十期员工持股计划、2025 年报+2026Q1（04-21）、2025 年度权益分派实施（2026-06-05，派 1 元）、实际控制人增持（2026-06-25 起）、2026 半年度业绩预告（07-14）。
- yahoo get_stock_info 股本快照 sharesOutstanding **505,855,256**（纯 A 股，洽洽转债存续转股致股本缓增，单点假设已标注）；get_stock_actions 首次调用报 API_CALL_ERROR(ticker check)，重试一次成功，17 行（2011..2026 分红史 + 2011/2012/2015 三笔送转），经 ingest 入 corporate_actions 20 行（分红/送转分行）。
- 入库：daily_bars 726 + 财报 8 期（available_at 入库降级）+ forecast 1 + 公告 events 200；股本经 load_share_snapshot 写 share_capital_events（effective_at=2023-08-16，sce_id=11，单点快照假设已标注）。
- 复权因子全量重建（version_id=16）：**5 个平台段，切换日 2024-06-14 / 2025-01-17 / 2025-06-20 / 2026-06-15**，f=0.874502→0.903457→0.912932→0.953846→1.0，均为现金分红级（1.0/0.3/1.0/1.0 元），**4/4 切换日与 corporate_actions 除权日对齐**（容差 2 交易日）；NOTE：2011/2012/2015 三笔送转（1.3/1.3/1.5）计入 share_factor（均在采集区间之前，区间内 share_factor 恒 1），已提示人工核对。周线同事务重建 154 周（末周 2026-08-14）。
- 缺口：2026 中报未披露（§2.5 记缺失，披露后补采 20260630）；stock_finance_data 公告接口仍故障，公告来源暂依赖天眼查。daily 全池由主流程统一跑，本批未单独执行。

## 2026-08-16（圣农发展 002299.SZ 估值排期卡 draft）

- 底稿 `cards/002299.SZ/inputs_2026-08-14.json`（行情 2026-08-14 / 财报 2026-03-31 / forecast 快照 2026-08-16）：现价 16.13（不复权），PE(TTM) 13.50≈p50，TTM 归母 14.85 亿 / EPS 1.1953。
- **锚类型 pe_scale（PE 历史带）**：刻度先移后稳——2023-08→2024-09 底部 PE 17.3→8.8 逐轮下移，2024-10/12 回升企稳 12.1-12.2，其后近 20 个月无新恐慌低点；锚设在企稳体系内。样本区间 2023-08-16~2026-08-14（726 日 24 个恐慌低点，§3.2 已标注）；2024-09 极端刻度 8.84 带全市场恐慌属性不作常规锚。
- **裂口 -80.3pp 有利方向**：券商 FY1 -8.9% vs 2026Q1 实际 +71.4%（券商严重滞后）；中性情景按实际趋势（月度销售收入 3-6 月 +19.1/+25.4/+21.3/+10.6%，供给收缩可见 2027Q1）设 1.20，不用券商 FY1；悲观 1.05 / 极悲 0.90。
- **机械档弃用**：build_schedule T1=15.0-15.6（中性×中性）与「≥中性×中性不建左侧仓」冲突，按天赐/牧原先例结构锚定（matrix_source 已注明）——**三档：T1 13.50-14.20（30%，悲观×中性≈13.65+前低 13.89±2%）/ T2 11.00-11.80（35%，需衰竭信号≥2 项）/ T3 9.40-10.00（35%，极悲×悲观≈9.45，深于 2024-09 大底）**；证伪线 9.40（连续 2 日收盘低于线 1% 确认）。现价 16.13 不到任何一档。
- **胜率/Kelly**：T1 50-60% / T2 60-70% / T3 65-75%（后两档按信号≥2 项已确认计；当前活跃 0 项，exhaustion 锚为 2024 段回声待重锚）；quarter-Kelly 上限（按下沿、结构档价手算）**T1 2.1% / T2 12.4% / T3 16.0%**（T1 为边际期望注，执行需届时证据推胜率至上沿）。
- 波段箱体 15.0-17.4（证伪 14.80，上限 15%）；右侧触发 17.55 / 止损 16.30，收复 19.40 为更强确认。分红口径提示：例行派现 0.2-0.3 元/股，除息后价格字段按 §5.4b 机械换算。
- 双产物：`cards/002299.SZ/圣农发展估值排期卡_draft_2026-08-16.md` + `draft_2026-08-16.json`；`create-draft` 入库 **card_version_id=002299SZ_4bb3717b**（draft-only，未 activate/reject，待人工确认）。next_review 2026-08-31（中报窗口）。
- `uv run pytest -q` 全绿。

## 2026-08-16（有友食品 603697.SH 估值排期卡 draft）

- 承接今日入池出卡：底稿 `cards/603697.SH/inputs_2026-08-14.json`（行情 2026-08-14 / 财报 2026-03-31 / forecast 快照 2026-08-16），现价 9.63（不复权），PE(TTM) 20.49，TTM 归母 2.01 亿 / EPS 0.47。
- **锚类型 pe_scale（PE 历史带）**：稳定消费扩张型、盈利为正且连续五季增长；3 年样本（2023-08-16~2026-08-14，726 日 20 低点，§3.2 标注）底部刻度逐轮上移 11.9→21.9→28.8 = 体系升级但未经恐慌检验；悲观 PE 13 按"升级失败回落 2024 旧体系带 11.9-14.6"设定（最近回调刻度 26.6/28.8 为上涨段非恐慌低点不作底部刻度）。PE 情景 乐观22/中性18/悲观13；EPS 情景 中性0.54（+25%）/悲观0.48（+10%）/极悲0.43（0 增长）。
- **裂口 +0.79pp ≈ 0**（FY1 +31.5% vs Q1 实际 +30.7%），预期与实际一致，不启用"以实际趋势修正预期"折价以外的处理。
- **机械档位失真弃用**（同天赐/牧原先例）：脚本 T3=极悲×悲观=5.6-6.0、证伪线 5.6 致 T1 赔率 0.60/Kelly 0%；改结构锚定（matrix_source 注明）：T1 9.20-9.70（2024-11-25 低 9.61/2025-04-07 低 9.47 支撑带，现价已入档）、T2 8.30-8.80（2024-11 低点群下半 + 07-27 低 8.91 上方）、T3 7.00-7.50（2024-10-28 低 7.35）；证伪线 6.90。
- **胜率/Kelly**：T1 60-65% / T2 70-75% / T3 75-80%（下沿 60/70/75 → quarter-Kelly 4.5%/13.8%/18.3%，目标 11.88=中性×乐观、证伪线 6.90 手工同公式计算）；T2/T3 释放加结构要求：≥2 项活跃且 ≥1 项核心信号（当前 2 项活跃 divergence+duration 均为加分项，核心 0 项）。
- 波段箱体 8.90-10.20（证伪 8.80）；右侧触发 10.30 / 止损 9.45；next_review 2026-08-31（中报窗口）。
- 产物：`cards/603697.SH/有友食品估值排期卡_draft_2026-08-16.md` + `draft_2026-08-16.json`；`create-draft` 入库 **card_version_id=603697SH_f938200e**（draft-only，activate/reject 待人工）。

## 2026-08-16（洽洽食品 002557.SZ 估值排期卡 draft）

- 承接入池出卡：底稿 `cards/002557.SZ/inputs_2026-08-14.json`（行情 2026-08-14 / 财报 2026-03-31 / forecast 快照 2026-08-16），现价 18.97（不复权），PE(TTM) 23.44 低于样本 p5（24.7，低谷利润口径），TTM 归母 4.09 亿 / EPS 0.8094，股本 5.06 亿。
- **锚类型 pe_scale（PE 带，远期 FY2026E 口径）**：当时 PE(TTM) 刻度 48.9→38.7→33.7 逐轮下移 = 成长消费→成熟消费**体系切换中**，旧体系 33.7-48.9 带仅作切换证据不作锚；样本区间 2023-08-16~2026-08-14（726 日 8 个恐慌低点，§3.2 已标注）。辅助锚 PB 1.94（近 5 年 1.2% 分位）+ 2025 年度股息率 5.17%（筛选报告口径）。
- **裂口 -11.2pp 有利方向**（券商 FY1 +106.6% vs Q1 实际 +117.8%），幅度小视为收敛；EPS 情景：中性 1.25（FY1 6.58 亿 ×96 折）/ 悲观 1.12（-10%）/ 极悲 0.95（-24%，仍较 FY2025 深谷 +51%）；PE 情景（远期口径）乐观 18 / 中性 15（=现价实际定价 15.2）/ 悲观 12.5。H1 预告（07-14）归母 +170.75~198.96% 隐含 2.40-2.65 亿为复苏底子。
- **机械档弃用**：build_schedule T1=中性×中性 18.0-18.8 贴现价无左侧折价，按天赐/牧原先例结构锚定（matrix_source 已注明）——**三档：T1 17.00-18.00（30%，悲观×中性 16.8-17.9 + 6-7 月低点区 17.40-18.92 下移带）/ T2 14.00-15.00（35%，极悲×中性≈14.25/悲观×悲观=14.0，需衰竭信号≥2 项）/ T3 11.50-12.50（35%，极悲×悲观 11.9±5% 双杀定价）**；证伪线 11.50（连续 2 日收盘低于线 1% 确认）。现价 18.97 未落档（高于 T1 上沿 5.4%），空仓等待。
- **胜率/Kelly**：T1 55-65% / T2 65-75% / T3 70-80%（下沿 55/65/70，修复目标 22.5=中性×乐观，证伪线 11.50，结构档价手算同公式）：**quarter-Kelly 上限 T1 0.3% / T2 13.0% / T3 17.1%**——T1 为边际期望注（赔率 0.83 被 34% 证伪距离吃掉），执行需届时证据（H1 兑现/葵花籽采购价低位）推胜率至上沿（65% 时上限 5.8%）。
- 衰竭信号：系统锚 weekly_anchors 最新 as_of 仍为 2024-09-27（前低 27.06），与现价严重脱节，no_new_low_3w 恒 new_low；系统当前活跃仅 1 项（duration），meets_min=false → T2/T3 不释放；当前下跌段（起点 2026-04-21 高 25.99）人工参照实数已写入卡（基数 ≈3674 万股/周、恐慌 ×2≈7347 万、干涸 40-60%≈1469-2204 万、前低 17.40），执行前以系统届时重算为准。
- 波段箱体 17.50-21.00（证伪 17.30，上限 15%，与 T1 重叠分开记账）；右侧触发 21.10 / 止损 19.50；next_review 2026-08-31（中报法定截止，验证 H1 2.40-2.65 亿与毛利率）。核心变量=新采购季葵花籽价格；实控人增持（06-24 增持 832 万股+拟续增 3000-6000 万）终止/转减持为立即复核触发器。
- 双产物：`cards/002557.SZ/洽洽食品估值排期卡_draft_2026-08-16.md` + `draft_2026-08-16.json`；`create-draft` 入库 **card_version_id=002557SZ_81fecdad**（draft-only，未 activate/reject，待人工确认）。
- `uv run pytest -q` 全绿。

## 2026-08-16（南航 600029.SH 执行录入 + 持仓状态核对）

- 人工执行录入：execution #5 —— 2026-08-13 买入 4000 股 @ 5.07（总额 20,280 元），tier=1，关联 active 卡 `600029SH_a9c860a3`（2026-08-11 生效），信号快照冻结至 2026-08-13（§5.7）。成交价 5.07 ∈ T1 [4.95, 5.20]，符合卡片第一档纪律（无附加信号要求）。用户口径由"约 3 万"更正为实际 20,280 元。
- 触发状态核对（signal_facts @ 2026-08-14）：海天 603288.SH T1 于 08-10 触发（close_in_tier_1_zone）但人工未执行（偏离记录，用户择时判断"仍向下"，等 T2 [32.50, 34.20]）；现价 35.82 已跌出 T1 下沿 36.40 之下。珀莱雅 603605.SH 收盘 57.18 在 T2 [54.70, 57.60] 内但衰竭信号仅 1 项 < 2，系统判定不触发；用户计划 ≤56 人工买入待确认（若执行需备注偏离：价格触发但信号不足；54.70 为 T2 下沿与箱体失效线重合位，跌穿即停手）。

## 2026-08-16（三张新消费卡人工激活）

- 人工指令激活（§5.6）：圣农 `002299SZ_4bb3717b` / 洽洽 `002557SZ_81fecdad` / 有友 `603697SH_f938200e`，effective_from 均 = 2026-08-14（数据截止日；无旧 active 卡冲突）；`current.md` 已刷新。
- 激活后全池 daily 2026-08-14 重跑 ok=13（revision=6，新三只单股报告 revision=2 转正 complete）。激活后首个决策点：**有友 603697.SH T1 触发**（收盘 9.63 ∈ [9.20, 9.70]，第一档无附加信号要求）；圣农价区外（距 T1 上沿 13.6%）、洽洽价区外（距 T1 上沿 5.4%），均无决策点。

## 2026-08-17（盘后例行 daily 2026-08-17，全池 ok=13）

- 增量（run_20260817_daily，窗口 2026-08-10..08-17）：**以 watchlist 表当日 active 清单为准，13 只 CN A 股**（含 08-16 入池的圣农/洽洽/有友，不复用旧清单）× none+forward 各 6 行落 `data/raw/stock_finance_data/price/2026-08-17/run_20260817_daily/`；窗口内 forward==none 逐文件 cmp 全一致（无除权）。**NOTE**：MCP file_path 相对路径按插件 cwd 解析，文件初落插件目录后移回（_meta.json 已注），后续采集传绝对路径。
- 指数：yahoo 000300.SS 单行（Date 2026-08-16T16:00Z=08-17）O 4672.28/H 4742.40/L 4662.71/C 4741.10，**gildata 指数日行情逐字段交叉一致**（核对件 data/raw/gildata/fin_query/2026-08-17/index_check_000300.csv，昨收 4665.88 亦对），直接入库无 _fixed 兜底；沪深300 收 4741.10（+1.61%）。
- daily 2026-08-17（revision=1）**ok=13**：13 只全部 complete（13 张 active 卡全生效——牧原卡 002714SZ_e8fd9ea7 已于 08-14 晚人工激活，非 draft）。event_study 阶段 success：公告事件 2249 条，写入 204 行（全部 pending/degraded 重算），跳过已完成 2045 行，分布 ok 38 / degraded 166（degraded 仍为 07-20..08-07 指数空洞段）。
- 决策点/观察：**南航 T1 触发连续第五日**（收 5.03 ∈ [4.95,5.20]，08-11 起；divergence+duration 2 项活跃）；**有友 T1 触发连续第二日**（收 9.64 ∈ [9.20,9.70]，divergence+duration 2 项）；**珀莱雅收 56.87 连续第二日处 T2 [54.70,57.60] 内，但活跃衰竭信号仅 duration 1 项 < 2 → tier_triggered=pending_signals 不触发**（P5 附注；波段口径收盘高于买区上沿 56.50 约 0.65%，box_position=mid_box 未翻 buy_zone，盘中低 55.96 曾入买区）；**天赐 box_position=sell_zone 触发**（收 40.04 ∈ 卖区 [39.50,40.50]，波段仓卖点信号）；海天 35.42 低于 T1 下沿 36.40 约 2.7%，tier_proximity active（3% 阈值内，P4）；恒力 17.46 高于 T1 上沿 16.40 约 6.5% 无触发；牧原 39.29 高于 T1 上沿 37.00 约 6.2%，衰竭 2 项活跃（duration+no_new_low_3w）无价区触发。
- 今日收盘：珀莱雅 56.87（-0.54%）/ 海天 35.42（-1.12%）/ 平安 51.80（+0.10%）/ 埃斯顿 36.40（+4.75%）/ 紫金 33.36（+2.55%）/ 南航 5.03（+0.20%）/ 豫光 13.52（+3.60%）/ 恒力 17.46（+1.93%）/ 天赐 40.04（+1.44%）/ 牧原 39.29（0.00%）/ 圣农 16.21（+0.50%）/ 洽洽 19.09（+0.63%）/ 有友 9.64（+0.10%）。

## 2026-08-17（修复 A/H 总股本口径）

- **缺陷确认**：share_capital_events 13 条快照全部来自 yahoo get_stock_info（share_count_type=issued），A/H 双上市公司实际只含 A 股股本，导致 PE 分母偏小（平安卡内 1.7 倍口径差此前已标注，本条为系统性修复）。确认口径：PE = A 股价 × 集团总股本（A+H）÷ TTM 集团归母（vendor 通用口径）。
- **采集与交叉核对**：stock_finance_data get_stock_info 全池 13 只（ths_total_shares_stock，A+H 全口径，落 `data/raw/stock_finance_data/stock_info/2026-08-17/group_total_batch{1..5}.csv`）；与库内 issued 快照不一致的 6 只用 gildata fin_query 股本结构交叉核对（落 `data/raw/gildata/fin_query/2026-08-17/share_check_<code>.csv`），两源总股本全部一致（gildata 万股两位小数换算，尾差 ≤ 百余股）：
  - 601318.SH 平安：10,660,065,083 → **18,107,641,995**（×1.6986；gildata：A 1,066,006.51 万 + H 744,757.69 万）
  - 601899.SH 紫金：20,601,793,140 → **26,590,714,622**（×1.2907；gildata 2026-04-03：A 2,060,179.31 万 + H 598,892.15 万）
  - 600029.SH 南航：13,476,972,212 → **18,120,969,844**（×1.3446；gildata 2026-07-31：A 1,347,697.25 万 + H 464,399.73 万）
  - 002714.SZ 牧原：5,462,773,044 → **5,772,996,144**（×1.0568；H 31,022.31 万，2026-02-06 H 上市 + 03-10 超配，同 08-14 核对件）
  - 603288.SH 海天：5,559,820,644 → **5,851,045,044**（×1.0524；H 29,122.44 万 = 2025-06-19 发行 27,903.17 万 + 07-21 超配 1,219.27 万）
  - 002747.SZ 埃斯顿：871,018,453 → **967,798,453**（×1.1111；**新发现 H 股**：2026-03-09 发行 9,678 万股 H 股上市，gildata 实时行情总股本 9.68 亿印证）
  - 其余 7 只（002299/002557/002709/600346/600531/603605/603697）两源与库内一致，纯 A 股股本不变。
- **代码改动**（scripts/indicators/valuation.py）：① `shares_at` 同一 effective_at 多口径并存时优先 `group_total`、回退 `issued`（纯 A 股行为不变）；② `load_share_snapshot` 冲突校验改为只在同 share_count_type 内比对；③ 新增 `load_group_total_snapshot` 解析 ths get_stock_info CSV（按 thscode 定位行、raw_objects 登记、同类型幂等/异股数抛股本冲突、非整数/缺值抛错不猜 §2.5）。
- **入库**：13 只各写 1 条 group_total 单点快照（sce_id=14..26，event_type=snapshot_group_total，source=stock_finance_data get_stock_info，effective_at 沿用各股数据起始日，details_json 标注 A+H 全口径与单点快照假设：H 股上市/增发/回购注销前的历史区间按当前总股本计算存在口径偏差，待 get_stock_actions 细化）。
- **重算**：6 只股本变化股票 `scripts.indicators.compute` 全量重算（pe_ttm 逐日精确 ×股本倍数，历史分位整体平移属预期）：

  | symbol | 旧股本(issued) | 新总股本(group_total) | 倍数 | PE 2026-08-14 前→后 |
  |---|---|---|---|---|
  | 601318.SH 平安 | 106.60 亿 | 181.08 亿 | ×1.6986 | 4.15 → 7.06 |
  | 601899.SH 紫金 | 206.02 亿 | 265.91 亿 | ×1.2907 | 10.86 → 14.02 |
  | 600029.SH 南航 | 134.77 亿 | 181.21 亿 | ×1.3446 | 21.93 → 29.49 |
  | 002747.SZ 埃斯顿 | 8.71 亿 | 9.68 亿 | ×1.1111 | 232.51 → 258.34 |
  | 002714.SZ 牧原 | 54.63 亿 | 57.73 亿 | ×1.0568 | 21.94 → 23.19 |
  | 603288.SH 海天 | 55.60 亿 | 58.51 亿 | ×1.0524 | 27.36 → 28.79 |
- **文档同步**：system_design §3.7/§4.1（share_count_type 增 group_total 取值与 PE 取数优先级）、database_schema share_capital_events 节。测试新增 2 项（group_total 优先级 + loader 幂等/冲突/跨口径并存），`uv run pytest -q` 303 全绿。
- **NOTE**：① 排期卡 PE 刻度/分位数随口径平移，历史卡片估值段与卡内 1.7 倍口径标注需人工复核（卡片为 [决策] 不自动改，后续人工流程）；② 平安 2025-09 回购注销 1.03 亿股（gildata 明细）使 group_total 快照对 2025-09 前区间略低估，与单点假设同方向记录；③ 牧原/紫金/海天 H 股上市日前的历史 PE 实际应以当时 A 股本计，当前按新总股本平移，偏差方向为高估历史 PE。

## 2026-08-18（财报披露日回填模块 pit_backfill，解除 D1.3 降级；补记于 08-23 提交时）

- **背景**：financial_reports 历史入库时 published_at 全 NULL、available_at 填入库时间（D1.3 降级），pe_status 长期带 `;degraded_available_at` 标注，严格点时过滤（§2.1）形同虚设。本模块用天眼查"上市信息-上市公告"原始 CSV（`data/raw/tianyancha/announcement/2026-08-17/pit_backfill/`，13 只 × p1/p2/p3 三批共 125 个 CSV）回溯真实披露日。
- **模块**（`scripts/pipeline/pit_backfill.py`，CLI `uv run python -m scripts.pipeline.pit_backfill --raw-dir ... [--symbol ...] [--dry-run] [--backup ...]`）：
  - 回填口径：published_at = 公告日 00:00 本地（Asia/Shanghai）→ UTC；available_at = 下一开市交易日 00:00 本地（复用 `adapters/tianyancha._next_open_available_at` 保守规则，§2.1）；每次改写 data_revisions 记旧值/新值（含天眼查 uuid 与来源文件 sha256）。
  - 匹配规则 `match_disclosure`（纯函数，§2.5 不猜）：按 (period_type, period_end) 生成标题关键词（年度/一季度或 Q1/半年度/三季度，含名称变体）；只取 stock_code 等于该股 6 位代码的行（A+H 混排的 H 股公告不算）；排除英文版/修订/更正/问询/回复/说明会等非首披文本；摘要与全文同步披露时全文优先、缺全文取摘要；同标题多条取最早披露日；匹配不上返回 None 保持降级不猜。
  - 安全设计：`--dry-run` 只匹配不写库；`--backup` 先导出 financial_reports 备份 CSV 再回填。
- **compute.py 联动**（`scripts/indicators/compute.py`）：`recompute_indicators` 的 `assume_visible_reports` 默认改 None 自动判定——该股任一财报 published_at IS NULL（仍是入库时间降级）时回退 assume_visible 并标注 `;degraded_available_at`；全部回填真实披露日后严格按 available_at <= as_of 点时过滤，标注消失（§2.1）。
- **测试 +12**（`tests/test_pit_backfill.py`）：标题关键词映射、全文最早优先且排除修订版、英文版跳过不猜、摘要 fallback、全文优先于摘要、季度名称变体、H 股行按 stock_code 过滤、非披露标题排除、无匹配返回 None、CSV 加载去重过滤、回填写库（匹配更新+未匹配保持原值）、dry-run 零写入。`uv run pytest -q` 303→315 全绿。
- **状态：模块就绪、尚未对生产库执行**——库内 98 期财报 published_at 仍全 NULL、data_revisions 无 pit_backfill 记录。执行流程（待排期）：`--backup` 导出备份 → `--dry-run` 核对各股匹配数 → 正式回填 → 全池重算指标 → 验证 pe_status 的 degraded_available_at 标注消失、PE 序列无异常跳变。

## 2026-08-21（通达信 tdx-connector 接入为第一优先数据源）

- **背景**：用户问"当前接口能否采集数据，是否有替代源"。实地探测发现 kimi-datasource 插件 access_token 失效（`access_token was rejected. Run /login again`），stock_finance_data 全套接口（行情/财报/预期/公告/股本）当前不可用；且公告接口自 8/13 持续 EMPTY_DATA、指数接口长期 EMPTY_DATA，单一源风险高。用户指令"将通达信接口也一起加入到数据源中，默认第一优先级"。
- **探测确认 tdx-connector 能力**（已 connected，未接入管线）：A 股日 K 线（603605.SH 实测，含 amount 弥补 kimi 缺 amount、HasLtgb 流通股本）、沪深300 指数（setcode=62）、港股 00700 K 线（setcode=31）、公告 wenda_notice_query、估值/股本/股东人数 tdx_quotes hasCwInfo=1、港股财报 tdx_api_data（fixedTag=1/2/3）。详见 `docs/probe_20260821_tdx.md`。
- **adapter 实现**（`scripts/adapters/tdx.py`，4 个 parse 函数）：
  - `parse_kline_csv`：A 股/港股日 K → daily_bars，按 setcode 推 symbol 后缀（1→SH 0→SZ 2→BJ 31→HK），复用 `_validate_bar_row`/`upsert_daily_bars`；**volume 按 unit 列换算**（tdx Rows.Volume 单位手，×100 转股与 kimi volume_raw 口径一致；指数 unit=1 不换算）；tqflag=1/2 前/后复权文件不入 daily_bars 留给复权模块。
  - `parse_index_csv`：指数 → index_bars（setcode=62/32），code 经 SETCODE_SUFFIX 归一（000300 → 000300.SH）。
  - `parse_announcement_csv`：公告 → events/event_symbols，无 uuid 按 `title|pub_date` 哈希去重（§3.6），available_at 取下一开市交易日 00:00 本地（§2.1）。
  - `parse_quotes_csv`：估值/股本快照 → share_capital_events（event_type=snapshot_group_total_tdx，share_count_type=group_total_tdx），**不参与 valuation.py PE 取数**（仅认 issued/group_total），details_json 含 pe/pb/mgsy/mgjzc/zsz/ltgb/gdrs/financials；GDRS 股东人数是系统长期缺的筹码集中度指标（§5.7）。
- **ingest 路由**（`scripts/pipeline/ingest.py` `_ROUTES`）：加 4 条 tdx 路径路由（tdx/kline、tdx/index、tdx/announcement、tdx/quotes），列在字典首位标示第一优先。
- **采集规范**（`skills/tdx-collect/SKILL.md`）：CSV 列格式与 adapter 严格对齐；tdx 第一优先、kimi fallback、tianyancha 公告兜底的优先级约定；全量初始化与增量模式；港股财报/资金筹码/港股日历三项已知限制。
- **BOM 修复**：Write 工具落盘 CSV 带 UTF-8 BOM，csv.DictReader 把首列读成 `\ufeffcode` 致 adapter "缺 code 列"。修复：tdx.py 4 处 `open(...)` 改 `encoding="utf-8-sig"`（自动剥 BOM）。
- **测试 +17**（`tests/test_tdx_adapter.py`）：K 线 OHLC 校验 + amount 非空 + volume ×100 换算 + 复权因子继承；港股 setcode=31 + HK 日历缺失 incomplete 降级；tqflag=1/2 跳过；指数 setcode=62 + unit=1 不换算 + 非 index setcode 拒绝；公告 title|date 去重 + symbol 关联 + 幂等 + 缺 ticker 拒绝；估值快照 group_total_tdx + 不污染 PE 取数 + 幂等；ingest CLI 4 类路由。`uv run pytest -q` **332 全绿**（原 315 + 新 17）。
- **实地试采 603605.SH**（`data/raw/tdx/{kline,index,announcement,quotes}/2026-08-21/run_probe_tdx/`）：K 线 5 根（08-17..08-21）inserted=4 updated=1（08-17 与 kimi 历史一致但 amount 从 None→284190240 补全，触发 data_revisions source_revision）、指数 3 根（08-19..08-21）inserted=3、公告 2 条 inserted=2、估值快照 1 条 inserted=1。**交叉校验通过**：08-17 tdx close=56.87 = kimi 56.87；volume 5037218（50372.18×100）≈ kimi 5037200；amount 284190240 补全；price_adj_factor 1.058587 继承正确（未因 tdx 入库重置）；000300.SH 08-19/20/21 新增（之前 yahoo 仅到 08-17）；603605.SH 公告 180（tianyancha）+2（tdx）并存；估值快照 shares=395976100 pe=15.38 pb=3.57 gdrs=73274。
- **文档同步**：system_design §3.2 数据源节（tdx 第一优先 + kimi/yahoo 兜底 + volume 单位换算说明）；handoff.md 数据源节；本日志；`docs/probe_20260821_tdx.md` 实测记录。
- **已知限制（二期）**：① 港股财报 tdx_api_data adapter 未实现 parse_financials_csv；② 资金/筹码分布（盘口夹板/托单）数据源不可得（§3.6）；③ 港股日历仍需 `config/calendar_HK_{year}.yaml` 种子；④ 跨源公告去重（tdx 与 tianyancha 同标题公告不去重，后续加 canonical_url）。

## 2026-08-21（全池数据采集：tdx 补齐 12 只 A 股缺口）

- **背景**：用户指令"进行一次数据采集，补齐当前跟踪股票的缺失数据"。库内 13 只 A 股中 12 只最新 bar 到 2026-08-17（缺 08-18/19/20 共 3 个交易日），公告多数到 8 月中旬，tdx 估值快照仅 603605.SH 有（试采时写的）。
- **采集范围**：12 只 A 股（排除已采的 603605.SH）× 3 类数据（K 线 + 公告 + 估值快照）= 36 个 CSV，落盘 `data/raw/tdx/{kline,announcement,quotes}/2026-08-21/run_collect/`。tdx-connector 为 streamable-http 类型（企业托管鉴权），Python 无法 subprocess 直调，改用 3 个并行 Agent 子任务分别采集（K 线/公告/估值），避免 36 次 MCP 响应污染主上下文。
- **采集执行**：
  - K 线：`tdx_kline wantNum=10 tqFlag=0`，剔除 08-21 盘中未收盘数据，保留 08-10..08-20 共 9 根/只。12 只全成功。
  - 公告：`wenda_notice_query bdate=各股最新公告日+1 edate=20260821`。12 只全成功，共 47 条（002557 洽洽、601899 紫金为空数据；601899 公告 CSV content_hash 撞库 skipped）。
  - 估值快照：`tdx_quotes hasCwInfo=1`。12 只全成功，PE/PB/GDRS 齐全（002714 牧原 PE=-18.66 亏损；601318 平安 PB=0.92 破净）。
- **ingest 入库**（`uv run python -m scripts.pipeline.ingest` 3 目录）：**inserted=95 updated=72 skipped=2 conflicts=0 errors=0 incomplete=0**。
  - K 线：inserted=36（12 只 × 3 天新增 08-18/19/20）updated=72（12 只 × 6 天重叠 08-10..08-17，amount 从 None→有值，触发 72 条 data_revisions source_revision）。
  - 公告：inserted=47 skipped=2。
  - 估值快照：inserted=12（group_total_tdx，不参与 PE 取数）。
- **采集后库内现状**：
  - 12 只 A 股最新 bar 到 2026-08-20（缺口补齐）；603605.SH 到 08-21（试采时盘中数据）。
  - amount 补全：最近 9 天非空（tdx 补，113/9525 行）；kimi 历史 9412 行 amount 仍 NULL（kimi 长期缺陷，如需补全需 tdx 重采 3 年历史 wantNum=750）。
  - 公告：13 只 98~254 条不等（tdx + tianyancha 并存）。
  - tdx 估值快照：13 只各 1 条 group_total_tdx（含 PE/PB/MGSY/MGJZC/ZSZ/ZGB/LTGB/GDRS/IPOPrice/财务三表摘要）。
  - 000300.SH 指数到 08-21（试采时补）。
- **_meta.json**：`data/raw/tdx/kline/2026-08-21/run_collect/_meta.json` 记录采集参数、入库统计、coverage_after、known_gaps。
- **NOTE**：① 08-21 收盘后（15:00 后）需重采当日完整数据（本次剔除盘中）；② kimi 历史 amount 全 NULL，本次只补增量 9 天，历史补全需 tdx 重采 3 年；③ 0700.HK 港股未采集（watchlist 未含，HK 日历=0 未填充）；④ 601318 平安 PB=0.92 破净、002714 牧原 PE 负值，估值快照已如实记录不猜（§2.5）。
- **NOTE**：① 08-21 K 线为 11:04 盘中部分数据（未收盘），仅验证 ingest 链路不跑 daily；② tdx 公告与 tianyancha 部分内容可能重复（跨源未去重），不影响事件研究（按 event_id 去重）；③ 0700.HK 在 watchlist 待采集（港股日历 + tdx 港股 K 线已具备接入条件）。

## 2026-08-21（盘后例行 daily 2026-08-20 + 08-21 盘中污染清理）

- **daily --date 2026-08-20**（13:36 执行，数据已入库不带 --raw-dir）：**全池 ok=13**，报告全 complete；event_study 写入 227 行（ok 35 / degraded 192，degraded 为 000300 07-20..08-07 指数空洞期）；全池日报 `reports/daily/2026-08-20.md` revision=1。
- **发现并清理 08-21 盘中 bar 污染（603605.SH）**：上午试采（run_probe_tdx）入库的 08-21 盘中 bar 使 weekly 模块把本周（08-17..08-21 周五未收盘）误判为完成周，生成 `weekly_bars week_end=2026-08-21` 伪周线与 `duration observed_on=2026-08-21` 信号（其余 12 只无 08-21 bar 不受影响，最新周均 08-14）。**违反 §3.4"weekly_bars 只保存完成周"**。清理：data_revisions 记 `intraday_partial_removed`（run_id=manual_20260821_clean）→ DELETE daily_bars 08-21 行 → DELETE weekly_bars 伪周行 → 重跑 daily 全量重算。清理后 603605.SH 最新 bar=08-20、最新周=08-14 与全池一致，报告 revision=2。**教训：盘中 bar 不得入库——收盘前采集的 CSV 必须剔除当日行（run_collect 批已执行该剔除，run_probe_tdx 遗漏）；tdx-collect skill 采集模式已含此纪律。**
- **全池观察状态（2026-08-20 收盘）**：
  - **P3 已确认决策点 ×3**：600029.SH 南航 T1 档位触发（收盘 5.03 入第一档 [4.95, 5.20]）；603697.SH 有友 T1 档位触发（收盘 9.46 入第一档 [9.20, 9.70]）；600346.SH 恒力右侧确认成立（关键位 18.50，retest_within_band_and_held，突破 18.685 后回踩 18.87 带内企稳，hold 18.315）。
  - **P4 临近观察点 ×2**：002709.SZ 天赐距 T1 上沿 36.50 差 1.6%；603288.SH 海天距 T1 下沿 36.40 差 1.8%。
  - **活跃衰竭信号 8 项 / 5 只**（最近完成周 08-14）：600029（divergence+duration）、603697（divergence+duration）、002714 牧原（duration+no_new_low_3w）各 2 项；002557 洽洽、603605 珀莱雅各 1 项（duration）。
  - **吸筹形态**：601318.SH 平安 watching（放量破位观察，未到缩量横盘判定）。
  - **603605.SH 珀莱雅**：收盘已进入第二档价区但同锚点活跃衰竭信号 1 项 < 2，tier_triggered=pending_signals 不触发（§5.4 第二档附加条件）。
  - 无 P1 数据异常、无 P2 证伪跌破/复核逾期/换算待确认。
- **NOTE**：① 000300 指数 07-20..08-07 历史空洞仍未补（tdx 只补了 08-19..08-21 增量，历史需 tdx_kline wantNum 更大重采）；② 事件研究 degraded 192 行随指数回补自动转正；③ 08-21 收盘后（15:00 后）例行采集→daily 闭环。

## 2026-08-22（08-21 收盘数据补采 + daily 闭环，周六例行）

- **采集**（run_collect，`data/raw/tdx/{kline,index,announcement}/2026-08-22/run_collect/`）：周六采 08-21 完整收盘数据。K 线 13 只（wantNum=10，08-10..08-21 全保留无剔除）+ 000300 指数重采（wantNum=5，修正昨日 11:00 盘中值 4625.20 → 收盘 4618.9）+ 公告 13 只 50 条（南航空数据）。估值快照跳过（13 只 08-21 快照昨日已入库，重采幂等）。2 个并行 Agent 子任务执行。
- **ingest**：inserted=38 updated=74 skipped=74 conflicts=0（K 线 13×1 新增 + 重叠日 amount 校验 update；公告新增 24 条：洽洽 2、天赐 1、牧原 2、埃斯顿 5、恒力 1、豫光 1、平安 2、紫金 5、有友 5）。
- **daily --date 2026-08-21**：**全池 ok=13**，报告全 complete；本周（08-17..08-21）为完成周，周线信号首次更新到 08-21 周；event_study 写入 251 行（ok 77 / degraded 174）；全池日报 `reports/daily/2026-08-21.md`。
- **本周完成周衰竭信号（10 项 / 6 只，较上周 8 项/5 只）**：600029 南航 **3 项**（divergence + **dry_up 新增** + duration）；603697 有友 2 项（divergence+duration）；002714 牧原 2 项（duration+no_new_low_3w）；002557 洽洽、603605 珀莱雅各 1 项（duration，珀莱雅仍差 1 项释放 T2）。
- **P3 决策点 3 只**：① 002557 洽洽新晋——波段箱体 buy_zone（收盘 18.55 入买入区 [17.50, 18.60]）；② 600029 南航 T1 档位维持（4.99 在 [4.95, 5.20]）；③ 603697 有友 T1 + buy_zone **双触发**（9.26 同时在 T1 [9.20, 9.70] 与买入区 [8.90, 9.30]）。600346 恒力右侧 confirmed 次日回 idle 落 P5（§5.4c terminal 纪律预期）。P4：603288 海天距 T2 上沿 34.20 差 2.5%（-1.96% 后 T1 上方够不着转监测 T2）；002709 天赐 +2.37% 远离 T1 落 P5。
- **08-21 全池涨跌**：豫光 +6.19%（13.90）、平安 +2.32%（53.35，距卡片卖出区下沿 53.50 仅 0.3%）、紫金 +1.43%（34.74）、埃斯顿 +2.44%、天赐 +2.37%；圣农 -2.91%、有友 -2.11%、海天 -1.96%。
- **⚠️ 紫金 XD 除息未处理（数据缺口）**：子任务探测紫金 08-21 为 XD 除息日，但 ① corporate_actions 表无 08-21 记录（tdx K 线采集不含分红事件流）；② 复权因子未调整（08-20/08-21 均 1.0611）。若除息额 D>0，复权序列在 08-21 存在约 D/34.25 的虚假缺口，污染紫金均线/周线/信号口径。**待补**：yahoo get_stock_actions 或 tdx 后复权（tqFlag=2）对比推因子 → corporate_actions 入库 → adjust 全量重建。
- **NOTE**：① 下周一（08-24）例行：采集增量 → daily --date 08-24；② 珀莱雅 T2 价区 + 1 项衰竭信号状态延续，若下周出现恐慌/干涸/不新低任一信号则 T2 释放；③ 事件研究 degraded 174 行仍为 000300 指数 07-20..08-07 历史空洞。

## 2026-08-23（矿业扩充定向筛选：西矿/湖金两只候选）

- **背景**：用户持豫光（铅锌冶炼+银）、紫金（金铜锂），问同业是否有更合适标的。tdx 快照拉 7 只同业估值（山东黄金 PE 29.5/赤峰 23.8/湖金 20.1/中金 13.8/洛钼 12.3/西矿 10.8/白银有色 75.2），圈定西矿、湖金两只候选走 earnings-surge-screener 定向评估。kimi-datasource 鉴权失效（gildata 不可用），数据源降级 tdx + WebSearch。
- **三轨路由**：西矿 Track A 命中（H1 归母 41.69 亿 +123% ≥50%，中报景气表述齐全，7-29 披露）；湖金 Track A 未命中（+46.01% 差 4pct 且锑价上半年 -25.8% 与景气表述相反）→ Track B 弱命中（万古金矿注入事件锚）+ 现金流闸门亮红灯（-1.06 亿）。
- **阶段二结论**（报告 `reports/screening/2026-08-23-矿业扩充.md`）：**西矿 = 第二梯队观察池候选不入池**（兑现度强 Q2 +143%、玉龙三期 2026 年末投产、PE ~11 未透支；但与紫金铜敞口同质，组合不需要第二个铜）；**湖金 = 排除**（产量全线下滑 + 现金流转负 + 存货翻倍 + 锑逆风；本质是金股且 89% 收入为 0.09% 毛利外购金贸易；停牌中）。对原问题的回答：紫金板块内综合最优不换、豫光按线兑现，无需替换。
- **紫金中报要点（顺带记录）**：H1 归母 391.7 亿 +68%、毛利率 27.7%→37.0%、金 47 吨 +13%、锂 +496%、铜 -5.7%（卡莫阿扰动）；2026E 机构预测 800-860 亿；8-31 卡片复核时 EPS 情景应上修，止盈线 31.7-31.9 维持线位但减仓幅度可从"减半"降为"减 1/4"（中报 +68% 后原线显保守）。

## 2026-08-23（西部矿业 601168.SH 入池 + 排期卡 draft）

- **背景**：同日矿业筛选结论把西矿列为"第二梯队观察池候选不入池"，用户随后指令直接入池跟踪并制作排期卡。
- **入池**：`config/watchlist.yaml` 末尾新增 601168.SH（market CN，aliases [西部矿业, 西矿, Western Mining]，benchmark 000300.SH）→ `uv run python -m scripts.pipeline.db seed` 导入 14 只全成功。
- **数据源偏差决定**：tdx-connector 为 workbuddy 远程 HTTP MCP（txmcp.tdx.com.cn:3001），本会话直连 401 无凭据不可用；按 tdx-collect skill 优先级约定走 kimi-datasource 兜底（kimi token 已恢复可用）。run_id=run_init_601168。
- **采集清单**（`data/raw/.../2026-08-23/run_init_601168/`，各目录含 `_meta.json`）：
  - kimi 不复权日线 725 行（2023-08-24~2026-08-21）+ 前复权 725 行（`price/.../601168.SH.csv` 与 `_forward_3y.csv`）；利润表 14 期（2023/2024/2025 四季报 + 2026Q1/H1，`financials/...`）；forecast；get_stock_info。
  - yahoo：stock_info + stock_actions 19 条分红（窗口内 4 次现金分红无送转；文件名用内部代码 `601168.SH.csv`，`.SS` 后缀 market_of 不认）。
  - 公告：kimi get_stock_announcement 164 条入库（`announcement/...`）。
  - 天眼查"上市信息-上市公告"25 页 500 行（2023-01-19~2026-08-17，`tianyancha/announcement/2026-08-23/pit_backfill_601168/`，子代理抓取）。
- **入库与管线**：ingest 全部 ok；股本快照双口径写入（`valuation.load_share_snapshot` + `load_group_total_snapshot`，均 2,383,000,000 股，effective_at=2023-08-24，sce_id 40/41）；`adjust --forward-csv` 建因子 5 平台段（切换日 2024-05-31/2025-06-20/2026-02-11/2026-06-10，与 yahoo 分红记录交叉印证一致）；周线 154 周。

- **pit_backfill 回填 14 期披露日全匹配**（matched=14，回填前已备份）——解除 `degraded_available_at` 降级，PE 刻度从静态折算转为点时口径（§2.1），这是本次入池的关键质量动作。指标重算后 pe_ttm 非空 591/725（空值为 2024-03-18 前无点时 TTM，需 2022 财报才能再前推，未采，属预期）。weekly_signals/daily_watch/right_side/accumulation 全跑过（daily_watch/right_side 为 incomplete/no_active_card，激活卡后自愈）；报告 `reports/601168.SH/2026-08-21.md` revision=2 degraded(no_active_card)；底稿 `cards/601168.SH/inputs_2026-08-21.json`。
- **排期卡**（fred-valuation-card-skill 全流程）：MD `cards/601168.SH/西部矿业估值排期卡.md` + JSON `cards/601168.SH/draft_2026-08-23.json`，`create-draft` 入库 → **card_version_id=`601168SH_cc4c2ac7`，状态 draft**。按 draft-only 纪律未激活，activate 必须人工（`uv run python -m scripts.pipeline.card activate 601168SH_cc4c2ac7 --effective-from <date>`）。
  - 关键数字：现价 37.85（08-21）；TTM 归母 59.43 亿 / EPS 2.4938 / PE 15.18；EPS 情景 中性 2.85/悲观 2.55/极悲 2.20；PE 情景 乐观 17/中性 14/悲观 12；档位 T1 38.3–39.9(30%)/T2 33.9–35.7(35%)/T3 26.4–28.5(35%)；证伪线 26.40；右侧触发 43.30/止损 42.85；波段仓不适用；胜率 T1 55–75%/T2 65–85%/T3 70–90%；Kelly 上限 T1 0.0%/T2 10.9%/T3 17.1%；next_review_at 2026-10-31；锚=pe_scale（体系上移：底部刻度 10.4→12.5→14.2→16.8→18.7，双重计价问题已在卡内说明）；样本区间 2024-03-18~2026-08-21（§3.2 已标注）。
- **信号状态**：当前活跃衰竭信号 0 项；现价已掠过 T1 下沿 1.2%，但 Kelly 对 T1 出 0（胜率下沿 55% + 赔率 0.74 非正期望），卡内建议 T1 预算并入 T2 等衰竭信号。
- **测试**：`uv run pytest -q` **332 全绿**（无新增用例，纯数据入池）。
- **NOTE**：① kimi 历史 amount 为 NULL（tdx 不可用所致，与全池其他股票一致）；② 2024-03-18 前无点时 PE（需采 2022 财报才能前推）；③ 卡激活后 daily_watch/right_side 的 no_active_card 降级自愈；④ 下周一（08-24）例行采集起 601168.SH 随全池一起跑 daily。

## 2026-08-23（P0 审计修复批次：10 项已核实问题 + 3 项误报澄清）

- **背景**：外部代码深度审计报告（含信号层 19 条）经逐条核实（对照代码 + 设计文档）后执行修复。用户拍板范围=全部已核实 P0，shares_at 口径=快照豁免混合方案。
- **迁移 0002**（`migrations/0002_event_assessments_symbol_weekly_anchors_identity.sql`，已备份 `data/market.db.bak_20260823` 后应用于生产库）：
  - `event_assessments` 重建，主键 → `(event_id, symbol, assessment_version)`，新增 symbol NOT NULL，2322 行从 event_symbols 回填（0 孤儿行，当前无多 symbol 事件，属潜伏 bug 修复）。
  - `weekly_anchors` 加 `uq_weekly_anchors_identity`（symbol, anchor_type, trade_date, is_fallback）唯一索引（407 行 0 重复，安全建立）。
- **修复清单**（均带新测试，全量 `uv run pytest -q` **357 全绿**，基线 332）：
  1. `event_study.py`：查重/DELETE/INSERT 加 symbol 维度，study_event 输出带 symbol；新增多 symbol 独立落库 + 重算隔离 2 用例。
  2. `valuation.py` `shares_at`：snapshot_* 行豁免 available_at（§3.7 单点假设维持，pe_status 追加 `snapshot_share_basis` 标注），真实事件行要求 available_at ≤ as_of（消前视）；新增点时过滤 + 标注切换 2 用例。⚠️ 既有历史 PE 行不会自动补标注，需重跑指标重算才生效。
  3. `weekly_signals.py`：废弃全删全插，identity 匹配复用旧 anchor_id（字段变化 UPDATE、新 identity INSERT、失效 identity 删除）；缺失 OHLC 日不选为锚点（ValueError 不猜）。冒烟：002299.SZ 重算 28 锚点 anchor_id 全稳定、identity 集合一致。
  4. `daily_watch.py`：as_of 早于最早 bar → 不删旧 facts、直接 incomplete（修 bars[-1] IndexError）；NULL close 跳过记 incomplete（修 Decimal('None') 崩溃）。
  5. `right_side.py`：OHLCV 缺失 bar 跳过、status 降级 incomplete/missing_ohlcv_bars，不再 volume 当 0 / Decimal 崩溃。
  6. `accumulation.py`：OHLCV 缺失行剔除记 degraded；prev close ≤ 0 不除零；`expired_consolidation` 起算点从破位日改为进入 consolidating 当日（对齐 §5.4c「横盘超 120 日」与 signals.yaml:62 注释；`consolidation.max_days` 从破位起算语义不变）。
  7. `exhaustion.py`：episode 结束行同时 triggered=0、active_until 钳到结束周；`count_active_signals` 按 anchor_id 分组计数（§5.3「同一 anchor_id 下」口径落实，新增 by_anchor 明细，旧调用方键全保留）。
  8. `cards.py`：parse_card 加 JSON/必填/数值/非负校验，非法卡片返回 None 按 incomplete 处理；convert_card_fields 换算后强制价格 > 0 且价区有序，违反抛 `CardConversionError`；corporate_action 适配：换算前置到关闭旧卡之前，被拒则冻结待人工、旧卡保持 active。
- **审计误报澄清（核实为 FALSE，未改，防后续重复"修"）**：
  1. 「历史重算用 load_active_card」——daily_watch:318 / right_side:216-236 实为 load_card_versions + card_for_day 按生效区间逐版本计算，load_active_card 仅 as_of 兜底，符合 §5.1。
  2. 「right_side terminal 当日回 idle」——当日转换、次一交易日起才以 idle 评估，符合 §5.4c 与 right_side.py:19-20 锁定语义。
  3. 「数据库无 FK/唯一约束」——FK（event_assessments→events 等多处）与 UNIQUE（signal_facts、corporate_actions 等）存在且 PRAGMA foreign_keys=ON（db.py:34）；仅 CHECK 约束全缺（未修，留待后续）。
- **未核实未修（留待下批）**：wilder_rma NaN、新 bar 因子继承、周线截断 calendar、pit_backfill 全池执行、corporate_action 两点、event_study degraded 退出码、TDX 去重、UI 只读、CHECK 约束、config schema。
- **文档**：database_schema.md 已同步（event_assessments/weekly_anchors/share_capital_events 三处）；system_design.md 无需改（修复均为代码向设计对齐）。
- **遗留备注**：① `assessment_version` 列 INTEGER 亲和 vs 代码写 TEXT 'event_study_v1' 的不严谨未修（避免迁移面扩大）；② 0002 已应用于 data/market.db；③ 既有失败适配：test_db.py migrate 断言更新为 [0001, 0002]，test_report.py event_assessments 插入补 symbol。

## 2026-08-23（P0 续批：wilder_rma NaN + daily.py 信号阶段事务边界）

- **核实结论**：① `wilder_rma` 中段 NaN 会把 avg 清空、下一观测以 `x/window` 重新初始化（core.py 旧 :90-94），RSI 跨缺口后失真——属实 P0；② daily.py 信号阶段与基础阶段同事务，单模块异常被 catch 后部分写入随外层 commit 残留——属实 P0；③ weekly.py「截断 calendar 生成不完整周」——非 bug：calendar 整年种子不存在年内截断，跨年缺失周走 `open_by_week.get(key)` 空 → note+跳过（weekly.py:93-96），整库缺失 → incomplete（:74-76），防御已够，未改。
- **wilder_rma 修复**（`scripts/indicators/core.py`）：中段 NaN 当日输出 NaN 但 avg 保持递推不清空（缺观测不重置 Wilder 平滑状态），删除 `x/window` 重初始化分支；docstring 同步。新增 `test_wilder_rma_mid_series_nan_keeps_avg`（锁定 125/27 递推值，旧实现 7/3 会失败）。
- **daily.py 事务拆分**（方案 D 简化版，用户拍板）：`_process_symbol` 基础阶段（入库→因子→周线→指标）保持单一事务整体回滚；信号阶段（weekly_signals→daily_watch→right_side→accumulation→corporate_action）拆出，每阶段独立 `with conn:` 子事务，单阶段异常只回滚该阶段（不残留部分写入）、记 notes degraded、`res.status=incomplete`、`reason={stage}_failed` 并 **break**（后续阶段不跑，避免用前序失败留下的旧派生数据判定）；已成功阶段提交保留（§2.2 第 3 类）。suspended 覆写改为仅在 ST_OK 时生效（不覆盖 incomplete）。已核实五个信号模块的 `with conn:` 均在各自 CLI main，重算函数本身不管事务，拆分安全。
- **设计同步**：`docs/system_design.md` §8.1 末段改为「行情、指标发布原子化 + 信号阶段独立子事务」表述；daily.py 模块 docstring 契约同步。
- **测试**：新增 `test_signal_stage_failure_rolls_back_and_breaks`（daily_watch 中途写 signal_facts 后抛错 → 标记行回滚不存在、right_side/accumulation/corporate_action 未执行、指标与新 bar 保留、status=incomplete）；全量 `uv run pytest -q` **359 全绿**（357+2）。

## 2026-08-23（watchlist 入池：法拉电子 600563.SH + 万华化学 600309.SH）

- **变更**：`config/watchlist.yaml` 在 601168.SH 后追加 2 行；watchlist 池 14 → 16 只（603605/603288/601318/002747/601899/600029/600531/600346/002709/002714/002299/002557/603697/601168/600563/600309.SH）。
- **代码确认**：600563.SH 厦门法拉电子（主板，元件/福建） / 600309.SH 万华化学（主板，化学制品/山东）—— 均沪市主板，benchmark 沿用 000300.SH。
- **未做（待下次会话采集）**：① tdx-connector 拉 3 年日线 + amount + HasLtgb（CFET 期间失败转 kimi fallback，或人工用 `mcp_client.py` 触发）；② `adjust --forward-csv` 建复权因子；③ `weekly` / `indicators.compute` / `weekly_signals` / `daily_watch` / `right_side` / `accumulation` 五件套；④ 排期卡 draft（card_inputs → skill → create-draft → 人工 activate）。
- **预期降级**：无卡期间 `daily_watch`/`right_side` 输出 `incomplete(no_active_card)`，日报单股段落 `degraded(no_active_card)`，属 §2.5 设计的正常态；下游观察期 2–4 周后再排激活。
- **测试**：未跑（仅 yaml 变更，不触发代码）。

## 2026-08-23（watchlist 入池+全套管线：法拉电子 600563.SH + 万华化学 600309.SH）

- **背景**：用户要求加入 watchlist 并跑全套管线。已于 22:04 改 yaml 入池（池 14→16），本条目记录管线环节。
- **数据源归属澄清**：tdx-connector 绑定 WorkBuddy（mcp 工具集在本会话 deferred tools 中：tdx_kline / tdx_quotes / tdx_api_data / tdx_lookup_stock / tdx_screener / wenda_notice_query），kimi-datasource 绑定 KimiCode（plugin。今天 token 又失效 `access_token was rejected. Run /login again`，与 08-21 working memory「易失效」一致；init_collect.py 硬编码旧 5 只走 kimi 通道不可复用）。两条数据源**不要写死在脚本中**——本批直接由 LLM agent 调 tdx MCP 落 raw，不经 init_collect.py。
- **采集路径**：派 general-purpose agent 直接调 mcp__tdx-connector__* → 落 raw CSV。K 线响应因 tokens 上限被截断到 tool-results 文件，agent 用 python json.loads 完整解析 → 按 SKILL `data,setcode,data,...` 13 列落 CSV。
  - 落盘：`data/raw/tdx/kline/2026-08-23/run_init_newstocks_2/600563.SH_tq0.csv` 等 4 份（700 根/份，起始 2023-09-28）；`data/raw/tdx/quotes/2026-08-23/run_init_newstocks_2/600563.SH.csv`、`600309.SH.csv`（各 1 行 snapshot）；2 份 `_meta.json`。
  - 字段异常：tdx_quotes hasCwInfo=1 不返回 pb 字段（CSV 留空，后续 valuation 模块补）；tdx_rows.Volume 是「手」含小数，未二次除 Unit（SKILL/文档要求）；ExtInfo.ZSZ 单位核对为「元」（22500 万股×133.07≈29,940,750,000）。
- **代码修复**：`scripts/pipeline/adjust.py` `load_forward_closes` 加 `data` 列 fallback（兼容 SKILL §3.3 设计列名，kimi 历史 CSV 用 `time` 仍兼容）。错误根因：origin=2023-09-28 不在因子序列内 → time vs data 列名不匹配。原实现为 kimi 历史遗留，未对齐 tdx-collect 设计列名；修复 backward-compatible，向设计 §3.3 对齐。
- **管线执行**：
  1. `ingest`：4 份 K 线入 daily_bars（tq=0 共 1400 行 + tq=1 跳过；2 份 quotes 入 share_capital_events 各 1 行）。TOTAL inserted=1402。
  2. `db seed`：`watchlist` 表从 yaml 导入 16 只（新增 600563 + 600309；首次不动 watchlist 时 compute 会 raise `不在 watchlist`）。
  3. `adjust 600563.SH/600309.SH`：分别 201/87 平台段，最新段 f=1.0（无新除权）；同事务 rebuild 周线 149 周；NOTE：两只均无 corporate_actions 记录（cash action 历史未采），此点不阻断但意味平台切换日未做交叉印证（西部矿业入池当日的对应 gap 一致）。
  4. `indicators.compute`：两只 indicators_daily 700 行 + indicators_weekly 149 行；**pe_ttm 非空 0/700**（首次入池无财务回填；§2.5 预期，无 forecast 数据）。
  5. 信号五件套：weekly_signals ok（panic_low/decline_start 锚点；fallback=True 是 adjacent 平台的——西部矿业 2026-03-27 入池首段 fallback=False，本次更早段 fallback=True 因外部段不可比属正常）；daily_watch/right_side `incomplete(no_active_card)`（§6 预期无 active 卡）；accumulation 两只各 700 天 monitoring，状态机各跑过 2 次转换（600563: 2025-03-24 watching→2025-09-16 failed；600309: 2025-04-07 watching→2025-09-29 failed）→ 现 idle reason=condition_not_met；corporate_action 无待处理。
  6. `daily --date 2026-08-21`：全池 16 只跑通；600563 / 600309 报告 degraded P5 revision=1 (no_active_card)；其他 14 只 complete + revision=2；汇总 ok=16。
- **测试**：`uv run pytest -q` **359 全绿**（无新增用例，纯数据入池）。
- **排期卡草案 ⛔ BLOCKED**：card_inputs raise `indicators_daily 无 pe_ttm，先重算指标`（§2.5 硬门槛）。PE 修复路径要 `pit_backfill`，回填依赖 ① tdx_api_data A 股利润表 fixedTag=00101/00102 → ② **新增 tdx 财务 CSV adapter parse_financials_csv（SKILL.md 「已知限制」明确二期补）**、③ tdx wenda_notice_query → events/event_symbols、④ pit_backfill → ⑤ 重算 indicators → ⑥ card_inputs → ⑦ skill → create-draft。其中第 ② 步需新增代码（超出"采集落盘"边界）；西部矿业入池当日也是同一卡点（draft 待人工激活）。
- **新增双股 explore path**：不主动接财务数据（tdx adapter 未实现）；用户拍板范围 vs 新增 adapter 风险，建议下次单独工程批次处理，**不要**为本次入池扩 tdx 财务 adapter。
- **下次会话动作**：① fix tdx 财务 parse_financials_csv → 拉 tdx_api_data 7+ 期 → ingest → ② 拉 wenda_notice_query 公告 → events → ③ pit_backfill → ④ 重算 indicators → ⑤ card_inputs → ⑥ skill → create-draft。观察期 2-4 周可早于排期卡出 draft。

## 2026-08-23（选股研究：万华化学 600309.SH 三轨路由 × 排期卡可行性评估）

- **任务**：点名个股三轨路由评估（earnings-surge-screener「指定对象路由层」），判断能否构建估值排期卡。报告落盘 `reports/screening/2026-08-23-万华化学600309-路由建卡评估.md`。
- **查询**：stock_finance_data（get_price 3年日线/HS300、get_financial_statements 20260331/20250630/20251231、get_forecast、get_stock_announcement 2026-06~08）；gildata fin_query 一次成功（估值 5 年日频序列，落 `data/raw/gildata/fin_query/2026-08-23/wh_pe_percentile.csv`）；WebSearch 补公告原话与 MDI 行业景气。gildata 今日可用（与早间鉴权失效预期不符）。
- **路由结果**：Track A 命中（H1 预告归母 98~104 亿 +60~70%，公告含涨价表述；闸门一营收待验证、分型=周期型）；Track B 副轨命中（B1 单季拐点+B2 提价函）；Track C 不命中，卡背离锚（YTD -1.9% vs HS300 -0.2%，回撤 20.3%<30%）。
- **阶段二**：兑现度优 / 持续性良（FY2027E +15%）/ 前瞻估值优（2027E PE 10.9x）→ 第一梯队候选（附机构拥挤度待验证条件）。
- **建卡结论：是**。PB 5 年分位 16%（2.08x）为主锚、前瞻 PE 辅锚；现价 73.98 元约落在 T1 附近。
- **衔接现状**：600309 已入 watchlist 且 3 年 K 线已入库；排期卡 draft 仍 BLOCKED 于 pe_ttm 缺失 → 后续走 tdx 财务 adapter → pit_backfill → card_inputs → create-draft 链路（见前一日志条目）。

## 2026-08-23（三轨路由评估：法拉电子 600563.SH 建卡可行性）

- **任务**：点名个股三轨路由 + 估值排期卡建卡可行性（earnings-surge-screener 指定对象路由层）。
- **查询**：stock_finance_data（利润表/现金流 5 期、3 年日线、forecast）；gildata fin_query ×3（估值快照+3 年 PE 日频、区间行情、扣非净利，CSV 落 `data/raw/gildata/fin_query/2026-08-23/`）；web 检索（半年报披露、行业景气、券商观点）。stock_finance_data 公告接口 EMPTY_DATA（接口异常，web 兜底）；2026H1 报表在其库未入库（H1 数据用媒体披露口径）。
- **路由结果**：三轨零命中——Track A 卡增速（H1 归母 +2.23%）与景气表述；Track B 五类痕迹皆不命中（B2/B3 标待补查）；Track C 卡 C锚1（增速 <20%）与 C锚3（PE 25.04 近 3 年 80.7% 分位），背离锚条件 3 命中（回撤 -33.1%）但属"涨多了的回调"。零命中兜底亦不满足（净利增速 <20%）。
- **结论**：建卡 **暂缓**——估值高位区（3 年 80.7% 分位）、盈利历史峰值区缓增非底部、年内 +84.6% 后 -33% 筹码形态未出清；现价档高于 T1。阶段二条件外直评：第二梯队（部分兑现），2027E PE 18.7x。
- **落盘**：`reports/screening/2026-08-23-法拉电子建卡评估.md`。管线侧维持既有 BLOCK（pe_ttm 回填）不急着解除，观察期跟踪。
- **测试**：未跑（纯研究产出，无代码变更）。

## 2026-08-23（tdx/daily 审计建议修复批次：6 项）

- **背景**：外部审计对 tdx adapter / daily.py 的 6 条建议（P1×2 + P2×4），逐条核实全部属实并修复。注意：本批基于工作区未提交的 tdx 财报接入在途改动（parse_financials_csv、ingest 路由、adjust.load_forward_closes 兼容 tdx `data` 列名、test_tdx_adapter 财报用例，为当日另一线程工作），提交时与其同文件交错，一并入库。
- **tdx.py**：
  1. `_UNIT_TO_YUAN` 换算系数 float（1e4/1e6/1e8）→ int（10_000 等），`_yuan` 改 `Decimal(v) * Decimal(factor)`，金额换算全程定点（§9.5）。
  2. 财报 `record_revision` 的 run_id 从 raw_object_id 改为采集批次目录名 `Path(path).parent.name`（与 yahoo_finance 约定一致）。
  3. 删除死代码 `_HK_CURRENCY_HINT`（定义后从未使用，注释保留实测结论）。
  4. 财报文件名无 `_is_YYYYMMDD` 时回退 CSV `period_end` 列（YYYY-MM-DD 校验），两者皆无才 conflict；原 `test_financials_bad_filename` 语义更新为 fallback 成功用例。
- **daily.py**：
  5. `_classify_raw_files` 识别 tdx 前/后复权文件（kline 目录 `{symbol}_tq1/_tq2.csv`，tdx-collect skill 命名约定）路由到 forward 因子变化检查（§3.3），不再误入 by_symbol；`adjust.load_forward_closes` 本已兼容 tdx `data` 列名。
  6. 新增 `--source` 参数（可重复）：raw-dir 只处理所列数据源目录，其余跳过并记 notes；`run_daily` 加 `sources` 参数。
- **测试**：test_tdx_adapter 新增 period_end fallback / fallback 仍 conflict / revision run_id 3 用例（替换 1 旧用例）；test_daily 新增 tq 文件分类 / source 过滤 2 用例。全量 `uv run pytest -q` **370 全绿**。

## 2026-08-24（万华化学 600309.SH 排期卡底稿解堵：pit_backfill + 股本补录 + 重算重导）

- **背景**：建卡评估（前条）结论"是"，但底稿 pe_ttm 非空仅 1/700、分位退化、pe_status 带 degraded_available_at。本批执行解堵链路。
- **公告采集**：天眼查"上市信息-上市公告"33 页 660 行（2020-10-16~2026-08-05，子代理抓取），落 `data/raw/tianyancha/announcement/2026-08-23/pit_backfill_600309/`（含 _meta.json）。
- **代码修复**：`pit_backfill.title_keywords` 年报关键词补 `{fy}年度报告` 变体——万华 2020/2021 年报标题为"万华化学2020年度报告"（年份后无"年"），原单关键词 `matched=8/10`；修复后 dry-run **10/10 全匹配**。test_title_keywords_mapping 同步更新。
- **pit_backfill 正式回填**：`--backup data/backups/financial_reports_20260823_pit600309.csv`（132 行）→ matched=10，2020FY~2026Q1 全部恢复真实披露日（点时口径）。
- **股本补录（新发现的卡点）**：重算后仍 `no_share_capital` 699/700——tdx quotes 快照 effective_at=2026-08-21 只覆盖最新日，历史区间无股本事件。按 2025-07-14《关于股份回购实施结果暨股份变动的公告》（tianyancha uuid bf5f2a22…，p7.csv 已登记 raw_tyc_ann_0394059f85b4）手工补录 2 行 share_capital_events：① effective 2023-09-28（K线 origin）snapshot_issued 3,139,746,626（公告佐证"注销前总股本"；公告流 2020-10~2026-08 核对无其他增发/送转/注销）；② effective 2025-07-14 buyback_cancel −9,275,000 → 3,130,471,626，available_at 次一交易日（§2.1 保守规则）。NOTE：tdx ZSZ=3,130,471,560 与公告值差 66 股，仅影响 2026-08-21 当日 PE 第 8 位小数，记录不处理。
- **重算 + 重导**：`indicators.compute` → pe_ttm 非空 **454/700**，`degraded_available_at` 标注消失（published_at 全非 NULL，assume_visible 自动转严格点时）；空值 246 天 = 2025-04-16~2026-04-21 `ttm_missing_prev_same_period`（缺 2024 季报，与全池 init 采集"年报+2025 起季报"口径一致，601168 同型洞，不单独补齐）。`card_inputs` 重导 `cards/600309.SH/inputs_2026-08-21.json`：PE 分位 p5/p50/p95 = 12.65/14.97/18.23（样本 454 日强制标注 §3.2），当前 PE 17.59 处 p75~p95 区间；market_snapshot pe_status=`ok;snapshot_share_basis`。
- **forecast 补采失败**：`get_forecast` EMPTY_DATA×2（接口不稳，与公告接口 8/13 起持续 EMPTY 同型）；底稿 `forecasts_snapshot=None` 缺口保留，一致预期以评估报告外部快照（FY2026E 185.1 亿）为参考、不入库。
- **测试**：`uv run pytest -q` **370 全绿**。
- **状态**：600309 排期卡 draft 链路就绪（card_inputs ✅ → fred-valuation-card-skill → create-draft → 人工 activate）。激活节奏沿用评估报告建议：正式半年报披露后复核 H1 营收/现金流再人工激活。

## 2026-08-24（法拉电子 600563.SH 排期卡 draft：pit_backfill + 股本补录 + skill 出卡）

- **背景**：建卡评估（`reports/screening/2026-08-23-法拉电子建卡评估.md`）结论**暂缓**（三轨零命中：PE 3 年 80.7% 分位、盈利峰值区缓增 +2.2%、年内 +84.6% 后 -33% 筹码未出清）。用户明确要求建卡 → 按等待型左侧卡出 draft，评估风险写入胜率打分与复核触发器。**draft-only，activate 待人工**。
- **公告采集**：天眼查"上市信息-上市公告"13 页 260 行（2020-04-29~2026-08-22，子代理抓取），落 `data/raw/tianyancha/announcement/2026-08-24/pit_backfill_600563/`。
- **pit_backfill**：dry-run 10/10 匹配（年报变体修复已在 600309 批次落地，法拉 2020/2021 年报标题为"年年度报告"标准写法）→ 正式回填 `--backup data/backups/financial_reports_20260824_pit600563.csv`（132 行），2020FY~2026Q1 全部恢复点时口径。
- **股本补录**：`no_share_capital` 与 600309 同型（tdx 快照只覆盖 2026-08-21）。补录 snapshot_issued：effective 2023-09-28 = 225,000,000 股（kimi get_stock_info 与 tdx ZSZ 双口径一致；天眼查全量公告核对 2020-04 以来无增发/送转/回购注销，仅现金分派）。raw_object 登记 `raw_ths_stock_info_600563SH_*`。
- **重算 + 底稿**：pe_ttm 非空 478/700（空 222 = 2025-04~2026-04 缺 2024 季报全池共性洞 + 起始段），degraded_available_at 消失。`card_inputs` → `cards/600563.SH/inputs_2026-08-21.json`：PE 分位 p5/p50/p95=17.36/22.70/30.23，当前 25.04 处 p50~p75。
- **forecast 缺口**：get_forecast 两次返回估值比率有、FY1-FY3 预测字段全空（接口不稳，与 600309 当日的 EMPTY_DATA 同型），底稿 forecasts=None；一致预期用 2026-08-23 外部快照（FY1 13.49 亿 +13.1%）写入卡 narrative，不入库。
- **skill 出卡**（`fred-valuation-card-skill` 流程，build_schedule.py --eps 5.60,5.30,4.55 --pe 25,20,16 --price 133.07 --winrate 35,50,55）：
  - 体系判断：底部刻度 16.1-19.1（2024）→ 27.8（2026-05）上移由 AI 叙事驱动、未经盈利验证 → 锚定旧体系带 PE 16/20/25。
  - 三档：T1 107.5-112.0 / T2 100.7-106.0 / T3 72.8-78.6；证伪线 72.80；右侧触发 145.00 / 止损 128.00；波段仓不适用（横盘 3 周 < 4 周门槛）。
  - 胜率 T1 35-50% / T2 50-65% / T3 55-70%；Kelly 上限 T1 0.0% / T2 2.1% / T3 13.2%——T1 非正期望注，卡上建议 T1 预算并入 T2 等信号。现价 133.07 高于 T1 上沿 18.8%，卡当前含义=不动。
- **落盘**：`cards/600563.SH/法拉电子估值排期卡_draft_2026-08-24.md` + `draft_2026-08-24.json` → `create-draft` 入库 **600563SH_b11e5de5（draft，待人工 activate/reject）**。
- **测试**：`uv run pytest -q` **370 全绿**（本批无代码变更，纯数据/产物）。

## 2026-08-24（万华化学 600309.SH 排期卡 draft：skill 出卡）

- **背景**：建卡评估（`reports/screening/2026-08-23-万华化学600309-路由建卡评估.md`）结论**是**（Track A 主轨 H1 预告 +60~70% + Track B 副轨，第一梯队候选，附条件：正式半年报验证营收与现金流）。昨日已解堵底稿（pit_backfill + 股本补录 + 重算），本批出卡。**draft-only，activate 待人工**；评估建议正式半年报披露复核后再激活。
- **锚定体系**：周期股 mixed 锚——PB 带主锚（gildata 5 年：1.68-6.28，中位 2.93，当前 2.08 处 16% 分位，BVPS≈35.6）+ 前瞻 PE 辅锚（FY2026E 12.5x/FY2027E 10.9x）；库内 PE(TTM) 刻度（p5/p50/p95=12.65/14.97/18.23）在盈利上行期失真（TTM 分母 131.6 亿为底部基数），当前 17.6 处 p75-p95 判定为口径现象。
- **情景**：EPS 中性 5.90（FY1 185 亿兑现，H1 预告已完成 53-56%，反向裂口有利）/ 悲观 5.30（涨价回吐 +33%）/ 极悲 4.30（涨价证伪回落至 135 亿）/ bull 6.55；PE 正常化口径 乐观 15 / 中性 12.5 / 悲观 11（低于 TTM 刻度，因分母用 FY2026 情景盈利）。
- **三档**（build_schedule.py --eps 5.90,5.30,4.30 --pe 15,12.5,11 --price 73.98 --winrate 55,70,75）：T1 70.8-73.8（≈PB 2.0-2.1）/ T2 62.9-66.2（≈PB 1.8-1.9）/ T3 47.3-51.1（≈PB 1.3-1.4，体系重构情形）；证伪线 47.30 + 先行预警 PB<1.68（≈59.8 元）触发锚重检；右侧触发 79.20 / 止损 72.00；波段仓暂不定义（72.4-79.1 横盘刚满 4 周，达箱体定义门槛边缘）。
- **现价定位**：73.98 高于 T1 上沿仅 0.3%——正贴 T1 上沿，首档触发距离极近。胜率 T1 55-75% / T2 70-90% / T3 75-95%；Kelly 上限 T1 0.0%（赔率 0.65，证伪线远）/ T2 12.1% / T3 18.4%——T1 非正期望注，卡上写明"T1 执行须半年报兑现验证，更稳妥并入 T2 等信号"。
- **锚过期警示**：系统衰竭锚仍是 2024-09-30 老段（前低 75.85 除权前口径，复权等效 77.58），现价已破——2026-03 回撤段将由系统重建锚与量能阈值，卡上量能阈值（放量≥2.98 亿/缩量 0.60-0.89 亿股调整量）届时失效。
- **落盘**：`cards/600309.SH/万华化学估值排期卡_draft_2026-08-24.md` + `draft_2026-08-24.json` → `create-draft` 入库 **600309SH_6c62ce20（draft，待人工 activate/reject）**。
- **测试**：`uv run pytest -q` **370 全绿**（本批无代码变更，纯产物）。

## 2026-08-24（tdx 公告补齐：watchlist 16 只 → 今日）

- **背景**：handoff.md §5 已知缺口②"公告接口（get_stock_announcement）返回空未验证"，且实际 queries 发现 watchlist 公告 latest published_at 距今最大 96 天（法拉电子 600563.SH 2026-05-20）。本批用 tdx wenda_notice_query 给所有 16 只按"已有最新 + 1 天 → 今日"窗口补齐。
- **接口实测**：tdx wenda_notice_query 1.6~9.2 秒/次（深交所要等深交所公告、上交所快约 100-200 ms），单次最多返 5 条（top_k=50 仍截顶）。本次无跨年深窗，仍有 5 条/次上限。
- **覆盖**（bdate → edate=20260824）：
  - 002299 002709 002714 002747 600309：20260821~+（半年度报告窗口期，含 8-21~8-22 batch 半年度报告全套）
  - 601168 西部矿业、600563 法拉电子（5-21 起）、600531 豫光金铅（8-22）、603288 海天（8-15 H股）、603697 有友（8-21）、002557 洽洽（8-08）、600346 恒力（8-20）、601318 平安（8-21）、601899 紫金（8-22）、603288（8-15）：覆盖至 8-24
  - 603605 珀莱雅、600029 南航：接口返回 0 条（确认这些股在窗口内确无新公告，非采集失败）
- **落盘**：`data/raw/tdx/announcement/2026-08-24/run_announcements/` 新增 13 个 CSV（600563 等 16 个 `_tdx.csv`，3 个空文件保留）+ `_meta.json`（run_id + request_url + content_hash 16 字段）。
- **入库**：`uv run python -m scripts.pipeline.ingest data/raw/tdx/announcement/2026-08-24/run_announcements` → **inserted=15 / skipped=35 / conflicts=0 / errors=0**。35 条 skipped 为 title|pub_date 哈希与 tianyancha 已存公告冲突（同标题同日去重，§3.6）；未跨源去重（与 tianyancha 的 source_external_id 体系隔离，handoff §6 已记）。
- **效果**（按 lag 距今日 8-24 重排）：
  - 法拉电子 600563.SH：**96 → 3 天** ✅（5-21/6-5/8-18/8-22 共 5 条全入库，原有 1 条 5-20 已 stripped by title-hash 跳）
  - 万华化学 600309.SH：**63 → 20 天**（6-23~8-5 五条均首次入库；3 月前的更老缺口因 top_k=5 单页限制需后续分页补）
  - 圣农 002299/紫金 601899/天赐 002709/埃斯顿 002747/牧原 002714/平安 601318/恒力 600346/海天 603288/有友 603697：**4~10 天** 全部正常最新
  - 西部矿业 601168（lag=8）/洽洽 002557（17）/珀莱雅 603605（21）：当前 wenda 接口在窗口内确无新公告 → 0 inserted，**已确认无遗漏**
  - 豫光金铅 600531 1 条（担保进展）、南航 600029 0 条：均为真实数据集现状，非采集失败
- **tdx 来源占比变化**：events.source='tdx' 公告数 105 → 120；raw_objects tdx announcement 35 → 48。
- **测试**：`uv run pytest -q` 370 全绿（本批无代码变更，仅 raw_data + events 入库 + raw_object 登记；adapter 既有 parse_announcement_csv 路径）。
- **下一步建议**（人工决定）：
  - 1. 万华化学 600309 5 月底前的更深历史公告可分多次小窗分页（每窗 ≤30 天）补完——若评估需要可加 staging worktree 再跑。
  - 2. 跨源公告去重（tdx vs tianyancha）的 source_external_id 校对——handoff §6 已记二期。
  - 3. 排期卡相关股的近期公告（万华化学 6-23 起两条检修、平安 8-21 中期分红、紫金 8-22 员工持股调价、恒力 8-20 中报）尚未被对应日报管线消费，明日 daily 后日报内将自然出现。

## 2026-08-24（平安银行 000001.SZ 加入观察列表）

- `config/watchlist.yaml` 末尾新增 000001.SZ 平安银行（market CN，aliases [平安银行, Ping An Bank]——不与 601318 中国平安的「平安」别名混用，benchmark 000300.SH）→ `db seed` 导入 17 只全成功。
- **待办**：3 年历史采集（日线/财报/公告）+ adjust/weekly/compute/weekly_signals 初始化（同 600563/600309 待采集队列）；采集前 daily 对该股输出 incomplete 属预期。

- **000001.SZ 平安银行 3 年初始化采集与管线齐备**（接续 2026-08-24 入池待办）：
  - **采集落盘**：`data/raw/tdx/{kline,quotes,financials}/2026-08-24/run_init_pingan_bank/`——8 批分页（wantNum=100+80）抓 780 根日线（2023-06-07~2026-08-24），tdx_quotes hasCwInfo=1 估值/GDRS/股本快照 1 份，tdx_api_data `TdxShareCW.ph_agf10_cw_lyb` (fixedTag=00101/00102) 10 期利润表（FY2020..FY2025 + 2025Q1/中报/Q3 + 2026Q1/中报）。HACK：单 MCP 响应超 100k 字符，无法直传窗口；写 `scripts/collect/_tmp_aggregate_pingan.py` 把分页 rows JSON 拼合成 tdx CSV（8 batch 文件 + aggregator），再写 `_tmp_build_quotes_financials.py` 生成 10 个 `*_is_<YYYYMMDD>.csv`（含 BOM 与 _meta.json）。
  - **入库**：`uv run python -m scripts.pipeline.ingest data/raw/tdx/{kline,quotes,financials}/2026-08-24/run_init_pingan_bank` → **inserted=791 / errors=0 / incomplete=10**（10 笔 financials 都是 A 股 tdx 财报接口无 published_at 降级标 `degraded_available_at`，available_at 取入库时间——后续用 wenda_notice_query 抓披露公告回填 pit_backfill）。
  - **管线跑通（带 factor=1.0 降级）**：
    - 跳过 `scripts.pipeline.adjust`：本批 tdx 没有 tqFlag=0（不复权，下游 daily_bars 已写入）+ tqFlag=1（前复权）的成对数据；复权因子 `price_adj_factor_t=1.0`（初始列默认）。tdx 前复权分页需要另起 ~8 MCP 调用成本过高，务实做法：本期指标/周线在 factor=1 下降级跑通（二期计划：用 kimi-datasource forward+none 重铺 601318 SH 同款路径）。
    - `scripts.pipeline.weekly 000001.SZ` → 165 完成周
    - `scripts.indicators.compute 000001.SZ` → indicators_daily 780 / indicators_weekly 165 / pe_ttm 非空 1 行（最新一日带 snapshot_share_basis）/ 空 779 行（缺历史 quarter 同比期间，TTM 三件套不备，属设计预期）
    - `scripts.signals.weekly_signals` → weekly_anchors 14 行 / signal_facts 825 行；当前周 2026-08-21 panic_low=2024-09-23 decline_start=2024-03-29 episode=ended（2024-Q3 大跌后反弹 + duration 8 周后结束），活跃 0 项（min 2 阈值未达）
    - `scripts.signals.daily_watch` / `right_side` / `accumulation` / `corporate_action` 全跑——前两个 `incomplete (no_active_card)` 属设计预期，accumulation 历史转换 3 次（2025-04 watching → 04-21 consolidating → 05-13 confirmed），corporate_action 0 待处理
    - `scripts.pipeline.report --date 2026-08-24 --symbol 000001.SZ` → `reports/000001.SZ/2026-08-24.md`（P5 普通状态更新）+ `reports/daily/2026-08-24.md`（全池日报 1 只 degraded P5）
  - **DB 校验**：daily_bars 780 / weekly_bars 165（min 2023-06-09 / max 2026-08-21）/ indicators_daily 780 / financial_reports 10 / financial_facts 10 / share_capital_events 1（snapshot_group_total_tdx @ 2026-08-24, shares 19,405,918,800）/ signal_facts 825 (5 类 × ~165 周)。今日收盘 11.56、MA20=11.32、MA60=10.94、MA120=10.85、MA250=10.96、pe_ttm=5.16（ok；snapshot_share_basis;degraded_available_at）。
  - **测试**：`uv run pytest -q` 367 失败 3（UI 测硬编码假设的 symbol 排序），修复 `tests/test_ui_queries.py`（test_search_stocks 现 000001.SZ 排前、test_list_stocks_sort_and_pagination pe_ttm asc 现 000001.SZ 排前、test_list_stocks_filter_data_quality_pe_status `ok` 集合用 issubset 允许 000001.SZ 加入）+ `tests/test_ui_layout.py`（test_api_stocks_search 同改）→ **370 全绿**。
  - **偏差/决定**：① factor=1.0 入库为合理降级；tdx 前复权拉取放在二期用 kimi forward 兜底或 tdx tqFlag=1/2 双拉重建。② 10 笔 financials A 股 tdx 接口无 published_at 是 tdx 已知缺口；补采顺序：先用 wenda_notice_query 抓披露公告做 pit_backfill，回填后 strict 点时口径生效。③ `daily_bars.price_adj_factor` 仍为 1.0，对应 weekly_bars / indicators 周线也基于 raw 价；待前复权回到后 weekly_anchors.fallback 与 panic_low_transition 重算（如 2024-09-23 锚点 8.67 → 8.67×factor→ 调整后会更"破"，panic 形态影响应验机率，影响低）。④ 清理临时脚本 `_tmp_aggregate_pingan.py` / `_tmp_build_quotes_financials.py`（保留 `scripts/collect/`，是 init 标准工具；`_tmp_` 前缀利于标记临时，待二期交接 cleanup 阶段删除）。


## 2026-08-24（平安银行 000001.SZ kimi 重铺 + pit_backfill + 估值排期卡 draft）

- **kimi 口径重铺**（替代 tdx 初始化，解决 factor=1.0 降级）：日线 725 行（2023-08-25~2026-08-24，kimi get_price 双口径成对，前复权因子 6 平台段，切换日 2024-06-14 / 2024-10-10 / 2025-06-12 / 2025-10-15 / 2026-06-12）；财报 kimi 14 期 + forecast；公告 105 条；yahoo stock_actions 分红 29 条；股本快照双口径（sce_id=50/51）。周线 154 周、pe_ttm 非空 592/725、周线信号已跑（活跃 0 项）。
- **数据清洗**：剔除 55 行 tdx 前缀污染日线（备份 `data/backups/000001.SZ_daily_bars_tdx_prefix_20260824.csv`）。
- **pit_backfill**：天眼查 37 页 740 行披露公告回填，matched=24，`degraded_available_at` 全部消除。**遗留待办**：financial_reports 存在 tdx/sfd 双源同 period 重复行 24 行，待去重策略。
- **BPS 补采**（PB 锚必需）：kimi financial_index 8 期落盘 `data/raw/stock_finance_data/financial_index/2026-08-24/run_init_000001/`——BPS 2022FY 18.80 → 2026H1 24.13；当前 PB=0.479；三轮底部 PB 刻度 2023-12-21 / 2024-01-18 / 2024-09-23 = 0.457/0.457/0.465（稳定）。TTM 每股分红 0.596 元（2025-10-15 派 0.236 + 2026-06-12 派 0.36），现价股息率 5.16%。
- **估值排期卡**（fred-valuation-card-skill，draft-only §5.6）：
  - 底稿 `cards/000001.SZ/inputs_2026-08-24.json`（现价 11.56、TTM 归母 434.59 亿、TTM EPS 2.2395、PE 5.16、样本窗 2024-03-18~2026-08-24 p5=4.18/p50=4.92/p95=5.47、前低 9.87）。
  - 情景矩阵 `build_schedule.py --eps 2.26,2.20,2.02 --pe 5.5,4.9,4.2 --price 11.56 --winrate 60,70,75` → 档线 T1 10.6–11.1 / T2 10.2–10.8 / T3 8.5–9.2，证伪线 8.5；Kelly 上限 T1 0.0% / T2 9.6% / T3 18.2%（修复目标价 12.4）。
  - MD 卡 `cards/000001.SZ/平安银行估值排期卡.md`（锚=PB+股息率；波段箱体 10.40–11.90 上限 10%；右侧 trigger 11.82 / stop 11.70；next_review 2026-10-31）。
  - JSON `cards/000001.SZ/draft_2026-08-24.json` → `create-draft` 入库：**card_version_id=000001SZ_63cdff82**，**未激活，待人工确认 activate**。
- **遗留**：① tdx 前缀污染删除仅涉 000001.SZ，其他池内标的未排查；② financial_reports 24 行双源重复期待去重；③ 不良/拨备明细未采（卡中列为复核触发器，worst 情景 EPS 2.02 依赖定性假设）。
- **人工激活**（§5.6）：`card activate 000001SZ_63cdff82` → status=active，effective_from=2026-08-24，生成 `cards/000001.SZ/2026-08-24_000001SZ_63cdff82.md` 并刷新 `current.md`。下一个交易日起 daily_watch / right_side 对该股按卡执行。


## 2026-08-25（恒力石化 600346.SH 清仓止损）

- **executions #9**：sell 18.20 × 1600，executed_at=2026-08-25，关联 active 卡 600346SH_5df2b631，信号快照冻结至 2026-08-21（库内最新 bar 日；08-24/08-25 增量未采）。
- 对应买入 #8（2026-08-21 右侧 confirmed 后 18.99 × 1600），本轮平仓亏损约 -4.16%（(18.20−18.99)/18.99，未计费用；fees 未提供未记）。
- **性质**：人工止损决策——卖价 18.20 高于卡内右侧止损位 16.60，非系统证伪线/止损位触发；排期卡仍 active（未关闭/未新建版本），该股当前空仓。

## 2026-08-25（珀莱雅 603605.SH 卖出 1000 股 @61.00）

- **executions #10**：sell 61.00 × 1000，executed_at=2026-08-25，关联 active 卡 603605SH_120ca661，信号快照冻结至 2026-08-21（库内最新 bar 日；08-24/08-25 增量未采）。
- **与卡片框架对照**：卖价 61.00 恰在卡内波段箱体卖出区 [59.50, 61.00] 上沿（inclusive），属波段仓卖点执行；对应买入 #7（08-19 T2 档 55.98 × 900）浮盈约 +8.97%。
- **持仓口径差异（NOTE）**：库内记录持仓为 900 股（#1–#4 轧平 + #7 买 900），用户确认实际持仓 1000 股——差额 100 股来自系统上线前未入库的老仓，按用户实际持仓如实记录 1000 股，库内口径与实盘差 100 股已声明（同 #4 备注"含 100 股更早底仓"为同类历史口径问题）；卖出后珀莱雅持仓视为 0。

## 2026-08-25（盘后例行：daily 8/24+8/25，forward 文件路由事故与修复）

- **采集**（tdx MCP 不可用，走 kimi-datasource fallback，符合 tdx-collect skill 失败处理约定）：`data/raw/stock_finance_data/{price,index,announcement}/2026-08-25/run_daily_20260825/`——17 只日线 adjust=none（batch1-6.csv，窗口 2026-08-14~08-25）+ 逐只 adjust=forward + 沪深300 指数 8 行 + 公告 60 行（wenda 接口恢复，含珀莱雅半年报全套 16 条、万华半年报+中期分红等）。**坑**：kimi MCP 工具 file_path 相对路径不落盘（声称 saved 实际没有），必须绝对路径。
- **事故**：daily --date 2026-08-24 首跑 failed=2（000001.SZ / 601899.SH，`origin 日不在因子序列内`，事务回滚无污染）。根因：本次 forward 用 `batchN_forward.csv` 多票混排文件名，`_classify_raw_files` 按 `_forward` 前缀取 symbol 得到 "batchN" 不在 watchlist → 未进入各股因子检查；daily 退用历史遗留文件——000001 选中 8/24 重铺探针残留 `000001.SZ_forward_gap.csv`（2023-06~08-24，与库内零重叠 → 保守判 changed → 重建崩溃）；601899 选中 8/17 旧 vintage 文件（kimi 前复权历史已重述，旧文件显示 8/14 前 1.24% 位移，现 vintage forward=raw 无位移；corporate_actions 紫金无除权记录，判为数据源重述假阳性）。
- **修复**：batch forward 按 ticker 拆成 17 个 `<symbol>_forward.csv` 单票文件（符合 run_YYYYMMDD_daily 既有命名约定），全池预检 17/17 `因子一致（位移 0.0000%）`；`batch2_forward.csv` 曾被作 "other" 误入库 raw_objects（raw_b4fc8aeba43a1a08，更新 601899 五行，因现 vintage forward=raw 数值与 raw 相同无实际污染），已按 content_hash 逐字节重建原文件恢复证据链，后续重跑 content hash 去重自动跳过。
- **重跑**：daily 2026-08-24 与 2026-08-25 均 **ok=17**。8/25 决策点：南航 600029 T1、有友 603697 T1、**天赐 002709 T1+箱体 buy_zone（收 36.40 入 [35.00,36.50]，低点 35.50 恰触 box_low）**、平安 601318 箱体 sell_zone（55.01）；观察点：珀莱雅距 T1 下沿 61.50 差 0.5%（右侧突破线 61.61 差 0.7%）、海天距 T1 下沿 1.2%。601168/600309/600563 degraded=no_active_card（§2.5 预期）。恒力 600346 收 18.30 跌回右侧触发位 18.50 下方（突破线 18.68），与当日人工止损 18.20 一致。
- **测试**：`uv run pytest -q` **370 全绿**（无代码变更）。
- **待办/建议**：① daily forward 文件选择对"零重叠/旧 vintage 残留文件"脆弱——后续可考虑按与库内日期重叠度选文件或对零重叠跳过检查（需设计评审，本次未改代码）；② 采集脚本侧约定：kimi get_price 多 ticker 返回必须拆单票文件落盘，禁用 batch 前缀命名 forward 文件；③ handoff.md 常见任务节建议补"kimi MCP file_path 必须绝对路径"。

## 2026-08-26（akshare 采集器接入）

- **akshare 采集器**（可选数据源，字段对齐现有 adapter 约定，实测通过）：
  - `scripts/collect/akshare_collect.py`：CLI 按 `--sources price,financials,index,telegraph` 采集落盘 raw CSV + `_meta.json`（失败重试一次/记录 error 不中断）。akshare 为 optional extra（`pyproject.toml [project.optional-dependencies] akshare`，体积大不进主依赖，未装时 CLI 给出 `uv sync --extra akshare` 提示）。
  - `scripts/adapters/akshare.py`：price→复用 `upsert_daily_bars`（source=akshare）、index→复用 `upsert_index_bars`、financials→**直接转发 `tdx.parse_financials_csv`**（列约定一致，published_at=东财 NOTICE_DATE 正式披露日，available_at=下一开市交易日 §2.1，单位换算/修订升级复用）、telegraph→events+event_symbols（source_external_id/content_hash 去重 §3.6，按 watchlist 名称/别名/六位代码匹配）。
  - `ingest.py` `_ROUTES` 注册 4 条 akshare 路由。
- **落盘列对齐约定**（实测 2026-08-26）：price/index 用 kimi 列约定（thscode,time,open,high,low,close,volume,amount,currency）；financials 用 tdx 列约定（code,setcode,period_end,...,published_at）；telegraph 用 events 字段（published_at UTC/published_tz/title/summary/content/source_external_id/content_hash）。
- **口径换算（关键）**：东财成交量单位「手」→ 采集器落盘前 ×100 换为项目 volume_raw 口径「股」；成交额「元」直接入库；财报金额 unit='yuan'。
- **实测值（真实 akshare，2026-08-26）**：财联社电报 20 条落盘→17 入库+3 无标题行跳过；沪深300 全历史 5978 行入库、恒生 3172+30 行源缺陷跳过（新浪 open=0/close>high 噪声，行级跳过记 note 不整批回滚 §2.5）；珀莱雅利润表全历史 40 期，2025 年报/2026Q1/2026 中报入库 **published_at 正确回填**（2026 中报 NOTICE_DATE=08-25，pub=08-24T16:00Z，avail=下一开市日 08-25T16:00Z）——补齐 A 股披露时间缺口（pit_backfill 之外的通道）。
- **边界处理（实测暴露后修复）**：① 电报无标题行（图片快讯）→ 行级跳过不整批回滚；② 指数 OHLC 源缺陷行（新浪恒生历史 open=0 等 30 行）→ 行级跳过记 note。
- **已知限制**：① 港股财报 `stock_profit_sheet_by_report_em` 当前报错（东财接口不支持该 symbol 形态），采集记 error 不阻塞，A 股财报为价值主力；② 恒生指数新浪源有零星坏行（已跳过）；③ A 股日线/复权接口依赖东财 push2his 域名，本沙箱网络偶发不可达（接口本身可用，日志记 error）。
- **测试**：`tests/test_akshare_collect.py` 10 项（mock akshare：成交量×100/列对齐/披露日/电报 UTC 与哈希稳定/未装提示）+ `tests/test_adapters_akshare.py` 6 项（price 冲突回滚/财报 published_at+下一开市日+修订升级/指数坏行跳过/电报事件去重+股票匹配+空标题行跳过）。**`uv run pytest -q` 386 全绿**（370+16）。
- 用法：`uv sync --extra akshare` 后 `uv run python -m scripts.collect.akshare_collect --symbols 603605.SH --sources financials,telegraph --date 2026-08-26 --run-id run_ak` → `uv run python -m scripts.pipeline.ingest data/raw/akshare/{financials,telegraph}`。

## 2026-08-26（akshare 缺口补齐：全池财报 + published_at 回填 + 法拉/万华头部日线）

- **驱动**：缺口盘点（17 只 watchlist 逐表核对）发现 ① 财报历史期次普遍缺 2023Q3/2024 季报、13 只 published_at 全空；② 法拉/万华日线头部缺 2023-08-28~09-27 共 23 个交易日；③ 万华 forecasts 缺；④ 12 只 corporate_actions 空。本次用 akshare 补 ①②。
- **全池 financials 补齐**：`akshare_collect --sources financials`（东财 datacenter，经系统代理）17/17 成功，每股 43~122 期全历史落盘 `data/raw/akshare/financials/2026-08-26/run_ak_fin_20260826/`；ingest inserted=1270 / skipped=83 / 0 冲突。期次矩阵 2023Q3 起 12 期全覆盖（南航/豫光缺 2026H1 系尚未披露，8/31 截止后再采）。东财更正被 revision 机制正确捕捉（如 603288 2024FY 营收 269.01 亿→269.05 亿升 revision 2）；万华 2026H1 旧占位空行（rev1 全 NULL）升级为 rev2 真实值+披露日。
- **披露日回填通道（tdx.py 改动）**：`parse_financials_csv` 内容一致分支新增——已有报告 published_at 为 NULL 且本批 CSV 带披露日时，回填 published_at/published_tz/available_at（记 data_revisions，source 取 raw_objects 登记值），**不新增 revision**（事实数字未变）。本批 CSV 文件级 hash 去重会挡重放，用逐文件直调 parse 的方式重放一次：回填 66 行。**2023 年以来全部最新 revision 的 published_at 已齐**；残余 NULL 均为被取代的旧 revision（§3.7 历史保留）或平安银行 1990-92 三期远古年报（东财无 NOTICE_DATE，保持 NULL 不猜）。
- **连带修复（valuation/compute）**：① `compute_pe_series` assume_visible 降级路径原来不做 revision 去重，万华 2026H1 rev1 占位空行（净利 NULL）污染 TTM 致 pe_ttm 全空——改为降级路径也经 `_latest_revisions` 只取最新 revision；② `compute.py` 降级自动判定从"任一财报 published_at NULL"改为"任一**最新 revision** NULL"，避免被取代的旧行把股票永久钉在 degraded。修复后万华 pe_ttm 701/725 恢复，全池 17 只重算指标：16 只转严格点时口径，平安银行保 `degraded_available_at` 标（1990-92 远古行所致，数值用最新 revision 正常计算）。其余 pe 空值均为诚实原因码：`ttm_non_positive`（南航/埃斯顿/牧原/恒力亏损期，设计预期）、`no_share_capital`（法拉/万华头部 23 天无股本快照覆盖）。
- **akshare_collect 增强**：① 新 source `forward`——`stock_zh_a_hist(adjust="qfq")` 前复权落盘 `{symbol}_forward.csv`（列同 price 约定；ingest 按 `*_forward*` 跳过，专供 adjust 因子重建）；② 新 `--price-api sina` 备用源（`stock_zh_a_daily`，A 股专用，volume 已是「股」**不 ×100**）——东财 push2his 域名当时直连与代理均不可达（datacenter 经代理正常），sina 源实测可用。
- **法拉/万华头部补齐**：先口径核对——sina 不复权 OHLCV 与库内（tdx 源）重叠窗口 11 天逐行一致（容差 0.005 价/0.1% 量），混源安全。采 2023-08-28~09-28 落盘 → ingest 各 inserted=23（重叠日 09-28 因浮点尾差 97.599998 vs 97.6 记 2 条 source_revision，无实质变化）→ `adjust --forward-csv`（sina qfq 全区间）全量重建：origin 前移至 2023-08-28，法拉 4 平台段（切换日 2024-06-14/2025-06-13/2026-06-12，年均 6 月分红）、万华 5 段（2024-04-22/2024-09-05/2025-05-30/2026-05-27），周线同事务重建 153 周 → compute 725 行 → weekly_signals 重算。因子 distinct 从 199/88（tdx 等比连续漂移口径）收敛为 4/5 平台段，与 §3.3 平台模型吻合。NOTE：两只 corporate_actions 为空，切换日未交叉印证（已知缺口④）。
- **增量/全量结论**（回答本轮问题）：采集层无自动增量——price/forward 按 `--start/--end` 手动指定区间，financials/index 接口全量返回；入库层幂等（upsert + content hash 去重 + data_revisions），全量拉重复入库安全。
- **测试**：新增 3 项（forward qfq 命名/列对齐、sina volume 不换算、财报 published_at 回填不升 revision 且幂等），`uv run pytest -q` **389 全绿**（386+3）。
- **遗留**：① 东财 push2his 恢复后 price 默认 em 源可切回（sina 仅 A 股）；② 平安银行 1990-92 披露日无源，degraded 标保留；③ 法拉/万华头部 23 天 pe_ttm 空（no_share_capital，需更早期本快照才可解，影响仅限该窗口）；④ 南航/豫光 2026H1 待披露后补采（重跑 financials 即可，published_at 回填已自动化）；⑤ corporate_actions 12 只空 + 换手率/股东人数 + 万华 forecasts + 法拉/万华公告簿偏薄，akshare 现有采集器未覆盖（telegraph 无历史区间）。

## 2026-08-26（洛阳钼业 603993.SH 加入观察列表）

- `config/watchlist.yaml` 末尾新增 603993.SH 洛阳钼业（market CN，aliases [洛阳钼业, 洛钼, CMOC]，benchmark 000300.SH）→ `db seed` 导入 watchlist 全池 18 只。
- **待办**：3 年历史采集（日线/财报/公告）+ adjust/weekly/compute/weekly_signals 初始化（与 600563/600309/000001 待采集同列）；采集前 daily 对该股输出 incomplete 属设计预期。洛阳钼业主营铜/钴/铌/磷，副产钼，跨基本金属与新能源金属两大主题（铜价、钴价、汇率、地缘等均可能成为消息面映射主题，待 §3.6 行业映射落地后入 themes_json）。

## 2026-08-26（洛阳钼业 603993.SH 3 年初始化采集与管线齐备）

- **采集**：`akshare_collect --symbols 603993.SH --sources price,forward,financials --start 2023-08-27 --end 2026-08-26 --run-id run_lyc`（东财 push2his 域名本次直连与代理均不可达，按 execution_log 08-26 已知降级改 `--price-api sina`）。落盘 70 文件 → `data/raw/akshare/{price,forward,financials}/2026-08-26/run_lyc/`：price 1（726 行 OHLCV + amount，2023-08-28~2026-08-26）、forward 1（同期 qfq 726 行，5 平台段切换日未交叉印证）、financials 66 期（2007-12-31~2026-06-30，published_at 来自 NOTICE_DATE §2.1，下一开市日生效）。
- **入库**：`uv run python -m scripts.pipeline.ingest data/raw/akshare/{price,financials}/2026-08-26/run_lyc` → **inserted=792 skipped=1 errors=0**（forward CSV 被 `*_forward*` 约定跳过不污染 daily_bars，专供 adjust）。
- **adjust**：`scripts.pipeline.adjust 603993.SH --forward-csv .../603993.SH_forward.csv` → 4 平台段（2023-08-28~2024-07-05 / 2024-07-08~2025-06-26 / 2025-06-27~2026-05-26 / 2026-05-27~2026-08-26），factor 由 ~0.94 漂移到 1.0；同事务重建 weekly_bars 153 周（最新完成周 2026-08-21，进行时周 2026-08-28 跳过）。`corporate_actions` 仍空（与 600563/600309/000001 已知缺口一致：akshare 不采集）。
- **指标 + 信号**：`indicators.compute` → indicators_daily 726 / indicators_weekly 153；pe_ttm 0/726 非空（**no_share_capital**：akshare 利润表无 shares_issued_end，share_capital_events 表空；属于设计预期降级，待 tdx/yahoo 补股本快照后转正）。`weekly_signals` 跑通，当前 anchor episode=2025-04-11 起，活跃 5 项（panic 8 / dry_up 1 / no_new_low 10 / divergence 8 / duration 28 行 per type，与 §5.3 一致）。`daily_watch` / `right_side` 写 `incomplete(no_active_card)`（无卡预期），`accumulation` 0 历史转换（当前 idle，condition_not_met）。
- **报告**：`scripts.pipeline.report --date 2026-08-26 --symbol 603993.SH` → `reports/603993.SH/2026-08-26.md`（degraded P5 revision=1，全池日报同步更新）。当日不复权收 19.59，ma20/60/120/250 略低于现价（具体见报告"指标快照"段）。
- **测试**：本次仅采集+管线初始化，未改代码，`uv run pytest -q` **389 全绿**（无变更）。
- **遗留**：
  - 1. share_capital_events 空 → pe_ttm 0/726 非空，影响估值/排期卡 draft 生成；与 600563/600309/000001 同期待 tdx_quotes hasCwInfo=1 补 group_total 快照，或 yahoo get_stock_info 补 issued 快照。任一即可转正。
  - 2. corporate_actions 空 → 平台段切换日未交叉印证，复权因子已重建但缺事件流；后续需 akshare 分红接口或 tdx/wenda 公告采集补齐。
  - 3. 公告（events）未采，本期跳过去重逻辑未触发；待 D3 消息评价落地或按需补 wenda_notice_query。
  - 4. 排期卡 draft 待人工激活流程（无卡 → degraded P5 持续，本周内可走 `pipeline.card_inputs` + fred-valuation-card-skill）。

## 2026-08-26（akshare 采集器新增 forecast/stock_info 两源 + 洛阳钼业股本/预期补齐 + 排期卡激活）

- **akshare 两新源（代码改动）**：
  - `scripts/collect/akshare_collect.py`：新 source `forecast`（同花顺 `stock_profit_forecast_ths` 净利/EPS/营收三指标，净利「亿元」×1e8 换算为元，FY1=--date 所在年，附加列 ak_np_orgs/min/max、ak_eps 全量保留；营收接口对 603993 返回空 → 留空标缺口 §2.5；FY1 净利增速 akshare 无直接口径留空，裂口检查降级）与 `stock_info`（东财 `stock_zh_a_gbjg_em` 股本结构最新行 → 集团总股本快照，列对齐 kimi stock_info 约定 thscode+ths_total_shares_stock）。两源均仅 A 股。
  - `scripts/adapters/akshare.py`：`parse_forecast_csv` 转发 `sfd.parse_forecast_csv`（sfd 侧加 `source` 关键字参数，默认行为不变，forecasts.source 正确记 akshare）；`parse_stock_info_csv` → share_capital_events（snapshot_group_total/group_total，**参与 PE 取数**），effective_at 推导自该股 daily_bars 最早交易日；**源可切换语义**：同 symbol 同 effective_at 已有其他来源 group_total 快照时股本一致幂等跳过、不一致记 conflict 交人工核对（§3.2），避免双源同口径并存造成 PE 取数歧义。
  - `scripts/indicators/valuation.py`：`load_group_total_snapshot` 加 `source/api_label/raw_prefix` 关键字参数（默认保持 stock_finance_data 行为不变），供 akshare 复用同一入库与幂等/冲突逻辑。
  - `ingest.py` `_ROUTES` 注册 ("akshare","forecast") / ("akshare","stock_info")。
- **洛阳钼业股本/预期补齐**：akshare 采集 `data/raw/akshare/{stock_info,forecast}/2026-08-26/run_603993/` 并 ingest（inserted=2）；kimi 源同日快照交叉核对——集团总股本 21,394,310,176 两源**完全一致**；FY1–FY3 净利 329.17/368.37/419.41 亿两源一致（kimi 精度到小数）。股本采用 akshare 源入库（effective_at=2023-08-28 日线起点），kimi stock_info CSV 留档不重复入库（防双 group_total 并存）；forecasts 两源快照均入库（全量保存 §3.7），card_inputs 取最新（kimi，含营收与 FY1 增速）。
- **指标转正**：`indicators.compute 603993.SH` 重算 → pe_ttm 726/726 非空（snapshot_share_basis），昨日遗留缺口①消除。
- **排期卡（人工确认激活）**：`card_inputs 603993.SH` 底稿 → fred-valuation-card-skill 产 draft（卡 md + draft_2026-08-26.json）→ `create-draft` 入库 `603993SH_67042523` → 用户明确指令后 `activate --effective-from 2026-08-27`（下一交易日起生效，next_review 2026-10-31）。要点：TTM 278.2 亿/EPS 1.30，现价 19.59、PE(TTM) 15.07；反向裂口 −24.4pp（FY1 预期 +61.8% < 2026H1 实际 +86.3%）；PE 刻度随 EPS 上台阶断崖下移（2023 年 31–48 → 2025 年 9.7–11.9）判为盈利驱动的体系切换期，PE 情景 18/14/11 按体系带给；EPS 情景 1.54/1.39/1.23；三档 20.70–21.56 / 18.49–19.46 / 13.53–14.61，证伪线 13.53；现价已掠过第一档（低 5.4%）、贴第二档上沿（信号 0 项不释放）；Kelly（55/65/70 下沿）T1 0.8% / T2 10.8% / T3 17.2%；波段箱体 16.20–21.60（2026-02 以来震荡）；右侧触发 21.65 / 止损 21.45。
- **测试**：新增 9 项（collect forecast/stock_info 列对齐、亿元换算、空接口、港股拒绝；adapter forecast 来源标注、stock_info 快照/无日线报错/跨源同股本幂等/跨源异股本冲突），`uv run pytest -q` **399 全绿**（389+10，含 1 项既有测试名修复）。
- **遗留**：① akshare forecast 营收接口对 603993 空（kimi 源有营收预测）；② kimi MCP file_path 相对路径不落盘（本次仍复现，已用绝对路径）；③ akshare 股本快照为单点（东财接口另有全历史变动行，未来可细化真实事件流替代快照假设）。

## 2026-08-26（盘后 daily：601899 因子污染修复 + daily.py 批量 forward 防御 + ok=18）

- **事件定位（紫金矿业 601899.SH raw 污染）**：昨日 20:45 daily_2026-08-25 首跑把 kimi 批量命名的 `batch2_forward.csv`（前复权）当 price 入库——`_classify_raw_files` 按文件名映射 watchlist 个股，`batch2` 映射不到个股落入 `other`，而 `other` 入库循环无 forward 跳过 → 601899 2026-08-14~08-20 共 5 行 close_raw 被前复权值覆盖（32.53→32.13 等，data_revisions 1039–1043）。600029.SH 同日被同文件触碰但无除权、价无差（仅 volume 尾差/amount→null，benign）；其余 16 只与 sina 不复权重叠窗口逐行比对无偏差，污染范围锁定 601899 这 5 行。
- **今日连锁暴露**：今日 sina 正确 raw 入库后，因子检查识别出 08-21 真实除权平台位移（1.24%）→ 触发全量重建，但手头的 forward CSV 仅 9 天短窗 → `origin 日 2023-08-10 不在因子序列内`，601899 单股回滚 failed（其余 17 只 ok）。
- **修复**：
  1. `akshare_collect --symbols 601899.SH --sources forward --price-api sina --start 2023-08-10 --end 2026-08-26 --run-id run_601899_fwd`（738 行全历史前复权）；
  2. ingest 今日 sina 不复权文件（inserted=1 updated=8，5 行脏值改回真实不复权）；
  3. `adjust 601899.SH --forward-csv run_601899_fwd/...` 全量重建：7 平台段（最近切换 2026-08-21→f 步进 1.0124，对应 2026 年中期分红），周线同事务重建 156 周；corporate_actions 无记录（已知缺口，平台切换日未交叉印证，与法拉/万华先例一致）。
- **代码修复（防复发）**：`daily.py` `other` 入库循环加防御——price 含 `_forward`、kline 含 `_tq1/_tq2/_forward` 的文件一律跳过并记 notes（与 ingest CLI 路由层、tdx adapter 口径一致）；`_classify_raw_files` 注释同步。回归测试 `test_batch_forward_file_never_ingested`（批量命名 forward 不落 daily_bars）。
- **教训（操作）**：`--raw-dir` 是单值参数，重复传参后者覆盖前者——今日首跑误只入库指数（ok=1/suspended=17），二跑 `--raw-dir data/raw/akshare` 才覆盖 price+index。
- **daily 2026-08-26 结果**：`uv run python -m scripts.pipeline.daily --date 2026-08-26 --raw-dir data/raw/akshare` → **ok=18**（calendar/event_study/summary success；report 阶段 degraded 仅因 4 只无激活卡：600309/600563/601168 no_active_card、603993 卡 2026-08-27 起生效，均属 §2.5 预期）。601899 因子检查通过、pe_ttm=13.55 正常，报告 complete P5。全池日报 revision=4：优先级 3 决策点 002557 箱体 buy_zone、000001 箱体 sell_zone、600029/603697 档位 T1 等（详见 reports/daily/2026-08-26.md）。
- **测试**：`uv run pytest -q` **400 全绿**（399+1 新增回归）。

## 2026-08-27（执行记录补登：601899 卖出）

- 用户报告 2026-08-26 卖出紫金矿业 601899.SH 600 股 @ 35.00（当日振幅 33.70–35.35，价可行）。`execution add` 记录 **#11**（card=601899SH_85cd7f52，信号快照截止 2026-08-26，key=auto_17910bd78c8e6605ee11d3be）。执行具体时分未知，executed_at 记 14:30 约定值（仅影响展示，快照按日冻结不受影响）；费用未提供留空。

## 2026-08-27（AKQuant 回测 Phase 1：单股双均线全链路验证）

- **背景**：依据 `~/Downloads/akquant_backtest_plan.md`（AKShare+AKQuant 回测方案）做差距分析并经用户拍板三件事——①并入本仓库（不另起 quant_project）；②本次只做 Phase 1 单股双均线验证；③数据复用库内后复权口径、结果仅打印不落盘。用户额外要求量化代码与既有管线尽量隔离。
- **akquant API 核实**（v0.3.52，官网/源码对照计划假设）：`run_backtest` 参数基本全对且更丰富（另有 commission_policy/transfer_fee_rate/min_commission/volume_limit_pct/fill_policy/benchmark/risk_config）；`IntParam` 内联参数、`order_target_percent/close_position/get_history/get_position/warmup_period` 均确认存在；`result.viz.report` 实为 `result.report`（Phase 1 未用）；**原生内置 run_grid_search / run_walk_forward 与 on_cross_section 横截面范式（Phase 2/3 架构风险大降）**；涨跌停未见原生支持（留待 Phase 4 补充验证）；停牌由缺 bar 天然处理。
- **隔离落地**：新增自包含子包 `scripts/backtest/`——自带只读连接（`db.py`，URI ro 模式 + BACKTEST_DB 环境变量测试注入，每次调用读取环境变量）与配置装载（`run.py:load_config`），不 import scripts/pipeline|adapters|indicators 任何代码；仅共享 data/market.db 文件与 `price_adj_factor/share_factor` 口径（§3.3）。依赖入 pyproject optional extra `backtest = ["akquant>=0.3.52"]`（同 akshare 处理，主环境不引入）。
- **实现**：
  - `config/backtest.yaml`：initial_cash 100 万 / lot_size 100 / t_plus_one / 万三佣金 + 卖出印花税 5bp / slippage 1bp / Asia/Shanghai；fill_policy 用默认 NextOpen()（T 日收盘信号 → T+1 开盘成交，无未来函数）。
  - `scripts/backtest/data.py`：daily_bars → akquant DataFrame（date/open/high/low/close/volume/symbol），调整价=raw×price_adj_factor、调整量=volume_raw÷share_factor，按 (date,symbol) 升序；无数据抛 ValueError 不猜（§2.5）。
  - `scripts/backtest/strategies/dual_ma.py`：DualMAStrategy（short/long/target_pct IntParam，on_start 设 warmup_period=long_window，on_bar get_history→MA 比较→order_target_percent(95%)/close_position），与计划 §6 示例一致。
  - `scripts/backtest/run.py`：CLI `uv run python -m scripts.backtest.run --symbol 000001.SZ [--start/--end/--json]`；打印核心指标 + 验证摘要（orders/trades 行数、拒单明细）；slippage 裸数字已废弃 → 显式转 `{"type":"percent","value":x}`；不落库不产文件。
- **真实跑通（000001.SZ，727 个交易日 2023-08-25~2026-08-26）**：total_return 17.13% / 年化 5.40% / Sharpe 0.436 / 最大回撤 15.34% / 胜率 32% / 25 笔完整交易；orders 52 笔中 filled 51 + rejected 1。
- **Phase 1 验收四项全部通过**：
  1. **T+1 生效实证**：2024-11-12 收盘信号买入 84600 股（11-13 开盘成交），当日收盘回落触发的卖出单被拒 `Insufficient available position, Available: 0`，11-14 重新提交成功——买入当日不可卖的教科书证据；
  2. **费用可复算**：trade.commission ≈ Σ(买单额×0.0003) + Σ(卖单额×(0.0003+0.0005))，逐笔 rel=5% 内吻合；
  3. **无未来函数**：warmup 生效（8 根 bar < long_window 20 时零订单）；NextOpen 保证信号日收盘数据不含当日成交价；
  4. **复权口径连续**：因子平台跳变处（raw ×2.0）复权序列无缺口。
- **测试**：新增 `tests/test_backtest.py` 9 项——数据导出 golden（×factor 平台/÷share_factor/区间过滤/缺数据报错）、配置加载与 slippage policy 组装、端到端指标存在、warmup 零订单、费用逐笔可复算、T+1 当日卖被拒合成序列构造（数学精确化：P>10 触买、P+C<20 次日即翻空）。注意 daily_bars.raw_object_id 有 FK，测试插入置 NULL；临时库用 pipeline db 的 migrate+seed_calendar 建 schema。`uv run pytest -q` **409 全绿**（400+9）。
- **偏差/决定**：① trades_df.side 全 'Long' 是 closed-trade 语义（开平合一笔）非只买不卖，UI/报告消费时勿误读；② reject_reason 列空串非 NULL，判断拒单要按长度过滤；③ akquant Rust wheel 安装较慢（uv sync --extra backtest 约 2 分钟），走后台完成；④ 双均线 MA 比较用 mean(closes[-short:]) vs mean(closes)（长均线=整窗均值），等价于窗口内对比，已在 docstring 说明；⑤ 监测系统 §1.2 "回测优化不进第一版"边界不变——本子包是平行研究工具，不接入 daily 管线、不影响监测信号任何输出。

## 2026-08-27（AKQuant 回测 Phase 2：18 只迷你池周频 Top-N 等权轮动跑通）

- **范围决定**：用户指示"继续"，进入计划 §19 Phase 2（股票池 + Top N + 等权 + 每周调仓）。库内现仅 18 只 watchlist 行情 → 先以 18 只为迷你池验证多股链路；扩容 ≥100 只需 akshare 批量采集入库，列为后续独立任务（接口已预留 `--symbols` 与 universe.py）。隔离原则同 Phase 1：全部新代码限于 `scripts/backtest/`，只读 market.db。
- **实现**：
  - `scripts/backtest/universe.py`：股票池加载——显式列表优先（空列表报错不回退），默认 watchlist active=1 排序输出；行情充足性交给 run_multi 按 lookback+裕量剔除并**明示原因**（§2.5 不静默）。
  - `scripts/backtest/strategies/topn_rotation.py`：TopNRotationBase——每周首个被处理的 bar 触发一次（ISO 周去重），`get_history(count=lookback)` 取各股截至上一收盘的序列算动量分（触发时点其他标的当日 bar 未入缓冲，横截面天然无未来函数），Top-N 等权经文档确认的 `rebalance_weights(target_weights, liquidate_unmentioned=True, rebalance_tolerance=0.01)` 下单；universe 经工厂函数以类属性注入（避免依赖构造器参数语义猜测）；`CASH_BUFFER=0.05` 目标权重和=95%（吸收滑点/佣金/跳空，双均线 95% 同一问题域）。
  - `scripts/backtest/run_multi.py`：CLI `uv run python -m scripts.backtest.run_multi [--symbols --top-n --lookback --start --end --json]`——逐股加载→样本不足剔除报告→concat 按 (date,symbol) 升序→回测→打印指标/交易/拒单数/参与标的/末日持仓快照/沪深300 同区间对比（index_bars 只读查询取首末收盘，缺数据打印跳过不猜）。
- **真实跑通（18 只 × 约 730 日）**：参数 top_n=5 / lookback=20 / buffer=0.05：total_return 4.52%、Sharpe 0.19、最大回撤 35.7%、377 笔完整交易、拒单仅 1（T+1 类，次周自愈）、margin 拒单归零；沪深300 同区间 15.71%。
- **重要研究警示（非 bug）**：buffer 2%→5% 的微调使 total_return 从 50.8% 摆到 4.5%。根因是迷你池动量轮动的**排名边界敏感性**：仓位口径变化改变个别周的 Top-5 成员与部分成交路径，收益路径随之剧变（且首轮 margin 拒单本身也在塑造路径）。结论：当前数字仅证明"多股回测链路可用"，不构成任何策略有效性证据——这正是计划 Phase 2 的定位；参数敏感性/扩池/正式因子框架属 Phase 3+ 工作。
- **引擎行为记录**：① 调仓同切片出现大量 `Deferred same-cycle order until cross-symbol reduce-first orders finish`——引擎自动先卖后买排序提示，属预期；② margin 拒单机制见上；③ 残余拒单订单次日过期、下周按实际权益重定目标自愈。两者都在 stdout 显形而非吞掉。
- **测试**：新增 `tests/test_backtest_multi.py` 10 项——universe 默认/覆盖/空池报错、短样本剔除明示、全样本不足抛错、4 股分化趋势端到端（交易标的 ⊆ 池、基准首末收盘语义、trades/orders/positions 结构存在）、持仓宽松上限（≤top_n×2 过渡容忍）、sell 订单按 ISO 周聚合 ≤13 周（节奏无未来函数佐证）。修坑记录：pd.Timestamp.isoformat() 带 T00:00:00 后缀会污染文本日期比较（真实库存纯日期无此问题）；daily_bars.raw_object_id FK 测试置 NULL；小资金等权起步腿差滑点即 margin 拒单（合成用百万资金+策略侧缓冲）。`uv run pytest -q` **419 全绿**（409+10）。

## 2026-08-27（AKQuant 回测 Phase 3：三因子横截面评分框架）

- **架构（计划 §10 因子与交易分离的最小实现）**：因子全部在 akquant 引擎外由 pandas 向量化预计算，`{date:{symbol:score}}` 表经工厂类属性注入策略；策略仅做"取分→排序→等权调仓"。取分纪律为**严格早于触发日的最近一天**（T-1 收盘信号）叠加 NextOpen 成交，双保险未来函数隔离；取分之外不动 bar.extra 注入路径（避开引擎日内到达顺序问题）。
- **新增文件**：
  - `scripts/backtest/factors.py`：FactorParams + 单股三因子（momentum=N日收益率 / volatility=N日收益率 std(ddof=0) 与 §4.1 BOLL 口径一致 / liquidity=N日均 amount_raw，成交额不做股份调整）+ 横截面逐日 winsorize(5%)→zscore→权重加权（vol 权重 -0.3 即方向统一）→score；中性语义两层——行级三因子缺任一即无分（覆盖率统计）；因子级截面 <min_names 或 std≈0 该因子当日贡献 0，**三因子全中性则整日无分**（不制造伪区分度让策略空转交易）。辅助 `select_scores_asof`（严格早于语义）/`build_score_map`。
  - `scripts/backtest/strategies/factor_rotation.py`：FactorTopNBase 继承 Phase2 TopNRotationBase 复用 CASH_BUFFER(5%)/ISO 周去重/rebalance_weights(tolerance 1%)，仅替换选股来源为外部 score 表。
  - `scripts/backtest/run_factor.py`：CLI `uv run python -m scripts.backtest.run_factor [--symbols --top-n --config --start --end --json]`；额外输出因子首末日/中性计数/**每只覆盖率%与 <80% 明细**/trade_leakage（参与交易 ⊆ 有分集合的外部佐证）。
  - `config/backtest.yaml` 追加 factors 块（窗口/权重/裁剪/min_names 默认即计划 §11 示例 0.5/-0.3/+0.2）。
  - `data.load_symbol` 加 `include_amount` 开关（默认关，Phase1/2 兼容）。
- **真实跑通（18 只 watchlist）**：策略收益 77.43% / 年化 20.7% / Sharpe 0.83 / 回撤 25.5% / win_rate 79% / 112 笔零拒单；沪深300 同期 15.71%。末日持仓恰为有分的 3 只各 ~95% 折算等权（top_n=5 > 截面数时自然收缩），拒单 0。
- **⚠️ 关键数据发现（决定后续研究价值）**：覆盖率报告显示 **15/18 只 coverage=0.0%**，仅 600309/600563/603993 三只有分——正是 handoff 已知缺口"amount 仅 tdx/akshare 部分股票有值"的直接体现（kimi 历史无成交额）。即当前三因子轮动实际运行在 3 只迷你截面上，momentum/vol 在 ≤3 只截面上的排名几乎无区分度，77% 数字**仅为链路验证结论而非因子有效性证据**。补齐路径已明确：用 akshare 东财源（price 输出自带 amount）对缺额股票全量重采并 ingest（amount 属规范化事实 upsert，data_revisions 可追溯），再重算 amt_* 指标——涉及监测管线指标面，须人工拍板后再做。
- **测试教训留档**：① 各股帧 RangeIndex 重叠 + concat 未 ignore_index → 按日 loc 写入跨股互相覆写（golden 复算暴露，修 factors.concat(ignore_index=True)）；② 等比缩放的合成面板 pct_change 完全相同 → 截面天然零区分度触发全中性（属新语义正确行为，改用独立漂移路径面板）；③ 截面剩余样本跌破 min_names 时整段日期无分为预期（NOAMT 用例配 min_names=2 验证 10/2 分布）。新增 `tests/test_backtest_factors.py` 10 项；bt_multi_db_path fixture 提升至 conftest 共享（模块间导入 fixture 不注册）。`uv run pytest -q` **429 全绿**（419+10）。

## 2026-08-27（amount 数据面补齐：15 只 A 股成交额全量回填 + amt_* 指标填充）

- **决策背景**：Phase 3 因子覆盖率报告暴露 15/18 只 liquidity 无分（库内 amount_raw 已知缺口，kimi 主源历史无成交额；仅此前 akshare 重铺过的 600309/600563/603993 三只完整）。用户拍板执行补齐。
- **采集**：`akshare_collect --sources price --price-api sina --run-id run_amt_backfill`（东财 push2his 域名代理仍不可达，按 2026-08-26 先例切新浪源，`stock_zh_a_daily` 输出自带成交额），15 只 × 各自库内起点至 2026-08-27，全部 ok（原始目录留档不动）。
- **两道裁剪门（避免越权写入）**：① 原始文件含当日 2026-08-27 bar——该交易日必须走每日管线因子检查流程，补采不得夹带，副本目录 `run_amt_backfill_trim` 截至各股库内末日（≤2026-08-26）；② 发现 42 个"CSV 有库无"日期，**全部为 2023-08-09~16 的各股原初采集起点边界**（原源起始日参差所致）而非停牌——在因子 origin 日之前，动它即触发 §3.3 前扩历史流程，本次排除并记录待议。
- **入库比对门**：15 只共 11031 行逐行对库校验——OHLC 偏差 0 行（0.005 容差）、成交量偏差 0 行（0.1% 容差），跨源逐分一致，纯 UPDATE 零 INSERT。`pipeline.ingest run_amt_backfill_trim` → **inserted=0 / updated=10896 / conflicts=0**；updated 数恰等于原缺额行数，每文件 skipped=9~13 恰为近几日已有额的行（内容不变幂等跳过），账目自洽。
- **指标重算**：15 只逐一 `indicators.compute` 全量重算（§4.3 幂等惯例）；2026-08 以来窗口 null_amt20=0、null_amt60=0，amt_mean20/60 抽查量级合理（000001 约 13 亿/日）。周线/信号不受影响（close/量未变， amt_* 仅报告展示消费）。
- **因子复验（回测侧闭环）**：`run_factor --top-n 5` → **18/18 只有分、覆盖率<80% 名单清空**、中性截面计数仅 3、首分日 2023-09-07；真实截面轮动 36.20% vs 沪深300 同期 15.71%（Sharpe 0.55、回撤 31.4%、326 笔、拒单 1 次周自愈），末日 5 持仓满配等权。因 Volume/close 未动，监测侧信号零扰动，`uv run pytest -q` **429 全绿**证实。
- **遗留**：① 42 个起点边界行是否前扩入库（每只需 §3.3 新因子版本+全量重建，价值有限建议搁置）；② 0700.HK 港股 24 行无 amount 未处理（港股流动性因子本就未纳入本期范围）。

## 2026-08-27（盘后 daily：全池 ok=18；补采 miss3 与洛钼因子重建 v31）

- **补采 miss3**：`run_amt_backfill` 只含 15 只，缺 watchlist 中 600563.SH / 600309.SH / 603993.SH。`akshare_collect --sources price,forward,index --price-api sina --run-id run_daily_20260827_miss3` 一并补齐三只及 000300.SH/^HSI 两指数。**踩坑留档**：首次执行漏传 `--end`，argparse 默认值硬编码 `2026-08-25` 且不随日期更新，三只 price/forward 文件静默截至今日前一日——入库前抽查 CSV 尾行发现（§2.5 纪律），未污染数据库，原地重采修正。教训：**akshare_collect 每日增量必须显式 `--end <当日>`**。
- **daily 结果**：`uv run python -m scripts.pipeline.daily --date 2026-08-27 --raw-dir data/raw/akshare` → **ok=18**，18 只 A 股全部有当日 bar（收盘抽查与源一致：603605 收 62.34、603993 收 19.51 等）；旧目录 content-hash 幂等跳过，仅新增行入库。无代码改动。
- **603993.SH 因子重建 version_id=31**：重叠窗口 max_dev=0.1013% 刚过 0.1% 容差（除权落在检查窗内），按设计触发防御性全量重建（origin 2023-08-10，platform 段口径，source_factor_at_origin=0.93966），周线 156/指标 daily=739/五类信号已随管线原子重算，报告 complete P4。
- **指数**：000300.SH 增至 2026-08-27（inserted=1）；^HSI 最新仍为 2026-08-26（sina 港指滞后一日，既有现象非本次退化）；其全历史解析另跳过 30 行非法行（o=0、c=h 等源质量问题，逐行带 note，与库既有行为一致）。index_bars 现状：000300.SH→08-27、^HSI→08-26。
- **报告**：15 complete + 3 degraded（600309/600563/601168 均 no_active_card，§2.5 预期）；全池日报 revision=1 → reports/daily/2026-08-27.md。杂项：`announcement/2026-08-26/run_lyc/603993.SH.csv` 每次 daily 提示无法路由——根因是 `_ROUTES` 无 (akshare, announcement) 注册：该通道曾实现并将本文件 135 条公告入库（commit 1341243），随后按用户要求整体回退代码（ea2dfd1），CSV 留档在 raw 树内所致。核实数据零丢失（events 表 akshare 公告恰 135 条，published_at 与 CSV 一致），仅提示噪音，处置方案待议 → 当日经用户拍板重构解决，见下节。

## 2026-08-27（重构：公告解析引擎下沉 adapters/announcements + 恢复 akshare 公告入库通道）

- **决策**：针对 run_lyc 无法路由问题，用户否决"akshare 直接借用 tdx 解析器"做法，拍板**不复用、正式重构**：把公告解析提取为源中立公共引擎后再恢复通道。上一版回退实现（1341243）的薄壳模式本身无架构问题，本次是把"共用"从隐式委托升级为显式公共层归属。
- **新增 `scripts/adapters/announcements.py`**：`parse_disclosure_csv(..., *, source)` 引擎承接标准公告线格式（title, time, url, source, summary, code, setcode, name）→ events/event_symbols 全部逻辑：title/time 必填整文件拒绝、stem ticker 推断优先 + code+setcode 回退、dedup event_id = sha256(f"{source}|{title}|{pub_date}")[:16] 命名空间隔离、published_at = 发布日当地 00:00 → UTC、available_at = 发布日 +1 开市交易日 00:00（§2.1）、calendar 缺失降级 +1 自然日记 incomplete。docstring 明确边界：线格式不同的源（sfd 列别名、tyc uuid 去重）不进本引擎。
- **common.py 上移三个跨 parser 工具并公开化**：`SETCODE_SUFFIX` / `symbol_from_code_setcode` / `next_open_available_at(calendar, pub_date, market)`（实现原样搬迁）；tdx 其余 kline/index/quotes/financials 解析改引 common 版本（全局重命名 6 处调用点），行为零变化；清 hashlib/timedelta 残留导入。tdx 内部 `_ticker_from_stem` 随公告逻辑迁入 announcements（唯二使用方）。
- **源适配器薄壳化**：tdx.parse_announcement_csv 与新增 akshare.parse_announcement_csv 均为纯委托（各自传 source='tdx'/'akshare'），签名不变；ingest._ROUTES 注册 `(akshare, announcement)`。
- **测试**：test_adapters_akshare.py 新增 4 项——aksource 入库字段/时点断言、跨源同公告 event_id 隔离互不吞并、同内容重跑幂等、**路由表锁定断言**（_ROUTES 必须含 ak.parse_announcement_csv 且薄壳落点为公共引擎，防再被静默移除）。踩坑记录：隔离测试初版两份 CSV 字节全同，先被全局 content-hash 门槛拦下（§9.5）到不了解析层——系测试构造不当而非引擎缺陷，改为有真实差异的两来源文件。`uv run pytest -q` **433 全绿**（429+4）。
- **真实验证**：`pipeline.ingest data/raw/akshare/announcement/2026-08-26/run_lyc` → 路由生效、content-hash 幂等跳过（raw_6084cd377dc7ec1c），events 表 akshare 公告维持 135 条零重复；后续 daily 不再出现该文件无法路由提示。
- **遗留/backlog**：① cninfo 抓取仍未进 akshare_collect（当时一并回退未恢复），采集仍靠一次性手段，要常态化需给 collector 加 announcement fetcher（含增量游标），涉及新外部接口探测，另议；② sfd/tyc 公告解析仍在各自 adapter（线格式/dedup 语义确实不同，如后续出现第三个异构公告源再评估是否抽归一化记录层）。

## 2026-08-27（akshare_collect 新增 announcement 通道：cninfo 公告常态化采集）

- **背景**：上一节 backlog① 由用户拍板执行。接口实测：`stock_zh_a_disclosure_report_cninfo(symbol=<6位>, market="沪深京", start_date, end_date)` 返回列 `[代码,简称,公告标题,公告时间('YYYY-MM-DD'),公告链接]`；**日期参数必须紧凑 YYYYMMDD——带 - 的格式静默返回空行（0 行不报错），已写入 docstring 与测试断言双保险**。
- **实现**：`collect_announcement`（仅 A 股，HK/非沪深京拒绝）→ `{symbol}.csv` 标准公告线格式：source 列固定"巨潮资讯"、summary 回填标题、自由文本 `_csv_escape` 防注入；时间规范化 `%Y-%m-%d %H:%M:%S`（引擎取前 10 位口径不变）。CLI 按既有模式接入分发（opt-in 不进默认 sources）；meta 记录 api/params 同其他源约定。
- **测试 +3**（FakeAk 补 cninfo 方法）：线格式列序/引号转义还原、**紧凑日期入参断言**（锁住踩坑点）、空结果 None / HK 拒绝。`uv run pytest -q` **436 全绿**（433+3）。
- **真实冒烟**：603993.SH 窗口 2025-08-26~2026-08-27 → 135 行落盘 `announcement/2026-08-27/run_daily_20260827_ann/`；ingest 全部事件级去重 skipped=135、inserted=0、conflicts=0（与存量公告完全一致；落盘 time 无秒位不影响 pub_date 口径）——采集→入库→幂等闭环验证完成。
- **待议**：法拉电子 600563.SH / 万华化学 600309.SH 公告簿偏薄缺口现可用本通道批量回填（用户点头后跑全池 A 股或指定标的即可）；D3 消息评价落地前 events 只进库不评价。

## 2026-08-27（盘后 daily 二跑：折入公告增量，revision=3；当日复盘完成）

- **daily 二跑**：公告回填后重跑 `daily --date 2026-08-27 --raw-dir data/raw/akshare` → ok=18 全幂等，新增公告事件 184 条折入 event_study（重算写入 61 行 ok），全池日报 revision=3。⚠️ 观察项：603993 因子检查两日内连续触发防御性重建（v31 max_dev=0.1013% → v32 0.1047%，均贴近 0.1% 容差），疑该除权点附近检查存在贴阈不稳定性——复权序列连续性由重建自身保障、不影响信号口径，但需另次排查根因，避免每日重复重建噪音（跟进项）。
- **复盘**：基于 reports/daily/2026-08-27.md + signal_facts/adjusted 口径 SQL 汇总，在对话中向用户交付（P1/P2 空；P3 五项决策点：珀莱雅 T1+右侧确认、南航/有友 T1、洽洽 buy_zone、平安 sell_zone；海天 -5.09% 跌入 tier_2 但衰竭信号 0<2 门控未过，属设计预期）。

## 2026-08-27（海天味业公告簿回填 +171 条；消息面盲窗消除，中报同日性强相关确认）

- **背景**：用户要求结合消息面分析海天当日 -5.09%（复权、放量 17.84 亿≈近16日均量~3.9 倍）。原事件簿 tianyancha 至 8/03、tdx 仅 8 月中旬起，存在盲窗。用户拍板回填。
- **执行**：`akshare_collect --symbols 603288.SH --sources announcement --start 2025-08-26 --end 2026-08-27` → 172 行（含内文件重复 1 行幺等跳过）；ingest **inserted=171 / conflicts=0**。事件簿现为三源：akshare 171（2025-08-28 起）+ tianyancha 154 + tdx 5。
- **关键发现（消息面结论性事实）**：cninfo 显示**正式半年报及摘要、半年度主要经营数据公告、董事会决议公告、H股公司秘书变更等于公告日 2026-08-27 同批挂网**（早于开盘可见；注意 events.published_at 为 UTC，date() 展示会偏差一日，原始 CSV 公告时间为准）——"昨天的中报作用很大"的用户判断得到结构支持：信息密集披露当日即出现全池最大放量跌幅。基本面数字本身温和（营收 +6.0% / 归母净利 +7.1%，此前 SQL 已算）；分渠道/品类明细在「主要经营数据公告」正文内，本系统仅采标题级事件，需人工经 canonical_url 查看。提醒消费侧 D3：同题公告现存在于 akshare/tdx/tianyancha 三命名空间，评价前需跨源归并。

## 2026-08-27（公告簿回填：法拉电子 +64 / 万华化学 +120 条 cninfo 公告入库）

- **执行**：用户拍板回填。`akshare_collect --symbols 600563.SH,600309.SH --sources announcement --start 2025-08-26 --end 2026-08-27 --run-id run_ann_backfill_fa_wanhua`（窗口与洛钼存档口径对齐，便于后续增量切换）→ 采集 68/121 行零错误；ingest `inserted=184 / skipped=5 / conflicts=0`——万华 121=120+1、法拉 68=64+4，5 条 skip 均为 cninfo 返回列表内同日同题行（同一公告多附件场景）被引擎按 title|pub_date 幺等去重，账目自洽。
- **结果**：事件簿现为双源并存（源命名空间隔离，§3.6）——万华 akshare 120 条（2025-09-03~2026-08-25）+ tdx 22 条；法拉 akshare 64 条（2025-09-12~2026-08-21）+ tdx 19 条。抽查关键节点（万华 2026 中报摘要 08-24、临时股东会法律意见书等）均在库。handoff 已知缺口⑦中"法拉/万华公告簿偏薄"已关闭（corporate_actions 空/万华 forecasts 缺仍保留）。
- **提醒（后续消费侧注意，非本次缺陷）**：同公告现可能同时存在 tdx 与 akshare 两条 event_id 不同的事件记录；D3 消息评价落地时需在消费端做跨源归并（canonical_url/title+pub_date 关联），§2.5 口径下不建议在采集层擅自合并。

## 2026-08-27（入池：美的集团 000333.SZ / 格力电器 000651.SZ，全链路首日跑通）

- **入池**：watchlist.yaml 新增两行（带注释），`db seed` 导入 20 只；采集 `run_onboard_midea_gree`（price/forward/financials/announcement 四源，sina 源，--start 2023-08-09 --end 2026-08-27 显式传参）。
- **采集/入库**：各 740 根日线 + forward + 财报（美的 78 期、格力 135 期，格力至 1993 年起全史）+ 公告（美的 532 条、格力 282 条入 akshare 命名空间，格力 7 条文件内重复幺等跳过）；ingest inserted=2485 / conflicts=0。
- **daily 首跑**：ok=**20**，两新店 adjust/weekly（各 156 周）/indicators（各 740）全链路生成；收盘 美的 86.18 / 格力 39.27；报告 degraded（no_active_card）属入池预期。⚠️ pe_ttm=NULL（no_share_capital）：两股无股本快照，待补采 stock_info 源（东财股本快照）后自动填充；forecast 可一并补。
- **测试**：`uv run pytest -q` **443 全绿**。+7 来自另一工作流今日新建的 `tests/test_backtest_events.py`（Phase A 时序事件回测，7 项，未跟踪文件，与本批改动无关），已核实无冲突。
- **排期卡 draft（LLM draft-only，待人工激活）**：card_inputs 底稿导出后按洛钼卡同套方法论起草两张 draft，`create-draft` 校验入库：
  - **美的 000333SZ_b6226347**：EPS 三情景 5.50/5.80/6.20（TTM 5.7935 为锚，无一致预期缺口）；PE 悲观/中性/乐观 12.5/13.8/15.5（p5/p50/p95，恐慌带 11.44–14.21 无体系切换）；三档 88.00–90.00 / 76.60–80.00 / 66.00–67.90；箱体 [74.00, 89.50]（买 74.00–76.50 卖 88.00–89.50，失效 71.50）；证伪线 66.00（= 锚定恐慌低点 66.20 下方）；next_review 2026-10-31
  - **格力 000651SZ_f7c4f770**：EPS 三情景 4.98/5.33/5.60（base=FY1 一致预期 298.25 亿，bear=TTM 持平下行情景）；PE 6.7/7.6/8.4（盈利下行期低估值窄幅刻度 6.52–8.43）；三档 44.50–47.20 / 40.70–42.30 / 36.50–37.50；箱体 [36.30, 42.50]（买 36.30–37.60 卖 41.00–42.50，失效 35.80）；证伪线 36.50（= 锚定恐慌低点 36.98 防线带）；next_review 2026-10-31
  - 两张均为 draft 状态，activate/reject 等待人工
- **股本快照补齐（同日追加）**：`stock_info` 两店入库（美的 76.29 亿股、变更日 2026-08-19 自主行权；格力 56.01 亿股、2026-06-30）→ indicators.compute 全量重算，pe_ttm 740/740 非空：美的 **14.88** / 格力 **7.54**。forecast：格力 FY1-FY3 净利预期 298.3/313.1/330.5 亿入库；美的同花顺接口返回空，按 §2.5 留缺口标（后续重试）。注意：单快照全局应用的 PE 历史为近似值，早期段严格点时需更早快照（与法拉/万华同款已知限制）。

## 2026-08-27（执行记录补登：平安 601318 4 笔波段 #13–#16）

- **用户报告**：2026 年两轮已完成波段——6/18 买入 1100 @49.4（当日放量下跌日，恰为 accumulation 模块 breakdown_date）、6/22 卖出 @51.8；6/24 买入 @49.25、7/20 卖出 @52.7。毛利 +2,640 / +3,795 元（费前）。四笔 `execution add --backfill` 补录为 **#13–#16**（card=601318SH_6c1eba32，时分未知记 14:30 约定值，backfill 语义不冻结信号快照）。
- **底仓情况**：另持有底仓 5300 股 @44.2（买入日期未提供，暂未入系统台账）；当前浮盈约 +26.4%，今日收盘 55.87 落在卡 sell_zone [53.50, 56.00] 内（P3 决策点），用户策略为保底仓做波段压成本；已向用户提供卖出现价/回补 buy_zone 的摊薄成本场景表（Python 计算，对话交付）。
- **底仓补录（同日追加）**：用户确认“购买时间较早，单独记一笔” → `execution add --backfill` **#17**（buy 5300 @44.2，executed_at 以系统行情覆盖起点 2023-08-09 约定标记，note 如实声明日期不详与摊薄口径）。平安台账齐整：底仓 #17 + 波段 #13–#16；后续波段即做即记即可维持审计线。
- **对照观察**：用户两轮买入价 49.4/49.25 均落在卡 buy_zone [47.00, 49.50] 上半区，卖出价 51.8/52.7 低于本轮 sell_zone 下沿 53.50——本轮价格已更高，场景差异已向用户标出。

## 2026-08-27（AKQuant 回测 Phase A：排期卡择时层忠实机械化——衰竭时序事件版）

- **定位**：回答"我的策略能否做成量化因子回测"的第一步——择时层（衰竭 5 项/锚点/吸筹）本来就是确定性规则且已有 3 年点时事实，直接消费 `signal_facts`+`weekly_anchors` 做时序事件回测；卡片价区/估值研判层明确不做（语义上不可回放，Phase B/C 议题）。仍全程 `scripts/backtest/` 隔离、只读共享库。
- **新增**：`event_signals.py`（SQL 直查：①同周同锚 active 计数取最大组、并列取小 id；②真实 decline_start 锚列表 `is_fallback=0` 过滤；③HFQ 止损换算 stop_adj=adjusted_price×(1−stop_pct)，锚点日因子一次性落位历史不漂移）；`strategies/exhaustion_timing.py`（入场=最近完成周 active≥min_signals 且本 episode 未开仓；出场①收盘≤止损线、出场②锚推进即旧 episode 终结；可选折扣门近似卡片段价区半条件）；`run_event.py` CLI 逐股独立账户回测+池级聚合。
- **关键发现一（粘性信号主导释放）**："≥2 项"极易被 duration（episode 内持续活跃）与 no_new_low_3w（创新低前持续）两个慢变量凑满——603605 3 年 155 周中 52 周满足。卡片语境有人工价区+复核节奏抑制换手；纯信号时序版在震荡区形成"绕线震荡"（603605 静态证伪线附近 2–4 天反复进出，74 笔、胜率 50% 但净亏 -33.6%）。
- **关键发现二（fallback 锚污染已修）**：weekly_anchors 全量加载会把"缺恐慌信号的每周兜底锚"当真锚推进 episode 索引（全池 503 锚仅 117 真实），episode 锁与终结出场双双失效——加 `is_fallback=0` 后恢复真实节奏（单股仅 2–3 个真实下跌起点）。
- **全池基线（纯信号版，stop 8%）**：18/18 参与，17 只有交易（601168 为未平仓浮盈 +156%，trades 只计 closed 属口径正常）：总收益中位 **+18.65%** / 均 +32%、正收益 12/17；分化极端——大赢家是"低位一次进场长持"的 beta 型（603993 +205%/601899 浮盈+93%），输家集中在粘性信号高频区绕线品种（002299 −27%/603605 −34%）。结论：链路忠实可用，但该数字是机制验证非策略有效性证据；半条件重要性获得直接证据——603605 加 5% 折扣门改善至 −30%（回撤 43→39.9%），25% 折扣门降至 10 笔/+4.1%/回撤 2.6%（参数未调优仅示敏感性）。
- **测试**：`tests/test_backtest_events.py` 7 项——计数 golden（多锚取大/并列小 id）、完成周 asof 边界、fallback 过滤+止损换算精确值、episode 锁单元（桩注入 order/close/get_position）、折扣门绑定、缺数据干净抛错、akquant 端到端一笔开平闭环（含 warmup 21 根门槛教训：事件周必须落在预热后才能被策略看到）。全量 **443 全绿**。
- **明确未做**：卡片三档价区/箱体机械化推导（Phase C）、横截面评分融合（Phase B）、扩池。参数（stop_pct/min_signals/discount）全部暴露为 CLI 参数且默认保持"忠实口径"，不作寻优。

## 2026-08-27（紫金矿业历史建仓补录 #12 + 锚点四维复核）

- **补录**：用户口述历史建仓 601899.SH 买入 25.05×1200 股（"前几个月"）。`execution add --backfill` 记 **#12**，executed_at 取约定近似值 2026-06-26T14:30+08（成本落在 6 月下旬低点带 24.70~25.14 内；时点沿用项目 14:30 约定），snapshot 显式标记 backfill=true 并注明日期为约数、可冲正重录。fees 未提供留空。card=601899SH_85cd7f52。
- **持仓口径更新**：成本 30,060；#11 已实现 +5,970（600 股@35.00）；持余 600 股按 34.57 计浮盈 +5,712 → **合计 +38.9%**。历史成本补齐后组合视角首次可自动核算。
- **锚点四维复核（只读）**：①因子一致性 ✔——恐慌锚 2025-09-04 隐含因子 1.037277 与 08-21 除息因子重建后的库内值完全一致（重建链路传导正确）；②恐慌周事实重演 ✔——量比 2.34+大阳（实体/全幅 65%、周涨 +5.7%），判定成立；③episode ✔——现价 34.57 远高于下跌起点 23.08，已终结、系统等待新周期；④fallback 漂移温和——26 周最低收盘窗落点 2026-06-26@adj26.63，较在册锚仅 +9.7%（修正此前"大幅过期"的口头推测）。⑤新周期触发门槛量化：单周量 ≥29.18 亿股（现均 14.59 亿×2）+反转形态+创可识别低点。
- **结论**：紫金锚"无移动"为正确状态（与洛钼同机制：锚表=周期事件日志）；但卡片阶梯（T1 20.91–21.78）较现价低 37% 的锚定过期仍成立，优先复核建议维持。

## 2026-08-27（研究记录：低估值分位 × 择时机制有效性——池内事件研究）

- **背景**：用户假设"机制在估值较低时更有效更安全"，追问"是否对周期股更有效"。纯只读研究，不改任何管线代码。
- **设计**：20 只 active CN 股（新增美的/格力已自动纳入）× 3,118 周观测；触发=同锚 active≥2 首次释放周（116 个，111 含 PE）；估值=pe_ttm 自身 52 周窗分位（自相对，点时）；前向 +4w/+8w 复权周收。
- **结果**：①低分位触发周 8w 胜率 66%/中位 +3.2%/左尾 8.0%，高分位 40.7%/−1.1%/最差 −26.3%——H1 成立、H2 半成立（非线性，"高分位禁行"优于"越低越安全"）；②信号增益低分位 +3.0pp vs 高分位 +1.4pp（信号非估值代理）；③风格内增益证伪 H3：周期仅 +1.5pp（时代β），制造成长 +11.6pp/胜率 81.8% 才是增益王，金融 −1.3pp。修正假说：机制收益∝波动/重估弹性，低 PE 分位是排除性风控而非收益引擎。
- **产物**：`reports/research/2026-08-27-低估值与择时机制有效性验证.md`（含全部表格、反例清单、局限声明与三级落地建议）。

## 2026-08-28（恒力石化 600346 首期卡复核：600346SH_5df2b631 → draft 600346SH_0f65e846）

- **触发**：next_review_at 2026-08-31 到期 + 2026 中报落地（H1 归母 72.06 亿，+136.2%，兑现 07-07 预增）。按 fred-valuation-card-skill 复核流程：`card_inputs` 新底稿（inputs_2026-08-27.json）→ 情景/刻度/档价/右侧位逐段复核 → `build_schedule.py` 重建矩阵 → draft 入库。**draft-only，待人工激活**。
- **TTM 更新**：89.34 亿/1.2692 → **112.30 亿/1.5954**（中报 published 2026-08-20 入库）。复核中发现 08-12 卡内 TTM 手推基数有误（当时把 2025Q1 当 2025H1 用），以系统重算为准；本轮对话早前"PE-TTM 10.9"口头估算同步修正为 **11.85**（现价 18.90 折算）。
- **刻度口径排查（重要）**：底稿"当时口径"PE 分位 p50 11.96→16.84 跳动，排查结论=①08-17 股本快照口径 issued→group_total（股数一致 70.391 亿）②08-26 财报全历史回填修正历史 TTM 基数（样本窗 2023-08-14/726 天→2023-10-30/688 天，2023-24 盈利谷底期当时 PE 41.7/53.0 进入序列）——系数据修正非行情因素，两代快照分位不可直比。折算口径（历史低点价÷1.5954）低点带 **8.56–9.40**（2024-11/2025-07/2025-08/2026-07 四低点），PE 三情景 13/11/9.5 **维持**（刻度稳定，体系未切换）。
- **情景上移**（盈利改善→档线上移纪律）：EPS 中性/悲观/极悲 1.49/1.21/0.99 → **1.56/1.35/1.21**（H1 锁定使旧 87 折 105 亿隐含 H2 -18% 过度悲观；极悲 0.99 需 H2 亏损与 H1 锁定矛盾）。矩阵：T1 15.70-16.40→**16.47-17.16**、T2→**14.11-14.85**、T3→**11.50-12.41**；证伪线 9.40→**11.50**（=1.21×9.5；3 年最低 11.11 为盈利谷底双杀，体系外参考）。胜率区间 50-60/60-70/65-75 维持，Kelly 上限 0/9.9%/15.8%（build_schedule 重算）。
- **右侧位重置**：旧触发位 18.50 已于 08-19 放量 2.09× 触发、08-20 confirmed（episode 终态用尽）；08-25/26 收盘跌破 hold 线 18.315（台账 #8→#9 止损 -4.2%），08-27 缩量收复 18.90。新触发位 **19.55**（半年线 19.25×38.2% 回撤 19.54 共振带上沿，状态机判定线 19.75）、止损 **17.90**（MA20 17.89/8 月下旬平台下沿）。波段箱体仍不适用。
- **产物**：`cards/600346.SH/draft_2026-08-28.json` + `恒力石化估值排期卡_draft_2026-08-28.md`（含复核对照表）；draft `600346SH_0f65e846`（--next-review 2026-10-31）。**缺口标注**：券商 forecast 快照停 2026-08-12（中报后是否上修未验证），激活前后应刷新；组合执行缺口（数据源无该接口，价差人工跟踪）。
- **数据纪律备注**：卡复核属例行流程无代码/配置改动；本次复核期间只读排查 pipeline_runs/indicators_daily/share_capital_events，未重算任何派生表（right_side/daily_watch 重算为幂等例行）。

## 2026-08-28（续：恒力石化 forecast 快照刷新 → draft 更新 600346SH_9b168869）

- **刷新**：`akshare_collect --symbols 600346.SH --sources forecast --date/--end 2026-08-28`（显式 --end，避开已知坑）+ ingest 1 行 → forecasts snapshot #20（akshare/同花顺源）。**中报后 FY1 上修 +4.6%：120.78→126.38 亿**（FY2 141.93 / FY3 159.54 亿）。
- **对 draft 的影响评估（只动注释层，矩阵不变）**：中性 110 亿（EPS 1.56）恰为新 FY1 的 87 折（109.95），中性数字不变、依据增强；bull 口径 1.72→**1.80**（FY1 足额）；裂口 −65.5pp→**−57.4pp** 仍有利（H1 实际 +136.2% vs FY1 +78.6%）；三档价区/证伪线/右侧位/PE 刻度全部不受影响。
- **产物**：draft_2026-08-28.json 三处更新后重新入库为 **draft 600346SH_9b168869**（取代 600346SH_0f65e846，后者建议人工 reject）；markdown 复核稿同步更新（对照表加 FY1 刷新行）。激活/拒绝均由人工执行。

## 2026-08-28（续②：恒力石化复核稿人工激活）

- **人工确认激活** draft `600346SH_9b168869`（effective_from=2026-08-31，衔接旧卡 next_review 日）；旧 active `600346SH_5df2b631` 自动置 superseded（effective_to=2026-08-31，排他端点，08-28（周五）盘后 daily 仍按旧卡算信号）。current.md 已刷新至新卡视图。
- **待清理**：被取代的复核初稿 `600346SH_0f65e846` 仍为 draft 状态，待人工 reject（agent 不代做）。
- 新卡要点备忘：T1 16.47–17.16 / T2 14.11–14.85 / T3 11.50–12.41；证伪线 11.50；右侧触发 19.55（判定线 19.75）/止损 17.90；next_review 2026-10-31。

## 2026-08-28（入池：中国神华 601088.SH / 长江电力 600900.SH / 陕西煤业 601225.SH，watchlist 23 只）

- **流程**：watchlist.yaml 加 3 行 → `db seed` upsert（active=23）→ akshare 六源采集（price/forward/financials/announcement/forecast/stock_info，--start 2023-08-09 --end 2026-08-27）→ ingest → daily 首跑 ok=23 → stock_info 回补 → indicators 全量重算（pe_ttm 全非空）→ pytest 443 全绿。
- **踩坑一（代理断连静默丢源）**：东财 push2his 走系统代理被 RemoteDisconnected，price 3 requests 全 error 但进程末尾只打 per-source 汇总，`tail -12` 只看到后续源 ok 险些漏检——靠 price 目录只有 _meta.json 无 CSV 才发现。改用 `--price-api sina` 备用源重采成功。教训：新入池首采后必须核对各源 CSV 实际落盘，不能只看末尾汇总行。
- **踩坑二（stock_info 与 daily 的先后依赖）**：首轮 daily failed=3——`daily_bars 为空，无法推导股本快照 effective_at（§2.5 不猜）`，且失败回滚该股全部阶段（价格也未入库）。移出 stock_info run 目录重跑 ok=23 后，再回移 + `ingest stock_info` + indicators 重算补 pe_ttm。与美的/格力 8/27 先例一致：**新入池六源采集时 stock_info 必须等价格入库后单独回补**。
- **数据覆盖**：神华 730 bars（2025-08-04~15 重组停牌 10 日，sina 源停牌日无行，日历 gate 口径一致非缺陷）/ 长电 740 / 陕煤 740；周线 154/156/156；信号 1502/1522/1522；财报全历史 81/95/62 期（远超 3 年价格窗，TTM 余量足）；公告 463/315/224 条；forecast 各 1 快照；pe_ttm（snapshot_share_basis）最新 20.11/19.05/16.19。
- **口径备注**：无卡期间日报对三只 degraded(no_active_card) 属预期；排期卡未排（神华/长电为强周期+类债水电，PE 刻度锚定思路与现有卡不同，待用户发起）；今日（08-28，周五）盘中，价格截至 08-27 完整日，当日 bar 由盘后例行 daily 补齐。

## 2026-08-28（续③：三连卡 draft——神华 601088SH_19b6dcd0 / 长电 600900SH_a8c0c9a4 / 陕煤 601225SH_5a669742）

- **流程**：三股当日入池后即跑 `card_inputs` → 锚选择（细则：神华/陕煤强周期、长电公用事业）→ 情景矩阵 `build_schedule.py` → 胜率打分 → draft-only 入库，**全部待人工激活**。产物：各股 `draft_2026-08-28.json` + 估值排期卡 markdown。
- **锚选择与体系判定**：①神华=类债分红（煤电路港长协平滑，2023-2025 三连降收敛），底部价位逐轮上移 27→32.7→37.5→**42.46（2026-03-02 真实锚）**，体系上移取最近底部 PE 18.8 为悲观基准（18/19/22）；②长电=细则公用事业**主锚股息率**，PE 机器层为线性映射（中性 PE 18.5=股息率 3.8%，DPS=中性 EPS×70% 承诺下限），带极窄 18.8-23.6 体系稳定，T1 即压 2024-11~2025-12 恐慌底带 26.44-27.37；③陕煤=强周期（细则主锚 PB 但系统无 PB 序列=人工缺口项），底部 PE 刻度 4.3→8.5→11.5 上移，T1=中性口径 13 倍 25.58-26.65（现价回档 1.3% 即入区）。
- **三卡要点**：神华 T1/T2/T3=41.95-43.70/37.91-39.90/32.40-34.99，证伪线 32.40，右侧 51.70/45.50；长电 T1/T2/T3=26.28-27.38/23.73-24.98/21.60-23.33，证伪线 21.60（=3 年最低带下沿），右侧 29.85/27.45，**唯一 T1 胜率下沿即正期望（Kelly 5.6%）**；陕煤 T1/T2/T3=25.58-26.65/22.85-24.05/17.60-19.01，证伪线 17.60，右侧 28.50/24.90，T1 质量三卡最低（赔率 b=0.78，Kelly 0，纪律从严）。全部 next_review 2026-10-31。
- **共同缺口（激活前人工核对项）**：①系统无分红/PB 序列（corporate_actions 空）——神华/长电股息率、陕煤底部 PB 均需人工补数核对；②神华/长电 2026H1 未披露（截止 08-27，披露临近=激活后立即复核触发器）；③长电 2026+ 分红承诺是否续期（2021-2025 ≥70% 到期）直接动摇其主锚。三家券商 FY1 快照=今日采集（akshare 源）：神华 583.4 亿（与 Q1 实际方向相悖，卡内已按实际趋势下修）、长电 357.7 亿、陕煤 203.8 亿（与 H1 实际裂口 +47.6% 有利）。
- **纪律提示**：三卡均 draft-only；activate/reject 由人工执行。

## 2026-08-28（续④：三连卡激活前立即复核——股息率/分红承诺/底部 PB 全部闭合，draft 换版）

- **复核方法**：缺口项均标注"系统无分红/PB 序列"，本次用一次性探测闭合（akshare stock_fhps_detail_em，含每股股利/每股净资产/股息率；仅复核消费不录入管线，来源已在卡内 input_snapshot 标注）；长电分红承诺在公告簿内直接命中（2025-08-14《未来五年（2026-2030年）股东分红回报规划的公告》）。
- **复核结论（全部通过，档价/矩阵零改动）**：
  - **神华**：2025 全年 DPS 2.01 元（中期 0.98+年度 1.03），分红率 75.6%；静态 TTM yield 现价 4.20%、T1 区 4.60-4.79%、T3 区≈5.96%；中性盈利口径（2.30×75.6%=DPS 1.74）T1 区≈4.0%——股息率主锚验证通过。
  - **长电（实质性闭合）**：2026-2030 分红规划**已公告续期**（原卡最大缺口消除）；2025 实际 DPS 1.00 元（三季 0.21+年度 0.79）、分红率 70.9% 达标；静态 TTM yield 现价 3.56%、T1 区 3.65-3.80%，与中性 DPS 1.036 口径一致。残留项：规划具体比例待读公告原文（次要）。
  - **陕煤**：点时 BPS 口径历史底部 PB 带 **1.9-2.1x**（2025-03 低 18.59/BPS 9.344≈1.99x；2026-02-03 锚 20.64/BPS 9.998≈2.06x）；现 PB 2.59x；T1 区 2.45-2.55x（回档位定位）、T3 区 1.69-1.82x 破底带——与证伪线"体系击穿"逻辑自洽，PB 主锚验证通过。另确认 2026 中期分红预案 10 派 0.58（"小中期+大年度"模式延续，2025 全年 DPS 0.948 / payout 54.8%）。
- **draft 换版**（数值层零改动、注释层闭合缺口后重新入库）：神华 **601088SH_ca2eab78**、长电 **600900SH_4988e3fb**、陕煤 **601225SH_a0f29c77** 取代初版（19b6dcd0/a8c0c9a4/5a669742，建议人工 reject）。激活/拒绝由人工执行。
- **教训**：公告簿（events）本身就是分红承诺类信息的第一核对源——本次长电规划公告早在簿内，"无分红序列"的缺口表述过重；后续类似核对应先查公告簿再外探。

## 2026-08-28（续⑤：三连卡人工激活）

- **人工确认激活**：神华 `601088SH_ca2eab78` / 长电 `600900SH_4988e3fb` / 陕煤 `601225SH_a0f29c77`（均 effective_from=2026-08-31，next_review 2026-10-31）。initial 代 draft（19b6dcd0/a8c0c9a4/5a669742）留待人工 reject；恒力被取代初稿 600346SH_0f65e846 亦同（agent 不代做）。
- 激活后全池状态（据库实查修正）：23 只股票 **18 张 active 卡**（8/10-8/14 批次 11 张 + 洛钼 8/27 + 恒力 v2 + 本批三连卡等）；无 active 卡仅 5 只（法拉/万华/美的/格力/西矿——西矿 draft cc4c2ac7 仍待人工激活）。**另发现：13 张卡 next_review=2026-08-31（周一）集中到期**，为 8/10-8/14 建卡批次的第一个复核窗口，构成下周一的批量复核工作量，建议逐张走 card_inputs 对照复核。

## 2026-08-28（续⑥：draft 清理）

- **人工确认 reject** 四张被取代的初稿：600346SH_0f65e846（恒力）、601088SH_19b6dcd0（神华）、600900SH_a8c0c9a4（长电）、601225SH_5a669742（陕煤）。库内版本状态：active 18 / superseded 1（恒力 v1）/ rejected 5 / draft 5。**剩余 5 张 draft 非本次产物、未处置**：美的 000333SZ_b6226347、格力 000651SZ_f7c4f770（均 2026-08-28 凌晨另一会话生成）、万华 600309SH_6c62ce20、法拉 600563SH_b11e5de5（8/24）、西矿 601168SH_cc4c2ac7（8/23，handoff 在册待人工激活）——activate/reject 均待各自人工决定，agent 不代做。

## 2026-08-28（续⑦：遗留五卡人工激活——全池 23 只 active 卡全覆盖）

- **人工确认激活** 存量 5 张 draft：美的 000333SZ_b6226347 / 格力 000651SZ_f7c4f770 / 万华 600309SH_6c62ce20 / 法拉 600563SH_b11e5de5 / 西矿 601168SH_cc4c2ac7（均 --effective-from 2026-08-31，next_review 2026-10-31）。激活前 30 秒体检：美的/格力数据截止 08-27 新鲜；万华/法拉/西矿截止 08-21（一周内，可接受）。
- **标注**：美的卡 financial_reports 截止停在 2026Q1（中报未入底稿）——若其 2026 中报已披露，EPS 情景先于中报，列入该卡下次复核优先项；美的/格力卡 right_side_trigger 为空（右侧仓未定义），日报右侧信号对这两只将无输出。
- **全池状态**：23/23 只股票 active 卡全覆盖（draft 清零）；draft/rejected/superseded 见前两节。2026-08-31 起全池卡片信号同口径进入日报；**next_review 分两批**：2026-08-31 到期 12 张（8/10-8/14 建卡批次）、2026-10-31 到期 11 张（平安 000001、恒力 v2、洛钼、三连卡、本批五卡）。

## 2026-08-28（续⑧：消息面研判 r2 合并设计稿）

- **产出**：`docs/superpowers/specs/2026-08-28-message-eval-design-r2.md`，取代 r1（`2026-08-26-message-eval-design-r1.md`），合并吸收 `~/Downloads/股票消息面研判系统设计.md`（v1.0）。
- **合并要点**：①定位分层——新稿作"业务规矩层"（四层分类/四道筛子/证伪条件/判断归因），r1 作"机器实现层"（采集/幂等/关联/LLM 初判/人审 gate/报告）；②`events.scope` 扩为五档（+flow 资金/情绪，静默入库不推送），新增 `source_tier` 信源分级（tier 5 不进决策链）；③`event_assessments` 重建时扩展 target/half_life/expectation_gap/action_hint/falsification/narrative 列（LLM 初判、人审 amend 补写，原始行不动）；④新增 `event_calendar`（L0 日历层，财报预约/解禁 akshare + 宏观手工 + 排期卡 next_review 派生 union，到期前 3 天提醒）与 `message_judgments`（L4 判断闭环：证伪条件+复核日期+归因）；⑤混合 gate 新增 company+tier≤2 一律人审；⑥报告新增"日历提醒+公司公告置顶（需读原文）+价格位置交叉验证行+背离样本 divergence 标记"；⑦落地改渐进四 Phase（Phase 1 日历+公告无 LLM 先行），migration 拆 0003–0006。
- **状态**：纯设计稿，无代码/库变更；待人工 review 后按 Phase 1 动工。

## 2026-08-28（续⑨：消息面 r2 Phase 1 交接文档）

- **产出**：`docs/superpowers/specs/2026-08-28-message-eval-r2-phase1-handoff.md`，供其他 Agent 接手实现 Phase 1（日历层 + 公司公告，无 LLM）。
- **范围圈定**：只做 Phase 1，做完即停等人工 review；Phase 2–4（macro_factors/flow 采集/LLM 评价链/judgments）明令禁止夹带。
- **落点核实**（explore 代理只读核查）：迁移由 `db.py:41 migrate()` 按文件名自动发现；公告入库链路已通（akshare_collect 有 announcement 源但不在默认 --sources；ingest._ROUTES 已路由至 announcements.py 公共引擎）；报告七段结构锁在 `test_report.py:224`，新段插"观察点"与"衰竭信号"之间需同步改断言；卡片复核到期提醒（queries.py get_dashboard_alerts review_due）为日历横幅现成复用模板；项目此前无任何事件日历实现。
- 无代码/库变更。

## 2026-08-28（续⑩：消息面 r2 Phase 1 实现——日历层 + 公司公告）

- **基线**：先提交存量未提交工作（`6ddf849`：公告公共引擎/backtest/23 股扩池/r1-r2 设计稿），Phase 1 全部改动单独成提交。
- **migration 0003**（`0003_message_calendar.sql`）：新表 `event_calendar`（cal_id PK + idx_event_calendar_date）；`events` 扩 `scope`/`source_tier`；`watchlist` 扩 `industry_code`/`themes_json`。真实库已迁移（备份 `data/market.db.bak_20260828`），重跑幂等验证通过。测试 `test_db.py` 迁移清单断言同步 +`test_event_calendar.py` 建表/幂等用例。
- **信源分级**：常量收敛在 `adapters/announcements.py`（`SOURCE_TIER_ANNOUNCEMENT=1`/`SOURCE_TIER_TELEGRAPH=4`）；公告走公共引擎（tdx+akshare 两源同时覆盖），电报在 akshare 薄壳写 4；tyc/kimi 历史公告路径保持 NULL（=未分级，不回填推断，database_schema §6 已写明语义）。
- **采集**：akshare_collect 默认 `--sources` 加 `announcement`（文档命令示例同步）；新增 `calendar` 源（`--calendar-period` 必填，不做默认推断——`--end` 硬编码教训）：`stock_report_disclosure`（全市场拉取仅留 watchlist 行，scheduled_date 取"当前预约"=三次变更依次覆盖）+ `stock_restricted_release_queue_em`（逐股，仅留采集日后未来行）→ `calendar/{date}/{run_id}/{report_disclosure,unlock}.csv`。新增 adapter `scripts/adapters/event_calendar.py`（stem 分派；cal_id 确定性哈希 `INSERT ON CONFLICT DO NOTHING` 幂等；ingest 路由 `("akshare","calendar")`）。**真实采集已验证**：半年报期次 23 只披露预约（神华/美的/南航 08-29、长电 08-31 落入提醒窗）+ 解禁 7 行落盘。
- **采集踩坑**：`pandas.NaT` 也有 `strftime` 属性但调用即抛 ValueError——`_date_str` 需 try/except 吞掉（首采 disclosure 全体 ERROR 后修复重采）。
- **手工种子**：`config/event_calendar.yaml`（FOMC 2026-09-16/10-28/12-09 精确值，来源 federalreserve.gov 官网核对；国内 CPI 9/9、社融 11、LPR 20 按惯例预填，note 注明以官方为准）；`db.py::seed_event_calendar` jsonschema 校验 + incomplete_todo 跳过 + cal_id upsert，挂入 `seed()`。watchlist.yaml 23 只补 `themes`（人工判读行业词）；`industry_code` 全部留 NULL（无可靠东财 BK 码来源，§2.5 不猜，待人工补）。
- **到期查询**：新模块 `scripts/signals/calendar_due.py::due_items`——event_calendar 行（窗口按每行 remind_before_days，**含两端边界日**）union active 卡 `next_review_at <= as_of`（card_review 派生项）；`relevant_to_symbol` 单股过滤（本股+宏观+本股卡）。报告与 UI 共用。
- **报告新段**：`## 5. 日历与消息面` 插入观察点与衰竭信号之间（**原 5/6/7 段顺延为 6/7/8**，标题/注释/docstring/test 八段断言同步）：`### 日历提醒（默认 3 日内）` + `### 公司公告`（`substr(available_at,1,10) = as_of` 日期化比较——直接 `available_at <= as_of` 对 datetime/日期字符串恒 False，r2 简写不可照抄；置顶"需读原文"，无公告写"今日无新增公告"）；`input_snapshot_json` 加 `calendar_due` 计数。
- **UI**：`get_dashboard_alerts` 并入 event_calendar 到期项（card_review 由原 review_due 覆盖不重复）；`page_cards` 过滤 review_due+calendar 传模板，`cards.html` 顶部横幅（Tailwind 琥珀色，无到期不渲染）。
- **测试 456 项全绿**（443→456，净增 13）：新 `tests/test_event_calendar.py` 6 项（迁移/种子校验与 upsert/窗口边界/CSV 幂等/路由锁定）+ source_tier 3 项（akshare 公告=1、电报=4、tdx 公告=1）+ UI 横幅 2 项（有到期渲染/空态不渲染）+ 报告段 1 项（窗口边界/公告点时可见性/空态/快照计数）+ 测试文件内既有用例更新（八段断言、_add_card next_review 参数化、迁移清单）。
- **端到端冒烟**：真实日历 CSV ingest 30 行 → 601088.SH 报告"日历提醒"正确给出 08-29 披露预约（首次预约 08-31 已变更）；Flask 实起 `/cards` 横幅渲染 5 条披露提醒（curl 验证）后即停。**勘误（续⑪）**：本条原记"写临时库副本、真实库除 migration 外未写"——经 raw_objects 时间戳（13:13:59Z=冒烟时刻）与备份数据库（20:12 备份无 calendar 数据）核实，**该批 30 行实际写入了真实库**；写入路径异常（命令带 `--db` 指向临时副本）当晚受控实验复测 `ingest --db` 行为正常，机制未定论。内容本身已逐行核对无误，且当晚用户已明确批准日历种子与采集数据入库，最终状态与批准一致。
- **真实库状态（8/28 晚终态）**：event_calendar = 6 手工种子 + 30 采集行（半年报批次披露预约 23 + 解禁 7）；用户批准执行 `db seed`（6 行）与 calendar 采集入库；2026三季预约源侧为空（三季报 10 月披露、预约表 9 月底才有），届时补采 `--calendar-period 2026三季`。
- **偏差/决定**：①报告与横幅不再叠加 kind 中文标签（note 在来源端自描述，消除"财报披露预约：财报披露预约"式重复）；②报告日历提醒行固定附"检查头寸是否落在计划档位内"尾注（r2 §3.1 提醒语义）；③事件日历行不做时点校验降级（非 §2.1 计算输入，仅提醒用途）。

## 2026-08-28（盘后 daily：修复后 ok=23；origin 起点踩坑；日历层首批数据入库）

- **采集**：默认五源 + forward 补采（sina 源，显式 `--end 2026-08-28`），run-id `run_daily_20260828`。⚠️ 新坑：23 只 × 5 源全量重采集超 900s 被超时中断，announcement 停在中段（仅 8/23 落盘）——采集器无断点续跑，按 `--sources forward,announcement` 单源补跑完成（幂等，重采 8 只内容一致）。最终 price/forward/financials/index/telegraph/announcement = 23/23/23/2/1/23 全 ok，23 只当日 bar 齐（收盘抽查与源一致：603605 收 61.35 等）。
- **daily 一跑 failed=2**（600531/603697，P1 回滚）：`ValueError: origin 日 2023-08-09 不在因子序列内`。根因链：新采 sina forward 成为该股最新 forward 文件 → `check_factor_change` 跨源比对（库内 kimi 系因子 vs sina qfq）在重叠窗口贴/越 0.1% 容差判"变化" → 防御性重建 origin 取库内最早 bar（2023-08-09）→ 采集 `--start` 用了 argparse 默认 2023-08-10，forward 序列缺 08-09 → 崩。**修复**：两股改 `--start 2023-08-09`（对齐库内 origin）重采 price+forward → daily 幂等重跑 **ok=23**，全池日报 revision=2。**教训：每日增量 `--start` 不得高于池内最早库内 bar（现 2023-08-09）**——argparse 默认 2023-08-10 对 08-09 起池的 6 只（603605/000333/000651/600900/601088/601225）是同款隐患，今日未崩仅因未判"因子变化"，一旦除权即触发。
- **因子重建**：600531 v44 / 603697 v45（origin 2023-08-09，sina 平台段口径；vs 旧 kimi 系因子微调 +0.10% / −0.04%，信号口径实质不变，周线/指标/信号随管线原子重算）；603993 v43+v46——**连续第 4 日同内容防御性重建**（source_factor_at_origin 均 0.93966，max_dev 恒贴 0.1% 容差），"贴阈不稳定"复现且每次 daily 必触发，待专项排查根因。
- **日历层首批数据入库（r2 Phase 1 收尾）**：① manual 种子 6 行（FOMC 3 + 宏观 3）随今晚 21:13 一次 `db seed` 入库——即续⑪ industry_code 同步所跑的 seed()，`seed_event_calendar` 挂在其内一并生效（续⑪"种子仍未入真实库"记载先于该 seed，以库实查为准）；② akshare 日历 30 行（半年报披露预约 23 + 解禁 7，与 Phase 1 冒烟同源文件）由本次 daily 的 other-files 循环自 `data/raw/akshare/calendar/` 扫入——"待人工核对再入库"被例行管道事实性绕过（内容与冒烟一致、无冲突；如需回滚删 `event_calendar WHERE source='akshare'` 30 行即可）。报告"5. 日历与消息面"实数据生效：陕煤披露预约 08-28、神华（首次预约 08-31 已变更至 08-29）/美的/南航 08-29、长电 08-31；今日 23 只公告簿无新增。
- **结果**：ok=23；报告 14 complete + 9 degraded（恒力/万华/美的/格力/法拉/西矿/神华/长电/陕煤 no_active_card，卡 2026-08-31 生效，§2.5 预期）。P1/P2 空；P3 三项：南航 T1（5.06，第 2 日）、有友 T1（9.56，第 2 日）、平安 sell_zone（55.85，贴 box_high 56.00）；P4 三项：珀莱雅跌出 T1 下沿 0.2%（61.35，−1.59%，昨日 P3 转 P4）、洛钼贴 T2 上沿 0.3%（19.51 持平，第 4 日）、天赐距 T1 上沿 1.8%。指数：000300.SH 增至 08-28（−0.46%）；^HSI 滞后一日（08-27，−0.34%，既有现象）。

## 2026-08-28（续⑪：industry_code 东财 BK 码回填 + push2 风控处置）

- **背景**：push2/push2his 对本网络整体不可达——诊断矩阵证实**直连与代理出口 IP 双双被服务端拒**（直连 TCP 通但 HTTP 层空响应；真实 Chrome 同样超时，排除 TLS 指纹；akshare 走 macOS 系统代理偶发 200 后归零）。08-26/27 能采是因为当时代理出口尚未进 EM 风控名单。行情历史继续用 `--price-api sina` 绕行；datacenter-web/cninfo 直连恒通不受影响。
- **突破口**：`push2delay.eastmoney.com`（东财延迟行情域）**直连同路径 API 可用**——`api/qt/clist/get` 全参数兼容。板块归属是静态数据，延迟域完全够用；`--noproxy`/`trust_env=False` 直连 ~60 请求/秒，496 板块成份全量 <10s。
- **体系澄清**：`fs=m:90+t:2` 现返回 496 个板块 = 东财新旧两套行业树并存（旧一级如 BK0437 煤炭/BK0478 有色金属与新三级如 BK1493 动力煤/BK1615 铜互相交叠，maximal 判定失效、每股命中多个板块）。**口径决定**：取每股所属**最细三级板块**（东财个股归属口径，全市场唯一）；并列时（旧树Ⅱ级与新树Ⅲ级成员完全相同时）取新分级 Ⅲ 命名，保证确定性。完整包含链留档 `/tmp/bk_final.json`。
- **回填**：23/23 全部命中——神华/陕煤 BK1493 动力煤、长电 BK1380 水力发电、紫金/西矿/洛钼 BK1615 铜、豫光 BK1614 铅锌、珀莱雅 BK1498 品牌化妆品等；`config/watchlist.yaml` industry_code 写回（含板块名注释），`seed_watchlist` 同步真实库（NULL 归零；事件日历种子仍未入真实库，待人工核对）。
- **测试**：456 项全绿（yaml 变更无测试面）。
- **遗留**：①push2 风控解除后 `stock_board_industry_name_em` 等接口恢复，可在 Phase 3 用同一套映射校验/重建 `symbol_industry` 全市场表；②本条映射口径（最细三级）需在 Phase 3 设计时与事件侧行业标签粒度对齐（r2 §5 关联层）。

## 2026-08-28（续⑫：消息面 r2 Phase 2——宏观因子 + flow 静默入库）

- **范围**（r2 §13 Phase 2，无 LLM）：migration 0004 macro_factors；macro 采集（商品/外汇清单驱动）；flow 层（龙虎榜+大宗）静默入 events；电报持续采集=既有 CLI 已覆盖（`--sources telegraph` 本就在默认清单），无需新代码。**解禁已在 Phase 1 完成**；两融（粒度设计未定：全市场余额 vs 个股明细）与热度榜（tier 5 情绪温度计语义）**明确缓办**，不属本期。
- **落点核实**（逐接口实测）：商品内盘 `futures_zh_daily_sina`（AU0/CU0/I0/RB0/SR0/SC0）、外盘 `futures_foreign_hist`（OIL 布伦特/CL WTI）、外汇 `currency_boc_sina`（央行中间价，缺失回退中行折算价）全部走 **sina 系域名直连可达**；龙虎榜 `stock_lhb_detail_em`、大宗 `stock_dzjy_mrmx` 走 data.eastmoney.com/datacenter-web，**不踩 push2**。
- **migration 0004**：macro_factors（PK (factor_type,code,trade_date) + idx_macro_factors_recent）。真实库迁移前备份 `data/market.db.bak_20260828`（同日第二份迁移，复用当日备份原则）。
- **采集**：`--sources macro`（config/macro_factors.yaml 清单驱动，jsonschema 校验；close 存来源原始值，change_pct 来源无则空；单因子失败记 stderr 继续不冒充）+ `--sources flow`（龙虎榜按"股票×日"合并多上榜原因、数值取首行不跨原因加总；大宗每笔一行；仅留 watchlist 行）。**两者进默认 sources**（Phase 3 LLM 需要每日宏观底稿；flow 盘后例行）。**防坑**：flow 查询窗口硬上限 10 天（`--start` 默认 2023 起会让龙虎榜查询跨三年）。
- **入库**：`adapters/macro_factors.py`（PK upsert，同日重采=事实刷新非版本化）+ `adapters/flow_events.py`（龙虎榜/大宗 → events(event_type='flow', scope='flow', source_tier=3——交易所公开数据的东财聚合加工视图，对齐 r2 §4 flow 3~5；available_at=published_at 当日可得；event_id 确定性哈希幂等）。ingest 路由 `("akshare","macro")`/`("akshare","flow")`。
- **信源分级常量**：announcements.py 新增 `SOURCE_TIER_FLOW=3`。
- **真实采集入库**：macro 11/11 因子全实值（沪金 999.28 元/克、布伦特 87.82 美元/桶、USDCNY 中间价 678.11 CNY/100USD 等，全部 08-28 收盘）；flow 龙虎榜 EMPTY（23 只当日无上榜，属正常）、大宗命中紫金两笔（08-21/08-27，摩根大通证券买方、机构专用卖方，折溢价 0%）→ 真实库 macro_factors 11 行 + events flow 2 行。
- **测试 463 项全绿**（456→463，净增 7）：新 test_macro_factors.py 3 项 + test_flow_events.py 4 项；test_db 迁移清单断言加 0004。
- **纪律确认**：flow 事件**不进报告、不进日报、不推送**（r2 §8.4）——本期 report.py/日报零改动；公告/电报的 scope 分类仍留 Phase 3（flow 是唯一本期填充的 scope 值）。
- **偏差/决定**：①龙虎榜/大宗 source_tier 定 3（聚合加工视图）而非交易所原文 tier 1——净买额/折溢价为计算值，与 r2 §4 flow 3~5 对齐；②宏观因子不设 index_proxy 因子（基准指数已有 index_bars 通道），schema 预留；③两融/热度榜缓办理由如上。

## 2026-08-28（续⑬：消息面 r2 Phase 3——LLM 评价链 + 人审 UI）

- **migration 0005**：①`symbol_industry` 新表（全市场行业归属，季度刷新）；②`event_assessments` 重建——assessment_version 改 TEXT NOT NULL（修 0002 遗留）+ 扩 target/half_life/expectation_gap/action_hint/falsification/narrative 研判字段，历史 event_study_v1 行 11877 条全量平移无损；③`event_human_review` 新表（confirm/dismiss/upgrade_materiality/note/amend，PK 含 reviewed_at 多次留痕，不改写原始行）。真实库迁移前再备份 `data/market.db.bak_20260828_2`。
- **LLM 模块**（`scripts/llm/`）：client.py（openai-compatible /chat/completions，指数退避重试，严格 JSON 解析剥围栏，api_key 走环境变量不入库）；prompts.py（系统铁律：不产数字/不预测/不建议买卖/只输出 JSON；四道筛子字段口径）；schema.py（事件级/叙事 JSON Schema，非法丢弃不冒充）；eval.py（6b1→6c→6b2 编排 + gate）。**config/llm.yaml 默认 enabled=false**——关闭态 daily 记 success+notes（设计关闭非 degraded），api key 配置后即启用。
- **gate（r2 §6.3）**：materiality∈{high,critical} ∨ confidence<0.4 ∨ rationale 命中禁用词 ∨ (scope=company ∧ tier≤2) → needs_review；needs_review 不进报告段。
- **关联层**（`scripts/signals/event_link.py`，L2 确定性）：scope 关键词初分（**macro 词表先于 policy**——"央行降准"归 macro，r2 §5.2 例）；关联候选 ①手工/既有保留（INSERT OR IGNORE）②symbol_industry 行业名命中 ③watchlist themes 词边界（后向排除"周/节"复合词——"黄金周"不误配"黄金"；前向不设挡；precision 局限由 LLM+人工补）。resolve_effective 人审 replay：dismiss 可被 confirm 撤销、amend 覆盖显示值、upgrade 覆盖 materiality，事件级先应用逐股后应用。
- **symbol_industry 采集**：新 `scripts/collect/industry_collect.py` 走 **push2delay 域**（push2 风控规避），全市场 496 板块成份反查 5641 只（每股最细三级，与 watchlist 回填同口径），ingest 路由 ("akshare","industry")；与 watchlist 23 只交叉核对 0 不一致。
- **daily 集成**：步骤 6b 池级（照 event_study 模式：`with conn` 池级事务、异常记 degraded 不阻断报告）。**真实跑验证**：llm_eval 阶段 status='disabled'、notes 如实（设计关闭），event_study success，报告照常生成。
- **报告**：`### 消息面（LLM 初判 + 人审后）` 子节——触发条件 effective ok + narrative 非空 + available_at ≤ as_of；needs_review/否决不冒充展示；预期差待人工补写显式标注；**价格位置行**（距最近档边界 + 活跃衰竭信号数，确定性 join）；snapshot 加 message_shown。
- **UI**：/message-review 人审页（服务端渲染表格：状态/四道筛子标签/rationale/关联股 + 每行表单：确认/否决/留痕/升级 materiality/补写预期差-证伪-target-half_life，actor 落库）；POST /message-review/<id>/action 写 event_human_review（未知 action 400）；导航新增"消息审"。
- **测试 474 项全绿**（463→474，净增 11）：test_event_link.py 4 + test_llm_eval.py 4（FakeLLM 全链/gate/disabled/schema）+ UI 2 + 报告段 1（dismiss 前后对比）。
- **过程坑**：连续快速重写同尺寸文件导致 `.pyc` mtime+size 碰撞复用过期字节码——出现"改了没生效"的灵异现象，清 `__pycache__` 解决（pytest 用户遇怪异结果先清缓存）。
- **遗留（Phase 4 / 运维）**：①`config/llm.yaml` 需人工填 api key 并 enabled:true 后，LLM 链才真实产出（模型 glm-4-flash 可换）；②message_judgments 判断闭环属 Phase 4；③主题词边界 precision 局限由 LLM+人工补（r2 §3.6 二期）。

## 2026-08-29（入池联影医疗 688271.SH / 迈瑞医疗 300760.SZ，watchlist 25 只）

- **流程**（复用 08-28 六源先例）：watchlist.yaml 加 2 行（行业码 BK1605 医疗设备，经 symbol_industry 表反查确认两股同属）→ `db seed` upsert（active=25）→ akshare 六源采集（price/forward/financials/announcement/forecast/stock_info，`--start 2023-08-09 --end 2026-08-28 --price-api sina --run-id run_mi_0829`）→ daily 首跑 ok=25 → stock_info 回补 → indicators 全量重算 → pytest 全绿（首次记 474，后经清 `__pycache__` 复跑核实为 **476**——474 为续⑬ pyc 陈旧缓存假象复现，无代码改动测试数不应变化即此信号）。
- **新坑（cninfo 502 自愈）**：announcement 采集两度 `JSONDecodeError`（首轮与单独重试 run_mi_0829b 均挂），手打 `www.cninfo.com.cn/new/hisAnnouncement/query` 与 `szse_stock.json` 核实均返回 **502 Bad Gateway（tengine）**——上游故障而非参数/风控（与 push2his 风控拒答现象不同：那里是空响应/超时，这里是网关错误页）。约十分钟后第三次重试（run_mi_0829c）自愈。**教训：akshare JSONDecodeError 先手打接口看原始响应定故障层，再决定等自愈还是换源。**
- **daily 首跑**：`--date 2026-08-28 --raw-dir data/raw/akshare` → **ok=25**（两新股全链路入库；23 只存量同日重跑 report_runs revision=4 属 §9.5 预期）。stock_info 沿踩坑二流程先移出、daily 后回补 + `ingest stock_info`（2 行 inserted）+ indicators 重算。
- **数据覆盖**：两股各 741 bars（2023-08-09..2026-08-28）/ 周线 157 周（至 08-28）/ 信号 1528 行 / forecast 快照 1 / 因子版本 联影 v51 / 迈瑞 v47（forward_over_none_plateau 平台段口径）。财报全历史利润表：联影 25 期（2018 年报起）、迈瑞 40 期（2014 年报起），published_at 全非空（akshare 直回，无需回填）。公告 ingest inserted=676（联影 387 / 迈瑞 289，31 skip 为 cninfo 同日同题附件幺等去重）。股本快照 snapshot_group_total：联影 8.24 亿股 / 迈瑞 12.12 亿股。
- **估值口径备注**：pe_ttm（snapshot_group_total，单快照全局应用，历史近似）最新 联影 49.20 / 迈瑞 25.41 @2026-08-28。
- **收尾状态**：无卡期 degraded 已随排期卡激活闭合（见下「续」节）。今日周六非交易日，无盘后例行。

## 2026-08-29（续：联影/迈瑞排期卡首期生成并激活，全池 active 25 张）

- **流程**（fred-valuation-card-skill 全流程，用户指令「生成排期卡，激活」即人工确认）：`card_inputs` 底稿九段 → 盈利底稿/裂口检查 → 情景矩阵（`build_schedule.py`）→ draft JSON + create-draft → **activate --effective-from 2026-08-31（周一）**。卡号：**688271SH_b7daa877** / **300760SZ_b257c0cd**；draft md 存 `cards/{symbol}/draft_2026-08-29.json` + 激活归档 `2026-08-31_*.md`（CLI 自动生成）。周末无例行，08-31 盘后 daily 起两股卡片相关信号（tier_proximity/tier_triggered/证伪线监测）开始产出。
- **联影医疗 688271.SH**（现价 105.56，PE 49.20）：盈利 2026H1 营收 +17.2% 但归母 -10.1%，Q2 单季 -20.7% 加速下滑，FY1 裂口 **-34.5pp**（券商 +24.4% vs 实际）未收敛——情景以实际趋势为准。EPS 2.05/1.85/1.65（中性=H2 -7.8%）、PE 55/45/43（底部带 42.65–47.74；**2024-09 谷底轮 38.5 排除出情景锚**——盈利谷底污染，同恒力卡 2024-02 处理）。三档 88.56–92.25 / 79.09–83.25 / 70.95–76.63，证伪线 70.95（极悲×悲观 PE；低于 3 年最低 92.00 约 23%）。胜率 30-45/40-55/45-60（轨迹 -15~-20 为主因），Kelly 0/5.1%/10.2%，T1 为纯赔率注。右侧预案 119.80（双顶）/109.20（MA20/60 带）。人工复核项：干涸量能阈值为 None（锚表无下跌起点）。
- **迈瑞医疗 300760.SZ**（现价 164.30，PE 25.41）：FY2025 -30.3% 深度下修后，中报（08-28 晚披露）Q2 单季 **+1.1% 转正**、收入 +10.5%——下滑减速/改善初现；FY1 裂口 -13.4pp 收敛中。EPS 6.60/6.00/5.35（中性=H2 +4.4%）、PE 30/26.5/22（底部带 20.2–22.7 三轮缓降后趋平：22.72→21.21→20.70→约20.2；2023 年 28.9–32.0 旧体系弃用；**若三季报后新低点 PE<20 悲观 PE 下修 18–19 并未买档线下移**）。三档 167.90–174.90 / 151.05–159.00 / 117.70–127.12，证伪线 117.70（低于 3 年最低 130.66 约 9.9%）。胜率 50-60/60-70/65-75（现价≈悲观 EPS×中性 PE，已定价下滑；Q2 转正 +5 与裂口 -5 对冲），Kelly 0/6.3%/15.7%。右侧预案 165.00（三重顶 162.0–164.8，现价贴下方 0.4%，最先可验证）/155.20（MA20）。人工复核项：**衰竭锚滞后 16 个月**（锚前低 206.80 vs 现价 164.30，2026-06 新低 130.66 未入锚），待系统以新下跌段重算。
- **共性**：均 pe_scale 锚、波段仓不适用（反弹/筑底段无成熟箱体）、next_review 2026-10-31（三季报）；两股中报均已落地无待披露复核项；胜率区间宽 ≥15pp 或下沿 ≤50 的档按固定比例下限执行。
- **测试**：`uv run pytest -q` **476 全绿**（清缓存后稳定值；卡片无新增用例——test_card/test_card_inputs 57 项 fixture 隔离，不受真实库影响）。
- **勘误**：本节上文（首段）测试计数 474 系缓存假象，已就地更正为 476。

## 2026-08-28（续⑭：移除 LLM API 自动通道，打标定型为 agent/skill 通道）

- **决定**（用户）：移除 r2 §6.1 的 API 自动打标通道（zhipu chat completions），打标定型为 **agent/skill 通道**——与排期卡同纪律：agent 读结构化底稿现场打标（质量优于单次 API 调用），draft 必过人审 gate。诱因：API 通道首采 6/6 全被 schema 拒（glm-4-flash 输出形态与严格 schema 不齐），对接摩擦与 key/成本负担不值得。
- **删除**：`scripts/llm/client.py`（API client）、`prompts.py`（规则并入 SKILL.md）、`eval.py`（6b1/6b2 API 编排，其中确定性 gate 迁入 inputs.py）、`tests/test_llm_eval.py`。
- **保留/增强**：`scripts/llm/schema.py`（+normalize_event：多余键丢弃、confidence 字符串归一）；`inputs.py` 新增 **--symbol 个股过滤**（export 只导该股 event_symbols 关联的未评价事件，个股深查模式）与 gate 内聚（读 config/llm.yaml review_gate）。
- **daily 变更**：步骤 6b 由 llm_eval（disabled 占位）改为**确定性关联层** stage 'link'（event_link.run_link_stage：scope 关键词初分 + themes/symbol_industry 关联，池级事务，失败 degraded 不阻断报告）。真实跑验证：link success、scope 更新 12553。
- **config/llm.yaml** 瘦身为 review_gate + prompt_version（API 字段全删；用户此前手工 enabled:true 一并失效，无需回滚）。
- **测试 474→473 全绿**（删 eval 4 项；新增 gate 规则 6 断言、--symbol 过滤、非法标签拒绝等 3 项）。
- **偏差决定（对 r2）**：①§6.1 的 6b1/6b2 API 自动编排不建，6b1/6b2 语义由 skill 通道的 agent 打标 + narratives 等价实现（同 schema 同 gate 同人审页）；②每日 daily 只做确定性 L2，LLM 打标人工触发；③API 通道如未来需要可从 git 历史（5800f27）恢复。

## 2026-08-29（skill 通道首轮真实打标——最近一周 180 事件）

- **通道增强**：inputs.py export 新增 `--start`（available_at 窗口下界，"最近一周"类批量打标）与 `--symbol` 个股过滤；底稿带 `linked_symbols`（已关联池内股，供 narrative 定位）。修 **CLI 提交 bug**：main() 直连库 import 后未 commit，close() 整体回滚——180 条首轮导入全部丢失后补 `conn.commit()`（export 只读不受影响）。
- **打标口径**（180 条 = 公告 167 + 电报 13，窗口 08-22~08-29 available）：程序性公告（董事会/摘要/制度/股东会等）neutral/low 批量规则；实质事件单条判——中报类 medium+schedule+eps+quarter（触发 8/31 底稿复核）、分红 positive/low/sentiment、减值 negative/eps/quarter（豫光/埃斯顿/天赐）、豫光定增受理 neutral/medium、万华装置复产 positive/eps/month、长电控股股东增持完成 positive/sentiment/week、圣农/恒力高管变动 negative/week、套保类 neutral/quarter；电报 13 条按行业/宏观/政策归类，非池内公司不给 narrative。
- **纪律执行**：不猜——中报无数据底稿不给业绩方向（neutral + "以报告原文为准"）；程序性不给 narrative；narratives 只给池内股。
- **gate 分布（已提交）**：company tier=1 needs_review 89（本周新采集）；company tier=NULL ok 78（历史回填通道公告，未分级按 gate 现规则放行——已知点：后续可考虑 NULL 也强制人审）；industry 6 ok + 2 needs_review；macro 2 needs_review（confidence<0.4 自动触发 ✓）+ 1 ok；policy 2 ok。
- **人工复核入口**：/message-review 现列 180 条（公告类 167 条为公司 tier1/未分级，逐一确认工作量较大——如需批量确认按钮说一声）。

## 2026-08-29（续：标签体系中文呈现——可读性整改）

- **问题**：人审页/报告直接暴露英文枚举（neutral / low / C0.60 / target=eps / half_life=quarter / hint=schedule），人工看不懂。
- **整改**：DB 枚举为存储契约不动；新增统一展示映射 `scripts/llm/labels.py`（DIRECTION/MATERIALITY/TARGET/HALF_LIFE/ACTION_HINT/STATUS → 中文），报告"### 消息面"与人审页共用，杜绝散落翻译。
- **渲染示例**：`方向 利空 ｜ 重要性 一般 ｜ 把握 55% ｜ 作用 盈利（EPS 底稿） ｜ 半衰期 季度级 ｜ 流程 触发排期卡复核`；人审表单选项同步中文化（提交值不变）。
- **测试**：473 全绿（断言同步中文化）。

## 2026-08-29（续②：人审页 UI 重设计 spec 评审后修订）

- **对象**：`docs/superpowers/specs/2026-08-29-message-review-ui-redesign.md`（卡片式布局重设计，未进入实现）。
- **修订内容**（对照现状代码核实后补口径，避免实现者自创）：
  1. §4.1 写死实现口径——`direction_cn` 等新增只读字段取值来自 `eff`（effective，与 `tags_cn` 同源）；映射一律走 `labels.cn()` 不用 `dict.get`；字段为 None 不渲染该枚徽章（`confidence` 除外，None 时"把握 —"，对齐 `tags_line`）。
  2. §5 补 `hidden` 优先级：已否决卡片 `data-status="hidden"` 与 `status` 无关。
  3. §2 措辞改为"不改后端契约与既有行为"，消除与 §4.1 例外条款的表面矛盾。
  4. §4.2 补 materiality=low 完整类名；§3 补 `line-clamp-2` 失效时退化不截断（不属验收项）。
  5. §9 明示不修现状超范围问题（`note` action 无输入框）；§8 验收新增 None 徽章与 data-status 断言项（编号顺延至 7 条）。
- **核实结论**：现有测试（test_ui_app.py:84-110）只断言页面含标题与"待人审/已过审"字样，不断言 HTML 结构，重排不碎测试；"已否决/待人审"为模板既有字面量，不违反 labels.py 唯一来源约束。
- **测试**：纯文档改动，未跑 pytest。

## 2026-08-29（续③：message-tag-skill review 整改——skill 与实现对齐）

- **缘起**：review `skills/message-tag-skill/SKILL.md` 发现与实现多处脱节（gate 规则只写一半、confidence 阈值误导、export 两种模式未写、canonical_url 无法查、narrative 校验空转）。
- **代码**（`scripts/llm/inputs.py`）：①export 底稿新增 `canonical_url`（events 表既有列，供查原文；无则 null）；②`import_tags` 的 narratives 补接 `validate_narrative`（schema 早已定义但未调用）——非法叙事（如超 150 字）单条丢弃记 notes，事件标签照常入库，不冒充。
- **SKILL.md 重写**：补全 gate 四条规则（materiality high/critical、confidence<0.4、禁用词、company+tier≤2 按 scope 判定）；铁律 5 改为"confidence 如实给低分"（消除原 ≤0.3 与阈值 0.4 的错位误导）；新增"三种打标场景"（默认/--symbol/--start）与 tags 文件命名约定；四道筛子写自包含判定口径（tier 1-5 含义、target/half_life 各档基准，口径引 r2 §2.1/§2.2）；补导入幂等说明（被拒行改完可直接重导）与好/坏对照示例；narratives 约束 symbol 必须在 linked_symbols 内。
- **测试**：`tests/test_llm_inputs.py` 新增叙事丢弃用例 + canonical_url 断言；**474 全绿**（473+1）。

## 2026-08-29（续④：人审页卡片式 UI 落地——按当日 spec 实现）

- **依据**：`docs/superpowers/specs/2026-08-29-message-review-ui-redesign.md`（评审修订版）。
- **改动**：①`scripts/ui/queries.py` `list_message_review` row dict 新增只读展示字段 `direction_cn / materiality_cn / target_cn / half_life_cn / action_hint_cn / confidence_pct`——取值同 `eff`，映射走 `labels.cn()`，None → 空串（confidence 例外，None 时"把握 —"）；既有字段不动。②`scripts/ui/templates/message_review.html` 整体重写为卡片流：状态点 + 标题 + scope/tier/关联股/event_id + 摘要（line-clamp-2，失效则退化为不截断）+ 徽章行（枚举驱动配色，None/`action_hint=none` 不渲染）+ 理由/预期差/证伪，右侧 260px 操作栏（表单字段名、action 值、POST 路径、hidden symbol 全部原样）；顶部筛选 pills 纯前端过滤（`data-status`，hidden 优先于 status），内联 `<script>` 于 scripts block；已否决整卡 `opacity-50 grayscale`；空态文案保留。
- **验证**：`uv run pytest -q` **474 全绿**；真实库冒烟 `/message-review` 200，180 张卡片渲染、徽章与 spec 示例一致（中性/一般/把握 55%/盈利（EPS 底稿）/季度级/触发排期卡复核）；空库空态文案保留。`labels.py` 未动、无新依赖、无 schema 变更。

## 2026-08-29（续④：message-tag-skill 补"先查证再打标"工序）

- **问题**（用户）：部分消息一眼看不出影响，打标者硬猜出的标签人审同样看不懂。
- **SKILL.md**：铁律新增第 6 条"看不懂先查再打"——陌生公司/冷门政策/专业术语先搜索弄清背景（主业/行业位置/政策指向）再判，查完仍不明 → confidence<0.4 + expectation_gap 写缺口；流程节补 rationale "自解释"要求（背景浓缩进影响路径，人审不查原文也能看懂；查不到写"待人工核"）；事实边界划线：搜索只用于理解背景，事件细节（金额/日期/主体）仍以底稿为准，搜到的数字不当作事件事实入库。
- **测试**：纯文档改动，未跑 pytest。

## 2026-08-29（续⑤：人审页两项增强——关联股带名称 + 一键确认）

- **需求**（用户）：①关联股不只显示代码，要带名称；②页面顶层支持一键确认。
- **改动**：①`queries.py` `list_message_review` 新增 `symbols_label`（"代码 名称"，名称查 watchlist，缺名回退纯代码不猜）；模板关联股行改用它。②`app.py` 新增 `POST /message-review/confirm-all`——复用 `list_message_review` 的 effective 口径，仅对 `needs_review 且未否决` 的事件逐条落 `event_human_review` confirm（actor 空 → "manual"，与单条一致）；模板顶栏右侧加表单（复核人输入 + 按钮带待人审计数 + JS confirm 防误点），空态不渲染。
- **范围说明**：一键确认属用户明确点名的批量操作，突破了当日 spec §9 的"不做批量"边界（spec 为 review 时点的保守范围，以用户后续指令为准）；单条动作的字段/路径契约未动。
- **测试**：`test_ui_app.py` 新增 confirm-all 用例（2 条 needs_review 落 confirm、ok 条不动、按钮计数归零）+ 关联股名称断言；**475 全绿**（474+1）。真实库冒烟：顶栏"一键确认待人审（93）"，关联股渲染"002299.SZ 圣农发展"。

## 2026-08-29（续⑥：人审页公司过滤）

- **需求**（用户）：人审页支持按公司过滤。
- **实现**：纯前端，与状态 pills 组合过滤——卡片加 `data-symbols`（关联股代码逗号串）；顶栏加公司下拉 `#mr-company`，选项为当前列表内出现过的关联股（`app.py` 从 rows 的 symbols/symbols_label 去重排序，不另查库）；JS 改为统一 `apply()`（状态 × 公司双条件），空关联股卡片只在"全部公司"下可见。无后端筛选参数、无新请求。
- **测试**：`test_ui_app.py` 补 `data-symbols` 属性与公司下拉 option 断言；**475 全绿**。真实库冒烟：180 卡均带 `data-symbols`，下拉渲染"000001.SZ 平安银行"等选项。

## 2026-08-29（续⑦：一键确认跟随公司筛选）

- **需求**（用户）：一键确认只作用于当前筛选的公司，而非全局。
- **改动**：①`app.py` `confirm-all` 接受表单 `company` 字段，非空时只确认 `symbols` 含该股的事件（口径复用 `list_message_review` effective）；②模板确认表单加隐藏 `company` 字段，JS 在公司切换时同步该字段与按钮计数（按 DOM 中 `needs_review` 且匹配公司的卡片数实时统计）；确认弹窗文案带公司范围（如"一键确认 002299.SZ 圣农发展 的全部待人审事件？"）。不带 company 时行为同前（全部）。
- **测试**：confirm-all 用例重写——带 company 只落关联该股 1 条、不带 company 落剩余全部、ok 条不受影响；**475 全绿**。
- **事故记录**：冒烟验证时误对真实库 POST 了 `company=002299.SZ` 的 confirm-all，插入 5 条 `actor='test'` 的 confirm 记录；已按 actor 精确 DELETE 清理并复核待人审数恢复 93（与操作前一致）。教训：写操作冒烟只许用临时库。

## 2026-08-29（续⑧：symbol_names 名称目录——池外关联股名称兜底）

- **问题**（用户）：人审页公司筛选下拉中 3 只池外关联股（000776.SZ/300672.SZ/600186.SH）无中文名称。根因：它们是电报事件涉事公司（广发证券/国科微/莲花控股），由打标通道写入 event_symbols，watchlist 无记录，库内无名称表可 fallback。
- **方案**（用户拍板）：建名称目录表。**migration 0006** `symbol_names(symbol PK, name, source, ingested_at)`；采集器 `scripts/collect/symbol_names_collect.py`——东财 push2delay clist 取 f12/f14（与 industry_collect 同源同域，该接口本就返回名称、此前被丢弃），全市场 A 股落盘 CSV + upsert，独立手触发不进 daily。`queries.py` 名称口径改 watchlist ∪ symbol_names（watchlist 优先），缺名仍回退纯代码（不猜）。
- **首采**：5905 只（含北交所），3 只缺名股齐：广发证券/国科微/莲花控股。
- **测试**：`test_db.py` migration 清单同步 0006；`test_ui_app.py` 补 symbol_names 兜底断言；**475 全绿**。`docs/database_schema.md` 补表条目。

## 2026-08-29（续⑨：行业基本面因子接入排期卡框架——skill 规则 + 底稿 factor_snapshot）

- **背景**（用户）：公司主业对行业因子的暴露（油价之于航空、铜价之于矿业）在系统中无表达——macro_factors 池级平铺只喂 LLM 打标底稿，排期卡 EPS 情景无显式因子假设。定位：基本面链条缺口，落点在底稿/skill，不进 signal_facts 确定性信号，LLM 参与边界不扩（skill 仍手触发）。
- **P0（skill 规则，纯文档）**：fred-valuation-card-skill SKILL.md——第 2 步读底稿 factor_snapshot 段；第 4 步周期/成本敏感型公司 EPS 情景必须显式挂因子假设（`earnings_scenarios_json.factor_assumptions`，`{code,name,unit,level,as_of_date,note}`）；第 8 步锚维护加"因子偏离建卡假设 ±alert_threshold_pct → 锚复核提醒"触发器（仅提醒，改卡仍 draft+人工激活）；card-template.md 加"因子假设"小节；cards.py docstring 补字段约定（可选、原样存档、不参与 parse_card 校验与 §5.4b 换算）。
- **P1（底稿第 10 段）**：新增 `config/industry_factors.yaml`（行业 BK 码/个股 → 因子 code，人工维护，symbol 覆盖替换行业，code 交叉校验 macro_factors 清单；首批：BK1479 航空 OIL+USDCNY、BK1615 铜矿 CU0、BK1569/BK1426 炼化化工 OIL、BK1512 生猪 LH0、紫金 CU0+AU0、豫光 AU0）；新增 `scripts/signals/factor_watch.py` 公共查询层（P3 日报提醒复用）；card_inputs.py 底稿九段→十段（schema 升 card_inputs_v2），config_params 增 industry_factors_hash 回声。
- **对齐口径**（factor_watch，§2.1/§3.7）：外盘（GLOBAL）因子对 A 股 as_of 取 `trade_date < as_of`（T-1——外盘当日收盘在 A 股收盘时不存在）；内盘同日；最新读数超 5 个 CN 交易日无更新标 stale；change_20d/60d 按因子自身读数序列、样本不足为 None（§2.5）；比较在来源原生单位上做；连续合约换月跳变 v1 不拼接（吃进变动值，skill 判读注意）。
- **P2（因子清单）**：macro_factors.yaml 每因子加 alert_threshold_pct（⚠️ 人工初值核对期纪律；采集器 `_MACRO_SCHEMA` 同步放行）。可得性探测：生猪 LH0 数据鲜活已入清单并挂 BK1512；动力煤 ZC0 自 2022-12 停摆无流动性——煤炭因子（神华/陕煤）记缺口；航油现货价无接口记缺口。
- **P3（设计冻结，未实现）**：日报"因子偏离"提醒。上线门槛（均满足才动工）：macro_factors 积累 ≥20 个交易日（当前仅 2026-08-28 一天）且至少一张 active 卡带 factor_assumptions。设计：读 active 卡假设 × factor_watch.latest_factor_close 算偏离，超阈值列"锚复核提醒"观察点；不进 signal_facts、不自动生成 draft。
- **测试**：新增 tests/test_factor_watch.py 10 项（T-1/同日边界、missing、stale、change 窗口样本不足、symbol 覆盖优先级、配置交叉校验）；test_card_inputs.py 十段结构与 v2 断言同步；**485 全绿**（475+10）。手跑 `card_inputs 600029.SH`：factor_snapshot 正确——USDCNY 678.11 同日可用；OIL 唯一读数即当日被 T-1 规则排除 → status=missing（§2.5 不猜，符合设计）。
- **文档**：system_design.md §3.6 补因子暴露映射与对齐口径、宏观因子行补消费方，§5.6 补 factor_assumptions 字段约定。

## 2026-08-29（续⑩：排期卡展示层重做——中文化 + 对齐现行卡语义）

- **审计结论**（用户提问触发）：卡片页三处漂移——①波段箱体已事实废弃（新卡 swing_box 全空）但 UI 残留箱体行；②胜率-赔率-Kelly 框架（input_snapshot.win_rate_estimate，含 tier_ranges/kelly_caps/recovery_target/合成说明）是现行决策核心但 UI 完全不显示；③eps_scenario_detail/earnings_basis/review_triggers/right_side_notes/regime 等中文研判埋在"查看完整 JSON"裸 dump 里。另有英文残留：/cards 表头 card_version_id、状态复选框与徽章英文、stock.js 面板 status 直出。
- **改动**（用户拍板三处一起改）：
  - `queries.py`：新增 `CARD_STATUS_CN`（active 生效中/draft 草稿/superseded 已替代/rejected 已否决/suspended 已暂停；DB 枚举不动）；`list_cards` 每项与 `get_card_detail` 补 `status_cn`，detail 另补 `name`（LEFT JOIN watchlist）。
  - 新增 `static/js/card_detail.js` **共享渲染器**（/cards 详情弹窗与单股页排期卡面板同口径）：分区流式——三档价区表（+胜率区间/Kelly 上限两列 + matrix_source 注）、交易框架（证伪线整行段落；swing_box 空→只显示"波段箱体：波段仓不适用…"一句；右侧预案+right_side_notes）、胜率与赔率（合成说明+修复目标价）、情景假设（锚回退逻辑保留 + eps_scenario_detail 逐情景假设 + 恐慌底刻度逐条带 note + 样本窗口 + regime 整段）、盈利底稿、复核触发；原始 JSON 收进末尾 `<details>` 折叠区。
  - `common.js` `renderStatusBadge(status, text)` 加可选中文显示文本（配色仍由枚举驱动，其它页面行为不变）。
  - /cards 列表：表头中文化、删"替代"列（版本链移至详情）、股票列带名称、状态筛选复选框中文、colspan 9→8；详情弹窗接共享渲染器（showChain）。
  - 单股页：排期卡面板整体接共享渲染器（删本地 kv/tierImplied/JSON 弹窗按钮，showModal 仍服务信号详情）；头部"无 active 卡片"→"无生效中的排期卡"；"箱体"数字卡在 swing_box 为空时显示"不适用"。
- **验证**：`uv run pytest -q` **485 全绿**；node --check 四个 JS 全过；node 桩渲染真实卡验证——新卡（300760）关键区块全在位、无英文/空箱体残留，老卡（603605 两版，无 win_rate/anchor 结构化字段）回退分支正常；页面冒烟 /cards、/stock/300760.SZ 200。

## 2026-08-29（续⑪：单股页两个 UX 修补——一致预期出框 + 信号状态中文）

- **券商一致预期出框**：`.num-card .value` 是 `white-space:nowrap`，fd-fc 文本 "FY1 87.9 / FY2 99.67 / FY3 112.26 亿" 溢出卡片。修：app.css 加 `.num-card .value.long`（允许换行；Tailwind 工具类优先级压不过既有 CSS，故走修饰类）；fd-fc 文案缩短为 `87.9 / 99.7 / 112.3 亿`，FY 说明移入 sub 行。
- **信号中文化**：状态枚举的中文映射早已在 `queries.py`（STATE_TEXT/BOX_STATE_TEXT/ACCUMULATION_STATE_TEXT，`_state_text()`），前端一直没消费。修：stock.js 信号现状徽章用 `state_text`；`list_signals`/`get_signal_details` 补 `signal_name`/`state_text` 只读字段；/signals 页列表徽章、类型/状态筛选复选框（value 仍为英文枚举，label 中文）、详情弹窗标题全部中文化；stock.js 信号详情弹窗标题同步。
- **测试**：485 全绿；node --check stock.js/signals.js 过；API 冒烟 accumulation→吸筹形态 / idle→无形态 正确。

## 2026-08-29（续⑫：卡片关联因子现状上页面 + 豆粕补因子缺口）

- **需求**（用户）：卡片应展示当前股票关联的因子状态（油价/大豆价等）与宏观指数（CPI 等）。
- **UI 落地**：`get_stock_overview` 新增 `factor_snapshot`（复用 `factor_watch.snapshot_for_symbol`，as_of=该股最新交易日，industry_code 取 watchlist；配置错误捕获后如实标注不 500）；stock.js 排期卡面板底部新增"关联因子现状"区块——因子名/代码/上行利好利空（红/绿）/最新读数+单位+日期（stale 标"陈旧"、missing 标"缺数据"）/近 20、60 读数变动（样本不足显示 —）/映射 note；无卡股票同样显示该区块。
- **豆粕补缺口**：`macro_factors.yaml` 新增 M0 豆粕（domestic 连续合约，alert_threshold_pct 0.12 为人工初值⚠️核对期）；`industry_factors.yaml` 填上原"禽料成本"缺口——BK1511 圣农 → M0 negative 饲料成本（玉米 C0 未纳入，注释说明）。手采+ingest 13 因子（M0 3340.0、LH0 12055.0 首入）。
- **现实约束（如实告知用户）**：①macro_factors 数据 2026-08-28 才起采，仅 1 个交易日——20/60 读数变动全部样本不足显示 —，外盘因子按 T-1 口径首日必 missing（如南航 OIL），随积累自愈；②**CPI 等月度宏观指数当前无数据源**（现管线为日频商品/外汇），接入需新采集域（akshare 宏观库/统计局），待用户拍板后单独立项。
- **测试**：`test_ui_queries.py` 新增 overview factor_snapshot 断言（无映射如实标注）；**486 全绿**。真实库冒烟：圣农 M0 豆粕 3340.0 元/吨 ok；南航 USDCNY ok、OIL missing（T-1 口径符合设计）。

## 2026-08-29（续⑬：关联因子区块移至单股页顶部）

- **需求**（用户）：关联因子现状从"当前排期卡"面板底部移到页面顶部。
- **改动**（纯展示层）：stock.html 在头部数字卡之后、基本面之前新增独立 section（标题"关联因子" + `factor-asof` 截至日期 + `factor-body` 容器）；stock.js `renderFactors` 改为直接写该容器（as_of 移到标题旁），不再由 `renderCardPanel` 追加；`loadAll` 中独立调用。渲染内容与口径不变（缺数据/陈旧如实标注）。
- **验证**：486 全绿；服务重启后 /stock/002299.SZ 含新区块、overview 的 factor_snapshot 正常（M0 ok，as_of 2026-08-28）。

## 2026-08-29（续⑭：入池油气三巨头+电信双龙头，watchlist 30 只，五卡 draft 待激活）

- **流程**（复用六源先例）：watchlist.yaml 加 5 行（601857.SH 中国石油/600028.SH 中国石化 BK1569 炼油化工、600938.SH 中国海油 BK1574 油气开采Ⅲ、600941.SH 中国移动/601728.SH 中国电信 BK1587 电信运营商，行业码经 symbol_industry 表反查）→ `db seed` upsert（active=30）→ akshare 六源采集（`--start 2023-08-09 --end 2026-08-28 --price-api sina --run-id run_ot_0829`，price/forward/financials/announcement/forecast/stock_info 全 ok）→ stock_info 先移出 → daily 首跑 `--date 2026-08-28 --raw-dir data/raw/akshare` **ok=30** → stock_info 回补 ingest（5 行 inserted）→ indicators 重算（各 pe_ttm 741/741 非空）。
- **数据覆盖**：5 只各 741 bars（2023-08-09..2026-08-28）/ 周线 157 / 信号 1528 行；财报全历史 84/104/25/36/76 期且 published_at 全非空；公告 260/557/219/268/297 条；股本快照 group_total 各 1 行。pe_ttm 最新：中石油 12.99 / 中石化 18.36 / 海油 11.74 / 移动 16.10 / 电信 19.37。
- **缺口**：①forecast 海油/移动 EMPTY（同花顺接口，同美的先例）；②OIL 因子读数 missing（macro_factors 08-28 起数，对齐窗口不足——随积累自愈）；③BK1574/BK1587 无 industry_factors 映射（海油对油价正敏感，人工补配候选）；④4 只衰竭锚滞后（中石油/中石化/海油锚 as_of 2026-01-30，电信 as_of 2025-02-14 滞后 18 个月）——2026-06-29~07-02 市场普跌新低（8.59/4.43/26.40/85.38/5.15）均未入锚，等系统以新下跌段重算，卡内 front_low_raw 以人工记录为准；⑤中石化/移动/电信 PB-股息率锚核对=激活前人工项（系统无分红/净资产序列，同陕煤/神华/长电缺口）。
- **五张排期卡 draft 入库（draft-only，待人工激活）**：**601857SH_6ec22096** / **600028SH_60fd1681** / **600938SH_f51971c7** / **600941SH_0f926a39** / **601728SH_2cf61fc0**，均 next_review 2026-10-31；draft md 存 `cards/{symbol}/{股票名}估值排期卡_draft_2026-08-29.md`。共同背景：五只均于 2026-06-29~07-02 见底后反弹 15–31% 至 3 个月高位，现价普遍未定价坏消息，左侧排期整体在深下方。
- **要点**：中石油（11.23）锚 pe_scale 体系上移后回落（刻度 8.00→9.87→12.02→9.94），悲观 PE 10 取 2026-07 最近刻度，三档 9.60–10.00/8.50–9.00/7.00–7.60，证伪 7.00，裂口 -15.4pp（FY1 +17.3% vs Q1 +1.9%），胜率 45-55/55-65/60-70，Kelly 0/7.2%/14.4%。中石化（5.46）PE 被动抬高失真（TTM 刻度 10.2→21.4），PE 情景改 FY2026E 口径 17/15/13，H1 +19.3% 修复确认但 FY1 裂口 -21.4pp，现价超矩阵顶端（5.27）非左侧买点，三档 4.50–4.70/4.00–4.20/3.20–3.50，证伪 3.20，胜率 25-45/35-55/40-60，Kelly 0/0/9.0%。海油（34.19）锚 pe_scale（主带 8–9.2，2026-07 刻度 10.08），H1 +23.4% 改善确认，三档 33.10–34.50/29.50–31.10/24.00–25.90，证伪 24.00，现价落 T1 内但 Kelly=0 仅赔率注，胜率 55-65/65-75/70-75 为五只最高，Kelly 0/10.9%/17.0%。移动（97.86）类债温和降档（15–16.8→14），H1 -6.3% 转弱，锚 as_of 2026-06-26 为五只唯一新锚，三档 89.30–93.00/79.50–83.70/67.20–72.60，证伪 67.20，胜率 35-50/45-60/50-65，Kelly 0/1.5%/11.5%。电信（6.30）H1 -14.9% 连续两季双位数下滑（恶化中），现价超矩阵顶端 13%（PE p75+盈利下滑最差组合），三档 4.80–5.00/4.30–4.50/3.60–3.90，证伪 3.60，胜率 20-35/30-45/35-50，Kelly 0/0/7.4%。
- **踩坑**：卡片 JSON schema 根字段无 `earnings_scenarios`（additionalProperties:false）——`factor_assumptions` 须放 `earnings` 对象内（入库映射 earnings_scenarios_json）；SKILL.md 第 9 节示例写法（根级 earnings_scenarios）与实现不一致，已就地修正 draft，skill 文档待改。
- **测试**：`uv run pytest -q` **486 全绿**（先清 __pycache__）。

## 2026-08-29（续⑮：五卡激活，全池 active 30 张）

- 用户指令「激活这些卡片」即人工确认：601857SH_6ec22096 / 600028SH_60fd1681 / 600938SH_f51971c7 / 600941SH_0f926a39 / 601728SH_2cf61fc0 全部 **activate --effective-from 2026-08-31（周一）**，next_review 2026-10-31；激活归档 `cards/{symbol}/2026-08-31_*.md` + current.md 刷新（CLI 自动生成）。全池 active 25→30。08-31 盘后 daily 起五只卡片相关信号（tier_proximity/tier_triggered/证伪线监测）开始产出。激活前人工项（股息率/PB 锚核对：中石化/移动/电信）未做，保留为持卡观察项（同神华/长电先例在复核日历中跟踪）。

## 2026-08-30（估值排期卡 skill 吸收 stock-entry-decision 两点：卡复用检查 + 行动纲领）

- **来源**：用户微信收到的 `stock-entry-decision` SKILL.md（估值排期 + 消息研判编排层 skill）。评估结论：两个子流程本系统已有对应物（fred-valuation-card-skill / message-tag-skill + r2 设计 §7），缺的是编排层；按用户拍板走轻量路线，只吸收两点，不新建编排 skill（等消息面 r2 人审流程跑顺后再议）。
- **改动**（`skills/fred-valuation-card-skill/SKILL.md`，纯文档）：
  1. 新增 **### 0. 排期卡复用检查**：已有卡且锚未过期（未跨新财报/证伪线未触发/未到 next_review）→ 复用并刷新四项（现价落位、信号状态重核、箱体与右侧关键位有效性、锚维护日历到期事项先调档线）；锚过期禁止复用、禁止用过期锚出结论。
  2. 第 9 步新增 **「行动纲领」对话开头格式**：钱 × 价位 × 动作大白话、禁术语、必说中间地带，按落位给五种句式骨架（未到位/左侧+波段/第 N 档执行/右侧待命/证伪触发）；"语气与边界"规则语言条款同步加例外。
- **验证**：纯 skill 文档改动，无代码；未跑测试（无测试覆盖该文件）。

## 2026-08-30（续：右侧状态机持仓跟踪段 + as_of 卡片解析口径统一——恒力止损复盘落地）

- **背景**：恒力 600346 止损复盘（前节）发现两个系统级问题：①right_side confirmed 后直接回 idle，持仓期（8-21~8-27）零落行，8-25/26 连续两日收破 hold 线无任何事实行与日报决策点，人工止损时系统沉默；②8-28 新旧卡交替空档（旧卡 superseded 窗口仍覆盖、新卡 effective 08-31）下 right_side 报 no_active_card、daily_watch 报 card_not_effective_at_as_of，而 falsification_breach 等仍按旧卡正常计算——同日同股两种答案。
- **right_side 加持仓跟踪段（signals_v2）**：`evaluate_segment` 增 stop 参数（卡 right_side_trigger.stop_level），confirmed 后→**holding** 逐日落行（triggered=0，details 含 stop_level/距止损 distance_to_stop_pct/confirmed_on/days_since_confirm）；收盘 ≤ stop_level（≤ 锁定、不再加容差、单日即触发）→ **stopped_out**（triggered=1）回 idle。卡无 stop_level → confirmed 后直接回 idle + details 记 tracking=no_stop_level + result notes 提示（§2.5 无线不猜）。STATES 增 holding/stopped_out。
- **as_of 卡片解析统一为窗口语义（§5.1）**：daily_watch/right_side/report 三处的 as_of 生效卡判定从 `load_active_card`（status+窗口双过滤）改为 `card_for_day`（纯窗口，含 superseded 仍覆盖的版本）；cards.py 文档锁定分工——load_active_card 只留 execution 关联快照/card_inputs 底稿等"当下活跃卡"语境。报告"当前卡片"行对非 active 状态如实标注（"status=superseded，生效区间仍覆盖当日"）。
- **报告层**：stopped_out 当日进决策点并升 **P2**（证伪族，文案含收盘/止损位/确认日）；holding 时观察点段显示止损位与距离（替代突破线距离）；新增**执行闭环提醒** `_right_side_followups`——右侧 confirmed/stopped_out 后 10 个日历日内无 executions 记录 → 决策点段列"[右侧确认/止损待执行]"并升 P4，人工录入后消失（档位/箱体是持续状态不提醒，避免长驻噪音）。
- **UI**：queries.py STATE_TEXT 与右侧汇总映射补 holding=持仓跟踪中/stopped_out=止损触发。RULE_VERSION 升 signals_v2。
- **真实库验证**：重跑 600346.SH/603605.SH right_side——恒力 8-21~8-28 补齐 6 行 holding（距止损 10.2%~18.4%，8-25/26 破 hold 线但远未触 16.60 止损的事实清晰可见）、8-28 空档误报消除；珀莱雅 confirmed 8-27 → 8-28 holding（止损 59.50，距 3.1%）。重跑 daily_watch 600346.SH 恢复 status=ok。8-28 报告 revision=7 重跑：珀莱雅日报 P4 出现「右侧确认待执行 2026-08-27 触发至今无执行记录」——正是复盘中发现的漏执行；恒力 8-20 confirmed 因 8-21 有执行记录（#8）不出提醒，口径正确。
- **测试**：test_signals_daily.py 右侧面全部改三元组返回 + 新增 4 项（holding 跟踪与 ≤ 边界/stopped_out 集成/空档窗口语义/daily_watch 空档）；test_report.py 新增 3 项（stopped_out P2 决策点/待执行提醒出现与消失/holding 观察点）；rule_version 断言改引 RULE_VERSION。**493 全绿**（486+7）。
- **文档同步**：system_design §5.1（窗口语义条款）/§5.4（状态机图+落行语义+P2/提醒）/§6.2/§6.3；database_schema signal_facts 表 right_side 行；handoff 常见任务增"录入执行前先跑当日 daily"（快照不得早于 T-1）；估值 skill 第 7 步锁定"右侧仓止损=stop_level 单一来源，禁止第二止损线"。
- **遗留（人工项，agent 不代做）**：恒力复核初稿 600346SH_0f65e846 仍 draft，待人工 reject；珀莱雅右侧确认仓是否执行由用户决策。

## 2026-08-30（续②：入池创新药双龙头+CXO 龙头，watchlist 33 只）

- **需求**（用户）：百济神州 688235.SH / 恒瑞医药 600276.SH / 药明康德 603259.SH 加入 watchlist 并采集数据。
- **流程**（复用六源先例）：watchlist.yaml 加 3 行（行业码经 symbol_industry 表反查：百济/恒瑞 BK1594 化学制剂、药明 BK1600 医疗研发外包）→ `db seed` upsert（33 只）→ akshare 五源采集（`--end 2026-08-30 --run-id run_pharma`）：price/forward 东财 push2his 代理断连 3/3 失败（已知坑第三次复现），改 `--price-api sina --run-id run_pharma_sina` 重采全 ok；financials 28/105/41 期（三股 20260630 中报均在）；announcement 471/798/573 条；forecast 恒瑞 ok、**百济/药明 EMPTY**（同花顺接口，同美的/海油/移动先例，留缺口）。→ daily 首跑 `--date 2026-08-28 --raw-dir data/raw/akshare` **ok=33**（无卡期 daily_watch/right_side incomplete=no_active_card 属预期）→ stock_info 回补采集+ingest（3 行 inserted）→ indicators 逐股重算（各 pe_ttm 740/740 非空）。
- **数据覆盖**：3 只各 740 bars（2023-08-10..2026-08-28，sina 源）/ 周线 157 / 公告簿 471/798/573 条 / 股本快照 group_total 各 1 行。pe_ttm 最新（08-28，snapshot_share_basis）：**百济 97.87**（中报入库后 TTM 盈利上修，前一日口径 136.41）/ 恒瑞 40.65 / 药明 21.77。
- **缺口**：①forecast 百济/药明 EMPTY；②三股均无排期卡，日报 degraded(no_active_card) 属预期。
- **测试**：`uv run pytest -q` **493 全绿**。

## 2026-08-30（续③：药明康德 603259.SH 估值排期卡 draft 603259SH_9681230e）

- **流程**（fred-valuation-card-skill 第 0–9 步）：复用检查 list=0 张 → 底稿 `card_inputs`（→ cards/603259.SH/inputs_2026-08-28.json；行情 08-28/财报 06-30/forecast EMPTY）→ 盈利底稿（TTM 归母 216.70 亿/EPS 7.2296，2026H1 +29.4%）→ 刻度判断：3 年样本 2023-08-10~2026-08-28 共 11 恐慌低点，逐轮下移 24.9→14.9（2023→2024 体系降档），最近两轮 15.2/14.9 收敛→锚设新体系（悲观 PE 12.5=14.9×0.85/中性 15/乐观 18）→ 矩阵 `build_schedule.py --eps 7.6,6.85,6.1 --pe 18,15,12.5 --price 157.4 --winrate 50,70,75` → T1 109.44–114.00 / T2 97.61–102.75 / T3 76.25–82.35，证伪线 76.25 → 胜率 T1 50–60/T2 70–75/T3 75–80，Kelly 上限 0.0%/12.6%/18.4% → 现价 157.40 高于 T1 上沿 38.1%（≈中性EPS×PE 20.7，超出乐观情景 136.8，三档均未到位）。
- **波段/右侧**：波段不适用（06 低 88.32→08-20 高 173.60 单边趋势段）；右侧触发 175.34（=173.60×1.01）/止损 171.85（=173.60×0.99 唯一止损口径）。
- **产物**：Markdown `cards/603259.SH/药明康德估值排期卡_draft_2026-08-30.md` + JSON `cards/603259.SH/draft_2026-08-30.json` → `card create-draft` 入库 **draft 603259SH_9681230e**（next_review 2026-10-31；draft-only，激活待人工）。
- **缺口**：forecast EMPTY（裂口检查缺数据，卡中标注不编造）；无行业因子映射（省略 factor_assumptions）；衰竭量能阈值锚定 2024 年下跌段（as_of 2024-09-27），相对现价结构偏旧，新下跌段起系统重算。
- **测试**：无代码改动，未跑 pytest；create-draft schema+语义校验通过（含 valuation.anchor/sample_window、价格定点十进制）。

## 2026-08-30（续④：恒瑞医药 600276.SH 估值排期卡 draft 600276SH_438f17da）

- **流程**（fred-valuation-card-skill 第 0–9 步）：复用检查 list=0 张 → 底稿 `card_inputs`（→ cards/600276.SH/inputs_2026-08-28.json；行情 08-28/财报 06-30/forecast akshare 快照 08-30 已入库）→ 盈利底稿（TTM 归母 77.26 亿/EPS 1.1641；2026H1 营收 154.56 亿 −1.9%、归母 44.65 亿 +0.3%，Q1 +21.8%→H1 持平，Q2 单季走弱；BD 首付款高基数为用户提示非库内事实）→ 刻度判断：3 年样本 2023-08-10~2026-08-28 共 10 恐慌低点（8 个 is_fallback），逐轮下移 66.6→52.0/50.5→43.3，现价 PE(TTM) 40.65 已破 p5(42.80) 与最近底部刻度 → 体系切换中，锚设新体系（悲观 PE 38=43.3×0.88/中性 47=最近两轮均值/乐观 52=2025 低点簇上沿）→ 矩阵 `build_schedule.py --eps 1.16,1.04,0.93 --pe 52,47,38 --price 47.32 --winrate 35,45,50` → T1 52.34–54.52 / T2 46.44–48.88 / T3 35.34–38.17，证伪线 35.34 → 胜率 T1 35–45/T2 45–55/T3 50–60（裂口未收敛 −5~−10：券商 FY1 93.42 亿隐含 +21.2% vs 实际 +0.3%；体系切换 −10；信号仅 1 项 duration），Kelly 上限 0.0%/0.0%/11.8% → 现价 47.32 落 T2 价区但信号不足（1<2）+Kelly 下沿为 0 → 不动。
- **波段/右侧**：波段不适用（08-20 放量 2.39 亿股跌破 52–55 平台，08-25 阶段新低 46.22，下跌段非震荡结构）；右侧触发 52.00（收复破位平台下沿）/止损 50.90（回踩确认区下沿失守，唯一止损口径）。
- **产物**：Markdown `cards/600276.SH/恒瑞医药估值排期卡_draft_2026-08-30.md` + JSON `cards/600276.SH/draft_2026-08-30.json` → `card create-draft` 入库 **draft 600276SH_438f17da**（next_review 2026-10-31；draft-only，激活待人工）。
- **缺口**：无行业因子映射（省略 factor_assumptions）；forecast yoy_pct.fy1 与 FY1–FY3 营收字段为空（仅净利序列，FY1 隐含增速为库内数字直算）；底稿无现金流量表（生意质地源按 0）；前低 48.43 已被现价跌破（no_new_low=new_low），衰竭锚待系统随新下跌段重建。
- **测试**：无代码改动，未跑 pytest；create-draft schema+语义校验通过（含 valuation.anchor/sample_window、价格定点十进制）。

## 2026-08-30（续⑤：百济神州 688235.SH 估值排期卡 draft 688235SH_f820a833）

- **流程**（fred-valuation-card-skill 第 0–9 步）：复用检查 list=0 张 → 底稿 `card_inputs`（→ cards/688235.SH/inputs_2026-08-28.json；行情 08-28/财报 06-30/forecast EMPTY）→ 盈利底稿（TTM 归母 42.82 亿/EPS 2.7761；2026H1 营收 222.20 亿 +26.8%、归母 32.71 亿 +627.1%，Q2 单季 16.63 亿 +205%；2025A 扭亏 14.61 亿）→ 刻度判断：PE(TTM) 刻度样本仅 93 天（2026-04-16 TTM 扭亏起，§3.2 强制标注），带 PE 恐慌底仅 1 轮（2026-06-29 @124.78），盈利爆发期刻度被动下移；亏损期底部为价格底带逐轮上移 98.5→227.75 → 锚定 mixed（情景 EPS×远期 PE 折回 + 价格底带辅助），PE 按前瞻口径体系带给（悲观 60=恐慌底 227.75 对中性前瞻 EPS 折算 58.6 取整/中性 75/乐观 95），禁止 TTM 刻度乘前瞻 EPS（双重计价）→ 矩阵 `build_schedule.py --eps 3.89,3.50,3.11 --pe 95,75,60 --price 271.7 --winrate 50,60,65` → T1 280.08–291.75 / T2 249.38–262.50 / T3 186.60–201.53，证伪线 186.60 → 胜率 T1 50–65/T2 60–75/T3 65–80（盈利轨迹 +5~+10、裂口缺数据 +0、体系样本不足 −10~−5、信号 T1+0/后两档 +10），Kelly 上限 0.0%/8.9%/15.9% → 现价 271.70 低于 T1 下沿 3.0%、高于 T2 上沿 3.5%（已掠过 T1，但 T1 Kelly=0 非正期望注，预算并入 T2 等价位+信号）。
- **波段/右侧**：波段不适用（2026-01-09 312.99 下跌段→06-29 227.75 恐慌底后修复段，底稿无可确认箱体）；右侧触发 281.70（=2025-09-11 前低 278.90×1.01）/止损 278.90（突破失败位，唯一止损口径）。
- **产物**：Markdown `cards/688235.SH/百济神州估值排期卡_draft_2026-08-30.md` + JSON `cards/688235.SH/draft_2026-08-30.json` → `card create-draft` 入库 **draft 688235SH_f820a833**（next_review 2026-10-31；draft-only，激活待人工）。
- **缺口**：forecast EMPTY（裂口检查缺数据，卡中标注不编造）；无行业因子映射（创新药，省略 factor_assumptions）；PE 刻度样本仅 93 天（锚置信度低，体系稳定性扣分来源）；现金流/毛利率未入底稿（生意质地按 0）；衰竭活跃 2 项均为持续型且现价已高于前低 19.3%，信号时效性激活前需人工核对。
- **测试**：无代码改动，未跑 pytest；create-draft schema+语义校验通过（含 valuation.anchor=mixed/sample_window、价格定点十进制）。

## 2026-08-31（代码/skill 审查修复：`docs/superpowers/specs/2026-08-31-code-skill-review-handoff.md` 10 项代码 + 3 项 skill 文档）

> 背景：承接 code/skill 审查交接文档，修复 10 项已确认代码缺陷并同步 3 处 skill 文档滞后。测试 500 全绿（+7：1.1/1.5/1.6/1.7/1.8 新增 6 项 + 1.2 更新断言 1 处）。

### A. 右侧状态机（1.1 + 1.2）
- **1.1** `scripts/signals/right_side.py:181`：holding 分支由裸 `else` 改 `elif state == "holding"`，末尾补 `else: raise ValueError`——非法状态不再被静默当 holding 吞掉（当前合法路径不可达，属结构性护栏）。新增 `test_right_side_states_always_reset_not_silently_held`：stopped_out 后必须回 idle 才能开新一轮 episode。
- **1.2** `scripts/pipeline/report.py:316`：right_side 决策点分支增 `holding`（observed_on==trade_date）→ 输出 `[右侧持仓跟踪] 止损位…，现价距止损…%，已跟踪…日`。holding 是 triggered=0 逐日行，原实现多日持仓期日报 P2/P3/P4 静默；优先级仍落 P5（状态行不升级）。`test_right_side_holding_observation` 增决策点断言。

### B. 因子层（1.4 + 1.5 + 1.6）
- **1.4** `scripts/signals/factor_watch.py:latest_factor_close`：f-string 嵌 `op`（`<`/`<=`）拆成 GLOBAL/CN 两个参数化查询分支（SQLite 不支持操作符参数化）。
- **1.5** 原逐因子 `_factor_market` N+1 查询 → 新增 `_factor_markets` 单次 `IN + GROUP BY` 批量取 market；`snapshot_for_symbol` 复用，factor 条目新增 `market` 字段。测试 `test_snapshot_market_lookup_is_batched`：4 因子仅 1 次批量 market 查询。
- **1.6** `load_industry_factors` 模块级 `_INDUSTRY_CACHE`（按 path+双文件 mtime 命中），单股页请求不再重复解析 YAML。测试 `test_load_industry_factors_caches_by_mtime`：同 mtime 返回同一对象、改动后重载。

### C. 人审页（1.3 + 1.7 + 1.9）
- **1.3** `scripts/ui/queries.py:list_message_review`：`symbol_names` 表查询前用新增 `_table_exists` 守卫——0006 未迁移时降级为只显示代码，不 500。测试 `test_list_message_review_without_symbol_names_table`（同时锁定 watchlist 覆盖优先生效）。
- **1.7** `confirm-all`：新增 `queries.list_message_review_for_confirm(conn, company)`，SQL 侧过滤（status=needs_review 且事件级无 confirm/dismiss 记录 + company 关联 EXISTS），替代全量 `list_message_review` + Python 过滤；`app.py` 直接消费。测试 `test_message_review_confirm_all_skips_dismissed`：已否决事件不被一键确认。副作用修正：原实现受 `list_message_review` LIMIT 200 限制，现处理全部目标行。
- **1.9** `message_review.html`：`data-symbols` 分隔符逗号改 `|`（防公司名/字段含逗号误切），JS `split('|')`。`test_message_review_page_and_action` 断言同步。

### D. 报告 followup（1.8）
- **1.8** `scripts/pipeline/report.py:_right_side_followups`：逐事件 `SELECT COUNT(*) FROM executions` N+1 → 单条 `LEFT JOIN executions ... GROUP BY observed_on, state` 聚合。测试 `test_right_side_followup_single_join_query`：两事件仅 1 次 executions 查询。

### E. 前端（1.10）
- **1.10** `card_detail.js`：`anchorIsPe` 由二值误判改三态分支——true=纯 PE 锚 / false=非锚仅分位参考 / null（旧卡无结构化 anchor）=「旧卡未结构化锚，仅分位参考」，不再把 null 静默渲染成"PE 三情景"。`node --check` 通过；浏览器侧需人工验收（旧卡 `/stock/{symbol}` 标签文案）。

### F. Skill 文档同步（2.1–2.4）
- **2.1** `skills/fred-valuation-card-skill/SKILL.md` §9：JSON 键名规范改为字段一览表（draft 根字段→入库列），`sample_window` 明确在 `valuation` 段（非 `earnings`），`factor_assumptions` 与 `eps` 同级放 `earnings` 内、根级 additionalProperties=false 措辞修正。
- **2.2** §7 末尾补 holding/stopped_out 状态机段（确认后逐日跟踪、收盘 ≤ stop_level 触发 stopped_out 回 idle、无 stop_level confirmed 直接回 idle、holding 期日报不静默）。
- **2.3** §4 因子假设锚点明确：以 `card_inputs_v2` 底稿 `factor_snapshot` 当期读数（close+trade_date）为锚，不自行外采，stale/missing 如实标注不编造。
- **2.4** `skills/message-tag-skill/SKILL.md` §6 补 rationale 自解释模板示例（人审不查原文也能看懂影响路径）。

### 验收
- `uv run pytest -q` **500 全绿**（474 → 500）；`node --check` 通过；模块导入正常。
- 待人工验收（浏览器/CLI）：/message-review 公司筛选；旧卡 anchor 标签文案；holding 股报告决策点输出。

## 2026-08-31（盘后 daily：ok=33 + 修复 origin 窗口回滚；12 张到期卡批量审核）

### 采集（本轮网络持续抖动，sina/cninfo/emweb 反复 SSL EOF/502，多轮定点重试）

- **price/forward 33/33 齐**：sina 源，显式 `--end 2026-08-31`，run-id `run_daily_20260831`（price/forward 同目录落盘）。sina SSL EOF 约 40–60% 失败率，靠幂等重试多轮补齐（price 缺 7 只→3 只→0；forward 缺 14→9→4→0）。em 源本轮全程不可用（push2his/emweb SSL/Proxy EOF）。
- **⚠️ 新坑（origin 窗口）**：`akshare_collect --start` 默认硬编码 `2023-08-10`。池内 8/10 前建仓的老批次 6 只（600028/600531/601318/601728/601857/603697）库内因子 origin=2023-08-09，今日 sina forward 序列从 08-10 起 → 因子平台切换检测触发重建 → origin 日不在新序列 → `ValueError: origin 日 2023-08-09 不在因子序列内（该股当日全部阶段回滚）`，daily 首跑 **failed=6, ok=27**。修复：6 只 forward 显式 `--start 2023-08-01 --run-id run_daily_20260831_d` 重采（含 2023-08-09）→ 二跑 **ok=33**。**教训：每日 forward 采集建议显式 `--start 2023-08-01`，防 origin 早于默认窗口**。
- **index/telegraph/flow ok**；**macro 11/13**：AU0/CU0/I0/RB0/SR0/LH0/M0/SC0/OIL/CL/HKDCNY 齐（跨 run_daily_20260831_b/c 并集），USDCNY/EURCNY（中行牌价接口）今日缺，留缺口。
- **announcement/financials 大面积抽风**：cninfo JSONDecodeError（已知 502 自愈型，本轮持续数小时仅零星自愈窗口）、emweb 33/33 挂。自愈窗口抢到：**600900 长电 + 601857 中石油今日披露的 2026 半年报公告与财报数字已采集入库**（run_daily_20260831_f/g），其余 31 只 08-29~08-31 三日公告增量缺失。**财报面无缺口**：池内 33 只 2026-06-30 中报全部在库（核实过）。公告缺口明日例行幂等回补（采集窗口全历史覆盖）。
- **ingest**：`data/raw/akshare` 全目录，inserted=15（长电/中石油公告+财报、今日 macro/index/flow/telegraph），symbol_names 路由 error=1（历史已知非管线文件，不属本批）。

### daily（revision=4，ok=33）

- 首跑 failed=6（origin 坑，见上）→ 二跑 ok=33（revision=2）→ ingest 长电/中石油 → 三跑 **ok=33（revision=4）**，折入今日公告与两家半年报。珀莱雅报告 P3、南航 P3（T1 区内）、天赐/有友卡片信号已触发（详见下节审核）。`uv run pytest` 未跑（无代码改动）。

### 12 张到期排期卡批量审核（next_review=2026-08-31，8/10–8/14 建卡批次）

- **流程**：12 只逐只 `card_inputs` 新底稿（inputs_2026-08-31.json）→ 对照卡内 EPS 情景/档价/证伪线/信号事实/右侧状态。中报预期差用 2026H1−Q1=Q2 单季 + 2025H1 同比。
- **结论：11 张锚过期（卡生效后新披露 2026 中报），按 §纪律 禁止复用、禁止用过期锚出结论，需走完整重建（新 draft + 人工激活）；1 张复核通过**：

| 优先 | 股 | 中报(披露) | H1 同比 | 关键变化 | 现价 vs 档价 | 审核判定 |
|---|---|---|---|---|---|---|
| 1 | 603605 珀莱雅 | 08-24 | **+46.3%** | TTM EPS 4.72 vs 卡 base 3.56 超预期；**右侧 holding 第 3 日，收盘 60.33 已破 hold 线 60.39，距止损 59.50 仅 1.4%** | 距 T1 下沿 61.50 −1.9%（proximity 触发） | 锚过期→重建；**右侧止损决策点，人工优先处理** |
| 2 | 600029 南航 | 08-28 | 减亏但仍亏 | Q2 单季 −51.8 亿；base EPS 0.05 失效 | **T1 区内 5.01，dry_up+duration 2 信号已触发** | 锚过期→**触发基于过期锚，先重建复核再执行** |
| 3 | 002709 天赐 | 08-20 | **+967.9%** | H1 28.6 亿 vs 25H1 2.7 亿；档价大概率整体上移 | **T1 区内 36.44（0 信号）** | 锚过期→重建；同上先复核再执行 |
| 4 | 002714 牧原 | 08-20 | **−157.7% 盈转亏** | TTM EPS −0.19 vs 卡 base 3.66 完全失效；PE 锚体系不适用 | 高 T1 上沿 +14% | 锚过期→重建（**需换体系：PB/价格底带**） |
| 5 | 002299 圣农 | 08-20 | **−44.8%** | TTM 0.78 vs base 1.20 大幅低 | 高 T1 上沿 +16.8% | 锚过期→重建（档线下移风险） |
| 6–11 | 002557 洽洽(+177.4%)/002747 埃斯顿(减亏)/600531 豫光(+12.5% 吻合)/601318 平安(+36.1%)/601899 紫金(+68.2%)/603288 海天(+7.1%) | 08-21~08-26 | — | 各自 TTM vs base 偏离见底稿 | 均未到档、证伪线未触 | 锚过期→常规重建排队 |
| ✅ | 603697 有友 | 中报 08-15 00:00（北京）早于建卡 08-16 | +14.4% | 卡内已含中报 | **T1 区内 9.40，divergence+dry_up+duration 3 信号触发** | **锚新鲜，复核通过复用**；T1 触发有效，是否执行由用户决策 |

- **全部 12 张证伪线今日无触发**（falsification_breach=0）。紫金 08-21 中期分红除权已入因子，不复权档价不受影响。
- **待人工决策**：①珀莱雅右侧持仓（收盘破 hold 线但未触止损 59.50，纪律=收盘≤止损才 stopped_out）；②有友 T1+3 信号执行；③11 张重建排期（建议按上表优先级，每张一个会话走 fred-valuation-card-skill 完整流程）；④有友卡 next_review 已到期但复核通过——系统无自动续期，如需清提醒可出同值 refresh draft 换新 next_review（draft+人工激活）。
- **缺口**：31 只 08-29~08-31 公告簿增量未采（cninfo 抽风，明日回补）；macro USDCNY/EURCNY 今日缺。

## 2026-08-31（续：11 张锚过期卡批量重建——11 张 v2 draft 入库，draft-only 待人工激活）

- **范围**：审核判定锚过期的 11 张（珀莱雅/南航/天赐/牧原/圣农/洽洽/埃斯顿/豫光/平安/紫金/海天），逐张走 fred-valuation-card-skill 第 0–9 步：底稿（inputs_2026-08-31.json，前节已导）→ 盈利底稿 → 刻度/体系判断 → 情景矩阵（build_schedule.py，含 --winrate quarter-Kelly）→ 胜率打分 → 波段/右侧 → draft JSON + md + create-draft。**全部 draft-only，激活须人工**。
- **11 张 v2 draft 清单**（取代各自旧 active 卡，激活后旧卡自动 superseded）：

| 股 | draft | 锚/情景要点 vs v1 | 三档 T1/T2/T3 | 证伪线 | 现价落位 |
|---|---|---|---|---|---|
| 珀莱雅 | 603605SH_9d997d38 | EPS base 3.56→**4.60**（中报超预期）；PE 18/15→20/15；右侧沿用 61.00/59.50（在管 episode 连续性） | 88.3–92 / 78.8–83 / 54.0–58.3 | 54.0 | T3 上方 3.4%，proximity |
| 南航 | 600029SH_a2fc672e | base 0.05→**−0.11**（转亏）；T3 下移 3.95→**3.55–3.85**（BPS 侵蚀）；胜率大修 T1 Kelly=0 | 4.95–5.2 / 4.45–4.75 / 3.55–3.85 | 3.55 | **T1 区内，信号 2 项——但 v2 口径不再自动可买** |
| 天赐 | 002709SZ_a2e04513 | 中报=预告中值兑现，**情景全部维持**，矩阵重跑 | 40.3–42 / 34.6–36.4 / 22.0–23.8 | 22.0 | 贴 T2 上沿，信号 0 项不释放 |
| 牧原 | 002714SZ_c0e31adf | 2027E base 3.66→3.55；T3 用 **PB 底带 1.8–1.9×BPS 14.75**=26.5–28.0 覆盖矩阵值 | 37.5–39 / 32.9–34.6 / 26.5–28.0 | 26.5 | T1 上方 8%；右侧触发 42.70 贴现价 |
| 圣农 | 002299SZ_387e8e84 | base 1.20→**0.78**（Q2 −68% 崩塌）；全部档位深移 −39%+ | 9.7–10.1 / 8.3–8.7 / 5.8–6.2 | 5.8 | T1 上方 63%，远离档位 |
| 洽洽 | 002557SZ_a6dd769e | base 1.25→1.20（H2 兑现压力）；极悲 0.95→0.85 | 17.3–18 / 15.0–15.8 / 10.6–11.5 | 10.6 | T1 上方 4% |
| 埃斯顿 | 002747SZ_6fd732f1 | 价格平台锚维持（结构未破坏）；EPS 微调 | 26.0–27.5 / 21.0–23.0 / 14.5–16.5 | 14.5 | T1 上方 15% |
| 豫光 | 600531SH_6e36e4af | **锚上移 PE 9.5→13**（v1 遗留"中枢上移未验证"经完整回调+三轮恐慌底 PE 11.3–13.7 闭合）；挂 AU0 因子假设 | 9.4–9.8 / 7.7–8.1 / 5.5–5.9 | 5.5 | T1 上方 40% |
| 平安 | 601318SH_153b6efc | **修正 v1 基数错位**（EPS 前瞻 11.60×旧基数 PE 3.0–3.5 不可乘）：统一为 base 8.60×恐慌底刻度带 6.6–8.8；三档整体上移 | 63.6–66.2 / 57.8–60.8 / 42.0–45.4 | 42.0 | T2 下方 3.4%（现价<p5 已定价偏深） |
| 紫金 | 601899SH_616cc92c | **深熊假设折算刻度（4.5–8）回归标准恐慌底刻度**（13.9–15.8 三轮稳定）；base 3.96（FY2027 属性）→3.07（FY2026 实际）；挂 CU0/AU0 因子假设 | 42.7–44.5 / 37.9–39.9 / 28.8–31.1 | 28.8 | T2 下方 11%（已定价盈利回落） |
| 海天 | 603288SH_fb3699b8 | base 1.43（+19% 假设）→**1.28**（+6% 实际外推）；T1 下移 36.4→31.9–33.3；波段箱体 33.5–36.4 启用（首笔减半） | 31.9–33.3 / 29.6–31.2 / 24.8–26.7 | 24.8 | **贴 T1 上沿 +1%** |

- **方法论记录**：①刻度统一原则——同卡内 EPS 情景与 PE 刻度必须同盈利基数可比（平安/紫金两卡的 v1 均存在新旧基数错乘，v2 修正后档位大幅变化，激活前需人工确认理解）；②负盈利/微利公司的锚替代——南航（PB+价格带）、牧原（前瞻 EPS×PE+PB 校验）、埃斯顿（价格平台）三条路径延续 v1 口径；③右侧在管 episode 连续性——珀莱雅 v2 沿用旧触发/止损（61.00/59.50），避免换卡即时改变在管持仓的止损口径；④无 BPS/EV/股息率数据缺口已在卡内标注（南航/牧原 BPS、平安 EV）。
- **人工决策项（激活前必读）**：①11 张激活顺序建议：珀莱雅（右侧决策联动）→ 南航/天赐（T1/T2 贴价，旧锚触发执行纪律）→ 牧原/海天 → 其余；②南航 v1 口径下 T1+2 信号曾达执行态，v2 胜率下修（T1 Kelly=0）——按旧卡执行还是等新卡激活由用户决策；③平安/紫金/豫光三张档位结构变化大，激活前逐张核对卡内"regime"段。
- **数据纪律**：全程只消费 card_inputs 底稿与库内事实，未人工编造数字；BPS/EV 缺口如实标注；测试未跑（无代码改动）。

## 2026-08-31（续②：11 张 v2 卡人工确认激活）

- **用户指令「全部激活」即人工确认**（同 08-28/08-29 先例）：11 张 v2 draft 全部 `activate --effective-from 2026-09-01`——今日（08-31）daily 与报告（revision=4）已按旧卡口径算完，新卡自 09-01（周二）起生效，旧卡 superseded effective_to=2026-09-01（排他端点），无口径空档。
- **激活后全池状态**：active 30 张不变（有友卡复核通过未动），11 张旧卡（8/10–8/14 建卡批次）全部 superseded，next_review 分两批：2026-10-31 一批 29 张 + 有友 1 张（next_review 已过期待处理）；剩余 draft 3 张均为历史遗留（含恒力 600346SH_0f65e846），待人工 reject。
- **09-01 盘后 daily 注意事项**：①南航收盘若仍在 T1 区（4.95–5.20）将按 v2 卡触发 tier_triggered（v2 口径 T1 Kelly=0，机器照常落行、执行决策在人）；②海天贴 T1 上沿（33.63 vs 33.30），跌破 33.3 即触发 T1；③珀莱雅右侧在管 episode 的 stop_level 59.50 新旧卡一致，跟踪无缝衔接；④Tier 档位全面切换后首轮 proximity/tier 信号建议人工抽查与卡面核对。

## 2026-08-31（续③：有友卡续期 + 药明/恒瑞/百济激活——全池 33 只 active 卡全覆盖）

- **有友食品 next_review 续期**（复核通过复用的收尾动作）：从库内现卡 603697SH_f938200e 导出同值 refresh draft 603697SH_e86a63ae（档位/矩阵/右侧零改动，仅 next_review 2026-08-31→**2026-10-31**，input_snapshot 标注 refresh 性质）→ create-draft → **activate --effective-from 2026-09-01**（用户确认续期即人工确认）。到期提醒清除。
- **药明/恒瑞/百济三张 08-30 draft 激活**（用户在事实核实后选择激活；此前本日志"3 张历史遗留 draft 待 reject"表述有误——三张实为 08-30 新建的有效 draft，非废弃稿，恒力初稿 600346SH_0f65e846 早已清理）：603259SH_9681230e / 600276SH_438f17da / 688235SH_f820a833 全部 **activate --effective-from 2026-09-01**（数据截止 08-28、中报已含，新鲜度足够）。
- **激活后全池状态**：**active 33 = watchlist 33 只全覆盖**（次日盘后 daily 报告 degraded(no_active_card) 归零）；draft 清零；rejected 5 / superseded 13。next_review 两批：2026-10-31（32 张）+ 有友 2026-10-31（并入同批）。
- **方法记录**：无导出命令的卡续期走"DB 行 → 同值 refresh draft JSON → create-draft → activate"路径（本轮脚本式操作，可复用）。

## 2026-09-01（盘后 daily：ok=33 一次通过；新卡首轮信号落地；换卡 episode 边界行为记录）

- **采集（网络正常日，全程零重试）**：price/forward 33/33（sina，**显式 `--start 2023-08-01` 修复生效**——forward 覆盖全部 origin 日）；financials 33/33（首批 31 只后超时中断，补采恒瑞/药明完成）；index 2 / telegraph 1 / announcement 33/33 / macro / flow 全净。**公告缺口回补完成**：08-30（周日）13 条 + 08-31 18 条入库（08-29 周六无披露属正常），前日 cninfo 抽风缺口闭合。
- **daily**：`--date 2026-09-01 --raw-dir data/raw/akshare` **ok=33 一次通过**，报告 revision=1，无证伪触发。
- **14 张新卡（09-01 生效）首轮信号**：
  - **T1 触发 3 只**：南航 600029（T1 区内，dry_up+duration——v2 口径 Kelly=0，执行决策在人）、有友 603697（T1 区内，信号延续）、**迈瑞 300760（新进 T1 区 + duration 1 项）**；
  - **proximity 3 只**：海天 603288（贴 T1 上沿 33.30）、恒瑞 600276、百济 688235；
  - 其余新卡远离档位，无异常。
- **珀莱雅右侧"缺行"排查（非 bug，设计语义留档）**：right_side 按 §5.1 逐卡版本生效区间独立重放——v1 episode（08-27 confirmed）止于 08-31（holding），v2 卡 09-01 起**新 episode 从 idle 起算**；09-01 收 62.62（+3.8%）未满足放量突破条件 → 无行可写，日报右侧段回退引用 08-31 行（"来源 @ 2026-08-31"如实标注）。**影响**：①换卡后 episode 账本重置，若需跨卡连续跟踪需在卡复核时人工衔接（本轮 v2 与 v1 触发/止损同值 61.00/59.50，经济含义无变化）；②09-01 收盘远离止损，前日"距止损 1.4%"压力解除；③"右侧确认待执行"提醒（08-27 触发无 executions 记录）仍挂——**珀莱雅执行录入仍是唯一遗留人工项**。
- **杂项**：000333/000651 right_side 阶段标 degraded（error=None，无触发位卡的 idle 标记路径），不阻断；event_calendar 提醒：09-05 前后关注 2026 三季预约披露源补采（9 月底 `--calendar-period 2026三季`）。

## 2026-09-01（续：消息面研判——全部 tier4 电报无公司公告；油价冲击南航）

- **今日事件簿**：8 条全部电报（tier4，不进决策链），中报季后公告真空；关键两条：WTI/SC 原油 +3%（WTI 7-24 以来新高）、纳斯达克 100 期货 −1.4%；其余为半导体链（英伟达/阿斯麦/三星/兆易，池内无标的）与蔚来成本言论。
- **宏观因子实读 vs 卡内假设**：**OIL（布伦特）92.61**，两日 +4.9%（88.27→90.9→92.61），破南航卡中性平台（85–90）上沿——未到 ±15% 提醒线（103.5），方向连续两日利空；CU0 109220 / AU0 959.94 平稳（紫金/豫光因子假设维持）；LH0 11685（11.7 元/kg，牧原成本线下磨底）；USDCNY 678.09 平稳。
- **影响评估（结合卡位，解读层不产生规范化数字）**：①南航——T1 触发第二日 + 油价冲击，Q3 旺季转盈分水岭难度上移，bear_mid（H2 8 亿）概率上移；T1 本就 Kelly=0，油价强化"先不动"；若布伦特续涨逼近 103.5 触发锚复核提醒。②埃斯顿——纳指期货跌+AI 情绪链，题材第二波预期承压（深左无操作影响）。③牧原——猪价成本线下磨底符合卡内中性偏悲观情景。④紫金/豫光——铜金平稳无冲击。⑤其余消费/金融今日无直接消息，卡面信号主导。
- **观察**：明日油价若续涨→南航因子偏离提醒方向；纳指情绪若传染 A 股→题材/成长档位可能加速接近买区，按卡执行不抢跑。

## 2026-09-02（入池：东方航空 600115.SH，watchlist 34 只）

- **流程**：watchlist.yaml 加行（BK1479 航空，与南航同业；aliases 含"中国东航/东航/C919"）→ `db seed`（34 只）→ akshare 采集（`--start 2023-08-01 --end 2026-09-01`，run_add_600115）：price/forward 750 行零错误（forward 显式 --start 防 origin 坑）+ financials 110 期 + announcement + forecast 有行 → **daily 首跑 ok=34**（09-01 幂等重跑，600115 ok，报告 degraded P5=no_active_card 属预期）→ stock_info 回补（股本快照 inserted=1）→ indicators 重算。
- **基本面快照**：FY2025 −16.33 亿；26Q1 +16.33 亿；**26H1 −21.79 亿（Q2 单季 −38.12 亿）**——TTM 转负，pe_status=ttm_non_positive（历史仅 2026-05~08 窗口 TTM 为正），PE 失真。**将来建卡须走南航 v2 同款路径（PB+价格带锚，周期底部看资产不看利润）**。
- **行业码**：symbol_industry 已有 BK1479 航空运输（08-28 push2delay 批次已覆盖，seed 未重复写入）。
- **覆盖**：bars 749（2023-08-01~09-01）/ 周线 158 / 指标 749（pe_ttm 83 非空，其余窗口 ttm_non_positive 如实降级）。
- **背景**：用户问题"东航波动为何大于南航"引发入池决策——东航资产缓冲垫薄（PB ~5.9）、C919 主题属性、国际线占比高，波动结构异于南航，值得独立跟踪建卡。
- **注意事项**：①600115 暂无排期卡，日报 degraded 属预期；②若建卡，PE 锚不可用（TTM 亏损），参照南航 v2 手工锚定模板；③09-02 盘后例行 daily 将自然纳入该股（--raw-dir 不变）。

## 2026-09-02（续：东方航空 600115.SH 估值排期卡首建——draft 600115SH_67ba0e63）

- **流程**：card_inputs 底稿 → 价格结构手工锚定（负 EPS+PB 双失真，南航 v2 模板）→ 胜率打分 → draft JSON+md → create-draft **600115SH_67ba0e63**（draft-only 待人工激活）。
- **锚定要点**：①PE 失真（TTM 转负）；②**PB 锚亦不可用**——东航 BPS≈0.60（净资产被历史亏损侵蚀），PB 5.6–9.0 全区间失真，比南航（BPS 1.85 可用 PB）更极端 → 退化为**纯价格底带锚**：3.38–3.77 六次探底验证（2023-10/2024-02/2024-04 大底/2024-09/2025-04/2026-06~09），体系稳定非降档；③**东航与南航最大差异=无 PB 资产底交叉校验**，T3/证伪线（2.50）为样本外深位、低置信，纯"生存定价+技术延伸"逻辑，卡内已如实标注。
- **三档**：T1 3.40–3.70（**现价 3.49–3.56 已在区内**，Kelly=0）/ T2 2.95–3.15 / T3 2.50–2.70；证伪线 2.50。
- **基本面**：单季极端季节性（25Q3 +35.34 / 26Q2 −38.12 亿），年度 EPS 无意义；26H1 −21.79 亿（营收 +11.1%，油价吞噬利润）；券商 FY1 +3.75 亿隐含 H2 +25.5 亿 vs 25H2 实际 −2.0 亿——极度乐观不采信；**因子假设已挂 OIL（中性 85–90，09-01 实读 92.61 超上沿）+ USDCNY（破 6.9 复核）**。
- **波段/右侧**：波段箱体 3.43–3.82 可启动（现价贴买区，首笔减半、上限 15%）；右侧=放量收复 3.80（止损 3.40）；卡内标注东航波动两倍于南航（薄净资产+C919 主题+融资盘），只认收盘口径。
- **胜率**：T1 40–50 / T2 45–55 / T3 55–65（基准 55–60：贴底带+最差中报已定价；盈利恶化 −10；券商裂口 −5~−10；无资产底 −5；生存质地 +5）。
- **验证**：无代码改动未跑 pytest；create-draft schema+语义校验通过（含 earnings.factor_assumptions 挂载、负值 EPS/PE 定点字符串、价格带 tiers）。

## 2026-09-02（续②：东航卡人工确认激活——全池 34 只 active 卡全覆盖）

- **用户指令「激活」即人工确认**：draft **600115SH_67ba0e63** activate --effective-from **2026-09-03**，next_review 2026-10-31；激活归档 cards/600115.SH/2026-09-03_*.md + current.md 刷新。
- **全池状态**：active 34 = watchlist 34 只全覆盖；draft 清零。09-03 盘后 daily 起东航卡片相关信号（tier_triggered/proximity/证伪线监测）同口径产出。
- **提醒**：东航现价贴 T1 区（3.49–3.56 in 3.40–3.70）——激活后首日 T1 即触发 tier_triggered（无信号要求档）；同期波段买区（3.43–3.55）亦可能落决策点，两处提醒属卡面设计内状态，执行决策在人。

## 2026-09-02（盘后 daily：ok=34 一次通过）

- **采集（网络正常日，全程零重试）**：`akshare_collect --sources price,forward,financials,index,telegraph,announcement,macro,flow --start 2023-08-01 --end 2026-09-02 --price-api sina --run-id run_daily_20260902`，price/forward/financials/announcement 34/34 全齐，index 2、telegraph 1（10 条入库/10 行缺标题跳过）、flow（lhb EMPTY 属正常、dzjy ok）。**macro 12/13**：CL（WTI，sina GlobalFutures SSL EOF）今日缺，其余含 USDCNY/EURCNY 全齐（前日缺口自愈）。
- **daily**：`--date 2026-09-02 --raw-dir data/raw/akshare` **ok=34 一次通过**，报告 revision=1，无证伪触发、无复核逾期。600115 东航 degraded(no_active_card) 属预期（卡 600115SH_67ba0e63 effective 2026-09-03，明日起出卡片信号）。
- **今日决策点（P3）**：T1 触发 6 只——南航 4.98（v2 口径 Kelly=0，执行在人）、迈瑞 167.92、陕煤 26.10、海油 33.77、有友 9.59、西部矿业 39.59；海天 33.87 波段箱体 buy_zone（33.50–34.30）。P4 贴边 8 只，其中洛钼距 T2 下沿 18.49 仅 0.2%、天赐距 T2 下沿 34.60 仅 0.5%。珀莱雅"右侧确认待执行"提醒仍挂（08-27 触发无 executions，唯一遗留人工项）。
- **宏观因子**：OIL 布伦特 94.22（88.27→90.9→92.61→94.22 四日连涨，超南航/东航卡中性平台 85–90 上沿，未到 ±15% 提醒线 103.5）；USDCNY 678.29、CU0 108040、AU0 938.22、LH0 11740 平稳。油价方向对航空双卡连续利空，逼近锚复核节奏需关注。
- **测试**：无代码改动，未跑 pytest。

## 2026-09-02（续③：东航卡提前生效 + 4 笔执行录入）

- **用户指令「现在就救活」即人工确认**：旧卡 600115SH_67ba0e63（effective 2026-09-03，从未覆盖任何交易日）reject 人工废止（窗口闭合成 [09-03, 09-02) 空区间，如实记录）；同值 draft JSON 重建 **600115SH_fa3ae52b** 并 activate **--effective-from 2026-09-02**（内容零改动：三档 3.40–3.70 / 2.95–3.15 / 2.50–2.70，证伪线 2.50，next_review 2026-10-31）。注：activate 护栏禁止 effective_from 早于当前 active 卡生效日（§5.1 不回填历史），故须先废止旧卡。
- **daily 重跑**（激活后同日幂等重跑，revision=3；多跑一次为操作冗余无害）：600115 转 complete，09-02 起卡片信号落地——tier_triggered T1（收盘 3.41 入 [3.40,3.70]）、box_position=below_box、证伪线 0/2 日、衰竭 0 项。全池 ok=34。
- **执行录入 4 笔（#18–#21，append-only）**：#18 2025-07-30 buy 17700@3.89、#19 2026-01-07 sell 10800@6.15、#20 2026-02-05 sell 6000@6.31（三笔 --backfill，如实标记系统上线前手工单）；#21 2026-09-02 buy 15000@3.41 走正常 add（卡当日已生效），冻结 09-02 信号快照（T1 触发态）。持仓净额 15900 股。明早提醒 cron 已删（事已完成）。

## 2026-09-03（基本面深度分析落地：财务三表补采 + fundamental-analysis-skill 首单）

- **起因**：用户提出把一套"华尔街式 8 段股票分析"提示词改造后接入系统。评估结论：
  框架完整但无数据锚定（幻觉风险）、角色扮演无约束力、#1 与 #2–#6 冗余；改造方向=
  按系统"底稿→LLM draft→人工复核"模式重构。用户定边界：①补采财务数据（先看数据源
  是否具备）；②先按需 skill，打磨成熟再考虑进日报。
- **数据探测**（`docs/probe_20260903_financial_statements.md`）：akshare 三接口实测通过——
  sina 资产负债表/现金流量表（2013 起全历史、单位元、与存量 financial_facts 逐分一致）、
  THS 财务摘要（80 指标×45 期，含 ROE/资产负债率/毛利率）；仅 A 股，港股标缺口；
  无披露日走 D1.3 降级。kimi bs/cf + financial_index 留作未来兜底（本期未建 adapter）。
- **数据层（migration 0007 + 采集/adapter）**：新表 balance_sheet_facts / cash_flow_facts
  （挂 financial_reports.report_id，同 financial_facts 模式）+ financial_indicator_snapshots
  （THS 摘要 payload 快照，只作交叉核对不作规范化来源）。akshare_collect 新增手触发源
  `balance_sheet/cash_flow/fin_abstract`（**不进 daily 默认 sources**）；adapters/akshare.py
  三个 parse：表头复用（历史期新建降级表头，仅服务趋势分析不进信号链）、内容一致幂等
  跳过、变化原地更新+data_revisions；摘要解析带 income 对账（0.5% 容差，超差记
  incomplete 不覆盖）+ 历史期 income facts 回填（只补 NULL 列）。
  **设计偏差记录**：计划原定 BS/CF 内容变化走"revision 升级"，实现改为原地更新+
  data_revisions 日志（报表更正罕见，header revision 语义仍专属利润表）——理由：
  BS/CF 挂在共享表头上，bump header revision 会惊动利润表通道。
- **回填**：真实库先备份 `market.db.bak_20260903` → migrate 0007 → 34 只 A 股
  （run_fin_backfill）：BS 2487 / CF 2463 / 快照 2538 行，0 冲突；603993 洛钼 2008 年报
  对账超差 1 处（重述差异，记 incomplete 待人工核）；0700.HK 无源缺口。
  **意外收获**：真实库 financial_reports 实际覆盖全历史（2013 起，非交接文档印象中的
  "2023Q3 起 12 期"——那是 published_at 点时口径的覆盖范围），故 income 回填路径
  基本未触发，"5 年财务分析"数据底子比预期完整。
- **分析层**：新导出器 `scripts/pipeline/fundamental_inputs.py`（schema
  fundamental_inputs_v1，纯读取，复用 card_inputs 的 meta/earnings/forecasts/
  valuation_scale/factor_snapshot）→ `reports/{symbol}/fundamental_inputs_{date}.json`
  八段；派生指标全部 Python 计算（净利率/ROE 期末归母权益口径/资产负债率/有息负债/
  FCF/OCF 净利比；毛利率取 THS 快照标 _ths）；**隐含回报区间表替代 DCF**（一致预期
  EPS × PE 历史分位 p25/p50/p75，样本区间强制标注）。新 skill
  `skills/fundamental-analysis-skill/`（draft-only）：8 段提示词重构为三模块
  （事实解读→定性研判→对抗呈现），铁律沿用 message-tag-skill 纪律（不产规范化数字/
  事实推断观点三分/每条结论附证伪条件/禁用词/不做买卖建议——仓位归排期卡体系），
  产出 `reports/{symbol}/fundamental_{date}_draft.md` 人工定稿。
- **首单验证（603605.SH 珀莱雅）**：采集→入库→底稿→draft 全链路跑通
  （`reports/603605.SH/fundamental_2026-09-02_draft.md`）。数字抽查：Python ROE
  17.41% vs THS 17.61%（分母口径差，合理）；资产负债率 33.66% 与 THS 一致；FCF/同比
  序列与公开财报走向吻合；2026H1"营收 +0.2% 净利 +46.3%"裂口 -37.7pp 如实呈现；
  缺 BS 2 期（上市前）如实标 incomplete。draft 初稿两处自算数字（净现金差值、
  "连续 18 个月"表述）被自查抓获并改写——skill 铁律有效性实证。
- **测试**：新增 22 项（adapter 12 + 导出器 10），**522 全绿**（test_db migration
  清单同步加 0007）。
- **文档**：probe 记录 + system_design（§3.7 新小节/§7 表清单/§9.4 二期扩展落地标注/
  §2.4 LLM 边界补第三 skill）+ database_schema（三表逐字段）+ 本日志 + handoff。
- **后续（未做，按需再议）**：①skill 进日报/每日 sources（用户明确缓办，打磨后另立项）；
  ②kimi bs/cf adapter（港股 0700.HK）；③§9.4 自算历史 PB 序列（BS 数据已就位）；
  ④603993 2008 年报对账差异人工核对；⑤池外竞争对手数据采集（现仅池内横截面）。

## 2026-09-03（盘后 daily：ok=34 一次通过）

- **采集（分三个 run-id，financials 超时拆分补采）**：主跑 `run_daily_20260903`（price/forward/financials/index/telegraph/announcement/macro/flow）在 financials 中途撞 15 分钟命令超时（09-01 同款坑），已落盘 price/forward 34/34 + financials 22/34；拆 `run_daily_20260903_fin2` 补齐剩余 12 只 financials（12/12 零错误）；`run_daily_20260903_rest` 补 index/telegraph/announcement/macro/flow。**announcement 31/34**：601728 电信 / 601899 紫金 / 603288 海天三只 cninfo JSONDecodeError——手测确认非随机抽风而是分页中途上游故障（大窗口逐页抓取中途断，小窗口亦失败，多重试 + 间隔 15s 三轮仍失败），同 08-29/08-31 先例，**缺口明日回补**。
- **daily**：`--date 2026-09-03 --raw-dir data/raw/akshare` **ok=34 一次通过**，报告 revision=1，无 P1/P2（无证伪触发、无复核逾期、无 degraded）。34 只当日 bar 齐全；000300.SH 至当日、^HSI 滞后一日（09-02）属常规；macro **13/13**（昨日缺的 CL WTI 自愈，92.14）。
- **今日决策点（P3）7 只**：海天 603288 波段 buy_zone（34.12）；南航 600029 T1（4.99，v2 口径 Kelly=0 执行在人）；平安银行 000001 波段 sell_zone（11.88）；海油 600938 T1（33.43）；**东航 600115 T1 + buy_zone 双触发（3.43，卡 fa3ae52b 激活后首个交易日即双落，卡面设计内状态）**；陕煤 601225 T1（26.24）；有友 603697 T1（9.63）。P4 观察点 6 只：迈瑞距 T1 下沿 0.1%、西部矿业距 T1 上沿 0.3%、美的距 T1 下沿 0.9%、恒瑞距 T2 下沿 1.0%、百济距 T2 上沿 2.5%、珀莱雅"右侧确认待执行"仍挂（08-27 触发无 executions，唯一遗留人工项）。P5 内"档位触发待确认"3 只（平安 601318 / 洛钼 603993 / 天赐 002709 收盘进价区但同锚衰竭 0 项 < 2 不触发）。
- **宏观因子**：OIL 布伦特 **96.68（88.27→90.9→92.61→94.22→96.68 五日连涨）**，逼近南航/东航卡 ±15% 提醒线 103.5（还差 ~7%），对航空双卡连续利空，若续涨一两个交易日即触发锚复核提醒；USDCNY 678.07、CU0 108410、AU0 958.38、LH0 11835 平稳。
- **测试**：无代码改动，未跑 pytest。

## 2026-09-03（续：盘后复盘）

- **决策点复盘（P3，7 只）**：T1 触发 5 只——南航 4.99（活跃衰竭仍 2 项、贴区 0.05%，v2 Kelly=0 油价背景"先不动"强化）、**东航 3.43（卡激活首日即 T1+buy_zone 双触发，卡面设计内；回购进展公告）**、海油 33.43 / 陕煤 26.24 / 有友 9.63（常规进区、衰竭均 0 项）；箱体 2 只——**海天 34.12 buy_zone（同时距 T1 上沿 2.5%，箱体+档位双重接近，今日最值得人工复核的复合位置）**、平安银行 11.88 sell_zone（贴 box_high 11.90 差 0.2%）。
- **P4 贴边群**：迈瑞距 T1 0.1%、西矿距 T1 上沿 0.3%、美的距 T1 0.9%、恒瑞距 T2 1.0%、百济距 T2 2.5%——五只同贴边，明日小幅波动即可能新增多个决策点；平安 601318/洛钼/天赐"进区衰竭不足待确认"延续。
- **消息面**：电报 11 条全 tier4 不进决策链——美初请偏高、**沃勒鹰派（通胀偏热考虑支持加息）**+现货金 +2%（对 AU0 因子方向扰动，AU0 958.38 尚平稳）；池内公告常规：恒瑞 AL 获受理、联影/洛钼/平安分红实施、珀莱转债回售提示、东航回购进展、南航转债到期兑付提示。
- **宏观**：OIL 96.68 五日连涨（累计约 +9.5%），距航空双卡 ±15% 提醒线 103.5 约 7%——再涨 1–2 日即触发锚复核提醒；USDCNY/CU0/LH0 平稳。日历：09-09 CPI/PPI、09-11 社融。
- **明日清单**：①珀莱雅右侧执行录入（遗留人工项，现价 62.72 距止损 59.50 缓冲约 5%，压力较 08-31 缓和但不能继续拖）；②回补电信/紫金/海天公告簿；③盯 P4 贴边群转正（迈瑞/西矿优先）；④油价逼近提醒线预备航空双卡锚复核。

## 2026-09-03（续②：珀莱雅"右侧确认待执行"人工决定闭环——不跟单，无需录入）

- **用户澄清**：08-27 右侧确认（v1 卡 episode，confirmed→holding 至 08-31）后**没有买入**，非漏录入。上一笔执行仍是 #10（08-25 sell 1000@61.00）。
- **处理**：零操作。该提醒是执行闭环窗口提醒（report.py `_right_side_followups`，FOLLOWUP_WINDOW_DAYS=10）：confirmed/stopped_out 后 10 日窗内无 executions 即提示，人工不跟单属设计内合法结局；**不补录伪执行**（append-only 纪律）。08-27 触发行 09-06（含）前报告仍显示，**09-07 起自然过期**。
- **跟踪连续性**：v2 卡（09-01 生效）episode 已从 idle 重置，后续满足"放量突破→回踩确认"将重新走状态机，不回填历史确认。本条同时修正本日复盘清单中"录入不紧迫但不能继续拖"的表述——该项了结，唯一遗留人工项清零。

## 2026-09-03（续③：平安 601318.SH 基本面深度分析 draft——fundamental-analysis-skill 第二单）

- **流程**：fundamental_inputs 底稿（数据截止 09-03/财报 2026-06-30/一致预期快照 08-10）→ 三模块 draft → `reports/601318.SH/fundamental_2026-09-03_draft.md`（待人工定稿）。
- **核心张力**：盈利超预期（H1 净利 +36.06% vs FY1 一致预期 +9.89%，裂口 −26.17pp；ROE(_ths) 半年 7.2→9.0）vs 估值刻度上沿（PE 6.5922 > 3 年样本 p75=6.5345，隐含回报表 FY1 全档隐含价 40.17–53.45 低于现价 58.00，FY2 p75 才持平）。限定：3 年样本含保险估值压制期 + PE 锚对保险业适配弱（P/EV 才行业标准，EV 未入库，卡 review_triggers 自认重锚事项）。
- **财务稳健**（负债率六期 89.81–90.06% 稳定；有息负债 2346→2562 亿单边上行列为观察）；盈利质量按 incomplete（FCF null、OCF/净利对保险诊断力弱如实注明）。
- **排期卡衔接**：收盘 58.00 入 T2 区 [57.80,60.80] 首日，同锚衰竭 0/2 不触发（tier_triggered pending_signals）；右侧状态机 v2 生效以来无任何状态行（卡内预案触发 56.50/判定 57.07 未被机器确认，09-01/09-03 两日 +2.4~2.5% 放量是否满足卡定义属人工核对观察点，本稿仅记事实不下结论）。
- **skill 纪律实证**：ROE 引 roe_ths 标注口径；池内 BK1358 无同业如实写"仅自身"；禁用词/三分标注/证伪条件全覆盖。

## 2026-09-03（续④：平安右侧状态机"零行"排查——非 bug，放量定义未满足）

- **现象**：601318.SH signal_facts 中 right_side 历史行数为 0，而 v2 卡（153b6efc）right_side_trigger_json 结构化字段齐全（trigger 56.50/stop 54.60），且 09-01/09-03 收盘 57.23/58.00 均越过判定线 57.065。
- **排查（systematic-debugging）**：①假设"无触发位"排除（字段在）；②复算突破条件：close ≥ trigger×1.01 **且** volume_adj ≥ 2.0×前 20 日均量（shift1）——两日量 10809/10871 万手，前 20 日均量约 7270 万手，量比约 1.49 < 2.0，**量能条件不满足**；③v1 窗口（触发线 56.56）无任何收盘达标日；④模块 CLI 权威复核：`right_side 601318.SH --as-of 2026-09-03` → status=ok，当前状态 idle，**转换 0 次**，与设计一致（"每次状态转换写行；idle 不落行；仅无卡/无触发位写 incomplete 行"）。
- **根因**：非缺陷。平安两次越线均为"缩量版突破"（量比 ~1.5x），机器按 signals.yaml `right_side.vol_multiple=2.0` 的放量定义不予确认。与珀莱雅（08-26 放量→waiting_retest 行）口径自洽。
- **是否调参**：vol_multiple=2.0 对低波动大盘股是否过严属参数问题——signals.yaml ⚠️ 参数处人工核对期内，不调整；如需调整待核对期结束后走 config_hash 可追溯变更。
- **同步**：平安 fundamental draft §6 空方第 4 条已补量能复核明细。

## 2026-09-03（续⑤：right_side vol_multiple 2.0→1.5 落地——用户指令，含参数回放依据）

- **纠错**：002747.SZ = **埃斯顿**（机器人/自动化，池内创始成员，active 卡 002747SZ_6fd732f1），此前对话中误称"晨光"（晨光文具=603899，池外），特此更正。
- **回放依据**（一次性脚本，复用 right_side.evaluate_segment 真实代码路径，按卡片版本窗口全池重放，脚本不入库）：2.0 基线 2 episode（恒力 +4.35% 持有中 / 珀莱雅换卡切断 −3.22% 标记）；1.5 新增平安（09-02 确认 @56.63→标记 +2.42%）与迈瑞（09-02 确认 @167.92→−0.15%），无止损；1.4 额外放入埃斯顿 08-19 假突破（@36.04→08-24 止损 @30.82，**−14.48%**）。结论：1.5 在样本内干净、1.4 已现噪声代价；样本仅 17 个交易日，不支持强统计结论，本质是滤波严格度取舍。
- **变更**：`config/signals.yaml right_side.vol_multiple 2.0→1.5`（该参数不在 ⚠️ 冻结清单；yaml 注释记录调整日期与理由）。测试适配：`test_right_side_breakout_boundaries` 量能边界断言从硬编码 2 倍（200/199.99）改为按 `P["right_side"]["vol_multiple"]` 推导（配置相对化，兼容未来调参）。**522 项测试全绿**。
- **全池重放**：34 只跑 `signals.right_side` CLI，32 ok + 2 设计内 `incomplete(no_trigger_level)`（美的/格力无触发位卡）。新增行：平安 601318（09-01 waiting_retest / **09-02 confirmed** / 09-03 holding）+ 迈瑞 300760（同构三行），与回放预测逐日一致；其余 32 只重放幂等无变化。
- **报告 revision=2**：全池日报与单股报告重出。新增决策点：平安/迈瑞各两条——[右侧持仓跟踪]（止损 54.60/155.20，距止损 6.2%/8.0%）+ [右侧确认待执行]（09-02 触发无 executions，10 日窗内持续提醒，录入后消失）；珀莱雅 08-27 提醒照旧（09-07 自然过期）。
- **注意**：新 config_hash 随本次 run 入库，旧 signal_facts 保留旧 hash 可追溯；未来每日管线自动按 1.5 判定。平安/迈瑞是否按右侧信号执行由人工决定（T2 衰竭闸门 0/2 未变，档位框架仍不触发）。

## 2026-09-04（换手率 + 股东户数落地——§5.7 筹码集中度缺口闭合 2/3）

- **背景**：用户问三类缺口数据源。探测结论：①换手率——sina 日线自带 `turnover`（小数）现有源即可；②股东户数——`ak.stock_zh_a_gdhs_detail_em`（东财 datacenter，实测珀莱雅 37 期全历史）当前网络可用；③筹码分布——`stock_cyq_em` 依赖 push2his 仍断连（与 price 切 sina 同因），且系东财模型估算值，缓办。用户指令"执行采集"。
- **数据层（migration 0008/0009，真实库先备份 `market.db.bak_20260904`）**：①`daily_bars` 加 `turnover REAL` 列（**小数口径**，sina 原生/东财百分点采集侧归一；港股与 forward 行留空；**派生快照元数据，差异更新不记 data_revisions**——价格事实字段 revision 语义不变）；②新表 `holder_stats`（UNIQUE(symbol, stat_date)，delta_pct 存小数，announced_at=公告日期为 PIT 可见日；upsert 快照风格同 macro_factors，无 revision 链）。
- **采集/adapter/ingest**：`collect_price` 三路径（sina/em/HH）写 turnover 列（em 换手率百分点 ÷100；港股、qfq forward 留空）；新 source `gdhs`（`collect_holder_stats` → `{symbol}_gdhs.csv`，手触发不进 daily 默认 sources）；`parse_price_csv` 透传 turnover + upsert 后差异 UPDATE（旧行兼容 9 列旧格式不清空）；新 `parse_holder_stats_csv`（文件名正则取 symbol，行级幂等/更新）；ingest 路由 ("akshare","gdhs")。
- **回填实测**：turnover **25524/25524 CN bars 100%**（34 只，run_turnover_backfill，sina 全窗口重采）；holder_stats **1862 行/30 只**（2013 起全历史，run_gdhs_backfill）。**缺口 4 只**：601899 紫金 / 600029 南航 / 603993 洛钼 / 600115 东航——东财 RPT_HOLDERNUM_DET 源侧返回空（curl 手验 code=9201"返回数据为空"，非限流/非参数，稳定复现）；tdx quotes gdrs 字段为其备援（非序列）。**筹码分布**：cyq 探测仍被 push2his 阻断，如实记录。
- **测试**：+5（adapter turnover 1 + holder_stats 2 + collector 2），量能边界断言改配置推导（上一节），**527 全绿**。数据库 schema 清单同步 0008/0009。
- **文档**：database_schema（daily_bars.turnover 行 + holder_stats 表节 + 速查清单）+ 本日志 + handoff。
- **未做**：①换手率/股东户数进报告 §7 快照与 UI 展示（数据已就位，展示层按需另做）；②4 只股东户数缺口的他源补采；③筹码分布待 push2his 恢复后评估（且需先定义模型估算数据的消费边界）。

## 2026-09-04（续：自算筹码分布设计方案产出——待外部 Agent 评审）

- **背景**：用户问筹码分布他源。实测：东财 cyq 直连与代理（127.0.0.1:7897）均被拒（RemoteDisconnected，东财对代理出口风控）；Tushare cyq_perf/cyq_chips 需新供应商；tdx 已探测不可得。提出自算路线（换手率衰减模型，输入=昨日回填的 3 年 OHLC+量+换手率已 100% 就位），用户选定"自算 + 出设计方案交其他 Agent 评审"。
- **产出**：`docs/superpowers/specs/2026-09-04-chip-distribution-design.md`（draft 待评审）。要点：换手率衰减模型（A=1.0/k_cap=0.8/三角核）、复权域直算输出折回（除权无跳变）、burn_in=60 日打标、chip_distribution 表（migration 0010，不存网格）、golden+性质+除权连续性测试计划、东财恢复后秩相关交叉验证（ρ≥0.8 带宽比对非逐点）。**定位纪律不变：模型估算观察项，不进信号链**。
- **给评审的 7 个开放问题**（分布核形状/A 与 cap/复权域直算假设/burn_in 松紧/网格不落库/网格外价格打标/命名）已列 §8，评审通过后按 §9 六步实施（~400 行，一次会话）。
- **本条为文档产出，无代码/库变更，未跑 pytest。**

## 2026-09-04（续②：筹码分布设计 v2——按评审意见修订完毕，可实施）

- **评审**：用户提交外部 Agent 评审结论"conditional approve"（9 项修改清单），其中含对我 v1 两处实质纠错：①复权域直算的隐含假设——现金分红也会把历史成本平移，系统性低估真实股东成本（须显式声明且"不还原真实股东成本"入边界）；②burn-in 残留数学错误（v1 写"60 日 <20%"，实际 (1−0.02)^60≈30%，A=0.7 下 43%）。
- **v2 修订**（全部采纳，映射表见文档 §0.1）：A=1.0→**0.7**（k_cap 重定位为异常护栏，A=0.7 下仅 turnover≥114% 触发）；burn_in=60→**90**+残差公式显式化+状态名 'ok'→'mature'；三角核 vwap 峰→**close 峰**（涨停日质量自然落板价、amount 缺口行免疫，vwap/均匀降为对照参数）；§2.3 补现金分红偏差声明+折回反向验证；表加 `amount_used`+rule_version 编码形状 `chip_v1_close_tri`；测试新增除权连续性/衰减截断/次新首日/折回反验 4 组；adjust.py 加 chip 重算 NOTE 提示（不耦合事务）；§6 交叉验证清单补 turnover 分母口径偏差源（sina 流通股本 vs 总股本）；开放问题 7→2（保留尖峰族与网格打标）。
- **状态**：设计文档 `docs/superpowers/specs/2026-09-04-chip-distribution-design.md` v2 就绪，按 §9 七步实施（~550 行含测试）。本条为文档修订，无代码/库变更。

## 2026-09-04（盘后 daily：ok=34 一次通过）

- **采集**：单 run-id `run_daily_20260904`（price/forward/financials/index/telegraph/announcement/macro/flow，`--start 2023-08-01 --end 2026-09-04 --price-api sina`，后台无超时运行规避 15 分钟命令超时坑），**8 源全部 0 errors**：price/forward/financials/announcement 各 34/34，index 2、telegraph 1（7 条入库/13 行缺标题跳过）、macro 13/13、flow（lhb EMPTY 属正常、dzjy 1 笔入库）。**09-03 公告缺口回补**：海天 603288 的 09-02/09-03 共 6 条已随全窗口重采入库（采集 34/34 成功，cninfo 今日正常）；电信 601728 / 紫金 601899 09-03 源侧无公告（非缺口，采集窗口已全覆盖）。
- **daily**：`--date 2026-09-04 --raw-dir data/raw/akshare` **ok=34 一次通过**，无 P1/P2（无证伪触发、无复核逾期、无 degraded 股）。34 只当日 bar 齐全；000300.SH 与 ^HSI 均至当日（恒指罕见未滞后）。
- **603697 有友防御性因子重建**：除权落入检查窗（max_dev=0.1227% > 0.1%）触发全量重建 version_id=127，周线/指标（daily=752、pe_ttm 非空 746/752）/五信号同事务重算，报告 complete；adjust 阶段记 degraded=warnings（corporate_actions 空、平台切换日未交叉印证，已知缺口⑦同型，非错误）。美的/格力 right_side degraded=no_trigger_level（卡内无右侧触发位，设计内）。
- **今日决策点（P3）9 只**：有友 603697 波段 sell_zone（9.93，贴 box_high 10.20 差 2.4%）；南航 600029 T1（5.03，v2 口径 Kelly=0 执行在人，连续第二日）；迈瑞 300760 T1（169.91）+右侧持仓跟踪（止损 155.20，距 9.5%）+右侧确认待执行（09-02 触发）；平安银行 000001 波段 sell_zone（11.89，贴 box_high 11.90 差 0.1%）；海油 600938 T1（33.41，连续）；西矿 601168 T1（38.79，昨日 P4 贴边 0.3% 今日转正）；陕煤 601225 T1（26.24，连续）；东航 600115 T1+buy_zone 双触发（3.49，连续第三日）；圣农 002299 波段 sell_zone（17.27）。
- **P4 观察点 6 只**：洛钼距 T2 下沿 18.49 差 0.1%、美的距 T1 下沿 88.00 差 0.5%、恒瑞距 T2 下沿 46.44 差 0.9%、天赐距 T2 下沿 34.60 差 2.2%；珀莱雅 603605（08-27 触发，09-07 自然过期）与平安 601318（09-02 触发，同时"进区衰竭不足待确认"延续+右侧持仓跟踪距止损 6.7%）右侧确认待执行——**执行决策在人**。
- **宏观因子**：OIL 布伦特 **95.32（-1.4%，五连涨中断）**，距航空双卡 ±15% 提醒线 103.5 约 8.6%，压力暂缓；CL WTI 90.83 同步回落；AU0 965.96 续涨（沃勒鹰派后金价未回头）；USDCNY 677.87、CU0 108780、LH0 11765 平稳。日历：09-09 CPI/PPI、09-11 社融。
- **测试**：无代码改动，未跑 pytest。

## 2026-09-04（续：盘后复盘）

- **决策点复盘（P3，9 只）**：卖出侧 3 只箱体——平安银行 11.89 贴 box_high 11.90 仅 0.1%（连续两日贴边，卖区最紧）、有友 9.93（距箱顶 2.4%）、圣农 17.27（sell_zone 内 71% 位置）；买入侧 T1 6 只——迈瑞 169.91 / 西矿 38.79（昨日 P4 贴边 0.1%/0.3% 双双转正）、南航 5.03（连续，Kelly=0）、海油 33.41 / 陕煤 26.24（连续）、东航 3.49 T1+buy_zone 双触发连续三日。迈瑞另挂右侧持仓跟踪（止损 155.20，距 9.5%）+右侧确认待执行（09-02 触发）。
- **昨日清单核对**：①珀莱雅右侧执行——昨日已闭环（不跟单），提醒 09-07 自然过期，今日仍在列属预期；②电信/紫金/海天公告簿回补——完成（cninfo 自愈，海天 6 条入库；电信/紫金当日源侧无公告）；③P4 贴边群——迈瑞/西矿转正，美的（0.5%）/恒瑞（0.9%）仍贴边，百济脱离贴边区；④油价——五连涨中断（96.68→95.32，-1.4%），距航空双卡提醒线 103.5 约 8.6%，暂缓但仍在 95+ 高位不解除预备。
- **消息面**：电报 7 条全 tier4 不进决策链——花旗推迟美联储降息预期至 2027（与昨日沃勒鹰派同向，海外利率收紧主线延续）、非农大超预期后下周美国通胀数据成关键、中东航线运费大幅跳涨、合成橡胶夜盘 +5%。池内公告：恒瑞药品注册批准 + 上市许可受理（连续第二日管线进展）、南航转债到期兑付第二次提示、珀莱转债回售第二次提示、洽洽转债停止交易、电信中期分红实施、神华临时股东会通知、海天回购进展 + H 股中报。大宗：万华 2.32 亿元平价成交（折溢价 -0.004%，中信总部自国泰海通营业部接盘，tier3 静默入库）。
- **宏观**：OIL 95.32 回落、CL 90.83 同步回落；AU0 965.96 续涨（鹰派言论未压回金价）；USDCNY 677.87 / CU0 108780 / LH0 11765 平稳。日历：09-09 CPI/PPI、09-11 社融。
- **下周一清单**：①迈瑞/平安 601318 右侧确认待执行人工决定（距止损 9.5%/6.7%，不跟单也须明确闭环）；②P4 贴边群盯转正——洛钼距 T2 仅 0.1% 最优先，美的/恒瑞次之；③卖区三只人工复核（平安银行贴箱顶 0.1% 最紧）；④油价 95+ 高位，航空双卡锚复核预备不解除；⑤09-09 CPI/PPI 发布前留意。

## 2026-09-04（续③：自算筹码分布落地——migration 0010 + chip_v1_close_tri 全池首算）

- **前置**：存量未提交工作（09-03 基本面/审查修复等 58 文件）先行综合提交 `f711a40`（用户指令；.gitignore 补 data/*.db 杂散忽略）。
- **实施（设计 v2 §9 七步）**：①migration 0010 `chip_distribution`（UNIQUE(symbol,trade_date)，params_json+rule_version+config_hash 三重可审计，不存网格——派生可重建）；②config/indicators.yaml chip 段（A=0.7/k_cap=0.8/peak=close/triangular/n_bins=2000/burn_in=90，⚠️ 待东财交叉验证）；③`scripts/indicators/chip_distribution.py`：纯函数 `compute_chip_series`（right_side.evaluate_segment 模式）+ CLI（单股/--all 单一全局 run_id，DELETE+重插+pipeline_runs stage='chip_distribution' 同事务）；④adjust.py 因子重建结果 notes 加 chip 重算提示（仅提示不耦合事务，评审 #8）；⑤真实库 migrate 0010（纯 CREATE TABLE 未另行备份）→ **全池首算 25558 行/34 只 ok=34**（单 run_id）。
- **实现期抓虫一处**：`_tri_cdf_at` 初版用 np.where 顺序覆盖，支撑区以下（x<a）边缘被 val_left=(x−a)² 误抬致 kernel 和为 −1——改布尔索引分区域赋值修复；golden 测试同时纠正两处测试预期错误（uniform 支撑 F(12)=1.0 非 0.5；送转后 raw 等效成本合法移出当前 raw 区间，不变量改复权域）。
- **测试**：新增 10 项（golden 2 日手算/涨停右三角/一字板点分布/冻结三态/衰减与 cap/除权连续性/次新 burn_in/性质不变量/rule_version 编码/CLI smoke），**537 全绿**。
- **抽查**：①珀莱雅 09-04 winner=0.5468 而现价低于 avg_cost 65.38——成本分布右偏（低位密集+上方长尾）自洽；②turnover_used 与 daily_bars.turnover 全量一致（0 不一致）、NULL 语义一致；③concentration 公式全量自洽；④winner 全池 [0,1] 均值 0.5031；⑤burn_in 精确 3060=34×90。**回归锚**：golden 手算样例锁公式，真实数据锚=首算 run_id=chip_20260904T161228Z 可复算对照。
- **文档**：database_schema（chip_distribution 节+速查）、system_design（§7 表清单）、handoff、本日志。
- **提醒（非本任务）**：①09-04（周五）bars 已随 turnover 回填顺带入库（34 行，因子继承逻辑正常），但正式盘后 daily 未跑——indicators/signals/报告仍截止 09-03，需补跑 `daily --date 2026-09-04`；②报告 §7/UI 展示层接入筹码分布按需另做。

## 2026-09-05（补跑 09-04 盘后 daily：ok=34；发现昨日深夜已有一轮）

- **采集**：快七源（price/forward/index/telegraph/announcement/macro/flow，run_daily_20260904）一条命令 600s 超时但实际全部落盘（price 34/34 含 4 只 sina SSL 瞬断后重试成功记录、announcement **34/34——昨日电信/紫金/海天三只缺口随全窗口重采自动闭合**）；financials 东财 emweb 域 SSL 抽风（与 push2his 同族），三轮补采后 34/34 全齐（fin/fin2/fin3）。
- **daily**：`--date 2026-09-04 --raw-dir data/raw/akshare` ok=34。**发现 report_runs revision=1 已于昨晚 23:46（北京）存在**（run_id=daily_2026-09-04，非本会话产出，推测用户手动跑过）——本次为幂等重跑 revision=2（§9.5），两次均 ok=34 无害。
- **数据完整性**：bars 34/34、000300 至 09-04、macro 13/13（OIL 95.32 五连涨后首度回落、CL 90.83、USDCNY 677.87）。P1/P2 空。
- **决策点（P3，9 只）**：**迈瑞 300760 T1 触发（167.9 区内）+右侧 holding+待执行三连**（昨日距 T1 0.1% 贴边转正，1.5 参数右侧确认首个完整信号）；**西矿 601168 T1 触发**（昨日距上沿 0.3% 转正）；南航 T1 5.03、海油 T1、陕煤 T1、东航 T1+buy_zone 延续；**有友 603697 转 sell_zone（9.93）**、圣农 002299 转 sell_zone、平安银行 sell_zone 延续。P4：洛钼距 T2 0.1%、美的距 T1 0.5%、恒瑞距 T2 0.9%、天赐距 T2 2.2%；**平安 601318"右侧确认待执行"首个提醒**（09-02 confirmed，1.5 参数产物，执行在人）；珀莱雅 08-27 待执行提醒最后两日（09-07 自然过期）。
- **chip_distribution 无需重算**：09-04 bars 前日已随 turnover 回填入库且今日价格无修正，chip 已含 09-04 行。
- **测试**：无代码改动，未跑 pytest。

## 2026-09-05（模拟盘设计方案产出——待外部 Agent 审核）

- **需求澄清（5 问）**：①录入场景=信号触发时录决策（follow/skip/counter）；②成交价=决策日收盘价（系统取，零挑价空间）；③资金=每笔固定名义仓位（隔离判断力与仓位管理）；④退出=信号退出+60 交易日兜底（对称测买卖判断）；⑤载体=CLI+每日报告模拟盘段。
- **产出**：`docs/superpowers/specs/2026-09-05-paper-trading-design.md`。核心框架：**判断力 = 主观组合收益（follow 仓实际盈亏）− 机械基线收益（同决策点集信号即全跟的朴素机械化）**；skip/counter 虚拟评价单列；反作弊五防线（价格系统取/T+1 录入窗口 late 双口径/append-only 冲正/快照冻结/漏录可见）。migration 0011 两表（paper_decisions append-only + paper_positions 状态机）；机械基线自算不依赖 backtest 包（隔离纪律）。
- **自审抓漏**：tier_triggered 连续在区期间每日 triggered=1（南航连四日），不事件化会每天重复生成决策点——已修：状态型信号仅转变日产生决策点 + box 5 日去抖 + open 期间同股新 entry 点仅允许 skip（一股一仓）。
- **给评审的 7 个开放问题**：并发名义无上限/late 窗口长度/box 去抖参数/exit 信号集充分性/skip 评价窗口语义/late 默认口径/命名。通过后按 §9 八步实施（~1100 行含测试）。

## 2026-09-05（续：模拟盘设计 v2——按外部评审修订完毕，可实施）

- **评审**：外部 Agent 结论"设计整体扎实、conditional approve"——三项阻断（B1/B2/B3）、四项重要（I1–I4）、三项非阻断全部采纳，映射表见文档 §0.1。
- **阻断项修复**：①**B1** falsification 事件字段用错——`breached_today=true` 在 watch 态（run<confirm_days）即为真，会产出双决策点且撞"一点一决"唯一键；v2 改为确认日 `state='active' AND triggered=1`（run==confirm_days），entry/exit 同口径。②**B2** 复权因子版本错配——v1 entry 冻结因子 + exit 结算现算，adjust 重建因子后两日 origin 不一致扭曲 ret；v2 改 entry_adj/exit_adj 不落库、结算时按结算时点库内因子两日现取（同版本），"因子版本变化会重算"列为已知偏差声明。③**B3** 停牌处理：信号日必有 bar（信号前提）、结算/timeout 顺延至下一有 bar 日、hold_days 按交易日历含停牌（timeout 口径诚实选择）。
- **重要项修复**：I1 价格列 TEXT 定点（同 executions）；I2 tier 决策点改"当日 state='triggered' 且前日≠triggered"（pending_signals 已算在区，前日状态扫描实现）；I3 box 去抖精确定义"决策点后 5 交易日冷却期"+仅 buy_zone 计 entry；I4 §2.5 引用改 system_design 显式外部引用。
- **评审四问采纳**：结构性 skip 打标 `constraint='single_position'` 统计分层（防一股一仓污染自主 skip）；late 默认含+标注数、恒备剔除口径；新增 **deep_exit 档位深度脱离退出**（zone_low×(1−5%)，防 timeout 静默吸收大多数仓位）；机械基线退出全跟确认；scripts/paper/ docstring 声明与 backtest 边界。
- **自审补漏**：同日多 exit 信号 → 任一 follow 平仓、其余 superseded。
- **状态**：设计 v2 就绪，按 §9 八步实施（~1400 行含测试）。本条为文档修订，无代码/库变更。
