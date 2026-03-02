# COBRA State File Schemas

All state files live under `state/cobra/` in the workspace root.

---

## cobra-state.json

Master orchestrator state. Updated by `cobra-brain.py` every 15 min.

```json
{
  "version": 1,
  "regime": "TRENDING",
  "regimeConfidence": 0.82,
  "regimeChangedAt": "2026-02-28T12:00:00Z",
  "spawnedInstances": {
    "wolf-abc12345": { "type": "wolf", "budget": 3000 },
    "tiger-xyz78901": { "type": "tiger", "budget": 2500 }
  },
  "totalAllocated": 5500,
  "globalSignalPressure": 58,
  "lastBrainRun": "2026-02-28T12:15:00Z",
  "lastDecision": {
    "kills": 0,
    "spawns": 1,
    "actions": ["KEEP wolf-abc12345: score=10, no kill signals"],
    "portfolioBreakerTripped": false
  },
  "pendingActions": {
    "spawns": ["wolf-abc12345"],
    "kills": []
  },
  "createdAt": "2026-02-28T10:00:00Z",
  "updatedAt": "2026-02-28T12:15:00Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| version | int | Schema version |
| regime | string | Current market regime: TRENDING, RANGING, VOLATILE, UNKNOWN |
| regimeConfidence | float | Confidence 0.0–1.0 |
| regimeChangedAt | string\|null | ISO timestamp of last regime change |
| lastCircuitBreakerAt | string\|null | ISO timestamp of last portfolio circuit breaker trigger |
| spawnedInstances | object | Map of instance_id -> {type, budget} |
| totalAllocated | float | Sum of all spawned instance budgets |
| globalSignalPressure | int | 0–100, from cobra-signals.py |
| lastBrainRun | string\|null | ISO timestamp |
| lastDecision | object | Summary of last brain cycle decisions |
| pendingActions | object | Spawns/kills requested last cycle, verified on next run |
| createdAt | string | ISO timestamp |
| updatedAt | string | ISO timestamp (auto-updated on save) |

---

## cobra-performance.json

Aggregated performance metrics. Updated by `cobra-monitor.py` every 30 min.

```json
{
  "instances": {
    "wolf-abc12345": {
      "instanceId": "wolf-abc12345",
      "type": "wolf",
      "wallet": "0x...",
      "status": "active",
      "spawnBudget": 3000,
      "spawnedAt": "2026-02-28T10:30:00Z",
      "accountValue": 3150.50,
      "unrealizedPnl": 150.50,
      "roeSinceSpawn": 5.02,
      "utilization": 68.5,
      "marginUsed": 2158.09,
      "drawdownFromPeak": 1.2,
      "peakValue": 3188.00,
      "tradeStats": {
        "activePositions": 2,
        "avgROE": 5.02,
        "positions": [
          { "asset": "HYPE", "direction": "LONG", "roe": 8.5, "tier": "Tier 2" },
          { "asset": "SOL", "direction": "SHORT", "roe": 1.5, "tier": "Phase 1" }
        ]
      },
      "updatedAt": "2026-02-28T12:30:00Z"
    }
  },
  "global": {
    "totalAccountValue": 5650.50,
    "totalUnrealizedPnl": 250.50,
    "avgUtilization": 55.2,
    "activeWolves": 1,
    "activeTigers": 1,
    "globalSignalPressure": 58
  },
  "alerts": [],
  "updatedAt": "2026-02-28T12:30:00Z"
}
```

---

## cobra-signals.json

Signal pressure data. Updated by `cobra-signals.py` every 5 min.

```json
{
  "instances": {
    "wolf-abc12345": {
      "type": "wolf",
      "signalPressure": 72,
      "missedFirstJumps1h": 3,
      "missedFirstJumps4h": 8,
      "missedOpportunities1h": 5,
      "missedOpportunities4h": 12,
      "slotsUsed": 3,
      "slotsMax": 3,
      "positionQuality": { "phase1": 0, "tier1": 1, "tier2plus": 2 },
      "avgPositionROE": 8.5
    },
    "tiger-xyz78901": {
      "type": "tiger",
      "signalPressure": 45,
      "prescreenerDensity": 28,
      "avgPrescreenerScore": 72.3,
      "highConfluenceCount": 2,
      "slotsUsed": 2,
      "slotsMax": 3,
      "aggression": "NORMAL",
      "recentWinRate": 0.62
    }
  },
  "globalSignalPressure": 58,
  "marketOpportunityDensity": "HIGH",
  "updatedAt": "2026-02-28T12:05:00Z",
  "actionable": 1
}
```

### Signal Pressure Score Calculation

**WOLF instances:**
- `missedFirstJumps1h * 15` (each missed FIRST_JUMP is high-value)
- `missedOpportunities1h * 8` (175+ score opportunities)
- `+10` if slots full and any pressure > 0
- Capped at 100

**TIGER instances:**
- `highConfluenceCount * 10` (0.65+ confluence scanners)
- `(prescreenerDensity - 15) * 5` if density >= 25
- `+15` if slots full and any confluence signals
- `+10` if aggression ELEVATED or ABORT
- Capped at 100

---

## cobra-token-budget.json

Daily token usage tracking. Updated by `viper_gate.py` on each invocation.

```json
{
  "date": "2026-02-28",
  "totalTokensEstimated": 1250000,
  "invocations": {
    "COBRA/wolf-abc12345/Emerging-Movers": {
      "count": 40,
      "actionable": 3,
      "totalTokens": 36000
    },
    "COBRA/wolf-abc12345/DSL-Combined": {
      "count": 20,
      "actionable": 5,
      "totalTokens": 18000
    }
  },
  "recommendations": []
}
```

---

## Spawned Instance Files (state/cobra/spawned/)

One file per active instance: `wolf-{id}.json` or `tiger-{id}.json`.

### wolf-{id}.json

Each WOLF instance runs as a subagent with 3 lightweight wake crons (not per-script crons).

```json
{
  "type": "wolf",
  "instanceId": "wolf-abc12345",
  "wallet": "0x1234...abcd",
  "strategyId": "uuid-here",
  "budget": 3000,
  "slots": 3,
  "marginPerSlot": 900,
  "defaultLeverage": 10,
  "dslPreset": "aggressive",
  "status": "active",
  "spawnedAt": "2026-02-28T10:30:00Z",
  "subagentLabel": "wolf-abc12345",
  "cronNames": [
    "COBRA/wolf-abc12345/Wake-Scan",
    "COBRA/wolf-abc12345/Wake-Monitor",
    "COBRA/wolf-abc12345/Wake-Full"
  ],
  "spawnedBy": "cobra-brain"
}
```

### tiger-{id}.json

Each TIGER instance runs as a subagent with 2 lightweight wake crons.

```json
{
  "type": "tiger",
  "instanceId": "tiger-xyz78901",
  "wallet": "0xabcd...1234",
  "strategyId": "uuid-here",
  "budget": 2500,
  "maxSlots": 3,
  "goalPct": 5,
  "status": "active",
  "spawnedAt": "2026-02-28T10:45:00Z",
  "subagentLabel": "tiger-xyz78901",
  "cronNames": [
    "COBRA/tiger-xyz78901/Wake-Scan",
    "COBRA/tiger-xyz78901/Wake-Full"
  ],
  "spawnedBy": "cobra-brain"
}
```

### Killed Instance (status changes)

When COBRA kills an instance, additional fields are added. Status is `"killed"` if all
positions closed successfully, or `"kill_pending"` if some positions failed to close.
The brain retries kill_pending instances every `killPendingRetryMinutes` (default 5),
up to `killPendingMaxRetries` (default 3). After max retries, the instance is flagged
STUCK and an alert is emitted.

```json
{
  "status": "killed",
  "killedAt": "2026-02-28T14:00:00Z",
  "killReason": "All Phase 1 + negative ROE + signal pressure 72",
  "finalValue": 2890.50,
  "realizedPnl": -109.50,
  "remainingPositions": 0,
  "killRetries": 0,
  "firstKillAttemptAt": "2026-02-28T14:00:00Z",
  "closeResults": [
    { "asset": "HYPE", "direction": "LONG", "status": "closed" },
    { "asset": "SOL", "direction": "SHORT", "status": "closed" }
  ]
}
```

---

## cobra-config.json

User configuration. Created by `cobra-setup.py`, lives at workspace root.

```json
{
  "version": 1,
  "totalBudget": 10000,
  "reservePct": 15,
  "maxWolves": 2,
  "maxTigers": 1,
  "minSpawnBudget": 500,
  "telegramChatId": "5183731261",
  "midModel": "anthropic/claude-sonnet-4-20250514",
  "budgetModel": "anthropic/claude-haiku-4-5",
  "regime": {
    "adxTrendingThreshold": 25,
    "adxRangingThreshold": 20,
    "atrVolatileMultiplier": 2.0,
    "volatileDropPct": 5,
    "volatileDrop1hPct": 3.5
  },
  "killVsKeep": {
    "signalPressureKillThreshold": 60,
    "signalPressureSpawnThreshold": 50,
    "idleHoursBeforeKill": 2,
    "avgFeePerTrade": 32,
    "maxDrawdownPct": 20,
    "portfolioMaxDrawdownPct": 15,
    "killPendingRetryMinutes": 5,
    "killPendingMaxRetries": 3
  },
  "tokenBudget": {
    "dailyLimitTokens": 5000000,
    "lowActionableRateThreshold": 5
  }
}
```
