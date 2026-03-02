# COBRA Market Regime Classification Rules

COBRA classifies the BTC macro environment into one of three regimes every 15 minutes.
All indicators are computed from BTC candle data via `market_get_asset_data`.

---

## Indicators

| Indicator | Source | Periods | Purpose |
|-----------|--------|---------|---------|
| ADX (Average Directional Index) | BTC 4h candles | 14 | Trend strength |
| ATR (Average True Range) | BTC 4h candles | 14 | Volatility magnitude |
| ATR Ratio | Recent ATR / Older ATR | 14+14 | Volatility expansion/contraction |
| BTC 4h Change % | Last vs previous 4h close | 1 | Fast volatility detection |
| BTC 1h Change % | Last vs previous 1h close | 1 | Fast volatility detection |
| Funding Rate | BTC funding from MCP | — | Sentiment extreme detection |
| Trend Direction | 10-period close slope | 10 | Directional bias |

---

## Regime Definitions

### TRENDING

**Conditions:** ADX > 25, ATR expanding (ratio > 1.0), clear directional bias

**Confidence:** 0.5 + (ADX - 25) / 30, boosted by ATR expansion

**Capital Allocation:**

| Target | Allocation | Rationale |
|--------|-----------|-----------|
| WOLF | 60% | Momentum hunters thrive — FIRST_JUMPs and SM activity correlate with trending markets |
| TIGER | 25% | Calculated entries still work, but fewer mean-reversion setups |
| Reserve | 15% | Standard buffer |

### RANGING

**Conditions:** ADX < 20, ATR contracting (ratio < 1.0)

**Confidence:** 0.5 + (20 - ADX) / 20

**Capital Allocation:**

| Target | Allocation | Rationale |
|--------|-----------|-----------|
| WOLF | 20% | Few strong signals — reduce exposure to chop |
| TIGER | 50% | Mean reversion + compression patterns shine |
| Reserve | 30% | Higher reserve for opportunistic deployment |

### VOLATILE

**Conditions:** Significant price move (>5% on 4h or >3.5% on 1h), ATR ratio > 2x average, or extreme funding

**Confidence:** 0.7+ for significant price moves, 0.5 + (ATR ratio - 2.0) * 0.2 for ATR-based, boosted by funding extremes.

**Capital Allocation:**

| Target | Allocation | Rationale |
|--------|-----------|-----------|
| WOLF | 40% | Big moves = big FIRST_JUMPs in both directions, but also more noise |
| TIGER | 30% | Funding arb and volatility plays |
| Reserve | 30% | Protect against whipsaws |

---

## Regime Shift Handling

When regime changes (e.g., TRENDING -> RANGING):

1. **Don't kill instantly.** Adjust target allocations but let the next brain cycle evaluate with new targets.
2. **Portfolio circuit breaker.** If total portfolio drawdown exceeds `portfolioMaxDrawdownPct` (default 15%), all instances are killed regardless of regime. This is the only condition that triggers unconditional kills.

**Why no CRASH regime?** WOLF and TIGER are directionally agnostic — they follow smart money signals that go SHORT during selloffs. A big BTC drop is extreme volatility (captured by VOLATILE), not a reason to shut everything down. Killing instances during a crash books all unrealized losses, misses the highest-signal-density window, and prevents SHORT entries that profit from the move. The portfolio circuit breaker handles actual portfolio damage based on real losses, not market movement.

---

## Configurable Thresholds

All thresholds are configurable in `cobra-config.json` under the `regime` key:

```json
{
  "regime": {
    "adxTrendingThreshold": 25,
    "adxRangingThreshold": 20,
    "atrVolatileMultiplier": 2.0,
    "volatileDropPct": 5,
    "volatileDrop1hPct": 3.5
  }
}
```
