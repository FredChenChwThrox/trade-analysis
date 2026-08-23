---
name: tdx-collect
description: 通达信（tdx-connector MCP）数据采集 Skill，**默认第一优先级数据源**（2026-08-21 起）。调用 tdx_kline/tdx_quotes/wenda_notice_query 等 MCP 工具获取 A 股/港股/指数行情、估值快照、公告等原始数据，原样落盘到 data/raw/tdx/ 供 Python adapter（scripts/adapters/tdx.py）入库。只搬运，不加工：不计算指标、不评价消息、不生成规范化财务数字（设计 §3.1）。kimi-datasource 在 tdx 失败时作为 fallback。
---

# 通达信数据采集 Skill（第一优先级）

## 职责边界

- 只做三件事：调 tdx-connector MCP 工具 → 拿到原始响应 → 落盘 `data/raw/tdx/`。
- **禁止**：计算指标、修改/汇总数据、评价消息、生成财务数字。
- 落盘后由 Python adapter（`scripts/adapters/tdx.py`）完成校验与入库，本 Skill 不入库。

## 优先级与 Fallback

1. **第一优先级：通达信 tdx-connector**（本 Skill）——A 股/港股/指数行情、估值快照、公告全覆盖。
2. **Fallback：kimi-datasource**（`stock-collect` skill）——tdx 失败时兜底；kimi access_token 易失效（需 `/login` 修复），公告接口长期 EMPTY_DATA。
3. **公告补采兜底：tianyancha**（需公司全称作 keyword）——tdx 公告也缺时最后兜底。

## 落盘约定

```
data/raw/tdx/{data_type}/{YYYY-MM-DD}/{run_id}/{file}.csv
```

- `data_type`：`kline` / `index` / `announcement` / `quotes`
- `run_id`：每次采集生成一个（如 `run_20260821_1100`）
- 同一次采集的多个文件共用同一 `run_id`；同时写一份 `_meta.json` 记录请求参数、抓取时间、来源、失败明细。

## CSV 列格式（与 adapter 严格对齐）

### kline（A 股/港股日 K 线 → daily_bars）

```
code,setcode,data,open,high,low,close,volume,amount,name,period,tqflag,unit
```

- `code`：纯数字（603605 / 00700），不带后缀
- `setcode`：1=沪A 0=深A 2=北交所 31=港股 62=中证指数（指数文件应入 `index/` 而非 `kline/`）
- `data`：YYYYMMDD（如 20260821）
- `volume`：**tdx_kline 返回 Rows.Volume**（单位：手），adapter 按 `unit` 列换算为股
- `amount`：**tdx_kline 返回 Rows.Amount**（单位：元，弥补 kimi 缺 amount 缺陷）
- `tqflag`：0=不复权（入 daily_bars）、1=前复权（不入，留给复权模块）、2=后复权（同 1）
- `unit`：tdx_kline 返回 AttachInfo.Unit（A 股/港股=100 手，指数=1 股），adapter 用此列换算 volume

文件名建议：`{symbol}.csv`（如 `603605.SH.csv`）或 `{symbol}_tq{flag}.csv`。

### index（指数日 K 线 → index_bars）

```
code,setcode,data,open,high,low,close,volume,amount,name,unit
```

setcode 必须是 62（中证指数）/32（港股指数）之一，否则 adapter 冲突拒绝。`code` 经 SETCODE_SUFFIX 后缀归一（000300 → 000300.SH，与系统 §3.5 benchmark 口径对齐）。`unit` 指数固定为 1（volume 不换算）。

### announcement（公告 → events/event_symbols）

```
title,time,url,source,summary,code,setcode,name
```

- `time`：`YYYY-MM-DD HH:MM:SS`（wenda_notice_query 返回格式）
- `source`：来源（如 "上交所" / "深交所"），入 events.summary
- 通达信公告无 uuid，按 `title|pub_date` 哈希去重（adapter 处理）
- 文件名建议含 ticker：`{symbol}_p{pageNum}.csv`（如 `603605.SH_p1.csv`），adapter 优先从文件名推断 symbol

### quotes（估值/股本快照 → share_capital_events）

```
code,setcode,name,snapshot_at,hqdate,hqtime,now,close,pe,pb,mgsy,mgjzc,zsz,zgb,ltgb,gdrs,ipoprice,zzc,jzc,jly,yysr,jyxjl
```

- 单行单只股票快照（tdx_quotes hasCwInfo=1 返回 ExtInfo + CwInfo 展平）
- `zgb`/`ltgb`：万股（adapter 换算为股）
- `gdrs`：股东人数（系统长期缺的筹码集中度间接指标，§5.7）
- `pe`/`pb`/`mgsy`/`mgjzc`：估值指标，入 details_json
- 写 share_capital_events（event_type=snapshot_group_total_tdx，share_count_type=group_total_tdx），**不参与 valuation.py PE 取数**（仅认 issued/group_total），作为 tdx 估值备查快照

## 数据源路由

| 数据类型 | MCP 工具 | 关键参数 |
|---|---|---|
| A 股日 K | `tdx_kline` | `code=603605, setcode=1, period=4, tqFlag=0, wantNum=250`（1 年≈250 根） |
| 港股日 K | `tdx_kline` | `code=00700, setcode=31, period=4, tqFlag=0, wantNum=250` |
| 沪深300 指数 | `tdx_kline` | `code=000300, setcode=62, period=4, wantNum=250` |
| 港股指数 HSI | `tdx_kline` | `code=HSI, setcode=32, period=4`（待实测确认代码） |
| 公告 | `wenda_notice_query` | `name=公司全称, symbol=603605, bdate=20260801, edate=20260821` |
| 估值/股本快照 | `tdx_quotes` | `code=603605, setcode=1, hasCwInfo=1` |

> ⚠️ **code 参数只接受纯数字**（603605 / 00700 / 000300），不要传 603605.SH 或中文名。中文名先用 `tdx_lookup_stock` 查精确代码。

## 采集模式

### 全量初始化（新股票入池）

1. 日线 3 年：`tdx_kline wantNum=750`（3 年≈750 根日 K），tqFlag=0 不复权
2. 估值/股本快照：`tdx_quotes hasCwInfo=1` 一次
3. 公告 1 年：`wenda_notice_query bdate=去年今日, edate=今日`

### 增量（每日盘后）

1. 从调用方获得：ticker 列表、每只的库内最大日期、本次 `run_id`。
2. 调 `tdx_kline wantNum=N`（N = 库内最大日期至今的交易日数 + 5 重叠窗口）。
3. 调 `tdx_quotes hasCwInfo=1` 写估值快照（每日一份）。
4. 调 `wenda_notice_query bdate=库内最大公告日, edate=今日` 补增量公告。
5. 写 `_meta.json`，返回落盘清单 + 每只返回行数与日期范围（供 adapter 核对）。

## 与 daily 管线衔接

- 采集落盘后，由 `uv run python -m scripts.pipeline.daily --date <交易日> --raw-dir <本批目录>` 自动 ingest 入库。
- ingest CLI 按路径 `data/raw/tdx/{data_type}/...` 推断 (source=tdx, data_type) 路由到 `scripts/adapters/tdx.py` 对应 parse 函数。
- content hash 去重：同文件二次 ingest 跳过（§8.3 幂等）。

## 失败处理

- 单次请求失败可重试一次；仍失败则记录到 `_meta.json` 的 `errors` 并跳过该股，**不得**用旧文件冒充新数据（设计 §2.5）。
- tdx 失败时，**自动转 kimi-datasource 兜底**（走 stock-collect skill），并在 `_meta.json` 标注 fallback。
- 港股 00700 K 线 Name 字段返回"模塑科技"是已知数据瑕疵（数据正确但名字错），不影响入库（adapter 不依赖 Name 字段）。

## 已知限制

- 港股财报（tdx_api_data 港股损益/资负/现金流 fixedTag=1/2/3）：能力存在但 adapter 尚未实现 parse_financials_csv，二期补。
- 资金/筹码分布（盘口夹板/托单）：数据源不可得，§3.6 评估为无源。
- 港股日历：仍需 `config/calendar_HK_{year}.yaml` 种子文件，tdx 不提供交易日历。
