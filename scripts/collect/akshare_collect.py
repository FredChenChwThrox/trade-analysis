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
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[2]
WATCHLIST = ROOT / "config" / "watchlist.yaml"
DEFAULT_OUT = ROOT / "data" / "raw" / "akshare"

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
    parser.add_argument("--sources", default="price,financials,index,telegraph")
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
        else:
            print(f"[skip] 未知 source: {source}（可选 price,forward,financials,index,"
                  f"telegraph,forecast,stock_info）")
            continue
        write_meta(out_dir, source, args.date, args.run_id, requests, args.purpose)
        errs = [r for r in requests if r["status"] == "error"]
        print(f"== {source}: {len(requests)} requests, {len(errs)} errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
