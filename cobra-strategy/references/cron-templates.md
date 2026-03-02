# COBRA Cron Templates

COBRA itself runs 4 crons. Spawned instances use the subagent model: each gets
a persistent subagent session plus lightweight wake crons (3 per WOLF, 2 per TIGER)
that send periodic messages to the subagent via `sessions_send`.

---

## Session & Model Tier Configuration

| Cron | Frequency | Session | Model Tier |
|------|-----------|---------|------------|
| Brain | 15 min | **main** | **Primary** (your configured model) |
| Monitor | 30 min | isolated | Mid |
| Signal Scan | 5 min | isolated | Budget |
| Token Audit | 1 hr | isolated | Budget |

**Total: 4 crons.** Compare: WOLF alone uses 7, TIGER uses 12. COBRA adds minimal overhead.

---

## Cron Format Reference

**Main session** (systemEvent):
```json
{
  "name": "...",
  "schedule": { "kind": "every", "everyMs": 900000 },
  "sessionTarget": "main",
  "wakeMode": "now",
  "payload": { "kind": "systemEvent", "text": "..." }
}
```

**Isolated session** (agentTurn):
```json
{
  "name": "...",
  "schedule": { "kind": "every", "everyMs": 1800000 },
  "sessionTarget": "isolated",
  "payload": { "kind": "agentTurn", "message": "...", "model": "model-id" }
}
```

**Critical:** systemEvent uses `"text"`, agentTurn uses `"message"`. Do NOT mix them.

---

## 1. Brain (every 15 min) — main session

```
VIPER: If script output is exactly "HEARTBEAT_OK" or the JSON contains "actionable": 0, respond with exactly "HEARTBEAT_OK" — no analysis, no summary, no explanation. Only parse and act if output contains actionable items.

COBRA Brain: Run `PYTHONUNBUFFERED=1 python3 {SCRIPTS}/cobra-brain.py`, parse JSON.

This is the COBRA orchestrator output. Act on it IN ORDER:
1. If `subagentSpawns` is non-empty: for each entry, call `sessions_spawn` with the provided params (task, label, model, thread, mode). This creates a new WOLF/TIGER subagent in its own isolated session.
2. If `cronsToCreate` is non-empty: create each wake cron using OpenClaw. These crons periodically send messages to the spawned subagents via sessions_send.
3. If `subagentKills` is non-empty: for each entry, use `sessions_send` to deliver the killMessage to the subagent, then use `/subagents kill` with the label to terminate it.
4. If `cronsToDelete` is non-empty: delete each listed cron from OpenClaw.
5. If `subagentMessages` is non-empty: for each entry, use `sessions_send` to deliver the message to the target subagent (regime updates, etc).
6. If `regimeShifted`: alert {TELEGRAM} with regime change.
7. If any kills occurred: alert {TELEGRAM} with kill summary (PnL booked, capital freed).
8. If any spawns occurred: alert {TELEGRAM} with new instances created.
9. If `stuckInstances` is non-empty: CRITICAL alert {TELEGRAM} — these instances failed to close positions after multiple retries, manual intervention required.
10. If `actionable: 0`: HEARTBEAT_OK.
```

Replace:
- `{SCRIPTS}` — path to cobra-strategy scripts dir
- `{TELEGRAM}` — telegram:CHAT_ID

---

## 2. Monitor (every 30 min) — isolated / Mid model

```
VIPER: If script output is exactly "HEARTBEAT_OK" or the JSON contains "actionable": 0, respond with exactly "HEARTBEAT_OK" — no analysis, no summary, no explanation. Only parse and act if output contains actionable items.

COBRA Monitor: Run `PYTHONUNBUFFERED=1 python3 {SCRIPTS}/cobra-monitor.py`, parse JSON.

If `alerts` is non-empty: summarize alerts and send to {TELEGRAM}.
If drawdown exceeds 20% on any instance: CRITICAL alert to {TELEGRAM}.
Else HEARTBEAT_OK.
```

---

## 3. Signal Scan (every 5 min) — isolated / Budget model

```
VIPER: If script output is exactly "HEARTBEAT_OK" or the JSON contains "actionable": 0, respond with exactly "HEARTBEAT_OK" — no analysis, no summary, no explanation. Only parse and act if output contains actionable items.

COBRA Signals: Run `PYTHONUNBUFFERED=1 python3 {SCRIPTS}/cobra-signals.py`, parse JSON.

If `globalSignalPressure > 70`: alert {TELEGRAM} "High signal pressure: {globalSignalPressure}/100 — strong opportunities being missed."
Else HEARTBEAT_OK.
```

This cron is ultra-cheap: reads local JSON files, computes a number, no MCP calls.

---

## 4. Token Audit (every 1 hr) — isolated / Budget model

```
VIPER: If script output is exactly "HEARTBEAT_OK" or the JSON contains "actionable": 0, respond with exactly "HEARTBEAT_OK" — no analysis, no summary, no explanation. Only parse and act if output contains actionable items.

COBRA Token Audit: Run `python3 -c "
import sys; sys.path.insert(0, '{SCRIPTS}');
from viper_gate import get_budget_status;
import json;
status = get_budget_status();
print(json.dumps(status))
"`, parse JSON.

If `overBudget: true`: alert {TELEGRAM} with spend vs limit.
If `remainingPct < 20`: warn {TELEGRAM} approaching daily limit.
Else HEARTBEAT_OK.
```

---

## Spawned Instance Architecture

When COBRA spawns a WOLF or TIGER instance, it uses `sessions_spawn` to create
a **real subagent** in its own isolated session. The subagent receives a comprehensive
task description and manages all scanning/trading autonomously.

Lightweight **wake crons** periodically send messages to the subagent via `sessions_send`,
telling it to run its scan cycle. This keeps the subagent alive and the work scheduled.

**WOLF instance: 1 subagent + 3 wake crons**

| Component | Type | Purpose |
|-----------|------|---------|
| Subagent session | `sessions_spawn` (persistent, thread-bound) | Runs all WOLF scripts in isolated context |
| Wake-Scan cron | 90s, main, `sessions_send` | Tells subagent: run Emerging Movers + DSL |
| Wake-Monitor cron | 5 min, main, `sessions_send` | Tells subagent: run SM flip + watchdog + health |
| Wake-Full cron | 15 min, main, `sessions_send` | Tells subagent: full cycle + portfolio update |

**TIGER instance: 1 subagent + 2 wake crons**

| Component | Type | Purpose |
|-----------|------|---------|
| Subagent session | `sessions_spawn` (persistent, thread-bound) | Runs all TIGER scripts in isolated context |
| Wake-Scan cron | 5 min, main, `sessions_send` | Tells subagent: scanners + entry engine + position manager |
| Wake-Full cron | 30 min, main, `sessions_send` | Tells subagent: prescreener + full cycle + ROAR |

All wake crons are named `COBRA/{instance-id}/{cron-name}` for easy identification and deletion.

### Why subagents instead of direct crons?

Direct crons dump all output into shared sessions, causing context pollution.
With subagents, each WOLF/TIGER instance has its own context window. The main
session stays clean with only COBRA brain decisions. This is critical when
running multiple instances simultaneously.
