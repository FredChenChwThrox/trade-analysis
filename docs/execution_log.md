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
