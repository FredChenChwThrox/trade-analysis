"""技术指标核心计算（D1.7，设计 §4.1）。

口径（公式边界由 tests/test_indicators.py golden tests 锁定）：
- 输入一律为**复权后** OHLC（close_raw × price_adj_factor）与调整后成交量
  （volume_raw / share_factor）；成交额不做股份因子调整（§4.1）。
- 所有滚动窗口要求完整窗口，样本不足返回 NaN，不用较短窗口冒充（§4.1）。
- MA：简单移动平均；EMA：pandas `ewm(span, adjust=False)`（初值=首个观测，
  递推 e_t = α·x_t + (1−α)·e_{t−1}，α = 2/(span+1)）。
- MACD：DIF = EMA(fast) − EMA(slow)，DEA = EMA(DIF, signal)，柱 = 2×(DIF−DEA)。
- RSI：Wilder RMA——首个均值为前 window 个 delta 的简单平均，其后递推
  avg_t = (avg_{t−1}×(window−1) + x_t) / window；RSI = 100 − 100/(1+RS)。
  边界：avg_loss=0 且 avg_gain>0 → 100；avg_gain=0 且 avg_loss>0 → 0；
  两者皆 0（价格不动）→ 50。
- BOLL：mid=MA20，std 为总体标准差 ddof=0，upper/lower = mid ± num_std×std，
  带宽 = (upper−lower)/mid。
- KDJ：RSV = (close − LLV_n)/(HHV_n − LLV_n)×100；K/D 初始值 50，
  K_t = (1−1/k_smooth)·K_{t−1} + (1/k_smooth)·RSV_t，D 同法平滑 K，
  J = 3K − 2D；零振幅（HHV=LLV）时 RSV 无定义，K/D 沿用前值（§4.1）。
- 成交量标准差同为总体口径 ddof=0（与 BOLL 一致；设计只显式规定 BOLL，
  此处统一并锁定于 golden tests）。
- pct_chg / amplitude 以**百分比**存储（1.23 表示 1.23%）：
  pct_chg = close/close_{t−1} − 1；amplitude = (high−low)/close_{t−1}。
- 信号判定用"历史均值"：historical_mean = shift(1) 后再滚动均值，
  排除正在判断的当前 bar（§4.1）。
"""

from __future__ import annotations

import math

import pandas as pd

RULE_VERSION = "indicators_v1"


# ---------------------------------------------------------------- 基础算子

def sma(s: pd.Series, window: int) -> pd.Series:
    """简单移动平均，窗口不足为 NaN。"""
    return s.rolling(window, min_periods=window).mean()


def ema(s: pd.Series, span: int) -> pd.Series:
    """EMA adjust=False（§4.1 MACD 口径）。"""
    return s.ewm(span=span, adjust=False).mean()


def rolling_std(s: pd.Series, window: int) -> pd.Series:
    """总体标准差（ddof=0），窗口不足为 NaN。"""
    return s.rolling(window, min_periods=window).std(ddof=0)


def historical_mean(s: pd.Series, window: int) -> pd.Series:
    """历史均值：先 shift(1) 排除当前 bar，再取 window 均值（§4.1）。"""
    return sma(s.shift(1), window)


def macd(close: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    """DIF/DEA/柱：EMA adjust=False，柱 = 2*(DIF−DEA)。"""
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = 2 * (dif - dea)
    return dif, dea, hist


def wilder_rma(s: pd.Series, window: int) -> pd.Series:
    """Wilder 平滑均值：首个值 = 前 window 个观测的简单平均（位置 window−1），
    其后递推 avg_t = (avg_{t−1}×(window−1) + x_t)/window；之前为 NaN。"""
    out = pd.Series(math.nan, index=s.index, dtype=float)
    vals = s.to_numpy(dtype=float)
    if len(vals) < window:
        return out
    head = vals[:window]
    if pd.isna(head).any():
        # 前 window 个含 NaN：首个可用窗口顺延（窗口不足不冒充）
        start = None
        for i in range(window - 1, len(vals)):
            seg = vals[i - window + 1:i + 1]
            if not pd.isna(seg).any():
                start = i
                break
        if start is None:
            return out
    else:
        start = window - 1
    avg = float(pd.Series(vals[start - window + 1:start + 1]).mean())
    out.iloc[start] = avg
    for i in range(start + 1, len(vals)):
        x = vals[i]
        if pd.isna(x):
            out.iloc[i] = math.nan
            avg = math.nan
            continue
        avg = x / window if pd.isna(avg) else (avg * (window - 1) + x) / window
        out.iloc[i] = avg
    return out


def rsi(close: pd.Series, window: int) -> pd.Series:
    """Wilder RSI：delta 正部/负部分别做 Wilder RMA，RSI = 100 − 100/(1+RS)。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = wilder_rma(gain, window)
    avg_loss = wilder_rma(loss, window)
    out = pd.Series(math.nan, index=close.index, dtype=float)
    for i in range(len(close)):
        g, l = avg_gain.iloc[i], avg_loss.iloc[i]
        if pd.isna(g) or pd.isna(l):
            continue
        if l == 0:
            out.iloc[i] = 100.0 if g > 0 else 50.0
        elif g == 0:
            out.iloc[i] = 0.0
        else:
            out.iloc[i] = 100.0 - 100.0 / (1.0 + g / l)
    return out


def boll(close: pd.Series, window: int, num_std: float) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """BOLL 中轨/上轨/下轨/带宽（ddof=0；带宽 = (upper−lower)/mid）。"""
    mid = sma(close, window)
    std = rolling_std(close, window)
    upper = mid + num_std * std
    lower = mid - num_std * std
    bandwidth = (upper - lower) / mid
    return mid, upper, lower, bandwidth


def kdj(high: pd.Series, low: pd.Series, close: pd.Series,
        rsv_window: int, k_smooth: int, d_smooth: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    """KDJ：初始 K/D=50；窗口不足为 NaN；零振幅（HHV=LLV）沿用前值。"""
    hhv = high.rolling(rsv_window, min_periods=rsv_window).max()
    llv = low.rolling(rsv_window, min_periods=rsv_window).min()
    k = pd.Series(math.nan, index=close.index, dtype=float)
    d = pd.Series(math.nan, index=close.index, dtype=float)
    prev_k = prev_d = 50.0
    started = False
    for i in range(len(close)):
        h, l = hhv.iloc[i], llv.iloc[i]
        if pd.isna(h) or pd.isna(l):
            continue  # 窗口不足：K/D 保持 NaN
        started = True
        if h == l:
            k.iloc[i], d.iloc[i] = prev_k, prev_d  # 零振幅沿用前值
            continue
        rsv = (close.iloc[i] - l) / (h - l) * 100.0
        prev_k = prev_k * (1 - 1 / k_smooth) + rsv / k_smooth
        prev_d = prev_d * (1 - 1 / d_smooth) + prev_k / d_smooth
        k.iloc[i], d.iloc[i] = prev_k, prev_d
    j = 3 * k - 2 * d
    return k, d, j


# ---------------------------------------------------------------- 汇总入口

def compute_indicators(frame: pd.DataFrame, params: dict) -> pd.DataFrame:
    """按 config/indicators.yaml defaults 计算全部指标列。

    frame 列：open/high/low/close（复权后）、volume（调整后），
    可选 amount（不复权成交额，全空或缺列时 amt_* 全为 NaN）。
    返回列与 indicators_daily 指标列同名（pe_ttm/pe_status 由 valuation 补）。
    """
    ma_windows = params["ma_windows"]
    macd_p = params["macd"]
    boll_p = params["boll"]
    vol_p = params["volume"]
    kdj_p = params["kdj"]

    close, high, low, volume = frame["close"], frame["high"], frame["low"], frame["volume"]
    out = pd.DataFrame(index=frame.index)

    for w in ma_windows:
        out[f"ma{w}"] = sma(close, w)
    out["dif"], out["dea"], out["macd_hist"] = macd(
        close, macd_p["fast"], macd_p["slow"], macd_p["signal"])
    for w in params["rsi_windows"]:
        out[f"rsi{w}"] = rsi(close, w)
    out["boll_mid"], out["boll_upper"], out["boll_lower"], out["boll_bandwidth"] = boll(
        close, boll_p["window"], boll_p["num_std"])
    for w in vol_p["ma_windows"]:
        out[f"vol_ma{w}"] = sma(volume, w)
    for w in vol_p["stats_windows"]:
        out[f"vol_mean{w}"] = sma(volume, w)
        out[f"vol_std{w}"] = rolling_std(volume, w)

    amount = frame.get("amount")
    for w in vol_p["stats_windows"]:
        if amount is not None and amount.notna().any():
            out[f"amt_mean{w}"] = sma(amount, w)
            out[f"amt_std{w}"] = rolling_std(amount, w)
        else:
            out[f"amt_mean{w}"] = math.nan
            out[f"amt_std{w}"] = math.nan

    out["kdj_k"], out["kdj_d"], out["kdj_j"] = kdj(
        high, low, close, kdj_p["rsv_window"], kdj_p["k_smooth"], kdj_p["d_smooth"])
    out["pct_chg"] = close.pct_change() * 100.0
    out["amplitude"] = (high - low) / close.shift(1) * 100.0
    return out
