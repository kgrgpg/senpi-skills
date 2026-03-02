---
name: cobra-strategy
description: >-
  COBRA — Capital Orchestrator with Brain-Regulated Agents. A meta-orchestrator
  that dynamically spawns and manages WOLF and TIGER instances as subagents,
  allocating capital across them based on market regime. Embeds VIPER token
  optimization into every spawned cron to reduce LLM API costs by 40-60%.
  Classifies markets into TRENDING/RANGING/VOLATILE regimes using BTC
  macro indicators. Reads WOLF/TIGER screener outputs (smart money rankings,
  whale movements, confluence scores) to detect signal pressure — strong
  opportunities being missed due to capital constraints. Makes kill-vs-keep
  decisions when no partial withdrawals are possible: either let an instance
  ride or kill it entirely to free capital for better deployment.
  4 COBRA crons manage an unlimited number of spawned instances.
  Requires wolf-strategy and tiger skills installed, Senpi MCP, python3,
  mcporter CLI, and OpenClaw cron system.
license: Apache-2.0
compatibility: >-
  Python 3.8+. Requires mcporter CLI, OpenClaw cron system,
  Senpi MCP connection. Depends on wolf-strategy and tiger skills
  being available at the workspace level.
metadata:
  author: keshav
  version: "1.0"
  platform: senpi
  exchange: hyperliquid
---

# COBRA — Capital Orchestrator with Brain-Regulated Agents

One brain. Many hunters. Dynamic capital. Minimal tokens.

COBRA is a meta-orchestrator that spawns WOLF and TIGER instances as subagents, watches their performance, reallocates capital based on market regime, and wraps everything in a VIPER token-optimization layer.

**Key innovations:**
- **Regime-adaptive allocation:** Capital flows to the strategy type that thrives in the current market
- **Signal pressure awareness:** Reads WOLF/TIGER screener data to detect missed opportunities
- **Kill-vs-keep framework:** Makes decisive capital reallocation when partial withdrawals aren't possible
- **VIPER token optimization:** 40-60% reduction in LLM API costs on spawned crons

---

## Architecture

```
COBRA Brain (4 crons, main session — clean context)
├── cobra-regime.py     → Market classifier (BTC macro → TRENDING/RANGING/VOLATILE)
├── cobra-signals.py    → Signal pressure (reads WOLF/TIGER scan outputs, no MCP)
├── cobra-brain.py      → Orchestrator (regime + signals + kill-vs-keep → spawn/kill)
└── cobra-monitor.py    → Performance tracker (MCP + local file reads)

Spawned Subagents (via OpenClaw sessions_spawn — each in own isolated session)
├── WOLF Instance 1     → own session, own context, 3 wake crons
├── WOLF Instance 2     → own session, own context, 3 wake crons
└── TIGER Instance 1    → own session, own context, 2 wake crons

VIPER Layer
├── viper_gate.py       → Mandate wrapper, token tracking, cron payload generator
└── Token budget file   → Daily spend vs limit tracking
```

### Why Subagents?

Each WOLF/TIGER instance runs as a **real OpenClaw subagent** (`sessions_spawn`) in its own session. This provides:

- **Context isolation:** WOLF-1's scan results don't pollute WOLF-2's context or COBRA's main session
- **Focused agents:** Each subagent only sees its own wallet, positions, and scan history
- **Clean orchestrator:** COBRA's main session only contains brain decisions, not scan noise
- **Native lifecycle:** Kill cascades, announce-back, session management built into OpenClaw

This is the **orchestrator pattern** from OpenClaw: main → COBRA (orchestrator) → WOLF/TIGER (workers).

### Data Flow

1. Every 15 min, the Brain fetches BTC data via MCP and classifies the regime
2. Every 5 min, Signal Scan reads local screener files (no MCP) to compute signal pressure
3. Brain combines regime + signal pressure + performance to decide: spawn, kill, or keep
4. **Spawn:** Brain outputs `sessions_spawn` instructions — agent creates subagent with full WOLF/TIGER task. Wake crons periodically send messages to subagents via `sessions_send`.
5. **Kill:** Brain closes positions via MCP, sends kill order to subagent via `sessions_send`, then terminates the subagent session
6. **Regime shift:** Brain sends regime update messages to all active subagents via `sessions_send`
7. Monitor aggregates PnL, utilization, and trade stats across all instances

---

## Quick Start

1. Ensure Senpi MCP is connected (`mcporter list` should show `senpi`)
2. Ensure `wolf-strategy` and `tiger` skills are installed in the workspace
3. Run setup: `python3 scripts/cobra-setup.py --chat-id YOUR_CHAT_ID --total-budget 10000`
4. Create the 4 COBRA cron jobs output by setup (see `references/cron-templates.md`)
5. The Brain will automatically classify the regime and spawn instances on the first run

---

## Market Regime Classification

COBRA classifies BTC macro conditions using ADX, ATR, and price changes. Pure computation, no LLM.

| Regime | Condition | WOLF | TIGER | Reserve |
|--------|-----------|------|-------|---------|
| **TRENDING** | ADX > 25, ATR expanding | 60% | 25% | 15% |
| **RANGING** | ADX < 20, ATR contracting | 20% | 50% | 30% |
| **VOLATILE** | ATR > 2x avg, significant price move, or extreme funding | 40% | 30% | 30% |

Full rules: [references/regime-rules.md](references/regime-rules.md)

---

## Signal Pressure — COBRA's Unique Edge

WOLF and TIGER screeners produce rich signal data that goes unused when capital is locked. COBRA reads this data locally (no additional MCP calls) to detect **signal pressure**.

### What COBRA reads from WOLF instances
- `emerging-movers-history.json` — FIRST_JUMP, CONTRIB_EXPLOSION signals per hour
- `scan-history.json` — 175+ scored opportunities that went unacted when `anySlotsAvailable` was false
- DSL state files — position quality (Phase 1 / Tier 1 / Tier 2+ / Tier 4)

### What COBRA reads from TIGER instances
- `prescreened.json` — candidate density and scores (market richness indicator)
- `tiger-state.json` — active positions vs slots, aggression level, halt state
- `trade-log.json` — per-pattern win rates, recent outcomes
- `dsl-{asset}.json` — position quality (same DSL format as WOLF)

### Signal Pressure Score (0-100)

**WOLF:** `missedFirstJumps × 15 + missedOpportunities × 8 + 10 if slots full`

**TIGER:** `highScoreCandidates × 10 + (density - 15) × 5 if ≥ 25 + 15 if slots full + 10 if ELEVATED/ABORT - 20 if halted`

**Pressure > 60 + slots full = COBRA should spawn more capacity or kill an underperformer.**

**Global signal pressure** uses the max of all individual instance pressures, ensuring a single saturated instance is enough to trigger reallocation.

---

## Kill vs Keep Framework

Since Senpi does not support partial withdrawal, COBRA must decide: **kill the entire instance (close all, book PnL, reclaim capital) or let it ride.**

### KEEP when
- Any position is Tier 2+ DSL (trailing stop protecting significant gains) — let it run
- Signal pressure < 40 (not much is being missed)
- uPnL is strongly positive and trending up

### KILL when
- All positions Phase 1 + negative ROE (capital is unproductive)
- Signal pressure > 60 (many good signals being missed)
- `opportunity_ev > unrealized_pnl + booking_cost + restart_cost`
- Instance idle 2+ hours with 0 positions
- Portfolio circuit breaker (drawdown exceeds threshold)

### WAIT when
- Mixed positions (some good, some bad) — let DSL natural exits free slots
- Signal pressure moderate (40-60) — not urgent enough to kill
- Regime is shifting — wait for next cycle to confirm

### Kill Cost Model
```
booking_cost = open_positions × $32 avg fee
restart_cost = $50 (strategy creation + warmup)
opportunity_ev = missed_signals × win_rate × avg_win
```

Kill if: `opportunity_ev > uPnL + booking_cost + restart_cost`

---

## COBRA's 4 Crons + Subagent Wake Crons

### COBRA's own crons (always present):

| Cron | Interval | Session | Model | Purpose |
|------|----------|---------|-------|---------|
| Brain | 15 min | main | Primary | Regime + signal pressure + kill-vs-keep + spawn/kill via sessions_spawn |
| Monitor | 30 min | isolated | Mid | Performance + signal aggregation |
| Signal Scan | 5 min | isolated | Budget | Quick signal pressure (local files, no MCP) |
| Token Audit | 1 hr | isolated | Budget | Token budget check |

### Per-instance wake crons (created when subagent spawned):

| Instance Type | Wake Crons | Purpose |
|--------------|-----------|---------|
| WOLF | 3 crons (90s / 5min / 15min) | Send scan/monitor/full-cycle messages to WOLF subagent via sessions_send |
| TIGER | 2 crons (5min / 30min) | Send scan/full-cycle messages to TIGER subagent via sessions_send |

Only 4 crons for COBRA itself + 2-3 lightweight wake crons per instance. The wake crons just relay "go do your work" messages — the actual scanning and trading logic runs inside each subagent's isolated session.

Full templates: [references/cron-templates.md](references/cron-templates.md)

---

## VIPER Token Optimization

Every spawned cron mandate is prefixed with:

```
VIPER: If script output is exactly "HEARTBEAT_OK" or the JSON contains
"actionable": 0, respond with exactly "HEARTBEAT_OK" — no analysis, no
summary, no explanation.
```

### Savings Breakdown

| Optimization | Savings |
|-------------|---------|
| VIPER early-exit on idle crons (~90% of invocations) | ~50-80% output tokens |
| Shortened mandates (VIPER prefix replaces verbose inline rules) | ~300 tokens/cron |
| COBRA frequency management (reduce intervals during RANGING) | ~20-30% |
| Model tiering (Budget model for simple threshold checks) | ~60% cost per cron |

**Combined: 40-60% reduction in total LLM API spend across all managed instances.**

---

## Scripts

| Script | Purpose | MCP Calls | Frequency |
|--------|---------|-----------|-----------|
| `cobra_config.py` | Shared config, MCP helpers, state management | n/a | imported |
| `viper_gate.py` | Token optimization, mandate wrapping, cron generation | n/a | imported |
| `cobra-regime.py` | BTC macro → regime classification | `market_get_asset_data` | 15 min (inline from brain) |
| `cobra-signals.py` | Signal pressure from WOLF/TIGER scan files | **None** | 5 min |
| `cobra-monitor.py` | Performance aggregation | `strategy_get_clearinghouse_state` | 30 min |
| `cobra-spawner.py` | Create/kill WOLF/TIGER instances | `strategy_create_custom_strategy`, `strategy_top_up`, `close_position` | on-demand |
| `cobra-brain.py` | Main orchestrator | `market_get_asset_data`, `strategy_get_clearinghouse_state` | 15 min |
| `cobra-setup.py` | Initial setup wizard | n/a | once |

---

## Senpi MCP Tools Used

| Tool | Used By | Purpose |
|------|---------|---------|
| `market_get_asset_data` | cobra-regime.py | BTC candles for regime classification |
| `market_list_instruments` | cobra-regime.py | OI data for volatility detection |
| `market_get_prices` | cobra-monitor.py | Current prices for PnL calc |
| `strategy_create_custom_strategy` | cobra-spawner.py | Create new wallet for spawned instance |
| `strategy_top_up` | cobra-spawner.py | Fund spawned instance |
| `strategy_get_clearinghouse_state` | cobra-monitor.py, cobra-brain.py | Balance + positions per instance |
| `close_position` | cobra-spawner.py | Close positions when killing instance |
| `account_get_portfolio` | cobra-brain.py | Total portfolio value |

---

## State Files

All state lives in `state/cobra/` at the workspace root:

```
state/cobra/
├── cobra-state.json           # Regime, allocations, spawned instances
├── cobra-performance.json     # Aggregated metrics per instance
├── cobra-signals.json         # Latest signal pressure data
├── cobra-token-budget.json    # Daily token usage tracking
└── spawned/
    ├── wolf-{id}.json         # Per-instance config, cron names, status
    └── tiger-{id}.json
```

All writes are atomic (write to `.tmp`, then `os.replace`). Full schemas: [references/state-schema.md](references/state-schema.md)

---

## Safety Features

1. **Portfolio circuit breaker.** Compares current portfolio value against *allocated capital* (sum of instance budgets), not totalBudget. If drawdown exceeds `portfolioMaxDrawdownPct` (default 15%), all instances are killed. Unallocated/reserved cash is excluded from the calculation so partially-deployed portfolios don't false-trigger.
2. **Funding verification.** After topping up a new strategy wallet, COBRA verifies the balance arrived before proceeding.
4. **Kill-pending retry.** After closing positions, COBRA checks for remaining open positions. If any persist, the instance is marked `kill_pending`. The brain retries every `killPendingRetryMinutes` (default 5), up to `killPendingMaxRetries` (default 3). After max retries, the instance is flagged STUCK and an alert is emitted for manual intervention.
5. **Pending-action verification.** Each brain run verifies that spawns/kills requested in the previous run actually materialized. Missed kills are automatically re-issued; missed spawns generate warnings.
6. **Signal staleness detection.** If signal pressure data is older than 10 minutes, it's zeroed out to avoid acting on stale signals.
7. **Regime-aware leverage.** Default leverage scales with market conditions: full in TRENDING, reduced in RANGING (5-8x), halved in VOLATILE (3-5x).
8. **Dry-run mode.** Set `COBRA_DRY_RUN=1` to run all decision logic without executing MCP calls.

## Known Limitations

1. **No partial withdrawals.** Senpi requires closing all positions to withdraw from a strategy. This is why the kill-vs-keep framework exists.
2. **Per-instance workspace isolation.** Each subagent gets a dedicated workspace (`instances/{id}/`) via `OPENCLAW_WORKSPACE` env var. Scan files are written there so signal pressure is computed per-instance. Legacy instances that wrote to the shared root are handled via fallback reads.
3. **Agent must execute spawn/kill.** COBRA outputs `sessions_spawn` and `sessions_send` instructions — the agent on the main session must execute them. The brain verifies on the next cycle whether actions were carried out and re-issues missed kills automatically.
4. **No backtesting.** Regime classifier and signal pressure are based on live data only. Unit tests cover circuit breaker, kill-vs-keep, leverage, and retry logic.
5. **WOLF/TIGER skill dependency.** COBRA assumes wolf-strategy and tiger skills are installed and functional.
6. **Subagent session limits.** OpenClaw's `maxChildrenPerAgent` (default 5) caps concurrent subagents. With 2 WOLF + 1 TIGER, this is fine. Increase if running more instances.
7. **Subagent auto-archive.** OpenClaw archives subagent sessions after `archiveAfterMinutes` (default 60). COBRA's wake crons keep sessions alive by sending periodic messages.
8. **No automated subagent respawn.** If a subagent session dies, its wake crons detect `SUBAGENT_DOWN` but there is no automated recovery. The brain does not currently respawn dead subagents -- monitor manually and redeploy if needed.

---

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| Brain outputs empty decisions | No spawned instances | Wait for first spawn or check config budget |
| Signal pressure always 0 | No scan history files | WOLF/TIGER instances haven't run scanners yet |
| Spawn fails | MCP connection issue | Check `mcporter list`, retry |
| Kill fails to close positions | Positions already closed | Check clearinghouse state; spawner handles gracefully |
| Token budget exceeded | Too many spawned crons | Reduce maxWolves/maxTigers or increase dailyLimitTokens |
| Regime stuck at UNKNOWN | No BTC data from MCP | Check `market_get_asset_data` availability |
