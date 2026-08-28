"""akshare 采集器：调 akshare 拉取数据 → 落盘 raw CSV（字段对齐现有 adapter 约定）。

对齐约定（adapter 直接复用既有验证逻辑）：
- price / index：列 = kimi stock_finance_data 约定
  thscode,time,open,high,low,close,volume,amount,currency（time=%Y%m%d）
  → scripts/adapters/akshare.parse_price_csv / parse_index_csv（复用 upsert_daily_bars/upsert_index_bars）
- forward：同 price 列约定，adjust="qfq" 前复权，文件名 {symbol}_forward.csv；
  不入 daily_bars（ingest 按 *_forward* 跳过），只供 scripts.pipeline.adjust 因子重建
- financials：列 = tdx 约定
  code,setcode,period_end,fiscal_year,revenue,net_profit_attr,eps_basic,eps_diluted,
  currency,unit,is_cumulative,published_at
  → scripts/adapters/akshare.parse_financials_csv（转发 tdx.parse_financials_csv，
    published_at=akshare 的 NOTICE_DATE 披露日，§2.1 available_at 由 tdx 复用逻辑计算）
- telegraph：列 = events 字段（published_at UTC / published_tz / title / summary /
  content / source_external_id / content_hash）
- forecast：一致预期（同花顺盈利预测，仅 A 股）→ 列对齐 kimi forecast 约定
  （ths_fore_np_fy1..3_stock 净利预测均值，单位由「亿元」×1e8 换算为「元」；
  ths_fore_mbi_fy1..3_stock 营收预测，接口为空时留空标缺口 §2.5；
  ths_fore_np_yoy_stock akshare 无直接口径，留空）→ akshare.parse_forecast_csv
  （转发 stock_finance_data.parse_forecast_csv，source=akshare）
- stock_info：股本快照（东财股本结构 stock_zh_a_gbjg_em，仅 A 股）→
  列对齐 kimi stock_info 约定（thscode + ths_total_shares_stock 集团总股本，
  取最新变动行）→ akshare.parse_stock_info_csv → share_capital_events
  （snapshot_group_total / group_total，参与 PE 取数，与 kimi 源可切换）
- announcement：巨潮公告列表（stock_zh_a_disclosure_report_cninfo，仅 A 股）→
  标准公告线格式（title,time,url,source,summary,code,setcode,name；接口日期参数
  必须紧凑 YYYYMMDD，带 - 格式静默返回空，实测）→ announcements.parse_disclosure_csv 公共引擎薄壳
  akshare.parse_announcement_csv（events.source='akshare'，与 tdx dedup 隔离）
- calendar（r2 Phase 1，手触发不进 daily）：财报披露预约（stock_report_disclosure，
  沪深京全市场拉取后仅留 watchlist 行，scheduled_date 取"当前预约"=最后一次变更）
  + 解禁日程（stock_restricted_release_queue_em 逐股，仅留采集日之后未来行）→
  calendar/{date}/{run_id}/{report_disclosure,unlock}.csv
  → adapters/event_calendar.parse_calendar_csv（ingest 路由 akshare/calendar）；
  期次必须显式 --calendar-period（如 2026半年报），不做默认推断（--end 硬编码教训）
- macro（r2 Phase 2，进默认 sources）：宏观因子快照（config/macro_factors.yaml 清单驱动，
  内盘/外盘期货 sina 接口 + 中行外汇牌价"央行中间价"）→ macro/{date}/{run_id}/macro.csv
  → adapters/macro_factors.parse_macro_csv（ingest 路由 akshare/macro）；
  close 存来源原始值不换算，来源无涨跌幅则 change_pct 留空
- flow（r2 Phase 2，进默认 sources）：龙虎榜（stock_lhb_detail_em，按"股票×日"合并
  多上榜原因）+ 大宗交易（stock_dzjy_mrmx，每笔一行）仅留 watchlist 行 →
  flow/{date}/{run_id}/{lhb,dzjy}.csv → adapters/flow_events.parse_flow_csv
  （ingest 路由 akshare/flow；events scope='flow' 静默入库，不推送不进日报）

口径换算（与库 schema 对齐，§3.2/§9.5）：
- 成交量：东财接口单位「手」，落盘前 ×100 换算为项目 volume_raw 口径「股」（指数不换算）；
- 成交额：东财单位「元」，与 amount_raw 口径一致，不换算；
- 财报金额：东财利润表单位「元」，unit='yuan'（tdx _fin_unit_to_yuan 校验）。

依赖：akshare 为 optional extra（体积大）：
    uv sync --extra akshare   或   uv pip install akshare

用法：
    uv run python -m scripts.collect.akshare_collect \\
        --symbols 603605.SH,00700.HK --indexes 000300.SH,^HSI \\
        --start 2023-08-10 --end 2026-08-25 --date 2026-08-25 --run-id run_ak
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[2]
WATCHLIST = ROOT / "config" / "watchlist.yaml"
DEFAULT_OUT = ROOT / "data" / "raw" / "akshare"
MACRO_CONFIG = ROOT / "config" / "macro_factors.yaml"

CN_TZ = ZoneInfo("Asia/Shanghai")

# symbol 后缀 → akshare code + tdx setcode + currency + market
SYMBOL_META = {
    ".SH": ("", "1", "CNY", "CN"),
    ".SZ": ("", "0", "CNY", "CN"),
    ".BJ": ("", "2", "CNY", "CN"),
    ".HK": ("", "31", "HKD", "HK"),
}

# 系统 index_code → akshare symbol（新浪源，实测可用）
INDEX_META = {
    "000300.SH": ("sh000300", "stock_zh_index_daily"),
    "^HSI": ("HSI", "stock_hk_index_daily_sina"),
}

_A_SHARE_COLS = {"日期": "time", "开盘": "open", "最高": "high",
                 "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount"}
_HK_COLS = {"日期": "time", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount"}


def _load_akshare():
    """惰性导入 akshare（optional extra）。"""
    try:
        import akshare as ak  # noqa: PLC0415
        return ak
    except ImportError as exc:
        raise ImportError(
            "需要 akshare（optional extra）：uv sync --extra akshare 或 uv pip install akshare"
        ) from exc


def _date_compact(s: str) -> str:
    """2026-08-03 / 20260803 / Timestamp → 20260803。"""
    if hasattr(s, "strftime"):
        return s.strftime("%Y%m%d")
    return str(s).replace("-", "")[:8]


def _num(v) -> str | None:
    if v is None or v != v:  # None/NaN
        return None
    return str(v)


def _to_shares(vol) -> str | None:
    """东财成交量（手）→ 项目 volume_raw 口径（股）：×100。"""
    v = _num(vol)
    if v is None:
        return None
    return str(int(float(v) * 100))


def _symbol_parts(symbol: str) -> tuple[str, str, str, str]:
    suffix = symbol.rsplit(".", 1)[-1].upper()
    meta = SYMBOL_META.get("." + suffix)
    if meta is None:
        raise ValueError(f"不支持的 symbol 后缀: {symbol}")
    code, setcode, currency, market = meta
    return symbol[: -len(suffix) - 1], setcode, currency, market


# ---------------------------------------------------------------- price

def collect_price(ak, symbol: str, start: str, end: str, out_dir: Path,
                  date: str, run_id: str, *, adjust: str = "",
                  api: str = "em") -> Path | None:
    """A 股/港股日线 → {symbol}.csv（volume 统一为「股」口径）。

    adjust="" 不复权（入 daily_bars 用）；adjust="qfq" 前复权 → {symbol}_forward.csv，
    只供 scripts.pipeline.adjust 因子重建（ingest CLI 按 *_forward* 约定跳过）。
    api="em"（默认，东财 stock_zh_a_hist，成交量单位「手」需 ×100）；
    api="sina"（新浪 stock_zh_a_daily，A 股备用源，成交量已是「股」不换算，
    东财接口不可用时切换）。
    """
    code, _setcode, currency, _market = _symbol_parts(symbol)
    out = out_dir / "price" / date / run_id
    out.mkdir(parents=True, exist_ok=True)
    fp = out / f"{symbol}{'_forward' if adjust else ''}.csv"
    if api == "sina":
        if not symbol.endswith((".SH", ".SZ")):
            raise ValueError(f"sina 源仅支持 A 股: {symbol}")
        sina_code = ("sh" if symbol.endswith(".SH") else "sz") + code
        df = ak.stock_zh_a_daily(symbol=sina_code,
                                 start_date=_date_compact(start),
                                 end_date=_date_compact(end), adjust=adjust)
        if df is None or df.empty:
            return None
        with open(fp, "w", newline="", encoding="utf-8") as f:
            f.write("thscode,time,open,high,low,close,volume,amount,currency\n")
            for _, r in df.iterrows():
                f.write(",".join([
                    symbol, _date_compact(r["date"]),
                    _num(r["open"]), _num(r["high"]), _num(r["low"]), _num(r["close"]),
                    _num(r["volume"]) or "", _num(r.get("amount")) or "", currency,
                ]) + "\n")
        return fp
    if code and symbol.endswith((".SH", ".SZ", ".BJ")):
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date=_date_compact(start),
                                end_date=_date_compact(end), adjust=adjust)
        colmap = _A_SHARE_COLS
    else:
        df = ak.stock_hk_hist(symbol=code, period="daily",
                              start_date=_date_compact(start),
                              end_date=_date_compact(end), adjust=adjust)
        colmap = _HK_COLS
    if df is None or df.empty:
        return None
    with open(fp, "w", newline="", encoding="utf-8") as f:
        f.write("thscode,time,open,high,low,close,volume,amount,currency\n")
        for _, r in df.iterrows():
            vol = _to_shares(r.get("成交量"))
            f.write(",".join([
                symbol, _date_compact(r["日期"]),
                _num(r["开盘"]), _num(r["最高"]), _num(r["最低"]), _num(r["收盘"]),
                vol or "", _num(r.get("成交额")) or "", currency,
            ]) + "\n")
    return fp


# ---------------------------------------------------------------- financials

def _setcode_of(symbol: str) -> str:
    return _symbol_parts(symbol)[1]


def collect_financials(ak, symbol: str, out_dir: Path, date: str, run_id: str) -> list[Path]:
    """利润表（东财）→ 每期一个 {symbol}_is_{period}.csv（列对齐 tdx 约定）。"""
    out = out_dir / "financials" / date / run_id
    out.mkdir(parents=True, exist_ok=True)
    code, setcode, _c, _m = _symbol_parts(symbol)
    df = ak.stock_profit_sheet_by_report_em(symbol=f"SH{code}" if symbol.endswith(".SH") else symbol)
    if df is None or df.empty:
        return []
    paths: list[Path] = []
    for _, r in df.iterrows():
        period_end = str(r["REPORT_DATE"])[:10]
        fiscal_year = period_end[:4]
        notice = str(r["NOTICE_DATE"])[:10] if r.get("NOTICE_DATE") is not None else ""
        fp = out / f"{symbol}_is_{period_end.replace('-', '')}.csv"
        with open(fp, "w", newline="", encoding="utf-8") as f:
            f.write("code,setcode,period_end,fiscal_year,revenue,net_profit_attr,"
                    "eps_basic,eps_diluted,currency,unit,is_cumulative,published_at\n")
            f.write(",".join([
                code, setcode, period_end, fiscal_year,
                _num(r.get("OPERATE_INCOME")) or "",
                _num(r.get("PARENT_NETPROFIT")) or "",
                _num(r.get("BASIC_EPS")) or "",
                _num(r.get("DILUTED_EPS")) or "",
                (r.get("CURRENCY") or "CNY"), "yuan", "1", notice,
            ]) + "\n")
        paths.append(fp)
    return paths


# ---------------------------------------------------------------- index

def collect_index(ak, index_code: str, start: str, end: str, out_dir: Path,
                  date: str, run_id: str) -> Path | None:
    """指数日线 → {index_code}.csv（新浪源，volume 不换算）。"""
    meta = INDEX_META.get(index_code)
    if meta is None:
        raise ValueError(f"不支持的指数 index_code: {index_code}（可选 {sorted(INDEX_META)}）")
    ak_symbol, fn_name = meta
    out = out_dir / "index" / date / run_id
    out.mkdir(parents=True, exist_ok=True)
    fp = out / f"{index_code}.csv"
    df = getattr(ak, fn_name)(symbol=ak_symbol)
    if df is None or df.empty:
        return None
    currency = "CNY" if index_code.endswith(".SH") else "HKD"
    with open(fp, "w", newline="", encoding="utf-8") as f:
        f.write("thscode,time,open,high,low,close,volume,amount,currency\n")
        for _, r in df.iterrows():
            f.write(",".join([
                index_code, _date_compact(r["date"]),
                _num(r["open"]), _num(r["high"]), _num(r["low"]), _num(r["close"]),
                _num(r.get("volume")) or "", _num(r.get("amount")) or "", currency,
            ]) + "\n")
    return fp


# ---------------------------------------------------------------- telegraph

def collect_telegraph(ak, out_dir: Path, date: str, run_id: str) -> Path | None:
    """财联社电报 → telegraph_{date}.csv（events 字段，published_at 已转 UTC）。"""
    out = out_dir / "telegraph" / date / run_id
    out.mkdir(parents=True, exist_ok=True)
    fp = out / f"telegraph_{date}.csv"
    df = ak.stock_info_global_cls(symbol="全部")
    if df is None or df.empty:
        return None
    with open(fp, "w", newline="", encoding="utf-8") as f:
        f.write("event_type,published_at,published_tz,title,summary,content,"
                "source_external_id,content_hash\n")
        for _, r in df.iterrows():
            title = str(r["标题"])
            content = str(r["内容"])
            pub_date = r["发布日期"]
            pub_time = r["发布时间"]
            local = datetime(
                pub_date.year, pub_date.month, pub_date.day,
                pub_time.hour, pub_time.minute, pub_time.second,
                tzinfo=CN_TZ,
            )
            published_at = local.astimezone(timezone.utc).isoformat()
            ext_id = f"cls_{local.strftime('%Y%m%d_%H%M%S')}"
            content_hash = hashlib.sha256(
                f"{title}|{content}|{published_at}".encode()).hexdigest()
            summary = content[:120]
            f.write(",".join([
                "news", published_at, "Asia/Shanghai",
                _csv_escape(title), _csv_escape(summary), _csv_escape(content),
                ext_id, content_hash,
            ]) + "\n")
    return fp


def _csv_escape(s: str) -> str:
    if any(c in s for c in ',"\n'):
        return '"' + s.replace('"', '""') + '"'
    return s


# ---------------------------------------------------------------- forecast（一致预期，仅 A 股）

_FORECAST_COLS = [
    "ths_fore_np_fy1_stock", "ths_fore_np_fy2_stock", "ths_fore_np_fy3_stock",
    "ths_fore_np_in12m_stock", "ths_fore_np_yoy_stock",
    "ths_fore_mbi_fy1_stock", "ths_fore_mbi_fy2_stock", "ths_fore_mbi_fy3_stock",
    "ths_fore_mbi_yoy_stock", "thscode", "time",
    # akshare 同花顺口径附加列（payload_json 全量保留，card_inputs 不消费）
    "ak_np_orgs_fy1", "ak_np_orgs_fy2", "ak_np_orgs_fy3",
    "ak_np_min_fy1", "ak_np_max_fy1", "ak_np_min_fy2", "ak_np_max_fy2",
    "ak_np_min_fy3", "ak_np_max_fy3",
    "ak_eps_fy1", "ak_eps_fy2", "ak_eps_fy3",
]


def _yi_to_yuan(v) -> str | None:
    """同花顺盈利预测单位「亿元」→ 项目口径「元」（×1e8）。"""
    s = _num(v)
    if s is None:
        return None
    return str(int(float(s) * 1e8))


def _int_str(v) -> str | None:
    """整数值规范化（iterrows 行内 dtype 统一会把 int 读成 float）。"""
    s = _num(v)
    if s is None:
        return None
    return str(int(float(s)))


def _ths_forecast_rows(ak, code: str, indicator: str):
    """ak.stock_profit_forecast_ths → {年度: row dict}；接口空/异常返回 {}。"""
    try:
        df = ak.stock_profit_forecast_ths(symbol=code, indicator=indicator)
    except Exception:  # noqa: BLE001  源侧抖动（代理/限流），按缺口处理不猜
        return {}
    if df is None or df.empty:
        return {}
    return {int(r["年度"]): r for _, r in df.iterrows()}


def collect_forecast(ak, symbol: str, out_dir: Path, date: str, run_id: str) -> Path | None:
    """一致预期（同花顺 stock_profit_forecast_ths，仅 A 股）→ {symbol}.csv。

    列对齐 kimi forecast 约定（ths_fore_np_fyN_stock 等），FY1 = --date 所在日历年；
    净利单位「亿元」换算为「元」。FY1 净利增速（ths_fore_np_yoy_stock）akshare
    无直接口径，留空（card_inputs 的裂口检查降级，§2.5 不猜）。
    """
    if not symbol.endswith((".SH", ".SZ", ".BJ")):
        raise ValueError(f"forecast 源仅支持 A 股: {symbol}")
    code, _s, _c, _m = _symbol_parts(symbol)
    np_rows = _ths_forecast_rows(ak, code, "预测年报净利润")
    if not np_rows:
        return None
    eps_rows = _ths_forecast_rows(ak, code, "预测年报每股收益")
    rev_rows = _ths_forecast_rows(ak, code, "预测年报主营业务收入")
    fy1_year = int(date[:4])
    rec = {c: "" for c in _FORECAST_COLS}
    rec["thscode"] = symbol
    for year, r in np_rows.items():
        n = year - fy1_year + 1
        if not 1 <= n <= 3:
            continue
        rec[f"ths_fore_np_fy{n}_stock"] = _yi_to_yuan(r.get("均值")) or ""
        rec[f"ak_np_orgs_fy{n}"] = _int_str(r.get("预测机构数")) or ""
        rec[f"ak_np_min_fy{n}"] = _yi_to_yuan(r.get("最小值")) or ""
        rec[f"ak_np_max_fy{n}"] = _yi_to_yuan(r.get("最大值")) or ""
        if year in eps_rows:
            rec[f"ak_eps_fy{n}"] = _num(eps_rows[year].get("均值")) or ""
        if year in rev_rows:
            rec[f"ths_fore_mbi_fy{n}_stock"] = _yi_to_yuan(rev_rows[year].get("均值")) or ""
    out = out_dir / "forecast" / date / run_id
    out.mkdir(parents=True, exist_ok=True)
    fp = out / f"{symbol}.csv"
    with open(fp, "w", newline="", encoding="utf-8") as f:
        f.write(",".join(_FORECAST_COLS) + "\n")
        f.write(",".join(rec[c] for c in _FORECAST_COLS) + "\n")
    return fp


# ---------------------------------------------------------------- stock_info（股本快照，仅 A 股）

def collect_stock_info(ak, symbol: str, out_dir: Path, date: str, run_id: str) -> Path | None:
    """股本快照（东财 stock_zh_a_gbjg_em，仅 A 股）→ {symbol}.csv。

    列对齐 kimi stock_info 约定（thscode + ths_total_shares_stock 集团总股本，
    A+H 股含 H 股，与 stock_finance_data 同口径），取最新变动日期一行；
    附 ak_change_date / ak_change_reason / ak_float_a_shares 备查。
    """
    if not symbol.endswith((".SH", ".SZ", ".BJ")):
        raise ValueError(f"stock_info 源仅支持 A 股: {symbol}")
    code, _s, _c, _m = _symbol_parts(symbol)
    df = ak.stock_zh_a_gbjg_em(symbol=code)
    if df is None or df.empty:
        return None
    latest = df.iloc[0]  # 接口返回按变更日期倒序，首行即最新
    shares = _num(latest.get("总股本"))
    if shares is None:
        return None
    out = out_dir / "stock_info" / date / run_id
    out.mkdir(parents=True, exist_ok=True)
    fp = out / f"{symbol}.csv"
    with open(fp, "w", newline="", encoding="utf-8") as f:
        f.write("thscode,ths_total_shares_stock,ak_change_date,ak_change_reason,"
                "ak_float_a_shares\n")
        f.write(",".join([
            symbol, str(int(float(shares))),
            _date_compact(latest["变更日期"]),
            _csv_escape(str(latest.get("变动原因") or "")),
            _num(latest.get("已上市流通A股")) or "",
        ]) + "\n")
    return fp


# ---------------------------------------------------------------- stock_info（股本快照，仅 A 股）

# ---------------------------------------------------------------- announcement（cninfo 公告，仅 A 股）

def collect_announcement(ak, symbol: str, start: str, end: str, out_dir: Path,
                         date: str, run_id: str) -> Path | None:
    """巨潮公告列表（stock_zh_a_disclosure_report_cninfo，仅 A 股沪深京）→ {symbol}.csv。

    列 = 标准公告线格式（title,time,url,source,summary,code,setcode,name），
    由 scripts/adapters/announcements.parse_disclosure_csv 解析入库
    （events.source='akshare'，与 tdx dedup 命名空间隔离）。
    注意：接口日期参数必须紧凑格式 YYYYMMDD（带 - 的格式静默返回空，已实测）；
    同内容重跑入库侧幂等（content-hash + event_id 双门槛），可放心按窗口重复拉取。
    """
    if not symbol.endswith((".SH", ".SZ", ".BJ")):
        raise ValueError(f"announcement 源仅支持 A 股（cninfo 沪深京）: {symbol}")
    code, setcode, _c, _m = _symbol_parts(symbol)
    df = ak.stock_zh_a_disclosure_report_cninfo(
        symbol=code, market="沪深京",
        start_date=_date_compact(start), end_date=_date_compact(end))
    if df is None or df.empty:
        return None
    out = out_dir / "announcement" / date / run_id
    out.mkdir(parents=True, exist_ok=True)
    fp = out / f"{symbol}.csv"
    with open(fp, "w", newline="", encoding="utf-8") as f:
        f.write("title,time,url,source,summary,code,setcode,name\n")
        for _, r in df.iterrows():
            title = str(r["公告标题"]).strip()
            t = r["公告时间"]
            time_s = (t.strftime("%Y-%m-%d %H:%M:%S") if hasattr(t, "strftime")
                      else str(t).strip())
            url = str(r["公告链接"]).strip()
            name = str(r.get("简称") or "").strip()
            f.write(",".join([
                _csv_escape(title), time_s, url,
                "巨潮资讯", _csv_escape(title), code, setcode, _csv_escape(name),
            ]) + "\n")
    return fp


# ---------------------------------------------------------------- calendar（披露预约 + 解禁日程，r2 Phase 1）

def _date_str(v) -> str:
    """pandas Timestamp / NaT / str → 'YYYY-MM-DD'；空/NaT 返回 ''。

    注意 NaT 也有 strftime 属性但调用即抛 ValueError（真实采集踩过），需吞掉。
    """
    if v is None:
        return ""
    try:
        s = v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v).strip()[:10]
    except (ValueError, TypeError):
        return ""
    if not s or s.startswith("NaT") or s.lower().startswith("nan"):
        return ""
    return s


def collect_calendar_disclosure(ak, symbols: list[str], period: str,
                                out_dir: Path, date: str, run_id: str) -> Path | None:
    """财报披露预约（stock_report_disclosure，沪深京）→ report_disclosure.csv。

    全市场拉取后仅留 watchlist 行；scheduled_date 取"当前预约"（三次变更依次
    覆盖），首次预约/实际披露原样留档供 adapter 拼 note。
    """
    watch = {s.split(".")[0].zfill(6): s for s in symbols
             if s.endswith((".SH", ".SZ", ".BJ"))}
    df = ak.stock_report_disclosure(market="沪深京", period=period)
    if df is None or df.empty:
        return None
    out = out_dir / "calendar" / date / run_id
    out.mkdir(parents=True, exist_ok=True)
    fp = out / "report_disclosure.csv"
    n = 0
    with open(fp, "w", newline="", encoding="utf-8") as f:
        f.write("symbol,name,period,scheduled_date,first_scheduled,actual_disclosed\n")
        for _, r in df.iterrows():
            sym = watch.get(str(r["股票代码"]).split(".")[0].zfill(6))
            if sym is None:
                continue
            sched = (_date_str(r["三次变更"]) or _date_str(r["二次变更"])
                     or _date_str(r["初次变更"]) or _date_str(r["首次预约"]))
            f.write(",".join([
                sym, _csv_escape(str(r["股票简称"])), period, sched,
                _date_str(r["首次预约"]), _date_str(r["实际披露"]),
            ]) + "\n")
            n += 1
    return fp if n else None


def collect_calendar_unlock(ak, symbols: list[str], since: str,
                            out_dir: Path, date: str, run_id: str) -> Path | None:
    """解禁日程（stock_restricted_release_queue_em 逐股，仅留 >= since 未来行）→ unlock.csv。

    接口无简称列，CSV 不含 name；个股拉取失败记 stderr 后继续，不整批中止。
    """
    out = out_dir / "calendar" / date / run_id
    out.mkdir(parents=True, exist_ok=True)
    fp = out / "unlock.csv"
    n = 0
    with open(fp, "w", newline="", encoding="utf-8") as f:
        f.write("symbol,unlock_date,shares_free,ratio_total,share_type\n")
        for sym in symbols:
            if not sym.endswith((".SH", ".SZ", ".BJ")):
                continue
            try:
                df = ak.stock_restricted_release_queue_em(symbol=sym.split(".")[0])
            except Exception as e:  # noqa: BLE001
                print(f"[calendar] unlock {sym}: ERROR {e}")
                continue
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                d = _date_str(r["解禁时间"])
                if not d or d < since:
                    continue
                f.write(",".join([
                    sym, d, _num(r["解禁数量"]) or "", _num(r["占总市值比例"]) or "",
                    _csv_escape(str(r.get("限售股类型") or "")),
                ]) + "\n")
                n += 1
    return fp if n else None


# ---------------------------------------------------------------- macro（宏观因子快照，r2 Phase 2）

_MACRO_SCHEMA = {
    "type": "object",
    "required": ["schema_version", "factors"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "integer"},
        "factors": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["factor_type", "code", "name", "market", "unit", "api"],
                "additionalProperties": False,
                "properties": {
                    "factor_type": {"enum": ["commodity", "fx", "index_proxy"]},
                    "code": {"type": "string"},
                    "name": {"type": "string"},
                    "market": {"enum": ["CN", "GLOBAL"]},
                    "unit": {"type": "string"},
                    "api": {"enum": ["domestic", "foreign", "boc"]},
                    "boc_symbol": {"type": "string"},
                },
            },
        },
    },
}


def collect_macro(ak, out_dir: Path, date: str, run_id: str) -> Path | None:
    """宏观因子快照（config/macro_factors.yaml 清单驱动）→ macro.csv。

    内盘期货 futures_zh_daily_sina(连续合约)/外盘 futures_foreign_hist 取最后一根
    日线；外汇 currency_boc_sina 取窗口内最后一日"央行中间价"（缺失回退"中行折算价"）。
    close 存来源原始值；来源无涨跌幅，change_pct 留空（adapter 不计算，r2 §3.2）。
    单因子失败记 stderr 后继续，不整批中止（§2.5：缺的因子不冒充）。
    """
    doc = yaml.safe_load(MACRO_CONFIG.read_text(encoding="utf-8"))
    jsonschema.validate(doc, _MACRO_SCHEMA)
    out = out_dir / "macro" / date / run_id
    out.mkdir(parents=True, exist_ok=True)
    fp = out / "macro.csv"
    day = datetime.strptime(date, "%Y-%m-%d")
    boc_start = _date_compact((day - timedelta(days=10)).date().isoformat())
    boc_end = _date_compact(date)
    n = 0
    with open(fp, "w", newline="", encoding="utf-8") as f:
        f.write("factor_type,code,name,market,trade_date,close,change_pct,unit\n")
        for fac in doc["factors"]:
            try:
                if fac["api"] == "domestic":
                    df = ak.futures_zh_daily_sina(symbol=fac["code"])
                    row, d = df.iloc[-1], _date_str(df.iloc[-1]["date"])
                    close = _num(row["close"])
                elif fac["api"] == "foreign":
                    df = ak.futures_foreign_hist(symbol=fac["code"])
                    row, d = df.iloc[-1], _date_str(df.iloc[-1]["date"])
                    close = _num(row["close"])
                else:  # boc
                    df = ak.currency_boc_sina(symbol=fac["boc_symbol"],
                                              start_date=boc_start, end_date=boc_end)
                    row, d = df.iloc[-1], _date_str(df.iloc[-1]["日期"])
                    mid = _num(row["央行中间价"])
                    close = mid or _num(row["中行折算价"])
            except Exception as e:  # noqa: BLE001
                print(f"[macro] {fac['code']}: ERROR {e}")
                continue
            if not d or not close:
                print(f"[macro] {fac['code']}: EMPTY（{d or '无日期'}）")
                continue
            f.write(",".join([fac["factor_type"], fac["code"],
                              _csv_escape(fac["name"]), fac["market"], d,
                              close, "", _csv_escape(fac["unit"])]) + "\n")
            n += 1
    return fp if n else None


# ---------------------------------------------------------------- flow（龙虎榜 + 大宗，r2 Phase 2）

def collect_flow(ak, symbols: list[str], start: str, end: str,
                 out_dir: Path, date: str, run_id: str) -> tuple[Path | None, Path | None]:
    """龙虎榜 + 大宗交易（仅留 watchlist 行）→ flow/{date}/{run_id}/{lhb,dzjy}.csv。

    龙虎榜按"股票×日"合并多上榜原因（数值取首行，不跨原因加总）；大宗每笔一行。
    返回 (lhb_fp, dzjy_fp)，可为 None（源空/无 watchlist 命中）。
    """
    watch = {s.split(".")[0].zfill(6): s for s in symbols
             if s.endswith((".SH", ".SZ", ".BJ"))}
    out = out_dir / "flow" / date / run_id
    out.mkdir(parents=True, exist_ok=True)

    lhb_fp = out / "lhb.csv"
    n_lhb = 0
    grouped: dict[tuple[str, str], dict] = {}
    df = ak.stock_lhb_detail_em(start_date=_date_compact(start),
                                end_date=_date_compact(end))
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            sym = watch.get(str(r["代码"]).zfill(6))
            if sym is None:
                continue
            key = (sym, _date_str(r["上榜日"]))
            g = grouped.setdefault(key, {"reasons": [], "first": r})
            if r["上榜原因"] not in g["reasons"]:
                g["reasons"].append(r["上榜原因"])
    with open(lhb_fp, "w", newline="", encoding="utf-8") as f:
        f.write("symbol,trade_date,reasons,close,pct_chg,net_buy,net_buy_ratio\n")
        for (sym, day), g in sorted(grouped.items()):
            r = g["first"]
            f.write(",".join([
                sym, day, _csv_escape("；".join(g["reasons"])),
                _num(r["收盘价"]) or "", _num(r["涨跌幅"]) or "",
                _num(r["龙虎榜净买额"]) or "", _num(r["净买额占总成交比"]) or "",
            ]) + "\n")
            n_lhb += 1
    lhb_fp = lhb_fp if n_lhb else None

    dzjy_fp = out / "dzjy.csv"
    n_dzjy = 0
    df = ak.stock_dzjy_mrmx(symbol="A股", start_date=_date_compact(start),
                            end_date=_date_compact(end))
    with open(dzjy_fp, "w", newline="", encoding="utf-8") as f:
        f.write("symbol,trade_date,close,pct_chg,price,premium_rate,volume,"
                "amount,buy_branch,sell_branch\n")
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                sym = watch.get(str(r["证券代码"]).zfill(6))
                if sym is None:
                    continue
                f.write(",".join([
                    sym, _date_str(r["交易日期"]), _num(r["收盘价"]) or "",
                    _num(r["涨跌幅"]) or "", _num(r["成交价"]) or "",
                    _num(r["折溢率"]) or "", _num(r["成交量"]) or "",
                    _num(r["成交额"]) or "",
                    _csv_escape(str(r["买方营业部"] or "")),
                    _csv_escape(str(r["卖方营业部"] or "")),
                ]) + "\n")
                n_dzjy += 1
    dzjy_fp = dzjy_fp if n_dzjy else None
    return lhb_fp, dzjy_fp


# ---------------------------------------------------------------- meta / CLI

def write_meta(out_dir: Path, data_type: str, date: str, run_id: str,
               requests: list[dict], purpose: str) -> None:
    out = out_dir / data_type / date / run_id
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "run_id": run_id, "source": "akshare", "data_type": data_type,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": purpose, "requests": requests,
    }
    with open(out / "_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _watchlist_symbols() -> list[str]:
    doc = yaml.safe_load(WATCHLIST.read_text(encoding="utf-8"))
    return [s["symbol"] for s in doc.get("stocks", []) if s.get("active", True)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.collect.akshare_collect")
    parser.add_argument("--sources", default="price,financials,index,telegraph,announcement,macro,flow")
    parser.add_argument("--symbols", default="",
                        help="逗号分隔；默认取 config/watchlist.yaml 全部 active 股票")
    parser.add_argument("--indexes", default="000300.SH,^HSI")
    parser.add_argument("--start", default="2023-08-10")
    parser.add_argument("--end", default="2026-08-25")
    parser.add_argument("--price-api", default="em", choices=["em", "sina"],
                        help="price/forward 源：em=东财（默认），sina=新浪（A 股备用）")
    parser.add_argument("--date", required=True, help="落盘目录日期 YYYY-MM-DD")
    parser.add_argument("--run-id", default="run_ak")
    parser.add_argument("--out-root", default=str(DEFAULT_OUT))
    parser.add_argument("--purpose", default="akshare 采集器（可选数据源，字段对齐现有 adapter）")
    parser.add_argument("--calendar-period", default=None,
                        help="calendar 源必填：财报披露预约期次（如 2026半年报/2026三季/2026年报）")
    args = parser.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or _watchlist_symbols()
    indexes = [s.strip() for s in args.indexes.split(",") if s.strip()]
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    out_dir = Path(args.out_root)

    ak = _load_akshare()
    for source in sources:
        requests: list[dict] = []
        if source in ("price", "forward"):
            adjust = "qfq" if source == "forward" else ""
            api_name = ("stock_zh_a_daily" if args.price_api == "sina"
                        else "stock_zh_a_hist")
            for sym in symbols:
                try:
                    fp = collect_price(ak, sym, args.start, args.end, out_dir,
                                       args.date, args.run_id, adjust=adjust,
                                       api=args.price_api)
                    requests.append({"api": api_name if sym.endswith((".SH", ".SZ", ".BJ"))
                                     else "stock_hk_hist",
                                     "params": {"symbol": sym, "start": args.start,
                                                "end": args.end, "adjust": adjust,
                                                "price_api": args.price_api},
                                     "file": fp.name if fp else None,
                                     "status": "ok" if fp else "empty", "error": None if fp else "EMPTY_DATA"})
                    print(f"[{source}] {sym}: {'ok' if fp else 'EMPTY'}")
                except Exception as e:  # noqa: BLE001
                    requests.append({"api": api_name, "params": {"symbol": sym},
                                     "file": None, "status": "error", "error": f"{type(e).__name__}: {e}"})
                    print(f"[{source}] {sym}: ERROR {e}")
        elif source == "financials":
            for sym in symbols:
                try:
                    paths = collect_financials(ak, sym, out_dir, args.date, args.run_id)
                    requests.append({"api": "stock_profit_sheet_by_report_em",
                                     "params": {"symbol": sym},
                                     "file": ",".join(p.name for p in paths) or None,
                                     "status": "ok" if paths else "empty",
                                     "error": None if paths else "EMPTY_DATA"})
                    print(f"[financials] {sym}: {len(paths)} 期")
                except Exception as e:  # noqa: BLE001
                    requests.append({"api": "stock_profit_sheet_by_report_em",
                                     "params": {"symbol": sym}, "file": None,
                                     "status": "error", "error": f"{type(e).__name__}: {e}"})
                    print(f"[financials] {sym}: ERROR {e}")
        elif source == "index":
            for idx in indexes:
                try:
                    fp = collect_index(ak, idx, args.start, args.end, out_dir, args.date, args.run_id)
                    requests.append({"api": "stock_zh_index_daily",
                                     "params": {"index": idx},
                                     "file": fp.name if fp else None,
                                     "status": "ok" if fp else "empty",
                                     "error": None if fp else "EMPTY_DATA"})
                    print(f"[index] {idx}: {'ok' if fp else 'EMPTY'}")
                except Exception as e:  # noqa: BLE001
                    requests.append({"api": "stock_zh_index_daily", "params": {"index": idx},
                                     "file": None, "status": "error", "error": f"{type(e).__name__}: {e}"})
                    print(f"[index] {idx}: ERROR {e}")
        elif source == "telegraph":
            try:
                fp = collect_telegraph(ak, out_dir, args.date, args.run_id)
                requests.append({"api": "stock_info_global_cls", "params": {},
                                 "file": fp.name if fp else None,
                                 "status": "ok" if fp else "empty",
                                 "error": None if fp else "EMPTY_DATA"})
                print(f"[telegraph] {args.date}: {'ok' if fp else 'EMPTY'}")
            except Exception as e:  # noqa: BLE001
                requests.append({"api": "stock_info_global_cls", "params": {},
                                 "file": None, "status": "error", "error": f"{type(e).__name__}: {e}"})
                print(f"[telegraph] ERROR {e}")
        elif source == "forecast":
            for sym in symbols:
                try:
                    fp = collect_forecast(ak, sym, out_dir, args.date, args.run_id)
                    requests.append({"api": "stock_profit_forecast_ths",
                                     "params": {"symbol": sym},
                                     "file": fp.name if fp else None,
                                     "status": "ok" if fp else "empty",
                                     "error": None if fp else "EMPTY_DATA"})
                    print(f"[forecast] {sym}: {'ok' if fp else 'EMPTY'}")
                except Exception as e:  # noqa: BLE001
                    requests.append({"api": "stock_profit_forecast_ths",
                                     "params": {"symbol": sym}, "file": None,
                                     "status": "error", "error": f"{type(e).__name__}: {e}"})
                    print(f"[forecast] {sym}: ERROR {e}")
        elif source == "stock_info":
            for sym in symbols:
                try:
                    fp = collect_stock_info(ak, sym, out_dir, args.date, args.run_id)
                    requests.append({"api": "stock_zh_a_gbjg_em",
                                     "params": {"symbol": sym},
                                     "file": fp.name if fp else None,
                                     "status": "ok" if fp else "empty",
                                     "error": None if fp else "EMPTY_DATA"})
                    print(f"[stock_info] {sym}: {'ok' if fp else 'EMPTY'}")
                except Exception as e:  # noqa: BLE001
                    requests.append({"api": "stock_zh_a_gbjg_em",
                                     "params": {"symbol": sym}, "file": None,
                                     "status": "error", "error": f"{type(e).__name__}: {e}"})
                    print(f"[stock_info] {sym}: ERROR {e}")
        elif source == "announcement":
            for sym in symbols:
                try:
                    fp = collect_announcement(ak, sym, args.start, args.end,
                                              out_dir, args.date, args.run_id)
                    requests.append({"api": "stock_zh_a_disclosure_report_cninfo",
                                     "params": {"symbol": sym, "start": args.start,
                                                "end": args.end, "market": "沪深京"},
                                     "file": fp.name if fp else None,
                                     "status": "ok" if fp else "empty",
                                     "error": None if fp else "EMPTY_DATA"})
                    print(f"[{source}] {sym}: {'ok' if fp else 'EMPTY'}")
                except Exception as e:  # noqa: BLE001
                    requests.append({"api": "stock_zh_a_disclosure_report_cninfo",
                                     "params": {"symbol": sym}, "file": None,
                                     "status": "error", "error": f"{type(e).__name__}: {e}"})
                    print(f"[{source}] {sym}: ERROR {e}")
        elif source == "calendar":  # r2 Phase 1：日历源（手触发，不进 daily 惯例）
            if not args.calendar_period:
                requests.append({"api": "stock_report_disclosure", "params": {},
                                 "file": None, "status": "error",
                                 "error": "缺少 --calendar-period（如 2026半年报）"})
                print("[calendar] ERROR 缺少 --calendar-period（如 2026半年报）")
            else:
                try:
                    fp = collect_calendar_disclosure(ak, symbols, args.calendar_period,
                                                     out_dir, args.date, args.run_id)
                    requests.append({"api": "stock_report_disclosure",
                                     "params": {"market": "沪深京",
                                                "period": args.calendar_period},
                                     "file": fp.name if fp else None,
                                     "status": "ok" if fp else "empty",
                                     "error": None if fp else "EMPTY_DATA"})
                    print(f"[calendar] disclosure({args.calendar_period}): "
                          f"{'ok' if fp else 'EMPTY'}")
                except Exception as e:  # noqa: BLE001
                    requests.append({"api": "stock_report_disclosure", "params": {},
                                     "file": None, "status": "error",
                                     "error": f"{type(e).__name__}: {e}"})
                    print(f"[calendar] disclosure ERROR {e}")
                try:
                    fp = collect_calendar_unlock(ak, symbols, args.date,
                                                 out_dir, args.date, args.run_id)
                    requests.append({"api": "stock_restricted_release_queue_em",
                                     "params": {"symbols": len(symbols),
                                                "since": args.date},
                                     "file": fp.name if fp else None,
                                     "status": "ok" if fp else "empty",
                                     "error": None if fp else "EMPTY_DATA"})
                    print(f"[calendar] unlock: {'ok' if fp else 'EMPTY'}")
                except Exception as e:  # noqa: BLE001
                    requests.append({"api": "stock_restricted_release_queue_em",
                                     "params": {}, "file": None, "status": "error",
                                     "error": f"{type(e).__name__}: {e}"})
                    print(f"[calendar] unlock ERROR {e}")
        elif source == "macro":  # r2 Phase 2：宏观因子快照（清单驱动）
            try:
                fp = collect_macro(ak, out_dir, args.date, args.run_id)
                requests.append({"api": "futures_zh_daily_sina/futures_foreign_hist/"
                                         "currency_boc_sina",
                                 "params": {"config": "config/macro_factors.yaml"},
                                 "file": fp.name if fp else None,
                                 "status": "ok" if fp else "empty",
                                 "error": None if fp else "EMPTY_DATA"})
                print(f"[macro] {'ok' if fp else 'EMPTY'}")
            except Exception as e:  # noqa: BLE001
                requests.append({"api": "macro_factors", "params": {},
                                 "file": None, "status": "error",
                                 "error": f"{type(e).__name__}: {e}"})
                print(f"[macro] ERROR {e}")
        elif source == "flow":  # r2 Phase 2：龙虎榜 + 大宗（静默入库）
            # 窗口硬上限 10 天（--start 默认 2023 起会让龙虎榜查询跨三年，--end 硬编码教训）
            end_d = datetime.strptime(args.end, "%Y-%m-%d")
            flow_start = max(args.start, (end_d - timedelta(days=9)).date().isoformat())
            try:
                lhb_fp, dzjy_fp = collect_flow(ak, symbols, flow_start, args.end,
                                               out_dir, args.date, args.run_id)
                requests.append({"api": "stock_lhb_detail_em",
                                 "params": {"start": flow_start, "end": args.end},
                                 "file": lhb_fp.name if lhb_fp else None,
                                 "status": "ok" if lhb_fp else "empty",
                                 "error": None if lhb_fp else "EMPTY_DATA"})
                print(f"[flow] lhb: {'ok' if lhb_fp else 'EMPTY'}")
                requests.append({"api": "stock_dzjy_mrmx",
                                 "params": {"start": args.start, "end": args.end,
                                            "symbol": "A股"},
                                 "file": dzjy_fp.name if dzjy_fp else None,
                                 "status": "ok" if dzjy_fp else "empty",
                                 "error": None if dzjy_fp else "EMPTY_DATA"})
                print(f"[flow] dzjy: {'ok' if dzjy_fp else 'EMPTY'}")
            except Exception as e:  # noqa: BLE001
                requests.append({"api": "stock_lhb_detail_em", "params": {},
                                 "file": None, "status": "error",
                                 "error": f"{type(e).__name__}: {e}"})
                print(f"[flow] ERROR {e}")
        else:
            print(f"[skip] 未知 source: {source}（可选 price,forward,financials,index,"
                  f"telegraph,forecast,stock_info,announcement,calendar,macro,flow）")
            continue
        write_meta(out_dir, source, args.date, args.run_id, requests, args.purpose)
        errs = [r for r in requests if r["status"] == "error"]
        print(f"== {source}: {len(requests)} requests, {len(errs)} errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
