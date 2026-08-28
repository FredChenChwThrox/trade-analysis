# 交接：消息面研判 r2 · Phase 1 实现（日历层 + 公司公告）

> 目标读者：接手实现的 Agent。你不需要读任何会话历史，本文件 + 列出的项目文档足够。
> 任务范围：**只做 Phase 1**。Phase 2–4（macro_factors / flow 采集 / LLM 评价链 / judgments）不在本次范围，做完 Phase 1 即停，等人工 review。
> 设计依据：`docs/superpowers/specs/2026-08-28-message-eval-design-r2.md`（下称 r2，§13 落地顺序 Phase 1）。

## 0. 项目速览

个人 A 股监测系统：Python/SQLite 确定性管线 + LLM 受限参与。23 只 watchlist，每日盘后跑 `daily` 流水线生成单股报告与全池日报；Flask Web UI 展示排期卡。当前消息面能力为零（events 表已有公告/电报入库，但无日历、无评价、报告无消息面段）。

## 1. 必读文档（按序）

1. `AGENTS.md`（项目根）——硬性纪律，必须先读。
2. `docs/handoff.md`——环境、命令、当前状态。
3. `docs/superpowers/specs/2026-08-28-message-eval-design-r2.md`——本任务的设计稿，重点读 §2（分层与信源分级）、§3.1（Phase 1 迁移）、§4（采集）、§8.1（报告段）、§12（测试）、§13（Phase 1 范围）、§14（验收）。
4. `docs/database_schema.md` §6（events / event_symbols / event_assessments 现状）。
5. `docs/system_design.md` §2.1 / §2.5 / §3.6 / §5.5 / §8.1（纪律原文）。

## 2. 环境与命令

- 包管理用 uv，**所有 Python 命令一律 `uv run` 前缀**；包下载失败走系统代理。
- 测试：`uv run pytest -q`，**全绿才算完成**（当前 443 项）。
- 迁移：`uv run python -m scripts.pipeline.db migrate`；迁移文件落 `scripts/pipeline/migrations/NNNN_name.sql` 即被自动发现（`db.py:41 migrate()`，按文件名前缀数字排序，`schema_migrations` 表记录版本，单事务执行失败整体回滚）。
- 不要动 `data/market.db` 以外的用户数据；不要执行任何 git 变更（commit/push 等），除非用户明确要求。

## 3. Phase 1 任务分解

### 3.1 migration 0003（`scripts/pipeline/migrations/0003_message_calendar.sql`）

按 r2 §3.1 执行，四件事：

```sql
CREATE TABLE event_calendar ( ... );   -- 字段照 r2 §3.1，主键 cal_id
ALTER TABLE events ADD COLUMN scope TEXT;
ALTER TABLE events ADD COLUMN source_tier INTEGER;
ALTER TABLE watchlist ADD COLUMN industry_code TEXT;
ALTER TABLE watchlist ADD COLUMN themes_json TEXT;
```

注意：
- 现有迁移只有 0001/0002 两个文件，命名沿用 `NNNN_描述.sql` 风格。
- `events.scope` 取值域 macro/policy/industry/company/flow，Phase 1 只建列不填充（telegraph/公告的 scope 分类属 Phase 2/3）；`source_tier` 同理建列，但**公告入库路径本次就要写 tier=1**（见 3.3）。
- `docs/database_schema.md` 必须同步新增表/列说明（AGENTS.md 文档纪律）。

### 3.2 配置种子

- 新建 `config/event_calendar.yaml`：手工宏观/议息日历种子（格式自定但要有 schema 校验，参考现有 `config/*.yaml` 的解析方式；`status: incomplete_todo` 的种子文件跳过模式可参考 `seed_calendar` `db.py:95-161`）。种几条真实数据即可（如月度 CPI/社融发布日、FOMC 日程），不要编造完整年表。
- `config/watchlist.yaml`：23 只股票条目补 `industry_code` 与 `themes` 字段（现条目结构见 watchlist.yaml:4-11）。industry_code 用东财 BK 码——**若没有可靠来源，允许留空并在执行日志标注待人工补**（§2.5 不猜），不要为了填满而编。
- `scripts/pipeline/db.py::seed_watchlist`（db.py:68-92）：upsert 列加 `industry_code`、`themes_json`（JSON 序列化 themes 数组），`ON CONFLICT` 更新列同步加，避免 seed 后新列被清空。

### 3.3 公司公告接入 daily + 信源分级

- 公告采集已存在：`scripts/collect/akshare_collect.py::collect_announcement`（akshare_collect.py:405-439）落盘 `announcement/{date}/{run_id}/{symbol}.csv`，但 `announcement` **不在默认 `--sources`**（默认 `price,financials,index,telegraph`，akshare_collect.py:464）。把它加进默认 sources（注意现有调用方/文档里的默认值说明同步更新）。
- 入库链路已通：ingest `_ROUTES` 已有 `("akshare","announcement")`（ingest.py:50）→ `akshare.py:354-365` 薄壳 → 公共引擎 `adapters/announcements.py::parse_disclosure_csv`（announcements.py:54），写 events（event_type='announcement'）+ event_symbols（INSERT OR IGNORE）。daily 步骤 2 走同一路由，无需新代码。
- **本次要改的**：`parse_disclosure_csv` 写 events 时填 `source_tier=1`（公告/交易所原文）；telegraph 路径（`akshare.py::parse_telegraph_csv`）填 `source_tier=4`。映射关系放 `adapters/announcements.py` 或 adapter 层常量，不要散落到管线。

### 3.4 事件日历采集 + 到期提醒

- 新建采集：akshare 财报披露预约（`stock_report_disclosure`）与解禁日程（`stock_restricted_release_queue_em`）→ `event_calendar`（kind=report_disclosure / unlock，source='akshare'）。落盘 raw CSV 走现有 raw 落盘 + content_hash 约定，adapter 新增解析函数，幂等主键冲突跳过。采集器独立 CLI 入口（如 `akshare_collect --sources calendar`），**可手触发，不强求进 daily**。
- 到期提醒查询：新增函数（建议放 `scripts/ui/queries.py` 或新 `scripts/signals/calendar_due.py`）——union 三类：① `event_calendar` 中 `scheduled_date` 在未来 `remind_before_days`（默认 3）天内的行；② 排期卡 `strategy_card_versions.next_review_at <= today` 的到期复核（现成模板：`queries.py:1387-1394 get_dashboard_alerts` 的 review_due 逻辑）。返回统一结构 `(kind, symbol, date, note)`。

### 3.5 报告新增段（`scripts/pipeline/report.py`）

- 单股报告 `build_symbol_report()`（report.py:180，现有七段：运行状态/当前定位/决策点/观察点/衰竭信号/指标快照/来源与异常）**在"观察点"（report.py:404 起）与"衰竭信号"（report.py:478 起）之间**插入新段，渲染逻辑在 report.py:332-610 区间：
- `### 日历提醒`：到期窗口按每行 `remind_before_days`（含两端边界日：scheduled_date BETWEEN
  as_of AND as_of+remind_before_days），段标题表述用"默认 3 日"而非写死；event_calendar 到期项
  （该股相关 + 宏观项）+ 该股卡片 next_review 倒计时（观察点段已有类似逻辑 report.py:463-466，可复用查询不重复造）。
- `### 公司公告`：当日 `available_at` 日期 == as_of 的该公司公告（events
  event_type='announcement'），置顶、每条标"需读原文"。无公告时显示"今日无新增公告"，不省略整段。
  **注意**：`available_at` 是 UTC ISO datetime，`as_of` 是日期字符串，SQL 里直接
  `available_at <= as_of` 按字符串比较恒 False（r2 简写在 SQLite 不可照抄）——必须日期化比较
  （`substr(available_at,1,10) = as_of`）。
- 段标题与 r2 §8.1 对齐（r2 的 `## 4.x` 是占位符）：**实际编号取 `## 5. 日历与消息面`**，
  插入后原"衰竭信号/指标快照/来源与异常"顺延为 6/7/8——report.py:478/:510/:580 三个
  标题、:477/:509/:579 区间注释、:178/:332 的"七段"注释与模块 docstring 一并改；
  UI 与 daily 不依赖标题文本，execution_log 历史条目不改。新增代码统一加
  `# r2 Phase 1` 标记注释（区分度要求）。
- `input_snapshot_json`（组装在 report.py:613-623）加 `calendar_due` 计数；写入逻辑（`_insert_report_run` report.py:639-655）不动。
- **同步更新** `tests/test_report.py::test_single_report_seven_sections`（test_report.py:224）——它锁死七段标题，加段后改为八段断言；模块 docstring 同步。

### 3.6 UI 日历横幅（`scripts/ui/`）

- Flask 无 Blueprint，路由全在 `app.py::_register_routes`（app.py:108）内 `@app.get` 注册。
- 在 `/cards` 页（视图 `page_cards` app.py:367-369，模板 `templates/cards.html`，前端 `static/js/cards.js`）顶部加"日历提醒"横幅：
  - 数据：优先复用/扩展现有 `/api/dashboard`（app.py:314-315，alerts 已含 review_due），把 3.4 的日历查询并入 alerts；不够再新增 endpoint。
  - 展示：全池级提醒（卡片复核到期 + event_calendar 到期），样式与现有 Tailwind 配色一致，无新依赖。
- UI 测试走 conftest 的 `client` fixture（conftest.py:33-40）+ `tests/ui_seed.seed_ui_data`。

### 3.7 测试（新增 ≥ 6 项，全绿为前提）

- `tests/test_event_calendar.py`：迁移建表、种子解析（含 incomplete_todo 跳过）、到期窗口边界（remind_before_days 含/不含边界日）、akshare 预约/解禁 CSV 解析幂等。
- `tests/test_announcements.py` 或现有文件补充：source_tier 写入（公告=1、电报=4）。
- `tests/test_report.py`：八段断言更新 + 公告段渲染（有公告/无公告/available_at 未来不显示）+ calendar_due 快照字段。
- daily 全链路幂等参照 `test_report.py:396 test_pipeline_signals_and_report_idempotent` 的模式。
- 测试自建局部 conn + 手工插表 helper 的风格照 `test_report.py:30-110`（`_add_calendar`/`_add_watchlist`/`_add_bars` 等），不走 seed。

### 3.8 文档纪律（AGENTS.md 硬性要求）

- 每个改动追加 `docs/execution_log.md`（格式仿最近条目：日期 + 节标题 + 要点列表）。
- `docs/database_schema.md`：event_calendar 新表节 + events/watchlist 新列说明；
  **写明 `events.source_tier` 的 NULL 语义**（=未分级：tdx/akshare 公告写 1、电报写 4，
  tianyancha/kimi 等历史公告路径与 Phase 1 前入库行保持 NULL）。
- `docs/system_design.md`：§3.6 来源矩阵把 cninfo 公告标为已接 daily；§8.1 步骤 6 注释更新；§6.2 报告结构加段说明。
- `docs/handoff.md` 当前状态节：消息面 Phase 1 完成情况。

## 4. 验收标准（逐条自证）

1. `uv run pytest -q` 全绿（含新增测试）。
2. 新库从 0001 起 migrate 到 0003 无错；对现有 `data/market.db` 执行 migrate 幂等（重跑无变化）。**动真实库前先备份**（现有惯例：`data/market.db.bak_YYYYMMDD`）。
3. 报告含新段：有公告日显示公告并标"需读原文"，无公告显示"今日无新增公告"；日历提醒窗口边界正确。
4. /cards 页横幅展示到期项；无到期项时横幅不渲染或显空态。
5. 公告入库带 source_tier=1；重跑同日采集不重复写（content_hash / INSERT OR IGNORE）。
6. 文档四处（execution_log / database_schema / system_design / handoff）同步。

## 5. 禁止事项

- 不做 Phase 2–4 的任何内容（macro_factors、flow 采集、LLM 评价、event_assessments 重建、event_human_review、message_judgments、/message-review 页）。
- 不改 event_study.py 与现有确定性逻辑；不动 `config/signals.yaml` 中带 ⚠️ 的参数。
- 不给 LLM 任何新入口；Phase 1 无 LLM 参与。
- 不重构无关代码；不顺手"优化"报告其他段。
- 不擅自激活/拒绝任何排期卡 draft。
- 不用猜测填充数据（industry_code 无源就留空标注，§2.5）。

## 6. 已知坑（来自执行日志，别再踩）

- akshare_collect 的 `--end` 默认硬编码不随日期更新，每日增量必须显式 `--end <当日>`，否则静默截至前一日。
- `--raw-dir` 是单值参数，多源目录传公共父目录（如 `data/raw/akshare`）。
- 新源首采后必须核对各源 CSV 实际落盘，不能只看进程末尾汇总行（2026-08-28 代理断连静默丢源教训）。
- 公告 `available_at` = 发布日+1 开市日（`next_open_available_at`，announcements.py:86-89），日历缺失时降级 +1 自然日记 incomplete——报告段过滤用 `available_at <= as_of`，不要用 published_at。
- 0002 迁移遗留：`event_assessments.assessment_version` 列声明 INTEGER 亲和但存的是 TEXT（'event_study_v1'），SQLite 容忍。Phase 1 不涉及该表，**不要顺手修**（属 Phase 3 的 0005）。
