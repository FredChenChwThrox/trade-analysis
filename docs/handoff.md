# 项目交接文档（给其他智能体）

> 一句话：个人股票监测系统。Python/SQLite 确定性管线（采集 → 复权 → 周线 → 指标 → 信号 → 报告），LLM 只在排期卡生成等少数环节消费结构化底稿，不产生规范化数字。
> 读文档顺序：`docs/system_design.md`（设计基线，章节号下文以 § 引用）→ `docs/implementation_plan.md`（D 阶段计划）→ `docs/execution_log.md`（逐次执行记录与偏差决定）→ 本文件。数据库逐表逐字段说明见 `docs/database_schema.md`。

## 1. 环境

- Python ≥3.12，用 **uv** 管理：依赖 `pyproject.toml`（pandas/pyyaml/jsonschema/flask，dev: pytest），锁文件 `uv.lock`。
- 所有命令前缀 `uv run`；包下载失败时走系统代理。
- 测试：`uv run pytest -q`（当前 389 项，全绿才算完成）。
- 数据库：SQLite `data/market.db`（schema `scripts/pipeline/migrations/0001_init.sql`，`scripts/pipeline/db.py` 的 `migrate` 建库）。
- 数据源（2026-08-21 起）：**通达信 tdx-connector 第一优先**（`scripts/adapters/tdx.py`，A 股+港股+指数行情+公告+估值/股本快照，采集规范 `skills/tdx-collect/SKILL.md`）；kimi-datasource 兜底（`stock_finance_data` A 股全量 + `yahoo_finance` 港股/股本/FX，access_token 易失效需 `/login`，公告接口自 8/13 持续 EMPTY_DATA）；tianyancha 公告补采兜底；- **akshare 采集器**（可选数据源，字段对齐现有 adapter 约定，实测通过）：`scripts/collect/akshare_collect.py` + `scripts/adapters/akshare.py`，sources = price/forward/financials/index/telegraph/**forecast/stock_info**（后两者 2026-08-26 新增：forecast=同花顺盈利预测，stock_info=东财股本结构集团总股本快照，均仅 A 股；与 kimi 源可切换，stock_info 跨源同股本幂等跳过/异股本冲突）；需 `uv sync --extra akshare`；财联社电报→events、财报披露日 NOTICE_DATE 回填、指数全历史、A/H 行情，字段对齐既有 adapter 约定）。接口探测记录见 `docs/probe_20260809_stock_finance_data.md`、`docs/probe_20260815_tianyancha.md`、`docs/probe_20260821_tdx.md`。**港股源已通过 tdx 接入**（setcode=31，0700.HK 在 watchlist 待采集）。

## 2. 目录结构

```
config/           watchlist.yaml、signals.yaml（信号阈值）、indicators.yaml、calendar_*.yaml（交易日历种子）
data/raw/         采集落盘 CSV（按数据源分目录）；data/market.db 为主库（派生可重建）
scripts/
  adapters/       数据源适配器（解析/校验/入库，raw content hash 去重）
  collect/        mcp_client.py（插件调用）+ init_collect.py（首次全量采集）
  indicators/     core.py（指标公式，golden tests 锁定口径）+ compute.py（全量重算）
  pipeline/       db/ingest/adjust（复权因子）/weekly/calendar_check/daily（每日管线）
                  card（排期卡 CLI）/execution（执行记录 CLI）/card_inputs（skill 底稿导出）/report（报告生成）
  signals/        anchors（周线锚点）/exhaustion（衰竭信号 5 项）/weekly_signals（重算入口）
                  daily_watch（日频监测）/right_side（右侧状态机）/accumulation（吸筹形态）
                  cards（卡片加载）/corporate_action（除权处置）
tests/            161 项；test_indicators.py 是 golden tests（公式边界锁定）
cards/{symbol}/   排期卡存档（current.md、版本归档、inputs_*.json 底稿）
reports/{symbol}/ 单股报告；reports/daily/ 全池日报
docs/             设计/计划/执行日志/本文件
skills/           排期卡 skill（draft-only）等
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

# 只读 Web UI（第一期，docs/ui_design_phase1.md）
uv run python -m scripts.ui.app                    # http://127.0.0.1:5000/，/health 探活
uv run python -m scripts.ui.app --port 5001        # 覆盖 host/port
# 页面：/ /stocks /stock/{symbol} /indicators /signals /compare /cards /runs；筛选条件随 URL 保持。
# 前端模板 scripts/ui/templates/，JS scripts/ui/static/js/（无构建链，Tailwind/ECharts CDN）。

# akshare 可选源（字段对齐现有 adapter 约定；先 uv sync --extra akshare）
uv run python -m scripts.collect.akshare_collect \
    --symbols 603605.SH --indexes 000300.SH,^HSI \
    --sources price,financials,index,telegraph --date 2026-08-26 --run-id run_ak
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
- 已知缺口：① 港股日历未填充、港股源未接；② ~~公告接口（get_stock_announcement）返回空未验证~~；③ ~~财报披露时间缺失~~（2026-08-26 起已由 akshare NOTICE_DATE 通道补齐：tdx.parse_financials_csv 内容一致时自动回填 NULL published_at，2023 年以来最新 revision 全齐；平安银行 1990-92 三期远古年报无源保持降级标）；④ 吸筹形态参数待人工核对（珀莱雅近期两次 -5% 以上大跌量比 1.4–1.6 未达 2.0 阈值未触发）；⑤ 换手率/股东人数（筹码集中度间接指标）未采集；⑥ 筹码分布数据无源（数据源无接口，评估见执行日志）；⑦ corporate_actions 12 只空（复权因子由前复权价比值重建，平台切换日未交叉印证）、万华 forecasts 缺、法拉/万华公告簿偏薄。
- 下一例行事项：2026-08-10（周一）盘后首个正式 daily（先采集增量再 `--raw-dir`）。
- （2026-08-23 补记）601168.SH 西部矿业已入池（watchlist 14 只）：3 年日线/周线/指标/信号齐全，14 期财报披露日已 pit_backfill 回填（点时口径）；排期卡 draft `601168SH_cc4c2ac7` 待人工激活（next_review 2026-10-31）。详见执行日志 2026-08-23 节。
- （2026-08-26 补记）akshare 缺口补齐：全池财报全历史入库（期次 2023Q3 起 12 期全覆盖，published_at 66 行回填，16 只转严格点时口径）；法拉/万华头部 23 个交易日补齐（sina 源），因子重建为平台段口径（4/5 段），周线 153/指标 725/信号已重算。测试 389 全绿。详见执行日志 2026-08-26（akshare 缺口补齐）节。
- （2026-08-26 补记②）akshare 采集器新增 forecast/stock_info 两源（与 kimi 可切换，跨源幂等/冲突语义见执行日志）；603993.SH 洛阳钼业股本快照（akshare 集团总股本 213.94 亿股，与 kimi 一致）+ 双源一致预期入库，pe_ttm 726/726 转正；**排期卡 `603993SH_67042523` 已激活**（effective 2026-08-27，next_review 2026-10-31，三档 20.70–21.56 / 18.49–19.46 / 13.53–14.61，证伪线 13.53；现价 19.59 已掠过第一档、贴第二档上沿但信号 0 项不释放）。测试 399 全绿。详见执行日志 2026-08-26（akshare 两新源 + 洛阳钼业）节。
- （2026-08-26 补记③）盘后 daily ok=18。修复 601899 紫金 raw 污染（昨日 kimi `batch2_forward.csv` 批量命名前复权文件被 daily `other` 循环误当 price 入库，5 行 close_raw 被覆盖）并重做因子（7 平台段，补 2026-08-21 中期分红切换）；daily.py `other` 循环加 forward 防御跳过 + 回归测试，测试 400 全绿。注意：`--raw-dir` 单值参数，多源目录传公共父目录（如 `data/raw/akshare`），不要重复传参。详见执行日志 2026-08-26（盘后 daily）节。

## 6. 常见任务怎么做

- **加跟踪股票**：config/watchlist.yaml 加行 → 采集 3 年日线（init_collect 或 mcp_client）→ adjust/weekly/compute/weekly_signals 逐个跑或直接跑 daily --raw-dir → 无卡期间日报 degraded(no_active_card) 属预期。
- **改信号阈值**：config/signals.yaml（defaults；overrides 第一版不用）→ 重跑对应信号模块 → config_hash 随事实入库可追溯。锚点/衰竭/吸筹参数先过人工核对期。
- **加新信号**：照 `scripts/signals/accumulation.py` 模式（每日一行 signal_facts + DELETE 重插 + pipeline_runs 记录 + CLI），接入 daily.py 信号链，report.py 加展示，配 tests。
- **排查数据疑问**：先看报告"7. 来源与异常"段与 signal_facts.details_json（含阈值/原值/原因码），再核对口径（复权 vs 不复权）。
