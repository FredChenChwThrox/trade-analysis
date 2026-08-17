# 数据源实测记录：tianyancha 上市公告（2026-08-15）

> 背景：stock_finance_data get_stock_announcement 故障第 3 日（2026-08-13 起持续 EMPTY_DATA），
> 改走天眼查补采 8 只自选股近 1 年公告。原始文件：`data/raw/tianyancha/announcement/2026-08-15/run_20260815_ann/`

## 接口

- API 名：`上市信息-上市公告`，端点 `http://open.api.tianyancha.com/services/open/stock/announcement/2.0`
- 参数：`keyword`（公司全称，如「牧原食品股份有限公司」）、`pageNum`、`pageSize`（实测 20 可用）
- 返回按 `time` **倒序**分页；停采条件：当页末行 time 越过窗口下界（跨界行保留落盘）
- MCP 调用：`call_data_source_tool(tianyancha, tianyancha_api_call, {api_call_name, api_call_params, file_path})`，结果直接存 CSV

## 字段勘察（CSV 列）

| 列 | 含义 | 入库映射 |
|---|---|---|
| stock_name / name | 股票简称 | 不入库 |
| companyName | 公司全称 | 不入库 |
| stock_code | `002714` / `600031` 等 6 位代码；H 股行为 `HK.02714` 形式 | 校验用：不同 A 股→conflict；HK→行级跳过 |
| time | 公告日 `YYYY-MM-DD` | published_at（当日 00:00+08 转 UTC）；available_at=下一开市交易日 |
| title | 公告标题 | events.title |
| announcementType | 公告分类（可为空） | events.summary |
| ossUrl | PDF 原文链接 | events.canonical_url |
| uuid | 公告唯一标识 | events.source_external_id（幂等去重） |
| id | 天眼查内部 id | 不入库 |

## 实测发现

- **A+H 混排**：同一 keyword 返回混排 A 股与 H 股公告（`stock_code=HK.xxxxx`，繁体标题）。
  本批 8 只中 6 只含 HK 行：603288(HK.03288)、601318(HK.02318)、002747(HK.02715)、
  601899(HK.02899)、600029(HK.01055)、002714(HK.02714)；600531、603605 纯 A。
  002747（2026-03）与 002714（2026-02）为窗口内新上市 H 股，H 股公告自此混排出现。
- 公告量差异大：紫金矿业 20 页/400 行（年报季+回购日披露）为最大，珀莱雅 9 页/180 行为最小。
- `announcementType` 可为空字符串；个别 `title` 含中文引号/书名号，CSV 已正确转义（pandas/csv 解析无断行）。
- 同一公告日可能有多条记录（如 002714 在 2025-12-10 一批制度文件），按 uuid 区分不冲突。
- 无成交额/正文内容字段，仅为公告元数据流——满足 events 事实表需求，不含评价。

## 本批采集统计（窗口 2025-08-14..2026-08-14）

| 标的 | 页数 | 行数 | 实际覆盖 |
|---|---|---|---|
| 603605.SH 珀莱雅 | 9 | 180 | 2025-06-27 ~ 2026-08-04 |
| 603288.SH 海天味业 | 15 | 300 | 2025-06-18 ~ 2026-08-14 |
| 601318.SH 中国平安 | 10 | 200 | 2025-07-18 ~ 2026-08-15 |
| 002747.SZ 埃斯顿 | 12 | 240 | 2025-06-30 ~ 2026-08-12 |
| 601899.SH 紫金矿业 | 20 | 400 | 2025-06-27 ~ 2026-08-13 |
| 600029.SH 南方航空 | 13 | 260 | 2025-05-24 ~ 2026-08-14 |
| 600531.SH 豫光金铅 | 11 | 220 | 2025-07-05 ~ 2026-08-13 |
| 002714.SZ 牧原股份 | 17 | 340 | 2025-06-20 ~ 2026-08-11 |

合计 107 文件 2140 行（覆盖均越过窗口下界 2025-08-14，跨界行按批保留）。入库结果：inserted=1339 / skipped=801（HK 行）/ conflicts=0。
