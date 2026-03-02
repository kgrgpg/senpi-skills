#!/usr/bin/env python3
"""
cobra-setup.py — COBRA Setup Wizard.

Creates cobra-config.json, initializes state files, and outputs 4 COBRA
cron templates for the user to create in OpenClaw.

Usage:
    python3 cobra-setup.py --chat-id 12345 --total-budget 10000 \
        --max-wolves 2 --max-tigers 1 --mid-model "anthropic/claude-sonnet-4-20250514"

    # Interactive mode:
    python3 cobra-setup.py
"""

import json, sys, os, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cobra_config import (
    WORKSPACE, CONFIG_FILE, COBRA_STATE_DIR, SPAWNED_DIR,
    STATE_FILE, PERFORMANCE_FILE, TOKEN_BUDGET_FILE,
    DEFAULTS, atomic_write, utc_now, utc_today,
)
from viper_gate import generate_cron_payload, wrap_mandate

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

parser = argparse.ArgumentParser(description="COBRA Setup Wizard")
parser.add_argument("--chat-id", type=int, help="Telegram chat ID")
parser.add_argument("--total-budget", type=float, help="Total trading budget (USD)")
parser.add_argument("--max-wolves", type=int, default=2, help="Max WOLF instances (default: 2)")
parser.add_argument("--max-tigers", type=int, default=1, help="Max TIGER instances (default: 1)")
parser.add_argument("--reserve-pct", type=int, default=15, help="Reserve percentage (default: 15)")
parser.add_argument("--min-spawn-budget", type=float, default=500, help="Min budget per spawn (default: $500)")
parser.add_argument("--mid-model", default="anthropic/claude-sonnet-4-20250514", help="Mid-tier model")
parser.add_argument("--budget-model", default="anthropic/claude-haiku-4-5", help="Budget-tier model")
parser.add_argument("--primary-model", default=None,
                    help="Primary model (for main session crons; uses your configured model if omitted)")
args = parser.parse_args()


def ask(prompt, default=None, validator=None):
    while True:
        suffix = f" [{default}]" if default else ""
        val = input(f"{prompt}{suffix}: ").strip()
        if not val and default is not None:
            val = str(default)
        if validator:
            try:
                return validator(val)
            except Exception as e:
                print(f"  Invalid: {e}")
        elif val:
            return val
        else:
            print("  Required.")


print("=" * 60)
print("  COBRA — Capital Orchestrator with Brain-Regulated Agents")
print("  Setup Wizard")
print("=" * 60)
print()

chat_id = args.chat_id or ask("Telegram chat ID", validator=lambda v: int(v))
total_budget = args.total_budget or ask("Total trading budget (USD, min $1000)",
                                         validator=lambda v: float(v) if float(v) >= 1000 else (_ for _ in ()).throw(ValueError("Min $1000")))
max_wolves = args.max_wolves
max_tigers = args.max_tigers
reserve_pct = args.reserve_pct
min_spawn = args.min_spawn_budget
mid_model = args.mid_model
budget_model = args.budget_model

# Build config
config = {
    "version": 1,
    "totalBudget": total_budget,
    "reservePct": reserve_pct,
    "maxWolves": max_wolves,
    "maxTigers": max_tigers,
    "minSpawnBudget": min_spawn,
    "telegramChatId": str(chat_id),
    "midModel": mid_model,
    "budgetModel": budget_model,
    "regime": DEFAULTS["regime"],
    "killVsKeep": DEFAULTS["killVsKeep"],
    "tokenBudget": DEFAULTS["tokenBudget"],
}

# Create directories
for d in [COBRA_STATE_DIR, SPAWNED_DIR]:
    os.makedirs(d, exist_ok=True)

# Save config
atomic_write(CONFIG_FILE, config)
print(f"\n  Config saved to {CONFIG_FILE}")

# Initialize state
initial_state = {
    "version": 1,
    "regime": "UNKNOWN",
    "regimeConfidence": 0,
    "regimeChangedAt": None,
    "spawnedInstances": {},
    "totalAllocated": 0,
    "lastBrainRun": None,
    "lastDecision": None,
    "globalSignalPressure": 0,
    "createdAt": utc_now(),
    "updatedAt": utc_now(),
}
atomic_write(STATE_FILE, initial_state)
print(f"  State initialized at {STATE_FILE}")

# Initialize performance
initial_perf = {
    "instances": {},
    "global": {
        "totalAccountValue": 0,
        "totalUnrealizedPnl": 0,
        "avgUtilization": 0,
        "activeWolves": 0,
        "activeTigers": 0,
        "globalSignalPressure": 0,
    },
    "alerts": [],
    "updatedAt": utc_now(),
}
atomic_write(PERFORMANCE_FILE, initial_perf)
print(f"  Performance file initialized at {PERFORMANCE_FILE}")

# Initialize token budget
initial_budget = {
    "date": utc_today(),
    "totalTokensEstimated": 0,
    "invocations": {},
    "recommendations": [],
}
atomic_write(TOKEN_BUDGET_FILE, initial_budget)
print(f"  Token budget initialized at {TOKEN_BUDGET_FILE}")

# Calculate capital allocation preview
usable = total_budget * (1 - reserve_pct / 100)
wolf_share_trending = total_budget * 0.60
tiger_share_trending = total_budget * 0.25
per_wolf = round(wolf_share_trending / max_wolves, 2) if max_wolves else 0
per_tiger = round(tiger_share_trending / max_tigers, 2) if max_tigers else 0

print(f"""
{'=' * 60}
  COBRA Configuration Summary
{'=' * 60}

  Total Budget:       ${total_budget:,.2f}
  Reserve:            {reserve_pct}% (${total_budget * reserve_pct / 100:,.2f})
  Usable Capital:     ${usable:,.2f}
  Max WOLF instances: {max_wolves}
  Max TIGER instances:{max_tigers}
  Min Spawn Budget:   ${min_spawn:,.2f}
  Telegram:           {chat_id}

  Capital Allocation Preview (TRENDING regime):
    WOLF:  60% = ${wolf_share_trending:,.0f} ({max_wolves} instances x ${per_wolf:,.0f})
    TIGER: 25% = ${tiger_share_trending:,.0f} ({max_tigers} instances x ${per_tiger:,.0f})
    Reserve: 15%

  Models:
    Primary: Your configured model (main session)
    Mid:     {mid_model}
    Budget:  {budget_model}
""")

# Build COBRA's own 4 cron templates
tg = f"telegram:{chat_id}"

cron_brain = generate_cron_payload(
    name="COBRA Brain (15min)",
    schedule_ms=900000,
    session="main",
    model=None,
    mandate=(
        f"COBRA Brain: Run `PYTHONUNBUFFERED=1 python3 {SCRIPTS_DIR}/cobra-brain.py`, parse JSON.\n\n"
        f"This is the COBRA orchestrator output. Act on it IN ORDER:\n"
        f"1. If `subagentSpawns` is non-empty: for each entry, call `sessions_spawn` with the provided params (task, label, model, thread, mode). This creates a new WOLF/TIGER subagent.\n"
        f"2. If `cronsToCreate` is non-empty: create each wake cron using OpenClaw. These crons periodically send messages to the spawned subagents.\n"
        f"3. If `subagentKills` is non-empty: for each entry, use `sessions_send` to deliver the killMessage to the subagent, then use `/subagents kill` with the label to terminate it.\n"
        f"4. If `cronsToDelete` is non-empty: delete each listed cron from OpenClaw.\n"
        f"5. If `subagentMessages` is non-empty: for each entry, use `sessions_send` to deliver the message to the target subagent (regime updates, etc).\n"
        f"6. If `regimeShifted`: alert {tg} with regime change.\n"
        f"7. If any kills occurred: alert {tg} with kill summary (PnL booked, capital freed).\n"
        f"8. If any spawns occurred: alert {tg} with new instances created.\n"
        f"9. If `stuckInstances` is non-empty: CRITICAL alert {tg} — these instances failed to close positions after multiple retries, manual intervention required.\n"
        f"10. If `actionable: 0`: HEARTBEAT_OK."
    ),
    viper_wrap=True,
)

cron_monitor = generate_cron_payload(
    name="COBRA Monitor (30min)",
    schedule_ms=1800000,
    session="isolated",
    model=mid_model,
    mandate=(
        f"COBRA Monitor: Run `PYTHONUNBUFFERED=1 python3 {SCRIPTS_DIR}/cobra-monitor.py`, parse JSON.\n\n"
        f"If `alerts` is non-empty: summarize alerts and send to {tg}.\n"
        f"If drawdown exceeds 20% on any instance: CRITICAL alert to {tg}.\n"
        f"Else HEARTBEAT_OK."
    ),
    viper_wrap=True,
)

cron_signals = generate_cron_payload(
    name="COBRA Signal Scan (5min)",
    schedule_ms=300000,
    session="isolated",
    model=budget_model,
    mandate=(
        f"COBRA Signals: Run `PYTHONUNBUFFERED=1 python3 {SCRIPTS_DIR}/cobra-signals.py`, parse JSON.\n\n"
        f"If `globalSignalPressure > 70`: alert {tg} \"High signal pressure: "
        f"{{globalSignalPressure}}/100 — strong opportunities being missed.\"\n"
        f"Else HEARTBEAT_OK."
    ),
    viper_wrap=True,
)

cron_token_audit = generate_cron_payload(
    name="COBRA Token Audit (1hr)",
    schedule_ms=3600000,
    session="isolated",
    model=budget_model,
    mandate=(
        f"COBRA Token Audit: Run `python3 -c \"\n"
        f"import sys; sys.path.insert(0, '{SCRIPTS_DIR}');\n"
        f"from viper_gate import get_budget_status, recommend_frequency;\n"
        f"import json;\n"
        f"status = get_budget_status();\n"
        f"print(json.dumps(status))\n"
        f"\"`, parse JSON.\n\n"
        f"If `overBudget: true`: alert {tg} with spend vs limit.\n"
        f"If `remainingPct < 20`: warn {tg} approaching daily limit.\n"
        f"Else HEARTBEAT_OK."
    ),
    viper_wrap=True,
)

cron_templates = {
    "brain": cron_brain,
    "monitor": cron_monitor,
    "signals": cron_signals,
    "tokenAudit": cron_token_audit,
}

print(f"""{'=' * 60}
  Next Steps: Create 4 COBRA cron jobs
{'=' * 60}

  COBRA uses 4 crons to orchestrate everything:

  ┌────────────────────┬──────────┬──────────┬───────────────────────────┐
  │ Cron               │ Interval │ Session  │ Model                     │
  ├────────────────────┼──────────┼──────────┼───────────────────────────┤
  │ Brain              │ 15 min   │ main     │ Primary (your model)      │
  │ Monitor            │ 30 min   │ isolated │ Mid: {mid_model[:25]}...  │
  │ Signal Scan        │ 5 min    │ isolated │ Budget: {budget_model}    │
  │ Token Audit        │ 1 hr     │ isolated │ Budget: {budget_model}    │
  └────────────────────┴──────────┴──────────┴───────────────────────────┘

  The Brain cron will automatically spawn WOLF/TIGER instances
  and create their crons on the first run.

  Total cron overhead: 4 COBRA crons (compare: WOLF alone uses 7, TIGER uses 12).
""")

# Output JSON result
result = {
    "success": True,
    "config": config,
    "stateDir": COBRA_STATE_DIR,
    "configFile": CONFIG_FILE,
    "cronTemplates": cron_templates,
    "cronCount": 4,
}

print(json.dumps(result, indent=2))
