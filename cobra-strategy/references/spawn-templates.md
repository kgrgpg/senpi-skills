# COBRA Spawn Templates

When COBRA decides to spawn a new WOLF or TIGER instance, `cobra-spawner.py`
handles the full lifecycle. This document describes the spawn and kill flows.

COBRA uses OpenClaw's `sessions_spawn` to create **real subagent sessions** for
each instance, providing full context isolation.

---

## Spawn Flow

### 1. Strategy Creation (via Senpi MCP)

```
mcporter call senpi.strategy_create_custom_strategy name="COBRA-wolf-abc12345"
```

Returns: `{ wallet: "0x...", strategyId: "uuid-..." }`

### 2. Funding (via Senpi MCP)

```
mcporter call senpi.strategy_top_up amount=5000 strategyId="uuid-..."
```

### 3. Parameter Calculation

WOLF parameters are auto-calculated from budget:

| Budget Range | Slots | Margin/Slot | Leverage |
|-------------|-------|-------------|----------|
| $500–$2,999 | 2 | 30% of budget | 5–7x |
| $3,000–$9,999 | 3 | 30% of budget | 7–10x |
| $10,000–$29,999 | 4 | 30% of budget | 10x |
| $30,000+ | 5 | 30% of budget | 10x |

TIGER parameters:

| Parameter | Default | Notes |
|-----------|---------|-------|
| maxSlots | 3 | Configurable per spawn |
| goalPct | 5% (TRENDING) / 3% (RANGING) | Regime-dependent |

### 4. Subagent Creation (via OpenClaw sessions_spawn)

COBRA builds a comprehensive task description for the subagent containing:
- Instance identity (ID, wallet, budget, slots, leverage)
- Full WOLF or TIGER mandate (all scripts to run, all rules to follow)
- VIPER token optimization instructions
- How to respond to COBRA messages (kill orders, regime updates)

The Brain outputs a `sessions_spawn` instruction:

```json
{
    "tool": "sessions_spawn",
    "params": {
        "task": "<comprehensive WOLF/TIGER task description>",
        "label": "wolf-abc12345",
        "model": "anthropic/claude-sonnet-4-20250514",
        "thread": true,
        "mode": "session",
        "runTimeoutSeconds": 0
    }
}
```

The agent on the main session executes this, creating a persistent subagent
session at `agent:<id>:subagent:<uuid>`. The subagent has its own context
window — completely isolated from other instances and COBRA's main session.

### 5. Wake Cron Creation

Lightweight crons are created that periodically send messages to the subagent
via `sessions_send`. These just tell the subagent "run your scanners now" —
all the actual work happens inside the subagent's isolated session.

### 6. Registration

Instance config is saved to `state/cobra/spawned/{instance-id}.json` with
the subagent label and wake cron names for lifecycle management.

---

## Kill Flow

### Decision: Kill vs Keep vs Wait

COBRA evaluates each instance every 15 minutes:

**KEEP when:**
- Any position is Tier 2+ DSL (trailing stop protecting significant gains)
- Signal pressure < 40 (not much is being missed)
- Strong positive uPnL trending up

**KILL when:**
- All positions Phase 1 + negative ROE + signal pressure > 60
- `opportunity_ev > uPnL + booking_cost + restart_cost`
- Instance idle 2+ hours with 0 positions + signal pressure > 40
- Drawdown exceeds max threshold (default 20%)
- Portfolio circuit breaker triggered (drawdown exceeds threshold)

**WAIT when:**
- Mixed position quality (some good, some bad)
- Signal pressure moderate (40–60)
- Regime is shifting — wait for confirmation

### Kill Execution

1. **Close all positions** via `close_position` MCP call per active position (direct, doesn't rely on subagent)
2. **Deactivate DSL states** — set `active: false`, record `closedBy: "cobra-kill"`
3. **Record final PnL** in the spawned instance file
4. **Mark killed** — status changes to `"killed"` with timestamp and reason
5. **Send kill order to subagent** via `sessions_send` — graceful shutdown notification
6. **Kill subagent session** via `/subagents kill <label>` — terminates the session
7. **Delete wake crons** — brain tells the agent which OpenClaw crons to remove
8. **Capital returns** to the master budget — available for redeployment

The kill flow is designed to be resilient: positions are closed via MCP directly
(not through the subagent), so even if the subagent is unresponsive, capital is safe.

### Kill Cost Calculation

```
booking_cost = open_positions × $32 (avg fee per trade)
restart_cost = $50 (new strategy creation + warmup time)
total_kill_cost = booking_cost + restart_cost

opportunity_ev = missed_signals × historical_win_rate × avg_win_per_signal
```

COBRA kills when: `opportunity_ev > unrealized_pnl + total_kill_cost`

---

## VIPER Token Savings on Spawned Crons

### Without VIPER (baseline WOLF, old per-cron model)
- 98 crons/hr, ~90% idle
- Each idle call: ~1,000 input + ~100 output tokens
- Daily waste: ~2.3M tokens

### With VIPER wrapper
- Idle calls produce ~20 output tokens ("HEARTBEAT_OK") instead of ~100
- Mandates shortened by ~300 tokens each
- **Estimated savings: 40–60% of spawned instance token spend**

### With COBRA frequency management
- Brain can recommend reducing frequencies during RANGING (less activity)
- Brain adjusts frequencies during VOLATILE/RANGING (less activity)
- **Additional savings: 20–30%**
