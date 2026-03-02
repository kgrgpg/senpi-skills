#!/usr/bin/env python3
"""
cobra-regime.py — Market Regime Classifier for COBRA.

Classifies the current market into one of three regimes (TRENDING, RANGING,
VOLATILE) using BTC macro data from Senpi MCP. Pure computation,
no LLM reasoning needed.

Output JSON:
    {"regime": "TRENDING", "confidence": 0.82, "btcTrend": "up",
     "atr_ratio": 1.4, "adx": 32, "btcChange4h": 1.2, "funding": 0.01}

Cron: Runs inline from cobra-brain.py, also standalone for testing.
"""

import json, sys, os, math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cobra_config import mcporter_call_safe, output, load_config


def compute_atr(candles, period=14):
    """Compute Average True Range from candle data."""
    if not candles or len(candles) < period + 1:
        return 0, 0
    trs = []
    for i in range(1, len(candles)):
        high = float(candles[i].get("h", candles[i].get("high", 0)))
        low = float(candles[i].get("l", candles[i].get("low", 0)))
        prev_close = float(candles[i - 1].get("c", candles[i - 1].get("close", 0)))
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    if len(trs) < period:
        return 0, 0

    recent_atr = sum(trs[-period:]) / period
    older_atr = sum(trs[-period * 2:-period]) / period if len(trs) >= period * 2 else recent_atr
    return recent_atr, older_atr


def compute_adx(candles, period=14):
    """Compute ADX (Average Directional Index) from candle data."""
    if not candles or len(candles) < period * 2 + 1:
        return 0

    plus_dm_list = []
    minus_dm_list = []
    tr_list = []

    for i in range(1, len(candles)):
        high = float(candles[i].get("h", candles[i].get("high", 0)))
        low = float(candles[i].get("l", candles[i].get("low", 0)))
        prev_high = float(candles[i - 1].get("h", candles[i - 1].get("high", 0)))
        prev_low = float(candles[i - 1].get("l", candles[i - 1].get("low", 0)))
        prev_close = float(candles[i - 1].get("c", candles[i - 1].get("close", 0)))

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    if len(tr_list) < period * 2:
        return 0

    smoothed_tr = sum(tr_list[:period])
    smoothed_plus = sum(plus_dm_list[:period])
    smoothed_minus = sum(minus_dm_list[:period])

    dx_list = []
    for i in range(period, len(tr_list)):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_list[i]
        smoothed_plus = smoothed_plus - (smoothed_plus / period) + plus_dm_list[i]
        smoothed_minus = smoothed_minus - (smoothed_minus / period) + minus_dm_list[i]

        plus_di = (smoothed_plus / smoothed_tr * 100) if smoothed_tr > 0 else 0
        minus_di = (smoothed_minus / smoothed_tr * 100) if smoothed_tr > 0 else 0

        di_sum = plus_di + minus_di
        dx = (abs(plus_di - minus_di) / di_sum * 100) if di_sum > 0 else 0
        dx_list.append(dx)

    if not dx_list:
        return 0

    adx = sum(dx_list[-period:]) / min(period, len(dx_list))
    return round(adx, 2)


def compute_btc_change(candles, periods=1):
    """Compute BTC price change percentage over N candles."""
    if not candles or len(candles) < periods + 1:
        return 0
    current_close = float(candles[-1].get("c", candles[-1].get("close", 0)))
    past_close = float(candles[-(periods + 1)].get("c", candles[-(periods + 1)].get("close", 0)))
    if past_close == 0:
        return 0
    return round((current_close - past_close) / past_close * 100, 2)


def detect_trend_direction(candles, period=10):
    """Simple trend direction from close prices."""
    if not candles or len(candles) < period:
        return "neutral"
    closes = [float(c.get("c", c.get("close", 0))) for c in candles[-period:]]
    first_half = sum(closes[:period // 2]) / (period // 2)
    second_half = sum(closes[period // 2:]) / (period - period // 2)
    pct = (second_half - first_half) / first_half * 100 if first_half > 0 else 0
    if pct > 0.5:
        return "up"
    elif pct < -0.5:
        return "down"
    return "neutral"


def classify_regime(candles_4h, candles_1h, funding_rate=0, config=None):
    """Classify current market regime from BTC data.

    Returns:
        dict with regime, confidence, and supporting metrics.
    """
    cfg = config or load_config()
    thresholds = cfg.get("regime", {})
    adx_trending = thresholds.get("adxTrendingThreshold", 25)
    adx_ranging = thresholds.get("adxRangingThreshold", 20)
    atr_volatile_mult = thresholds.get("atrVolatileMultiplier", 2.0)
    volatile_drop_pct = thresholds.get("volatileDropPct", 5)
    volatile_drop_1h_pct = thresholds.get("volatileDrop1hPct", 3.5)

    adx = compute_adx(candles_4h)
    recent_atr, older_atr = compute_atr(candles_4h)
    atr_ratio = round(recent_atr / older_atr, 2) if older_atr > 0 else 1.0
    btc_change_4h = compute_btc_change(candles_4h, periods=1)
    btc_change_1h = compute_btc_change(candles_1h, periods=1) if candles_1h else 0
    trend = detect_trend_direction(candles_4h)

    regime = "RANGING"
    confidence = 0.5
    reasons = []

    # VOLATILE — significant price move (fast detection before ATR lags)
    significant_move = (abs(btc_change_4h) > volatile_drop_pct or
                        abs(btc_change_1h) > volatile_drop_1h_pct)
    if significant_move:
        regime = "VOLATILE"
        move = max(abs(btc_change_4h), abs(btc_change_1h))
        confidence = min(0.95, 0.7 + move / 20)
        reasons.append(f"Significant BTC move: {btc_change_4h}% on 4h, {btc_change_1h}% on 1h")
        if abs(funding_rate) > 0.05:
            confidence = min(0.95, confidence + 0.05)
            reasons.append(f"Extreme funding: {funding_rate}")
    # VOLATILE — ATR expansion
    elif atr_ratio > atr_volatile_mult:
        regime = "VOLATILE"
        confidence = min(0.9, 0.5 + (atr_ratio - atr_volatile_mult) * 0.2)
        reasons.append(f"ATR ratio {atr_ratio}x (threshold {atr_volatile_mult}x)")
        if abs(funding_rate) > 0.05:
            confidence = min(0.95, confidence + 0.1)
            reasons.append(f"Extreme funding: {funding_rate}")
    # TRENDING check
    elif adx > adx_trending:
        regime = "TRENDING"
        confidence = min(0.9, 0.5 + (adx - adx_trending) / 30)
        reasons.append(f"ADX {adx} > {adx_trending}")
        if atr_ratio > 1.2:
            confidence = min(0.95, confidence + 0.1)
            reasons.append(f"ATR expanding: {atr_ratio}x")
    # RANGING
    elif adx < adx_ranging:
        regime = "RANGING"
        confidence = min(0.9, 0.5 + (adx_ranging - adx) / 20)
        reasons.append(f"ADX {adx} < {adx_ranging}")
    else:
        regime = "RANGING"
        confidence = 0.45
        reasons.append(f"ADX {adx} between {adx_ranging}-{adx_trending}, defaulting to RANGING")

    return {
        "regime": regime,
        "confidence": round(confidence, 2),
        "btcTrend": trend,
        "adx": adx,
        "atr_ratio": atr_ratio,
        "btcChange4h": btc_change_4h,
        "btcChange1h": btc_change_1h,
        "funding": funding_rate,
        "reasons": reasons,
    }


def get_allocation(regime, config=None):
    """Get target capital allocation percentages for the regime.

    Returns:
        {"wolf": pct, "tiger": pct, "reserve": pct}
    """
    allocations = {
        "TRENDING":  {"wolf": 60, "tiger": 25, "reserve": 15},
        "RANGING":   {"wolf": 20, "tiger": 50, "reserve": 30},
        "VOLATILE":  {"wolf": 40, "tiger": 30, "reserve": 30},
        "UNKNOWN":   {"wolf": 25, "tiger": 25, "reserve": 50},
    }
    return allocations.get(regime, allocations["UNKNOWN"])


def run():
    """Main: fetch BTC data, classify regime, output JSON."""
    data_4h = mcporter_call_safe(
        "market_get_asset_data",
        asset="BTC",
        interval="4h",
        lookback=60,
    )
    data_1h = mcporter_call_safe(
        "market_get_asset_data",
        asset="BTC",
        interval="1h",
        lookback=30,
    )

    candles_4h = []
    candles_1h = []
    funding_rate = 0

    if data_4h:
        candles_4h = data_4h.get("candles", data_4h.get("data", []))
        funding_rate = float(data_4h.get("funding", {}).get("rate", 0))
    if data_1h:
        candles_1h = data_1h.get("candles", data_1h.get("data", []))

    if not candles_4h:
        output({
            "regime": "UNKNOWN",
            "confidence": 0,
            "error": "No BTC 4h data available",
            "actionable": 0,
        })
        return

    result = classify_regime(candles_4h, candles_1h, funding_rate)
    result["allocation"] = get_allocation(result["regime"])
    result["actionable"] = 1
    output(result)


if __name__ == "__main__":
    run()
