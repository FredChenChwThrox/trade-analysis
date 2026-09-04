# Code Review & Skill Review 交接（2026-08-31）

> 目标读者：接手 bugfix / 文档同步的 Agent。不需要读会话历史，本文件 + 列出的项目文档足够。
> 任务范围：修复本次 review 发现的代码缺陷，并同步 skill 文档与实现保持一致。
> 来源：`feat(msg-eval)` 链最近 5 次提交 + 当前未提交 working tree（34 files, ~+900/−300）。
> 必读：`docs/system_design.md` §2.5 / §5.1 / §5.4b / §6.3、`docs/handoff.md`、
> `skills/fred-valuation-card-skill/SKILL.md`、`skills/message-tag-skill/SKILL.md`。

## 0. 背景

本轮提交聚焦消息面 r2 Phase 3（LLM 标签通道 + 人审页）和右侧/排期卡状态机修复（holding/stopped_out）。
未提交变更继续推进：卡片底稿 v2 增加 `factor_snapshot`、右侧状态机新增持仓跟踪、报告增加 `stopped_out` 决策点与右侧 followup 提醒、message-review UI 增加公司筛选 + 一键确认。

本次 handoff 汇总 **10 项已验证代码问题** 和 **skill 文档 3 处滞后**，按优先级排列，可并行分配给不同 Agent。

---

## 1. 关键代码缺陷（10 项，按严重程度）

### 1.1 `scripts/signals/right_side.py:181` — holding 分支用裸 `else`

**问题**：状态机主循环中，`waiting_retest` 分支之后是 `else: # holding`，没有显式判断 `state == "holding"`。若 `state` 因未来重构或 bug 出现非预期值（如 `stopped_out` 未重置为 `idle`），会被当 holding 处理。

**复现路径**：在当前版本中，由于所有分支都正确回到 `idle`，不会立即触发，但属于结构性风险。

**修复方案**：
```python
elif state == "holding":
    ...  # 现有 holding 逻辑
else:
    raise ValueError(f"unexpected right_side state: {state}")
```

**测试要求**：
- `uv run pytest -q tests/test_signals_daily.py` 全绿（含 right_side 测试）
- 新增一个测试：显式传入非法 `state` 应抛出异常，或构造一段无法到达 `holding` 的 segment 验证状态转移正确。

**影响面**：right_side 状态机纯函数 `evaluate_segment`。

---

### 1.2 `scripts/pipeline/report.py:316` — holding 状态在日报中完全静默

**问题**：`build_symbol_report` 的 `decision_points` 只处理 `confirmed/invalidated/expired/stopped_out` 的 transition 行。`holding` 是 `triggered=0` 的逐日跟踪行，不会进入 `decision_points`，因此右侧确认后的多日持仓期间，日报 P2/P3/P4 没有任何提示。

**复现路径**：某股 right_side 进入 `holding` 状态后，连续多日运行 `uv run python -m scripts.pipeline.report --date <日期> --symbol <代码>`，仅确认日和止损日有输出，中间日期无。

**修复方案**：在 right_side 分支中增加 `holding` 处理：
```python
elif rs["state"] == "holding" and rs["observed_on"] == trade_date:
    det = rs["details"]
    dp.append(f"[右侧持仓跟踪] 止损位 {det.get('stop_level')}，现价距止损 "
              f"{_pct(det.get('distance_to_stop_pct'))}，已跟踪 "
              f"{det.get('days_since_confirm')} 日（来源 signal_facts right_side @ {trade_date}）")
```

**优先级建议**：与 1.1 一起交给同一位 Agent（同属 right_side 状态机与报告联动）。

**测试要求**：
- `tests/test_report.py` 新增或更新用例：holding 状态的 signal_fact 行应出现在 `decision_points` 中。
- 全量测试 `uv run pytest -q` 全绿。

---

### 1.3 `scripts/ui/queries.py:1489` — `/message-review` 无条件查 `symbol_names`

**问题**：`list_message_review()` 执行 `SELECT symbol, name FROM symbol_names` 前不检查表是否存在。如果 `scripts/pipeline/migrations/0006_symbol_names.sql` 未应用，整页 500。

**复现路径**：全新数据库或只恢复到 `0005` 之前，访问 `/message-review`。

**修复方案**：
```python
names: dict = {}
if _table_exists(conn, "symbol_names"):
    names.update({r["symbol"]: r["name"] for r in
                  conn.execute("SELECT symbol, name FROM symbol_names")})
```
在 `queries.py` 中新增 `_table_exists` 工具函数，或在读取前调用 `conn.execute` 捕获 `sqlite3.OperationalError` 降级。

**测试要求**：
- `tests/test_ui_queries.py` 中新增测试：在没有 `symbol_names` 表的连接上调用 `list_message_review` 不抛异常。
- 同时验证 watchlist 覆盖仍优先生效。

---

### 1.4 `scripts/signals/factor_watch.py:102` — f-string 嵌入 SQL 操作符

**问题**：`latest_factor_close()` 用 f-string 把 `op`（`<` 或 `<=`）嵌入 SQL。虽然 `op` 是本地常量，但不符合参数化查询纪律，且会被静态分析标记为 SQL 注入风险。

**修复方案**：拆成两个查询或用一个条件参数化写法。由于 SQLite 不支持把操作符作为参数，最干净的做法是拆成两个分支：
```python
if market == "GLOBAL":
    sql = """... WHERE code = ? AND trade_date < ? ORDER BY trade_date DESC LIMIT 1"""
else:
    sql = """... WHERE code = ? AND trade_date <= ? ..."""
row = conn.execute(sql, (code, as_of)).fetchone()
```

**测试要求**：
- `tests/test_factor_watch.py` 中验证 `GLOBAL` 和 `CN` 两个分支都能取到最新读数。
- `uv run pytest -q` 全绿。

---

### 1.5 `scripts/signals/factor_watch.py:158` — `_factor_market` N+1 查询

**问题**：`snapshot_for_symbol()` 对每个 factor 调用 `_factor_market(conn, e["code"])`，每个 factor 触发一次 `SELECT market FROM macro_factors WHERE code = ? LIMIT 1`。如果某股映射 5 个因子，则多 5 次查询。

**修复方案**：
- 在 `load_industry_factors()` 时顺便构建 `code → market` 缓存，或
- 在 `latest_factor_close()` 查询里直接返回 `market` 字段（SQL 已 SELECT `market`），把 `_factor_market` 调用合并到 `latest_factor_close` 结果中。

**推荐方案**：让 `latest_factor_close()` 返回的 dict 已经包含 `market`，`snapshot_for_symbol` 直接用 `latest["market"]`，不再调用 `_factor_market`。

**测试要求**：
- 原有 `test_factor_watch.py` 断言 `snapshot_for_symbol` 返回的 `factors` 中每个条目 `market` 字段正确。
- 可用 `unittest.mock` 或 `conn.set_trace_callback` 验证查询次数不随 factor 数量线性增长。

---

### 1.6 `scripts/ui/queries.py:725` — 每次请求重复加载 `industry_factors.yaml`

**问题**：`get_stock_overview()` 调用 `factor_watch.load_industry_factors()`，每次单股页请求都从磁盘解析 YAML。

**修复方案**：
- 最简单：在 `factor_watch.py` 模块级别缓存 `(mapping, hash, mtime)`，函数内检查文件 mtime，未变直接返回缓存。
- 注意：现有 `load_industry_factors` 已经返回 hash，可直接用于缓存键。

**实现建议**：
```python
_INDUSTRY_CACHE = None

def load_industry_factors(...):
    global _INDUSTRY_CACHE
    path = path or INDUSTRY_FACTORS_CONFIG
    mtime = path.stat().st_mtime
    if _INDUSTRY_CACHE and _INDUSTRY_CACHE[0] == mtime:
        return _INDUSTRY_CACHE[1], _INDUSTRY_CACHE[2]
    mapping, h = _load_from_disk(path, macro_path)
    _INDUSTRY_CACHE = (mtime, mapping, h)
    return mapping, h
```

**测试要求**：
- 验证连续调用两次返回同一对象（cache hit）。
- 修改文件后 mtime 变化，第三次调用重新加载（cache miss）。

---

### 1.7 `scripts/ui/app.py:423` — `confirm-all` 全量拉取后 Python 过滤

**问题**：`message_review_confirm_all()` 调用 `list_message_review(conn)` 获取全部事件，再用 Python 过滤 `status == 'needs_review' and not hidden and company in symbols`。当数据量大时浪费内存和 DB 资源。

**修复方案**：
- 在 `queries.py` 新增 `list_message_review_for_confirm(conn, company=None, status='needs_review', hidden=False)`，使用与 `list_message_review` 相同 join 但加 WHERE 过滤，只返回需要确认的行。
- `app.py` 直接调用该函数。

**测试要求**：
- `tests/test_ui_app.py` 新增测试：confirm-all 只处理 `needs_review` 且未 hidden 且匹配公司的行。
- 测试大数据量下 endpoint 不超时（可 mock 验证 SQL 参数）。

---

### 1.8 `scripts/pipeline/report.py:181` — `_right_side_followups` N+1 查询

**问题**：对每个 right-side 事件都运行 `SELECT COUNT(*) FROM executions WHERE symbol = ? AND substr(executed_at, 1, 10) >= ?`。

**修复方案**：改用一个 `LEFT JOIN` 查询：
```sql
SELECT s.observed_on, s.state, COUNT(e.id) AS n
FROM signal_facts s
LEFT JOIN executions e ON e.symbol = s.symbol
    AND substr(e.executed_at, 1, 10) >= s.observed_on
WHERE s.symbol = ? AND s.signal = 'right_side'
  AND s.state IN ('confirmed', 'stopped_out')
  AND s.observed_on <= ?
  AND s.observed_on >= date(?, '-' || ? || ' days')
GROUP BY s.observed_on, s.state
```

**测试要求**：
- `tests/test_report.py` 新增或更新：验证 followup 只在没有 execution 时产生，且查询次数固定。

---

### 1.9 `scripts/ui/templates/message_review.html:135` — 公司筛选用逗号 split

**问题**：JS 用 `data-symbols.split(',')` 做精确匹配。如果 `symbols_label` 或 `symbols` 某字段包含逗号（如公司名里的逗号或未来格式变化），筛选会出错。

**修复方案**：
- 后端在渲染卡片时把 `data-symbols` 设为一个安全分隔符（如 `|` 或 JSON），或
- 前端用 `data-symbols` 作为完整字符串，通过后端已过滤的 `company` 参数提交。

**注意**：当前 `data-symbols` 内容是 `{{ r.symbols | join(',') }}`，建议改成 `{{ r.symbols | join('|') }}`，JS split 用 `'|'`。

**测试要求**：
- 手动打开 `/message-review`，选择公司筛选，确认只显示该公司关联事件。

---

### 1.10 `scripts/ui/static/js/card_detail.js:78` — `anchorIsPe` null 时误判

**问题**：代码类似如下逻辑：
```javascript
let anchorIsPe = null;
if (card.anchor_type_note) { ... } else { anchorIsPe = null; }
if (anchorIsPe === false) { ... } else { /* 走 PE 三情景 */ }
```
`anchorIsPe` 为 `null` 时既不等于 `false`，就会错误地渲染成"PE 三情景"标签，而不是"PE 三情景（非锚，仅分位参考）"或"其他锚"。

**修复方案**：
- 明确 `anchorIsPe` 三态：true / false / null，模板分支应为：
```javascript
if (anchorIsPe === true) { /* 纯 PE 锚 */ }
else if (anchorIsPe === false) { /* 非锚，仅分位参考 */ }
else { /* 无结构化 anchor，显示 note 或隐藏 */ }
```
- 或者把 `null` 默认值改为 `false`（如果当前 `anchor_type_note` 就语义等价于"非 PE 锚"）。

**测试要求**：
- 打开 `/cards` 或 `/stock/{symbol}` 中显示旧卡片（无 `anchor` 结构化字段），检查标签文案。

---

## 2. Skill 文档同步（3 项）

### 2.1 `fred-valuation-card-skill/SKILL.md` §4 / §9 JSON 键名规范不一致

**问题**：
- §4 说"因子假设写入 `earnings.factor_assumptions`"。
- §9 说"`valuation` 段必须含 `sample_window` 样本区间标注"，但 sample_window 实际在 `valuation_scale`（卡片 JSON 的 `valuation` 段）中。
- §9 又说"`earnings_scenarios_json` 根级 additionalProperties=false"，但 factor_assumptions 应在 `earnings` 内，文档措辞容易让人误解为根级字段。

**修复方案**：
在 §9 增加一张明确表格：

| JSON 段 | 必填字段 | 说明 |
|---|---|---|
| `earnings_scenarios_json` | `eps: {bear, base, bull}` | EPS 三情景 |
| | `factor_assumptions?: [{code, name, unit, level, as_of_date, note}]` | 因子假设，可选，放 `eps` 同级 |
| `valuation_scenarios_json` | `pe: {bear, base, bull}` | PE 三情景 |
| `price_tiers_json` | `tiers: [{tier, zone_low, zone_high}]` | 三档价位 |
| `invalidation_json` | `line` | 证伪线 |
| `swing_box_json` | 各边界字段 | 波段仓 |
| `right_side_trigger_json` | `trigger_level`, `stop_level?` | 右侧触发位/止损位 |

并把 `sample_window` 说明移到 `valuation_scale/valuation` 段落，明确不是 `earnings` 的字段。

---

### 2.2 `fred-valuation-card-skill/SKILL.md` §7 未描述 holding/stopped_out 状态

**问题**：§7 只写了"右侧确认仓触发条件"和"止损位"，没说明确认后系统会进入 `holding` 状态，逐日写 `signal_facts` 跟踪行，跌破 `stop_level` 触发 `stopped_out`。

**修复方案**：在 §7 末尾新增一段：

> 触发后系统进入**持仓跟踪状态（holding）**，每日收盘写入跟踪行，记录止损位与现价距止损距离。跟踪期间收盘价 ≤ `stop_level` 即触发 `stopped_out` 并回 `idle`；无 `stop_level` 的卡片 confirmed 后直接回 `idle`（§2.5：无线不猜）。

---

### 2.3 `fred-valuation-card-skill/SKILL.md` §4 因子假设锚点未明确

**问题**：§4 说"每个情景注明关键因子的假设水平"，但没说这个水平必须以 `card_inputs_v2` 底稿的 `factor_snapshot.factors[].close` 为锚。

**修复方案**：在 §4 因子假设段落增加：

> 因子假设水平以 `card_inputs_v2` 底稿 `factor_snapshot` 段的当期读数（`close` + `trade_date`）为锚。skill 不自行从外部数据源（kimi-datasource、网页）取当前读数；若底稿 `status` 为 `stale` 或 `missing`，在卡中如实标注缺口，不得编造。

---

### 2.4 `message-tag-skill/SKILL.md` 小补充

**问题**：§6 "看不懂先查再打" 没给出把搜索到的背景写进 `rationale` 的格式示例。

**修复方案**：在 §2 / §6 增加一个 rationale 自解释模板示例：

> 示例：`"XX 公司主营 YY 业务，本轮 ZZ 政策影响其上游成本环节；若落地将压低毛利率，需跟踪细则执行口径"`。

---

## 3. 任务拆分建议

| 任务 | 文件 | 建议分配 | 关联 |
|---|---|---|---|
| A. 右侧状态机修复 | `right_side.py` + `report.py` | Agent 1 | 1.1 + 1.2 |
| B. 因子层优化 | `factor_watch.py` | Agent 2 | 1.4 + 1.5 + 1.6 |
| C. 人审页优化 | `app.py` + `queries.py` | Agent 3 | 1.3 + 1.7 + 1.9 |
| D. 报告 followup | `report.py` | Agent 4 | 1.8 |
| E. 前端修复 | `card_detail.js` + `message_review.html` | Agent 5 | 1.9 + 1.10 |
| F. Skill 文档同步 | `SKILL.md` × 2 | Agent 6（只改 docs） | 2.1–2.4 |

**注意**：B / D / A 之间无文件冲突；C 和 E 都涉及 `message_review.html`，建议由同一 Agent 处理，或先 C 后 E。

---

## 4. 验收标准

- [ ] `uv run pytest -q` 全绿（当前 474 项左右，不新增失败）。
- [ ] 新增测试覆盖：right_side 非法状态抛错、report holding 决策点、message-review 无 symbol_names 表降级、factor_watch 查询次数不随 factor 线性增长、confirm-all 只处理目标行。
- [ ] 手动验收：
  - 打开 `/message-review` 筛选公司，确认筛选正确；
  - 打开某股 `/stock/{symbol}`，检查旧卡片 anchor 标签渲染；
  - 跑 `uv run python -m scripts.pipeline.report --date <date> --symbol <sym>`，确认 holding 状态有输出。

---

## 5. 参考资料

- `docs/system_design.md`：§2.5（不猜）、§5.1（版本生效区间）、§5.4（右侧状态机）、§6.3（优先级）
- `docs/handoff.md`：环境与命令
- `skills/fred-valuation-card-skill/SKILL.md`：排期卡生成流程
- `skills/message-tag-skill/SKILL.md`：消息面打标流程
- `docs/superpowers/specs/2026-08-29-message-review-ui-redesign.md`：人审页 UI 设计（如 E 任务需要）
