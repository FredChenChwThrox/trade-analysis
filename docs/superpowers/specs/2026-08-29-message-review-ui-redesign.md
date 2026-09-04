# 消息面人审页 UI 重设计（卡片式布局）

> 目标读者：接手 review / 实现本设计的 Agent。不需要读会话历史，本文件 + 列出的项目文档足够。
> 任务范围：**只重排 `/message-review` 页面展示与交互**。不动后端、不动数据库、不动打标逻辑。
> 现状代码：`scripts/ui/templates/message_review.html`（现有 9 列表格版）、`scripts/ui/queries.py:1446 list_message_review()`、`scripts/ui/app.py:380-407`。

## 0. 背景与问题

`/message-review` 是消息面打标结果的人工复核页。当前是 9 列挤在一张表格里：

- 事件信息、scope/tier、状态、标签（`tags_cn` 一长串字符串挤一行）、rationale、预期差、证伪建议、关联股、人工复核表单全部堆在单元格里
- 表单控件（确认/否决/升级/补写 + 三个下拉 + 两个输入）全挤在最后一列，操作区很窄
- 标签是纯文本一行（如 `方向 中性 ｜ 重要性 一般 ｜ 把握 55% ｜ …`），无视觉层级，扫读困难

目标：改成**一条事件一张卡片的流式布局**，标签徽章化，操作区独立成栏，让逐条复核更省力。

## 1. 必读文档

1. `AGENTS.md`（项目根）——硬性纪律（复权口径、§2.5 不猜、文档纪律、LLM 不产规范化数字等）。
2. `docs/handoff.md`——环境与命令。
3. `docs/system_design.md` §8.1（UI 约定）与 §5.5（消息面）。
4. `scripts/ui/static/css/app.css`——现有 `.card` / `.status-*` / `table.data-table` 样式基座（Tailwind CDN 由 base.html 引入）。
5. `scripts/llm/labels.py`——中文映射唯一来源（本次**不改**，只消费它产出的 `tags_cn` / `status_cn` 之外，需要徽章级拆分时在模板内自行拆解，不新建映射文件）。

## 2. 设计约束（硬性）

- **不改后端契约与既有行为**：`queries.py` / `app.py` 的表单字段名、action 值、POST 路径 `/message-review/<event_id>/action` 全部保持原样，`message_review_action` 的 `payload` 键集合不变；唯一允许的后端改动是 §4.1 的新增只读展示字段。
- **中文只来自 labels.py**：任何新增的中文措辞不得另立映射文件；徽章配色由枚举值驱动（见 §4.2），枚举值本身是 DB 契约不得改。
- **不引入新的前端框架/依赖**：继续用 base.html 已引用的 Tailwind CDN；交互只用原生 JS（可放模板内 `<script>` 或 `static/js/`，二选一，不要两者都放）。
- **无未来函数 / 无数据猜测**：纯展示层改动，不新增任何数值推断。
- **兼容空态**：`rows` 为空时保持现有提示文案（"暂无 LLM 评价事件——用消息打标 skill…"）。

## 3. 布局结构

一条事件一张 `.card`，卡片内部两栏（flex）：

```
┌────────────────────────────────────────────────────────────────────┐
│ [状态点] 标题文字文字文字文字文字文字文字              2026-08-20     │
│ 公司级 / T1 · 关联股: 002299.SZ · evt_8844e505d03ac77c              │
│ 摘要文字（两行截断，过长展开/不展开可后续加，本次不做折叠交互）       │
│                                                                    │
│ [利好][重大][把握 55%][盈利（EPS 底稿）][季度级][触发排期卡复核]      │
│                                                                    │
│ 理由  中报披露，业绩细节以报告原文为准…                             │
│ 预期差  —                                                          │
│ 证伪   —                                                          │
├───────────────────────────────────────────┬────────────────────────┤
│                                            │ 复核人 [输入框]          │
│                                            │ [确认] [否决] [留痕]     │
│                                            │ [重要性▾] [升级]          │
│                                            │ [预期差补写]               │
│                                            │ [证伪补写]                │
│                                            │ [作用▾][半衰期▾] [补写]   │
└───────────────────────────────────────────┴────────────────────────┘
```

- 卡片内主内容区 flex-1，右侧操作区**固定宽度 ≈260px**（`w-[260px]`，竖排），两栏之间用 `border-l` 分隔。
- 已否决卡片（`r.hidden`）整卡 `opacity-50 grayscale`，与现逻辑一致。
- 摘要两行截断用 `line-clamp-2`（Tailwind 3.3+ 内置，base.html 的 Play CDN 当前可用）；若 CDN 版本变动导致该类失效，**退化为不截断即可**，不为此引入插件或钉版本——截断只是美观，不属验收项。

## 4. 徽章化设计

### 4.1 徽章来源

现 `tags_cn` 是拼接字符串，不适合逐枚上色。改为**在模板内基于单个字段渲染徽章**，字段仍在 `r.*`（`direction / materiality / confidence / target / half_life / action_hint`），**中文取值全部来自 `labels.py`**。模板里无法直接 import，因此本次允许在 `queries.py` 的 row dict 里**新增只读展示字段** `direction_cn / materiality_cn / target_cn / half_life_cn / action_hint_cn / confidence_pct`，供模板逐枚渲染；这是本次唯一允许的后端改动，且不改变任何既有字段。

实现口径（写死，不许自创）：

- **取值来源是 `eff`（人审后 effective 值）**，与现状 `tags_cn` / `status_cn` 同源，不是 SQL 原始行。
- **映射一律走 `labels.cn(mapping, value)`**，不得用 `dict.get`——`cn()` 已处理 None（返回 dash）与未知枚举（原样返回，不猜），`get` 会在未知值上返回 None 导致空徽章。
- **字段为 None 时不渲染该枚徽章**（除 confidence 外）；这与 §4.2 中 `action_hint = none` 不渲染的规则语义一致。即：`direction_cn / materiality_cn / target_cn / half_life_cn / action_hint_cn` 在 None 时置空串或 None，模板判空跳过。
- **`confidence_pct`**：`confidence` 非 None 时为 `f"把握 {conf:.0%}"`；为 None 时为 `"把握 —"`（对齐 `labels.tags_line` 现状），该徽章始终渲染。

### 4.2 配色规则（Tailwind 类，放模板）

| 枚举 | 值 → 徽章底色/文字 |
|---|---|
| direction | positive 红底 `bg-red-100 text-red-700` · negative 绿底 `bg-green-100 text-green-700` · neutral 灰底 `bg-gray-100 text-gray-600` |
| materiality | low 灰 `bg-gray-100 text-gray-600` · medium 蓝 `bg-blue-100 text-blue-700` · high 橙 `bg-amber-100 text-amber-700` · critical 深红 `bg-red-700 text-white` |
| confidence | 一律 `bg-blue-600 text-white`，文案 `把握 {:.0%}` |
| target | `eps` → 盈利（EPS 底稿）· `pe` → 估值（锚/风险偏好）· `sentiment` → 仅情绪面；底色浅蓝 `bg-sky-100 text-sky-700` |
| half_life | 浅紫 `bg-purple-100 text-purple-700`，文案用 labels.HALF_LIFE_CN |
| action_hint | `none` 不渲染徽章（无动作）· 其余浅黄 `bg-yellow-100 text-yellow-800`，文案用 labels.ACTION_HINT_CN |

徽章统一样式：`inline-flex items-center px-2 py-0.5 rounded-full text-xs whitespace-nowrap`，间距 `gap-1.5 flex-wrap`。

### 4.3 状态点

- 待人审：黄点 `bg-amber-500` + 文字"待人审"
- 已过审：绿点 `bg-green-600` + "已过审"
- 已否决：红点 `bg-red-600` + "已否决"
- 放在卡片标题行左侧，配 `status-badge` 或等价内联类

## 5. 筛选 pills（顶部）

- pills：`全部 / 待人审 / 已过审 / 已否决`
- 纯前端 JS 过滤：根据卡片 data 属性（`data-status="needs_review|ok|hidden"`）显示/隐藏卡片
- **`hidden` 优先于 `status`**（与现状模板"已否决压过一切"一致）：`r.hidden` 为真时 `data-status="hidden"`，无论 `r.status` 为何；否则取 `r.status` 原值
- 默认选中"全部"；切换只改 class，不重新请求后端
- 无后端改动

## 6. 操作区（表单）

表单字段、action 值、`name` 全部保持现状（`actor`、`materiality`、`expectation_gap`、`falsification`、`target`、`half_life`，action: `confirm/dismiss/note/upgrade_materiality/amend`）。仅重排视觉：

- 按钮统一等宽（`w-full` 或等宽 grid）
- 下拉与升级按钮同行
- 补写按钮右对齐
- POST 目标 `/message-review/{{ r.event_id }}/action` 不变，`<input type="hidden" name="symbol" value="__event__">` 保留

## 7. 交付物清单

- `scripts/ui/templates/message_review.html`：整体重写为卡片布局
- `scripts/ui/queries.py`：`list_message_review` row dict 新增只读中文/百分比字段（§4.1）
- （可选）`scripts/ui/static/js/message_review.js`：筛选逻辑；如仅 20 行内可直接内联模板 `<script>`

## 8. 验收标准

1. `uv run pytest -q` 全绿（现状 473 项，不得引入回退）。
2. `/message-review` 渲染无 500；空态文案保留。
3. 徽章中文与 labels.py 映射一致；无英文枚举残留在卡片展示区（表单 select 的 placeholder 文案如"重要性"为表单提示语，允许，不属展示区）。
4. 字段为 None 的徽章不渲染（confidence 除外，其 None 时显示"把握 —"）；已否决卡片 `data-status="hidden"`。
5. 筛选 pills 可切换且不刷新页面。
6. 已否决卡片整卡灰化。
7. 不改动 `scripts/llm/labels.py`、不引入新依赖、不改 DB schema。

## 9. 明确不做（YAGNI）

- 不做摘要折叠/展开、不做排序、不做分页、不做多选批量操作、不做"上一条/下一条"审阅模式、不动移动端响应式细节（现有水平滚动容忍即可，本次仅保证桌面端清晰）。
- 不改动任何打标/人审后端逻辑与 schema。
- 不修已知但超范围的现状问题——例如 `note`（留痕）action 没有对应输入框、`payload` 的 `note` 键永远不会被提交，这是既有行为，本次原样保留，不"顺手"修。
