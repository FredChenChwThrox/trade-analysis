# 项目交接文档（给其他智能体）

> 一句话：个人股票监测系统。Python/SQLite 确定性管线（采集 → 复权 → 周线 → 指标 → 信号 → 报告），LLM 只在排期卡生成等少数环节消费结构化底稿，不产生规范化数字。
> 读文档顺序：`docs/system_design.md`（设计基线，章节号下文以 § 引用）→ `docs/implementation_plan.md`（D 阶段计划）→ `docs/execution_log.md`（逐次执行记录与偏差决定）→ 本文件。数据库逐表逐字段说明见 `docs/database_schema.md`。

## 1. 环境

- Python ≥3.12，用 **uv** 管理：依赖 `pyproject.toml`（pandas/pyyaml/jsonschema/flask，dev: pytest），锁文件 `uv.lock`。
- 所有命令前缀 `uv run`；包下载失败时走系统代理。
- 测试：`uv run pytest -q`（当前 522 项，全绿才算完成）。
- 数据库：SQLite `data/market.db`（schema `scripts/pipeline/migrations/0001_init.sql`，`scripts/pipeline/db.py` 的 `migrate` 建库）。
- 数据源（2026-08-21 起）：**通达信 tdx-connector 第一优先**（`scripts/adapters/tdx.py`，A 股+港股+指数行情+公告+估值/股本快照，采集规范 `skills/tdx-collect/SKILL.md`）；kimi-datasource 兜底（`stock_finance_data` A 股全量 + `yahoo_finance` 港股/股本/FX，access_token 易失效需 `/login`，公告接口自 8/13 持续 EMPTY_DATA）；tianyancha 公告补采兜底；- **akshare 采集器**（可选数据源，字段对齐现有 adapter 约定，实测通过）：`scripts/collect/akshare_collect.py` + `scripts/adapters/akshare.py`，sources = price/forward/financials/index/telegraph/**forecast/stock_info/announcement**（后三者 2026-08-26 新增：forecast=同花顺盈利预测，stock_info=东财股本结构集团总股本快照，均仅 A 股；announcement=巨潮 cninfo 公告，2026-08-27 补齐采集侧：接口日期参数须紧凑 YYYYMMDD，采集落盘标准公告线格式后经公共引擎薄壳入库，events.source='akshare' 与 tdx dedup 命名空间隔离；与 kimi 源可切换，stock_info 跨源同股本幂等跳过/异股本冲突）；需 `uv sync --extra akshare`；财联社电报→events、财报披露日 NOTICE_DATE 回填、指数全历史、A/H 行情，字段对齐既有 adapter 约定）。接口探测记录见 `docs/probe_20260809_stock_finance_data.md`、`docs/probe_20260815_tianyancha.md`、`docs/probe_20260821_tdx.md`。**港股源已通过 tdx 接入**（setcode=31，0700.HK 在 watchlist 待采集）。

## 2. 目录结构

```
config/           watchlist.yaml、signals.yaml（信号阈值）、indicators.yaml、calendar_*.yaml（交易日历种子）
data/raw/         采集落盘 CSV（按数据源分目录）；data/market.db 为主库（派生可重建）
scripts/
  adapters/       数据源适配器（解析/校验/入库，raw content hash 去重）；
                  announcements.py 公共公告解析引擎（标准线格式，tdx/akshare 薄壳共用）
  collect/        mcp_client.py（插件调用）+ init_collect.py（首次全量采集）
  indicators/     core.py（指标公式，golden tests 锁定口径）+ compute.py（全量重算）
  pipeline/       db/ingest/adjust（复权因子）/weekly/calendar_check/daily（每日管线）
                  card（排期卡 CLI）/execution（执行记录 CLI）/card_inputs（skill 底稿导出）
                  /fundamental_inputs（基本面分析底稿导出，2026-09-03 新增）/report（报告生成）
  signals/        anchors（周线锚点）/exhaustion（衰竭信号 5 项）/weekly_signals（重算入口）
                  daily_watch（日频监测）/right_side（右侧状态机）/accumulation（吸筹形态）
                  cards（卡片加载）/corporate_action（除权处置）
  backtest/       AKQuant 回测子包（2026-08-27 起，与管线完全隔离：自带只读连接，
                  不 import pipeline/adapters/indicators；`uv sync --extra backtest` 后
                  `python -m scripts.backtest.run --symbol <sym>` 单股双均线（Phase 1）、
                  `python -m scripts.backtest.run_multi` 18 只池周频 Top-N 轮动（Phase 2）、
                  `python -m scripts.backtest.run_factor` 三因子横截面评分轮动（Phase 3，
                  momentum/volatility/liquidity，引擎外预计算注入）、
                  `python -m scripts.backtest.run_event` 排期卡择时层忠实机械化
                  （衰竭同锚≥2 入场 + decline_start 机械证伪线，Phase A）；
                  配置 config/backtest.yaml；仅打印不落库；结果仅为链路验证非策略结论。
                  2026-08-27 amount 已全量回填（sina 源 15 只 10896 行，42 个起点边界行
                  与港股未补，见执行日志），因子覆盖率 18/18。
tests/            161 项；test_indicators.py 是 golden tests（公式边界锁定）
cards/{symbol}/   排期卡存档（current.md、版本归档、inputs_*.json 底稿）
reports/{symbol}/ 单股报告；reports/daily/ 全池日报
docs/             设计/计划/执行日志/本文件
skills/           排期卡 skill / 消息打标 skill / 基本面分析 skill（均 draft-only）等
```

## 3. 常用命令

```bash
# 每日盘后（先采集增量 CSV 到某目录，再跑管线）
uv run python -m scripts.pipeline.daily --date 2026-08-10 --raw-dir <本批新采目录>
uv run python -m scripts.pipeline.daily status 603605.SH        # 个股近 5 日状态

# 单独重跑某环节（全量重算、幂等）
uv run python -m scripts.pipeline.weekly <symbol>
uv run python -m scripts.indicators.compute <symbol>
uv run python -m scripts.signals.weekly_signals <symbol>
uv run python -m scripts.signals.daily_watch <symbol> [--as-of D]
uv run python -m scripts.signals.right_side <symbol> [--as-of D]
uv run python -m scripts.signals.accumulation <symbol> [--as-of D]

# 报告
uv run python -m scripts.pipeline.report --date 2026-08-10 [--symbol 603605.SH]

# 排期卡（skill 产 draft JSON → 入库 → 人工激活；skill 只做 draft）
uv run python -m scripts.pipeline.card_inputs <symbol>          # 导出底稿 cards/{symbol}/inputs_*.json
uv run python -m scripts.pipeline.card create-draft <json>
uv run python -m scripts.pipeline.card activate <card_version_id> --effective-from <date>
uv run python -m scripts.pipeline.card reject <card_version_id>

# 执行记录（正常 / 补录历史手工单）
uv run python -m scripts.pipeline.execution add ... [--backfill --note "..."]

# 基本面分析底稿（fundamental-analysis-skill 主输入，2026-09-03 新增）
uv run python -m scripts.pipeline.fundamental_inputs <symbol>   # → reports/{symbol}/fundamental_inputs_*.json
# 财务三表补采（手触发，不进 daily 默认 sources；仅 A 股）
uv run python -m scripts.collect.akshare_collect --sources balance_sheet,cash_flow,fin_abstract \
    --date <日期> --run-id <run_id>   # 然后 ingest 对应三个目录

# 只读 Web UI（第一期，docs/ui_design_phase1.md）
uv run python -m scripts.ui.app                    # http://127.0.0.1:5000/，/health 探活
uv run python -m scripts.ui.app --port 5001        # 覆盖 host/port
# 页面：/ /stocks /stock/{symbol} /indicators /signals /compare /cards /runs；筛选条件随 URL 保持。
# 前端模板 scripts/ui/templates/，JS scripts/ui/static/js/（无构建链，Tailwind/ECharts CDN）。

# akshare 可选源（字段对齐现有 adapter 约定；先 uv sync --extra akshare）
uv run python -m scripts.collect.akshare_collect \
    --symbols 603605.SH --indexes 000300.SH,^HSI \
    --sources price,financials,index,telegraph,announcement --date 2026-08-26 --run-id run_ak
# announcement（cninfo 公告）2026-08-28 起进默认 sources（r2 Phase 1）；日历源手触发：
# uv run python -m scripts.collect.akshare_collect --sources calendar \
#     --date 2026-08-28 --run-id run_calendar --calendar-period 2026半年报
# （期次必填不做默认推断；--sources calendar 落盘 calendar/{date}/{run_id}/ 后
#  `uv run python -m scripts.pipeline.ingest data/raw/akshare/calendar` 入库 event_calendar）
# forward=qfq 前复权（{symbol}_forward.csv，供 adjust 用，ingest 自动跳过）；
# --price-api sina 切新浪备用源（A 股专用，东财 push2his 不可达时用）
uv run python -m scripts.pipeline.ingest data/raw/akshare/{financials,telegraph,index}
```

## 4. 关键约定（违反会出错的点）

- **口径纪律（§4.1/§5.4）**：指标与周线信号用复权价（raw × price_adj_factor）与调整量（volume_raw ÷ share_factor）；排期卡价区/证伪线/箱体/现价用不复权。两边比较前必须折回（÷当日因子），报告指标快照已带折回行。
- **不猜（§2.5）**：关键数据缺失输出 `incomplete/degraded` 及原因码，禁止伪装成"条件满足"。无 active 卡的股票卡片相关信号不产出。
- **无未来函数**：信号逐日/逐周只用当时可见数据（均量基数 shift(1)，样本不足不判定）。
- **派生表重算**：DELETE + 重插 + run 记录（pipeline_runs，含 config_hash/rule_version）同事务；每日管线单股原子，单信号模块异常记 degraded 不拖垮其余。
- **参数待核对**：signals.yaml 中锚点/衰竭/吸筹参数是第一版默认值，需人工核对数周后才可调整（文档与报告均带 ⚠️ 标注）。
- **LLM 边界**：LLM 只消费 card_inputs 底稿，产出排期卡 draft；activate/reject 必须人工。规范化数字一律 Python 计算。
- **文档同步**：任何改动必须追加 `docs/execution_log.md`；设计变更同步 `docs/system_design.md`。

## 5. 当前状态（2026-08-10）

- 进度：D0–D2 全部完成；D3.3 首张真实排期卡已激活（603605.SH 珀莱雅 `603605SH_120ca661`，effective 2026-08-10，next_review 2026-08-31）；**D3.4 数周并行观察期进行中**；D3 消息评价（LLM 事件研究）未做。**UI 第一期全部任务完成（docs/tasks/ 00–11），262 项测试全绿**。
- watchlist 6 只：603605.SH（唯一有卡，4 笔历史波段已补录 executions #1–#4）、603288.SH、601318.SH、002747.SZ、601899.SH、600029.SH（各 3 年日线+周线+指标+衰竭信号齐全，无卡，日报卡片项 incomplete 属预期）。
- 已知缺口：① 港股日历未填充、港股源未接；② ~~公告接口（get_stock_announcement）返回空未验证~~；③ ~~财报披露时间缺失~~（2026-08-26 起已由 akshare NOTICE_DATE 通道补齐：tdx.parse_financials_csv 内容一致时自动回填 NULL published_at，2023 年以来最新 revision 全齐；平安银行 1990-92 三期远古年报无源保持降级标）；④ 吸筹形态参数待人工核对（珀莱雅近期两次 -5% 以上大跌量比 1.4–1.6 未达 2.0 阈值未触发）；⑤ 换手率/股东人数（筹码集中度间接指标）未采集；⑥ 筹码分布数据无源（数据源无接口，评估见执行日志）；⑦ corporate_actions 12 只空（复权因子由前复权价比值重建，平台切换日未交叉印证）、万华 forecasts 缺、~~法拉/万华公告簿偏薄~~（2026-08-27 已用 akshare cninfo 通道回填：法拉 +64 / 万华 +120 条，与存量 tdx 簿双源并存，事件级按源命名空间隔离；详见执行日志）。
- 下一例行事项：2026-08-10（周一）盘后首个正式 daily（先采集增量再 `--raw-dir`）。
- （2026-08-23 补记）601168.SH 西部矿业已入池（watchlist 14 只）：3 年日线/周线/指标/信号齐全，14 期财报披露日已 pit_backfill 回填（点时口径）；排期卡 draft `601168SH_cc4c2ac7` 待人工激活（next_review 2026-10-31）。详见执行日志 2026-08-23 节。
- （2026-08-26 补记）akshare 缺口补齐：全池财报全历史入库（期次 2023Q3 起 12 期全覆盖，published_at 66 行回填，16 只转严格点时口径）；法拉/万华头部 23 个交易日补齐（sina 源），因子重建为平台段口径（4/5 段），周线 153/指标 725/信号已重算。测试 389 全绿。详见执行日志 2026-08-26（akshare 缺口补齐）节。
- （2026-08-26 补记②）akshare 采集器新增 forecast/stock_info 两源（与 kimi 可切换，跨源幂等/冲突语义见执行日志）；603993.SH 洛阳钼业股本快照（akshare 集团总股本 213.94 亿股，与 kimi 一致）+ 双源一致预期入库，pe_ttm 726/726 转正；**排期卡 `603993SH_67042523` 已激活**（effective 2026-08-27，next_review 2026-10-31，三档 20.70–21.56 / 18.49–19.46 / 13.53–14.61，证伪线 13.53；现价 19.59 已掠过第一档、贴第二档上沿但信号 0 项不释放）。测试 399 全绿。详见执行日志 2026-08-26（akshare 两新源 + 洛阳钼业）节。
- （2026-08-26 补记③）盘后 daily ok=18。修复 601899 紫金 raw 污染（昨日 kimi `batch2_forward.csv` 批量命名前复权文件被 daily `other` 循环误当 price 入库，5 行 close_raw 被覆盖）并重做因子（7 平台段，补 2026-08-21 中期分红切换）；daily.py `other` 循环加 forward 防御跳过 + 回归测试，测试 400 全绿。注意：`--raw-dir` 单值参数，多源目录传公共父目录（如 `data/raw/akshare`），不要重复传参。详见执行日志 2026-08-26（盘后 daily）节。
- （2026-08-27 补记④）盘后 daily ok=18（18 只 A 股当日 bar 齐全）。603993 洛钼因除权落入检查窗触发防御性因子重建 v31，周线/指标/信号已重算；000300.SH 增至当日、^HSI 仍滞后一日（停在 08-26）。**踩坑**：akshare_collect 的 `--end` 默认硬编码且不随日期更新，每日增量必须显式 `--end <当日>`，否则静默截至今日前一日。3 只 degraded 均 no_active_card 属预期。详见执行日志 2026-08-27（盘后 daily）节。
- （2026-08-27 补记⑤）入池美的集团 000333.SZ / 格力电器 000651.SZ（watchlist 20 只）：四源采集（price/forward/financials/announcement）+ ingest 2485 行 + daily 首跑 ok=20，报告 degraded（no_active_card）属预期。两股 pe_ttm 已补（stock_info 股本快照 + 全量重算：美的 14.88 / 格力 7.54；单快照全局应用，PE 历史近似）；格力 forecast 已入，美的同花顺接口空留缺口。同日：公告解析引擎下沉 adapters/announcements.py 公共层；海天/法拉/万华/洛钼公告簿已回填；平安执行台账 #13–#17 补录。详见执行日志 2026-08-27 各节。
- （2026-08-28 补记⑥）入池中国神华 601088.SH / 长江电力 600900.SH / 陕西煤业 601225.SH（watchlist 23 只）：六源采集 + daily 首跑 ok=23，无卡期 degraded 属预期。两个踩坑：①东财 push2his 代理断连致 price 静默丢源，改 `--price-api sina` 重采（新入池首采后必须核对各源 CSV 实际落盘）；②新入池时 stock_info 需等价格入库后单独回补（否则股本快照校验失败回滚整股阶段）。神华 2025-08 重组停牌 10 日无 bar 属正常。恒力石化同日完成首期卡复核并激活 600346SH_9b168869（effective 2026-08-31，next_review 2026-10-31）。详见执行日志 2026-08-28 各节。
- （2026-08-28 补记⑦）三连卡 draft 入库（待人工激活）：神华 601088SH_19b6dcd0 / 长电 600900SH_a8c0c9a4 / 陕煤 601225SH_5a669742，均 next_review 2026-10-31。锚选择：神华/长电类债股息率主锚（PE 机器层映射）、陕煤强周期（PB 缺口=人工项）。共同缺口：系统无分红/PB 序列（corporate_actions 空），股息率/PB 精确核对=激活前人工项；神华/长电中报未披露=激活后立即复核触发器。详见执行日志 2026-08-28（续③）。
- （2026-08-28 补记⑧）三连卡激活前立即复核完成（一次性 akshare 探测+公告簿核对，不入管线）：神华股息率锚（2025 DPS 2.01/分红率 75.6%，T1 区 4.6%+）、长电分红承诺续期确认（2026-2030 规划公告在公告簿，原最大缺口闭合；T1 区 3.65-3.80%）、陕煤底部 PB 带 1.9-2.1x 全部通过；档价/矩阵零改动。draft 换版：601088SH_ca2eab78 / 600900SH_4988e3fb / 601225SH_a0f29c77（取代初版，待人工激活）。教训：分红承诺类核对先查公告簿再外探。详见执行日志 2026-08-28（续③④）。
- （2026-08-28 补记⑨）**消息面研判 r2 Phase 1 完成**（设计 `docs/superpowers/specs/2026-08-28-message-eval-design-r2.md` §13，交接 `...-phase1-handoff.md`；做完即停等人工 review）：migration 0003（event_calendar + events.scope/source_tier + watchlist.industry_code/themes_json，真实库已迁移、备份 `data/market.db.bak_20260828`）；公告（cninfo）进 akshare 采集默认 sources 且事件带 tier=1（电报=4；tyc/kimi 历史公告路径 NULL=未分级）；报告新增"## 5. 日历与消息面"（原 5/6/7 段顺延为 6/7/8，快照加 calendar_due 计数）；/cards 顶部日历横幅。**真实库 event_calendar 已激活（36 行）**：手工种子 6 条（FOMC 精确 + CPI/社融/LPR 惯例预填待官方确认，用户已核执行 `db seed`）+ 半年报批次采集 30 条（披露预约 23 + 解禁 7，用户批准入库；近窗提醒=08-29 美的/南航/神华半年报、08-31 长电）；2026三季预约源侧为空，9 月底补采 `--calendar-period 2026三季`。industry_code 23 只经 push2delay 反查回填（东财细分行业 BK 码）。Phase 2–4 未动。
- （2026-08-28 补记⑩）**消息面 r2 Phase 2 完成**（无 LLM；做完即停等人工 review）：migration 0004 macro_factors（商品/外汇每日快照，真实库已迁移）；akshare 采集默认 sources 新增 **macro**（config/macro_factors.yaml 清单驱动：内盘期货 AU0/CU0/I0/RB0/SR0/SC0 + 外盘 OIL/CL + 中行牌价 USDCNY/HKDCNY/EURCNY，全部 sina 系域名直连可达，close 存原始值不换算）与 **flow**（龙虎榜按股×日合并多上榜原因 + 大宗每笔一行，仅 watchlist 行；查询窗口硬上限 10 天）。flow 事件入 events（event_type='flow'、scope='flow'、tier=3 聚合加工视图）**静默入库，不推送不进日报**，报告零改动。首采实测：macro 11/11 因子全实值（08-28 收盘）；flow 当日 23 只无龙虎榜上榜（正常）、大宗命中紫金两笔。两融（粒度未定）与热度榜（tier 5）明确缓办。测试 463 全绿（+7）。
- （2026-08-29 补记⑪）入池联影医疗 688271.SH / 迈瑞医疗 300760.SZ（watchlist 25 只）：六源采集 + daily 首跑 ok=25，无卡期 degraded 属预期。行业码均为 BK1605 医疗设备（symbol_industry 反查）。新坑：cninfo 公告接口两度 JSONDecodeError，手打接口核实为 502 Bad Gateway（tengine 上游故障，非参数/风控），约十分钟后第三次重试自愈。两股财报全历史 25/40 期且 published_at 全非空（akshare 直回）；pe_ttm（snapshot_group_total 口径）最新 49.20 / 25.41。详见执行日志 2026-08-29 节。
- （2026-08-29 补记⑬）入池油气三巨头+电信双龙头（watchlist 30 只）：601857.SH 中石油/600028.SH 中石化（BK1569）/600938.SH 海油（BK1574）/600941.SH 移动/601728.SH 电信（BK1587），六源采集 + daily 首跑 ok=30 + stock_info 回补。pe_ttm 12.99/18.36/11.74/16.10/19.37。**五张排期卡已激活**（用户指令，effective 2026-08-31，next_review 2026-10-31，全池 active 30 张）：601857SH_6ec22096 / 600028SH_60fd1681 / 600938SH_f51971c7 / 600941SH_0f926a39 / 601728SH_2cf61fc0。缺口：海油/移动 forecast EMPTY；OIL 因子读数 missing（08-28 起数待积累）；BK1574/BK1587 无因子映射；4 只衰竭锚滞后（2026-06-29~07-02 普跌新低未入锚，卡内 front_low 人工记录）；中石化/移动/电信股息率/PB 锚核对=激活前人工项。踩坑：factor_assumptions 须放卡片 JSON 的 earnings 内（根级 earnings_scenarios 被 schema 拒），SKILL.md 已修正。测试 486 全绿。详见执行日志 2026-08-29（续⑭）节。
- （2026-08-30 补记⑭）入池创新药双龙头+CXO 龙头（watchlist 33 只）：688235.SH 百济神州/600276.SH 恒瑞医药（BK1594 化学制剂）/603259.SH 药明康德（BK1600 医疗研发外包），五源采集（push2his 断连改 sina 重采，已知坑）+ daily 首跑 ok=33 + stock_info 回补 + 指标重算 pe_ttm 全非空（08-28：百济 97.87 / 恒瑞 40.65 / 药明 21.77；百济中报入库致 TTM 上修）。缺口：百济/药明 forecast EMPTY（同花顺接口）；三股无卡，日报 degraded 属预期。测试 493 全绿。详见执行日志 2026-08-30（续②）节。
- （2026-08-31 补记⑱）**收尾清理：全池 33 只 active 卡全覆盖**。①有友卡复核通过后续期：同值 refresh draft 603697SH_e86a63ae 激活（effective 2026-09-01，next_review 2026-10-31）；②药明/恒瑞/百济三张 08-30 draft 经用户确认**激活**（603259SH_9681230e / 600276SH_438f17da / 688235SH_f820a833，effective 2026-09-01）——此前"3 张遗留 draft 待 reject"表述有误（实为有效新卡，恒力初稿早已清理）。draft 清零，09-01 起日报 no_active_card 降级归零。注意：本日志更早的"3 张待 reject"勿再当作待办。
- （2026-08-31 补记⑰）**11 张锚过期卡批量重建并激活**：11 张 v2 draft 入库（珀莱雅 9d997d38 / 南航 a2fc672e / 天赐 a2e04513 / 牧原 c0e31adf / 圣农 387e8e84 / 洽洽 a6dd769e / 埃斯顿 6fd732f1 / 豫光 6e36e4af / 平安 153b6efc / 紫金 616cc92c / 海天 fb3699b8），用户确认**全部激活，effective 2026-09-01，next_review 2026-10-31**；旧 8/10-8/14 批次 11 张 superseded（effective_to=2026-09-01 排他，08-31 当日信号仍按旧卡，无空档）。全池 active 30 张。要点：平安/紫金/豫光三张档位结构大变（基数错位修正/深熊刻度回归/锚上移）；南航 T1 贴价但 v2 口径 Kelly=0；珀莱雅右侧沿用 61.00/59.50。09-01 盘后注意事项见执行日志 2026-08-31（续②）节。
- （2026-08-31 补记⑯）盘后 daily ok=33（revision=4）+ 12 张到期卡批量审核完成：**11 张锚过期需重建**（卡生效后新披露 2026 中报：珀莱雅/南航/天赐/牧原/圣农/洽洽/埃斯顿/豫光/平安/紫金/海天），**有友复核通过复用**（T1+3 信号触发有效）。重建优先级与中报预期差详表见执行日志 2026-08-31 节——珀莱雅右侧 holding 距止损仅 1.4% 为最高优先（人工决策）。**新坑**：`akshare_collect --start` 默认 2023-08-10，老批次 6 只因子 origin=2023-08-09 早于窗口 → daily 首跑 failed=6（origin 校验防御性回滚），forward 显式 `--start 2023-08-01` 重采后 ok。缺口：31 只 08-29~08-31 公告簿增量（cninfo 抽风，明日回补）。测试未跑（无代码改动）。
- （2026-08-31 补记⑮）**code/skill 审查修复完成**（交接 `docs/superpowers/specs/2026-08-31-code-skill-review-handoff.md`）：10 项代码缺陷 + 3 项 skill 文档全部修复——right_side 非法状态护栏/报告 holding 决策点；factor_watch f-string SQL 参数化 + market 批量查询 + mtime 缓存；人审页 symbol_names 降级 + confirm-all SQL 过滤 + data-symbols 分隔符；followup LEFT JOIN 聚合；card_detail anchorIsPe 三态。测试 500 全绿。详见执行日志 2026-08-31 节。
- （2026-09-04 补记⑱）**换手率 + 股东户数落地（§5.7 缺口闭合 2/3，用户指令）**：①migration 0008 `daily_bars.turnover`（小数口径，sina 原生/东财百分点归一；派生快照元数据不记 revisions），0009 新表 `holder_stats`（UNIQUE(symbol,stat_date)，announced_at=PIT 可见日；真实库先备份 `market.db.bak_20260904`）；②akshare price 三路径写 turnover 列 + 新手触发源 `gdhs`（`--sources gdhs`，不进 daily 默认；ingest 路由 ("akshare","gdhs")）；③回填 turnover **25524/25524 CN bars 100%**、holder_stats **1862 行/30 只**（2013 起）；**缺口**：紫金/南航/洛钼/东航四只东财源侧无数据（curl 手验"返回数据为空"，tdx gdrs 备援）、筹码分布 cyq 仍被 push2his 阻断缓办；④测试 +5 = **527 全绿**。未做：展示层接入、4 只他源补采。详见执行日志 2026-09-04 节。
- （2026-09-03 补记⑰）**daily ok=34 一次通过 + right_side vol_multiple 2.0→1.5**（用户指令）：全池参数回放显示 2.0 漏接平安/迈瑞"1.5 倍量过线"突破、1.4 会放入埃斯顿 −14.5% 假突破，取 1.5；测试边界断言配置相对化，522 全绿；全池重放后平安/迈瑞 09-02 confirmed（新增"右侧持仓跟踪+待执行"决策点，执行在人）；报告 revision=2。另：002747.SZ 是**埃斯顿**不是晨光（晨光文具 603899 池外）。公告簿 3 只缺口（电信/紫金/海天 cninfo 抽风）待回补；OIL 布伦特 96.68 五日连涨逼近航空双卡提醒线 103.5。详见执行日志 2026-09-03 各节。
- （2026-09-03 补记⑯）**基本面深度分析落地**（起因：用户"华尔街式 8 段提示词"改造需求）：①migration 0007 三表（balance_sheet_facts / cash_flow_facts 挂 financial_reports.report_id + financial_indicator_snapshots 存 THS 摘要快照），真实库已迁移（备份 `market.db.bak_20260903`）；②akshare 新增手触发源 balance_sheet/cash_flow/fin_abstract（sina 全历史+THS 摘要，仅 A 股，**不进 daily 默认 sources**），全池 34 只回填 BS 2487/CF 2463/快照 2538 行零冲突；③新导出器 `scripts/pipeline/fundamental_inputs.py`（八段底稿，派生指标全 Python 算，隐含回报区间表替代 DCF）+ 新 skill `skills/fundamental-analysis-skill/`（draft-only 三模块：事实解读→定性研判→对抗呈现，不进日报/信号链）；④首单 603605 全链路验证，draft 在 `reports/603605.SH/fundamental_2026-09-02_draft.md` 待人工定稿。第二单 601318 平安 draft 同日产出（`reports/601318.SH/fundamental_2026-09-03_draft.md`：盈利超预期 vs 估值刻度上沿张力、PE 锚对保险业适配弱待 EV 重锚）。测试 522 全绿。缺口：0700.HK 无源；603993 2008 年报对账超差 1 处待人工核；**注意真实库 financial_reports 实际覆盖 2013 起全历史**（此前"2023Q3 起 12 期"仅指 published_at 点时口径）。详见执行日志 2026-09-03 节与 probe 文档。

## 6. 常见任务怎么做

- **加跟踪股票**：config/watchlist.yaml 加行 → 采集 3 年日线（init_collect 或 mcp_client）→ adjust/weekly/compute/weekly_signals 逐个跑或直接跑 daily --raw-dir → 无卡期间日报 degraded(no_active_card) 属预期。
- **改信号阈值**：config/signals.yaml（defaults；overrides 第一版不用）→ 重跑对应信号模块 → config_hash 随事实入库可追溯。锚点/衰竭/吸筹参数先过人工核对期。
- **加新信号**：照 `scripts/signals/accumulation.py` 模式（每日一行 signal_facts + DELETE 重插 + pipeline_runs 记录 + CLI），接入 daily.py 信号链，report.py 加展示，配 tests。
- **排查数据疑问**：先看报告"8. 来源与异常"段与 signal_facts.details_json（含阈值/原值/原因码），再核对口径（复权 vs 不复权）。
- **录入执行记录（executions）前**：先完成当日采集 + daily，确保信号快照截止日不早于 T-1（2026-08-30 恒力止损复盘教训：8-25 两笔执行用的是 8-21 的冻结快照）。右侧确认/止损触发后及时录入执行，否则报告会持续列"待执行"提醒。
- **Bugfix 接力**：`docs/superpowers/specs/2026-08-31-code-skill-review-handoff.md` 汇总了最近一次代码审查与 skill 审查的 10 项代码缺陷 + 3 项 skill 文档滞后，按任务拆分建议可并行分配。
