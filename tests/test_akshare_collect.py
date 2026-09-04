"""akshare 采集器测试：mock akshare，验证落盘 CSV 字段与项目库 schema 对齐。

对齐目标（现有 adapter 已验证的列约定）：
- price/index    → kimi stock_finance_data 列约定（thscode,time,open,high,low,close,volume,amount,currency）
- financials     → tdx 列约定（code,setcode,period_end,...,published_at）
- telegraph      → events 表字段（published_at UTC / published_tz / title / summary / content_hash）
"""

import json

import pandas as pd
import pytest

from scripts.collect import akshare_collect as ac


class FakeAk:
    """akshare 的 DataFrame 形状模拟（列名与真实接口一致）。"""

    def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust):
        return pd.DataFrame({
            "日期": ["20260803", "20260804"], "股票代码": [symbol, symbol],
            "开盘": [10.0, 10.5], "收盘": [10.2, 10.8], "最高": [10.5, 11.0],
            "最低": [9.9, 10.4], "成交量": [10000, 12000], "成交额": [1.0e7, 1.3e7],
            "振幅": [6.0, 6.0], "涨跌幅": [1.0, 5.9], "涨跌额": [0.1, 0.6],
            "换手率": [0.5, 0.6],
        })

    def stock_zh_a_daily(self, symbol, start_date, end_date, adjust):
        return pd.DataFrame({
            "date": [pd.Timestamp("2023-09-28"), pd.Timestamp("2023-10-09")],
            "open": [97.60, 96.93], "high": [98.60, 97.70],
            "low": [96.28, 94.94], "close": [96.93, 95.60],
            "volume": [1749067.0, 1374150.0],  # 新浪单位已是「股」
            "amount": [170115471.0, 131923993.0],
            "outstanding_share": [225000000.0, 225000000.0],
            "turnover": [0.007774, 0.006107],
        })

    def stock_hk_hist(self, symbol, period, start_date, end_date, adjust):
        return pd.DataFrame({
            "日期": ["20260803", "20260804"], "开盘": [300.0, 302.0], "收盘": [301.0, 305.0],
            "最高": [303.0, 306.0], "最低": [299.0, 301.0], "成交量": [8000, 9000],
            "成交额": [2.0e8, 2.2e8], "振幅": [1.3, 1.6], "涨跌幅": [0.3, 1.3],
            "涨跌额": [1.0, 4.0], "换手率": [0.1, 0.1],
        })

    def stock_profit_sheet_by_report_em(self, symbol):
        return pd.DataFrame([
            {"SECUCODE": "603605.SH", "SECURITY_CODE": "603605", "SECURITY_NAME_ABBR": "珀莱雅",
             "REPORT_DATE": "2025-12-31", "REPORT_TYPE": "年报", "REPORT_DATE_NAME": "2025年报",
             "NOTICE_DATE": "2026-04-15", "CURRENCY": "CNY",
             "OPERATE_INCOME": 10700000000.0, "PARENT_NETPROFIT": 1500000000.0,
             "BASIC_EPS": 3.80, "DILUTED_EPS": 3.78},
            {"SECUCODE": "603605.SH", "SECURITY_CODE": "603605", "SECURITY_NAME_ABBR": "珀莱雅",
             "REPORT_DATE": "2026-06-30", "REPORT_TYPE": "中报", "REPORT_DATE_NAME": "2026中报",
             "NOTICE_DATE": "2026-08-25", "CURRENCY": "CNY",
             "OPERATE_INCOME": 5374800725.36, "PARENT_NETPROFIT": 1167896499.41,
             "BASIC_EPS": 2.96, "DILUTED_EPS": 2.94},
        ])

    def stock_zh_index_daily(self, symbol):
        return pd.DataFrame({
            "date": [pd.Timestamp("2026-08-03"), pd.Timestamp("2026-08-04")],
            "open": [4000.0, 4010.0], "high": [4020.0, 4030.0], "low": [3990.0, 4005.0],
            "close": [4015.0, 4025.0], "volume": [300000000.0, 320000000.0],
        })

    def stock_hk_index_daily_sina(self, symbol):
        return pd.DataFrame({
            "date": [pd.Timestamp("2026-08-03"), pd.Timestamp("2026-08-04")],
            "open": [22000.0, 22100.0], "high": [22200.0, 22250.0], "low": [21900.0, 22050.0],
            "close": [22150.0, 22200.0], "volume": [2500000.0, 2600000.0],
            "amount": [1.5e10, 1.6e10],
        })

    def stock_info_global_cls(self, symbol):
        return pd.DataFrame({
            "标题": ["珀莱雅：上半年净利同比增长", "某无关快讯"],
            "内容": ["财联社8月25日电，珀莱雅（603605.SH）发布中报。",
                     "财联社8月25日电，某行业数据公布。"],
            "发布日期": [pd.Timestamp("2026-08-25"), pd.Timestamp("2026-08-25")],
            "发布时间": [pd.Timestamp("10:30:00").time(), pd.Timestamp("10:31:00").time()],
        })

    def stock_profit_forecast_ths(self, symbol, indicator):
        if indicator == "预测年报净利润":  # 单位：亿元
            return pd.DataFrame({
                "年度": [2026, 2027, 2028],
                "预测机构数": [23, 23, 19],
                "最小值": [196.83, 207.52, 332.50],
                "均值": [329.17, 368.37, 419.41],
                "最大值": [385.10, 449.28, 538.99],
                "行业平均数": [167.41, 198.79, 229.54],
            })
        if indicator == "预测年报每股收益":
            return pd.DataFrame({
                "年度": [2026, 2027, 2028],
                "预测机构数": [23, 23, 19],
                "最小值": [0.92, 0.97, 1.55],
                "均值": [1.54, 1.72, 1.96],
                "最大值": [1.80, 2.10, 2.52],
                "行业平均数": [2.07, 2.53, 3.01],
            })
        return pd.DataFrame()  # 预测年报主营业务收入等：空

    def stock_zh_a_gbjg_em(self, symbol):
        return pd.DataFrame({
            "变更日期": ["2025-07-16", "2025-02-06"],
            "总股本": [21394310176, 21499240619],
            "已流通股份": [21394310176, 21499240619],
            "已上市流通A股": [1.746084e10, 1.756577e10],
            "变动原因": ["回购", "回购"],
        })

    def stock_zh_a_gdhs_detail_em(self, symbol):
        return pd.DataFrame({
            "股东户数统计截止日": ["2026-06-30", "2026-03-31"],
            "区间涨跌幅": [1.2, -3.4],
            "股东户数-本次": [52000, 55000],
            "股东户数-上次": [55000, 53000],
            "股东户数-增减": [-3000, 2000],
            "股东户数-增减比例": [-5.45, 3.77],   # 百分点，采集侧归一小数
            "户均持股市值": [91793.06, 88000.0],
            "户均持股数量": [4155.41, 4000.0],
            "总市值": [4.778e9, 4.84e9],
            "总股本": [395976100, 395976100],
            "股本变动": [0, 0],
            "股东户数公告日期": ["2026-08-20", "2026-04-25"],
            "代码": [symbol, symbol],
            "名称": ["珀莱雅", "珀莱雅"],
        })

    def stock_zh_a_disclosure_report_cninfo(self, symbol, market, start_date, end_date):
        # 记录入参供测试断言紧凑日期格式（接口对带 - 格式静默返回空，实测）
        self.last_cninfo_kwargs = {"symbol": symbol, "market": market,
                                   "start_date": start_date, "end_date": end_date}
        return pd.DataFrame({
            "代码": [symbol, symbol],
            "简称": ["洛阳钼业", "洛阳钼业"],
            "公告标题": ["洛阳钼业关于对外担保计划的公告", "标题含逗号，应被引号包裹"],
            "公告时间": [pd.Timestamp("2026-08-21"), pd.Timestamp("2026-08-22")],
            "公告链接": ["http://cninfo/ann/1", "http://cninfo/ann/2"],
        })


@pytest.fixture()
def fake_ak():
    return FakeAk()


@pytest.fixture()
def out_dir(tmp_path):
    return tmp_path / "akshare_out"


def _meta(out_dir, data_type):
    m = out_dir / data_type / "2026-08-25" / "run_ak" / "_meta.json"
    return json.loads(m.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- price

def test_collect_price_a_share_aligned(fake_ak, out_dir):
    path = ac.collect_price(fake_ak, "603605.SH", "20260801", "20260807",
                            out_dir, "2026-08-25", "run_ak")
    assert path.name == "603605.SH.csv"
    rows = list(csv_rows(path))
    assert list(rows[0].keys()) == ["thscode", "time", "open", "high", "low",
                                    "close", "volume", "amount", "currency",
                                    "turnover"]
    assert rows[0]["thscode"] == "603605.SH"
    assert rows[0]["time"] == "20260803"
    # 关键对齐：东财成交量单位"手" → 项目 volume_raw 口径"股"（×100）
    assert rows[0]["volume"] == "1000000"
    assert rows[1]["volume"] == "1200000"
    assert rows[0]["amount"] == "10000000.0"
    assert rows[0]["currency"] == "CNY"
    # 东财换手率为百分点 → 归一小数（0.5 → 0.005）
    assert rows[0]["turnover"] == "0.005"
    assert rows[1]["turnover"] == "0.006"
    ac.write_meta(out_dir, "price", "2026-08-25", "run_ak",
                  [{"api": "stock_zh_a_hist", "params": {"symbol": "603605.SH"},
                    "file": "603605.SH.csv", "status": "ok"}], "test")
    m = _meta(out_dir, "price")
    assert m["run_id"] == "run_ak"
    assert m["requests"][0]["status"] == "ok"


def test_collect_price_sina_turnover_fraction(fake_ak, out_dir):
    """sina 源 turnover 原生小数，透传不换算。"""
    path = ac.collect_price(fake_ak, "603605.SH", "20230901", "20231010",
                            out_dir, "2026-09-04", "run_sina", api="sina")
    rows = list(csv_rows(path))
    assert rows[0]["turnover"] == "0.007774"


def test_collect_price_hk_aligned(fake_ak, out_dir):
    path = ac.collect_price(fake_ak, "00700.HK", "20260801", "20260807",
                            out_dir, "2026-08-25", "run_ak")
    rows = list(csv_rows(path))
    assert rows[0]["thscode"] == "00700.HK"
    assert rows[0]["volume"] == "800000"  # 手 → 股
    assert rows[0]["currency"] == "HKD"
    assert rows[0]["turnover"] == ""  # 港股不采集换手率（流通股口径差异）


def test_collect_price_forward_qfq(fake_ak, out_dir):
    """adjust='qfq' → {symbol}_forward.csv（ingest 按 *_forward* 跳过，adjust 模块专用）。"""
    path = ac.collect_price(fake_ak, "600563.SH", "20230828", "20260825",
                            out_dir, "2026-08-26", "run_ak", adjust="qfq")
    assert path.name == "600563.SH_forward.csv"
    rows = list(csv_rows(path))
    assert list(rows[0].keys()) == ["thscode", "time", "open", "high", "low",
                                    "close", "volume", "amount", "currency",
                                    "turnover"]
    assert rows[0]["thscode"] == "600563.SH"
    assert rows[0]["turnover"] == ""  # forward 文件不采集换手率（ingest 跳过）


def test_collect_holder_stats_aligned(fake_ak, out_dir):
    path = ac.collect_holder_stats(fake_ak, "603605.SH", out_dir,
                                   "2026-09-04", "run_gd")
    assert path.name == "603605.SH_gdhs.csv"
    rows = list(csv_rows(path))
    assert list(rows[0].keys()) == [
        "code", "setcode", "stat_date", "holder_count", "holder_count_prev",
        "holder_count_delta", "holder_count_delta_pct", "avg_hold_value",
        "avg_hold_shares", "total_share", "share_change", "announced_at"]
    assert rows[0]["stat_date"] == "2026-06-30"
    assert rows[0]["holder_count"] == "52000"
    # 增减比例百分点 → 归一小数（-5.45 → -0.0545）
    assert rows[0]["holder_count_delta_pct"] == "-0.0545"
    assert rows[0]["announced_at"] == "2026-08-20"


def test_collect_price_sina_volume_not_converted(fake_ak, out_dir):
    """sina 备用源：volume 单位已是「股」，不再 ×100；date 列名与东财不同。"""
    path = ac.collect_price(fake_ak, "600563.SH", "20230928", "20231020",
                            out_dir, "2026-08-26", "run_ak", api="sina")
    assert path.name == "600563.SH.csv"
    rows = list(csv_rows(path))
    assert rows[0]["time"] == "20230928"
    assert rows[0]["volume"] == "1749067.0"      # 原样落盘，不换算
    assert rows[0]["amount"] == "170115471.0"
    assert rows[0]["currency"] == "CNY"
    # sina 源 qfq 同样走 *_forward 命名
    path2 = ac.collect_price(fake_ak, "600563.SH", "20230928", "20260825",
                             out_dir, "2026-08-26", "run_ak", adjust="qfq", api="sina")
    assert path2.name == "600563.SH_forward.csv"


# ---------------------------------------------------------------- financials

def test_collect_financials_aligned(fake_ak, out_dir):
    paths = ac.collect_financials(fake_ak, "603605.SH", out_dir, "2026-08-25", "run_ak")
    assert sorted(p.name for p in paths) == ["603605.SH_is_20251231.csv",
                                             "603605.SH_is_20260630.csv"]
    for p in paths:
        rows = list(csv_rows(p))
        assert list(rows[0].keys()) == ["code", "setcode", "period_end", "fiscal_year",
                                        "revenue", "net_profit_attr", "eps_basic",
                                        "eps_diluted", "currency", "unit",
                                        "is_cumulative", "published_at"]
    annual = list(csv_rows(out_dir / "financials" / "2026-08-25" / "run_ak" /
                           "603605.SH_is_20251231.csv"))[0]
    assert annual["code"] == "603605"
    assert annual["setcode"] == "1"
    assert annual["period_end"] == "2025-12-31"
    assert annual["fiscal_year"] == "2025"
    assert annual["revenue"] == "10700000000.0"
    assert annual["net_profit_attr"] == "1500000000.0"
    assert annual["eps_basic"] == "3.8"  # float 精度：3.80 → 3.8（adapter dec_str 定点化）
    assert annual["currency"] == "CNY"
    assert annual["unit"] == "yuan"
    assert annual["is_cumulative"] == "1"
    assert annual["published_at"] == "2026-04-15"  # NOTICE_DATE 披露日
    interim = list(csv_rows(out_dir / "financials" / "2026-08-25" / "run_ak" /
                            "603605.SH_is_20260630.csv"))[0]
    assert interim["published_at"] == "2026-08-25"
    assert interim["net_profit_attr"] == "1167896499.41"


def test_collect_financials_hk_symbol(fake_ak, out_dir):
    # 港股 symbol → code/setcode 映射（31=HK）
    paths = ac.collect_financials(fake_ak, "00700.HK", out_dir, "2026-08-25", "run_ak")
    assert paths  # FakeAk 返回的是 A 股形状；仅验证 symbol 推断不抛错


# ---------------------------------------------------------------- index

def test_collect_index_aligned(fake_ak, out_dir):
    path = ac.collect_index(fake_ak, "000300.SH", "20260801", "20260807",
                            out_dir, "2026-08-25", "run_ak")
    assert path.name == "000300.SH.csv"
    rows = list(csv_rows(path))
    assert rows[0]["thscode"] == "000300.SH"
    assert rows[0]["time"] == "20260803"
    assert rows[0]["close"] == "4015.0"
    # 指数 volume 不换算（保持原值）
    assert rows[0]["volume"] == "300000000.0"


def test_collect_index_hk(fake_ak, out_dir):
    path = ac.collect_index(fake_ak, "^HSI", "20260801", "20260807",
                            out_dir, "2026-08-25", "run_ak")
    rows = list(csv_rows(path))
    assert rows[0]["thscode"] == "^HSI"
    assert rows[0]["amount"] == "15000000000.0"


# ---------------------------------------------------------------- telegraph

def test_collect_telegraph_aligned(fake_ak, out_dir):
    path = ac.collect_telegraph(fake_ak, out_dir, "2026-08-25", "run_ak")
    rows = list(csv_rows(path))
    assert len(rows) == 2
    assert list(rows[0].keys()) == ["event_type", "published_at", "published_tz",
                                    "title", "summary", "content",
                                    "source_external_id", "content_hash"]
    assert rows[0]["event_type"] == "news"
    assert rows[0]["published_tz"] == "Asia/Shanghai"
    assert rows[0]["published_at"].startswith("2026-08-25T")  # 本地 → UTC ISO
    assert rows[0]["title"] == "珀莱雅：上半年净利同比增长"
    assert rows[0]["content"].startswith("财联社8月25日电")
    # content_hash 确定性（去重依据）
    r0, r1 = rows[0], rows[1]
    assert r0["content_hash"] and r0["content_hash"] != r1["content_hash"]
    # source_external_id 稳定性
    path2 = ac.collect_telegraph(fake_ak, out_dir, "2026-08-25", "run_ak")
    rows2 = list(csv_rows(path2))
    assert rows2[0]["source_external_id"] == r0["source_external_id"]


# ---------------------------------------------------------------- forecast（一致预期）

def test_collect_forecast_aligned(fake_ak, out_dir):
    path = ac.collect_forecast(fake_ak, "603993.SH", out_dir, "2026-08-25", "run_ak")
    assert path.name == "603993.SH.csv"
    rows = list(csv_rows(path))
    assert len(rows) == 1
    r = rows[0]
    assert r["thscode"] == "603993.SH"
    # 亿元 → 元（×1e8）
    assert r["ths_fore_np_fy1_stock"] == "32917000000"
    assert r["ths_fore_np_fy2_stock"] == "36837000000"
    assert r["ths_fore_np_fy3_stock"] == "41941000000"
    # FY1 = --date 所在年（2026）的预测
    assert r["ak_np_orgs_fy1"] == "23"
    assert r["ak_np_min_fy1"] == "19683000000"
    assert r["ak_np_max_fy3"] == "53899000000"
    assert r["ak_eps_fy1"] == "1.54"
    # akshare 无 FY1 净利增速直接口径、营收预测接口空 → 留空（§2.5 不猜）
    assert r["ths_fore_np_yoy_stock"] == ""
    assert r["ths_fore_mbi_fy1_stock"] == ""


def test_collect_forecast_empty(fake_ak, out_dir):
    class EmptyAk(FakeAk):
        def stock_profit_forecast_ths(self, symbol, indicator):
            return pd.DataFrame()

    assert ac.collect_forecast(EmptyAk(), "603993.SH", out_dir,
                               "2026-08-25", "run_ak") is None


def test_collect_forecast_hk_rejected(fake_ak, out_dir):
    with pytest.raises(ValueError, match="仅支持 A 股"):
        ac.collect_forecast(fake_ak, "00700.HK", out_dir, "2026-08-25", "run_ak")


# ---------------------------------------------------------------- stock_info（股本快照）

def test_collect_stock_info_aligned(fake_ak, out_dir):
    path = ac.collect_stock_info(fake_ak, "603993.SH", out_dir, "2026-08-25", "run_ak")
    assert path.name == "603993.SH.csv"
    rows = list(csv_rows(path))
    assert len(rows) == 1
    r = rows[0]
    assert r["thscode"] == "603993.SH"
    assert r["ths_total_shares_stock"] == "21394310176"  # 取最新变动行
    assert r["ak_change_date"] == "20250716"
    assert r["ak_change_reason"] == "回购"
    assert r["ak_float_a_shares"] == "17460840000.0"


def test_collect_stock_info_empty(fake_ak, out_dir):
    class EmptyAk(FakeAk):
        def stock_zh_a_gbjg_em(self, symbol):
            return pd.DataFrame()

    assert ac.collect_stock_info(EmptyAk(), "603993.SH", out_dir,
                                 "2026-08-25", "run_ak") is None


# ---------------------------------------------------------------- 未安装提示

def test_load_akshare_missing_raises(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "akshare":
            raise ImportError("No module named 'akshare'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="akshare"):
        ac._load_akshare()


# ---------------------------------------------------------------- 工具

def csv_rows(path):
    import csv
    with open(path, newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


# ---------------------------------------------------------------- announcement（cninfo 公告）


def test_collect_announcement_aligned(fake_ak, out_dir):
    """线格式列序/内容/引号转义 + 接口参数紧凑日期格式。"""
    path = ac.collect_announcement(fake_ak, "603993.SH", "2025-08-26", "2026-08-27",
                                   out_dir, "2026-08-27", "run_ak")
    # 接口日期参数必须紧凑 YYYYMMDD（带 - 的格式静默返回空，实测）
    assert fake_ak.last_cninfo_kwargs == {"symbol": "603993", "market": "沪深京",
                                          "start_date": "20250826", "end_date": "20260827"}
    assert path == out_dir / "announcement" / "2026-08-27" / "run_ak" / "603993.SH.csv"
    rows = list(csv_rows(path))
    assert len(rows) == 2
    r = rows[0]
    assert set(r) == {"title", "time", "url", "source", "summary",
                      "code", "setcode", "name"}
    assert r["title"] == "洛阳钼业关于对外担保计划的公告"
    assert r["summary"] == r["title"]  # 线格式约定 summary 回填标题
    assert r["time"] == "2026-08-21 00:00:00"
    assert r["url"] == "http://cninfo/ann/1"
    assert r["source"] == "巨潮资讯"
    assert r["code"] == "603993" and r["setcode"] == "1" and r["name"] == "洛阳钼业"
    # 含逗号标题须引号包裹且可被 DictReader 还原
    assert rows[1]["title"] == "标题含逗号，应被引号包裹"


def test_collect_announcement_empty(fake_ak, out_dir):
    class EmptyAk(FakeAk):
        def stock_zh_a_disclosure_report_cninfo(self, symbol, market,
                                                start_date, end_date):
            return pd.DataFrame()

    assert ac.collect_announcement(EmptyAk(), "603993.SH", "2026-08-01",
                                   "2026-08-27", out_dir, "2026-08-27", "run_ak") is None


def test_collect_announcement_hk_rejected(fake_ak, out_dir):
    with pytest.raises(ValueError, match="仅支持 A 股"):
        ac.collect_announcement(fake_ak, "00700.HK", "2025-08-26", "2026-08-27",
                                out_dir, "2026-08-27", "run_ak")
