#!/usr/bin/env python3
"""
cobra-spawner.py — Dynamic WOLF/TIGER Instance Lifecycle Manager for COBRA.

Uses OpenClaw sessions_spawn to create real subagent sessions for each
WOLF/TIGER instance. Each subagent runs in its own isolated context,
preventing context pollution in the main session.

Architecture:
    COBRA Brain (main session)
        └─> sessions_spawn → WOLF subagent (own session, own context)
        └─> sessions_spawn → TIGER subagent (own session, own context)

The subagent receives a comprehensive task description containing its
wallet, budget, parameters, and the full WOLF/TIGER mandate. It manages
its own scanning, entries, exits, and monitoring autonomously.

COBRA communicates with subagents via:
    - sessions_send: periodic instructions (regime changes, kill orders)
    - Subagent announces: results flow back to COBRA on completion

Called by cobra-brain.py when spawn/kill decisions are made.
"""

import json, sys, os, subprocess, glob, time, uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cobra_config import (
    load_config, load_spawned_instances, save_spawned_instance,
    get_instance_state_dir, get_instance_workspace, ensure_instance_workspace,
    mcporter_call, mcporter_call_safe,
    atomic_write, output, utc_now, load_json_safe, WORKSPACE,
    COBRA_STATE_DIR, SPAWNED_DIR,
)
from viper_gate import wrap_mandate, generate_cron_payload

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
WOLF_SCRIPTS = os.path.join(os.path.dirname(SCRIPTS_DIR), "..", "wolf-strategy", "scripts")
TIGER_SCRIPTS = os.path.join(os.path.dirname(SCRIPTS_DIR), "..", "tiger", "scripts")


def _resolve_scripts_dir(instance_type):
    """Resolve the scripts directory for a given instance type."""
    if instance_type == "wolf":
        candidates = [
            os.path.join(WORKSPACE, "skills", "wolf-strategy", "scripts"),
            os.path.normpath(WOLF_SCRIPTS),
        ]
    else:
        candidates = [
            os.path.join(WORKSPACE, "skills", "tiger", "scripts"),
            os.path.normpath(TIGER_SCRIPTS),
        ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]


def _generate_instance_id(instance_type):
    short = uuid.uuid4().hex[:8]
    return f"{instance_type}-{short}"


def _compute_slots(budget):
    """Scale trading slots with budget size."""
    if budget < 3000:
        return 2
    elif budget < 10000:
        return 3
    elif budget < 30000:
        return 4
    else:
        return 5


def _build_wolf_subagent_task(instance_id, scripts, tg, wallet, budget,
                               slots, margin, leverage, dsl_preset,
                               instance_workspace):
    """Build the comprehensive task description for a WOLF subagent.

    This is the single prompt that defines the subagent's entire behavior.
    It runs in its own session with its own context — no pollution from
    other instances or COBRA's main session.
    """
    ws_env = f"OPENCLAW_WORKSPACE={instance_workspace}"
    return f"""You are WOLF instance {instance_id}, managed by COBRA.

## Your Identity
- Instance ID: {instance_id}
- Type: WOLF (momentum trading via smart money signals)
- Wallet: {wallet}
- Budget: ${budget:.0f}
- Slots: {slots} (margin ${margin:.0f}/slot, {leverage}x leverage)
- DSL Preset: {dsl_preset}
- Telegram: {tg}

## Workspace
Your dedicated workspace: {instance_workspace}
IMPORTANT: Prefix every script command with `{ws_env}` so outputs are
written to your isolated directory, not shared with other instances.

## Your Mission
You are an autonomous WOLF trading agent. You scan for smart money movements,
enter positions on momentum signals, and manage exits via the DSL trailing
stop system. You operate independently in your own session.

## Recurring Work (execute on every wake)
Run these scripts in order, act on results:

### 1. Emerging Movers Scanner (PRIMARY — most important)
Run: `{ws_env} PYTHONUNBUFFERED=1 python3 {scripts}/emerging-movers.py`
- On FIRST_JUMP/CONTRIB_EXPLOSION/IMMEDIATE_MOVER signals: open position on {wallet}
- Max {slots} slots, ${margin:.0f}/slot, target {leverage}x leverage
- Route all entries to wallet {wallet}
- Alert {tg} for each entry
- If no signals: note "no signals" and continue

### 2. DSL Combined Runner
Run: `{ws_env} PYTHONUNBUFFERED=1 python3 {scripts}/dsl-combined.py`
- For closed positions: alert {tg} with asset, direction, close reason, PnL
- If any_closed: note freed slots for next scan cycle

### 3. SM Flip Detector
Run: `{ws_env} python3 {scripts}/sm-flip-check.py`
- On FLIP_NOW: close position on {wallet}, alert {tg}

### 4. Watchdog
Run: `{ws_env} PYTHONUNBUFFERED=1 timeout 45 python3 {scripts}/wolf-monitor.py`
- Liq buffer <30%: close lowest ROE position, alert {tg}

### 5. Health Check
Run: `{ws_env} PYTHONUNBUFFERED=1 python3 {scripts}/job-health-check.py`
- Alert {tg} on auto_created/auto_replaced/alert_only issues

### 6. Opportunity Scanner (every other wake)
Run: `{ws_env} PYTHONUNBUFFERED=1 timeout 180 python3 {scripts}/opportunity-scan-v6.py 2>/dev/null`
- Route 175+ scored opportunities to {wallet}
- Alert {tg}

## Rules
- ENTER EARLY on first jumps, not at confirmed peaks
- Target {leverage}x leverage (adjust down in volatile conditions, COBRA will send regime updates)
- If COBRA sends you a KILL order, close all positions immediately and announce results
- If COBRA sends a REGIME update, adjust your aggression and leverage accordingly
- Always output structured JSON summaries for COBRA to read

## VIPER Token Optimization
- If no scripts produce actionable output, respond with exactly "HEARTBEAT_OK"
- Keep responses minimal when no action is needed
- Only elaborate when entries, exits, or alerts occur
"""


def _build_tiger_subagent_task(instance_id, scripts, tg, wallet, budget,
                                max_slots, goal_pct, instance_workspace):
    """Build the comprehensive task description for a TIGER subagent."""
    ws_env = f"TIGER_WORKSPACE={instance_workspace}"
    return f"""You are TIGER instance {instance_id}, managed by COBRA.

## Your Identity
- Instance ID: {instance_id}
- Type: TIGER (calculated, multi-scanner goal-based trading)
- Wallet: {wallet}
- Budget: ${budget:.0f}
- Max Slots: {max_slots}
- Goal: +{goal_pct}% (${budget * goal_pct / 100:.0f})
- Telegram: {tg}

## Workspace
Your dedicated workspace: {instance_workspace}
IMPORTANT: Prefix every script command with `{ws_env}` so state files are
written to your isolated directory, not shared with other instances.

## Your Mission
You are an autonomous TIGER trading agent. You use 5 independent scanners
(compression breakout, BTC correlation lag, momentum breakout, mean reversion,
funding rate arb) to build confluence scores, then enter positions when
multiple scanners agree. A goal engine adjusts aggression based on performance
vs target. You operate independently in your own session.

## Recurring Work (execute on every wake)

### 1. OI Tracker (every wake — builds history for scanners)
Run: `{ws_env} python3 {scripts}/oi-tracker.py`
- Samples open interest for all prescreened assets
- Compression and reversion scanners need ~1h of OI history

### 2. Prescreener (every other wake)
Run: `{ws_env} python3 {scripts}/prescreener.py`
- Scores all ~230 assets in one API call, writes top 30 to prescreened.json
- All scanners read from this instead of doing their own filtering

### 3. Scanner Battery (run all 5, act on signals)
Run each scanner, evaluate outputs for entry signals:
- `{ws_env} python3 {scripts}/compression-scanner.py` (BB compression breakout)
- `{ws_env} python3 {scripts}/correlation-scanner.py` (BTC correlation lag)
- `{ws_env} python3 {scripts}/momentum-scanner.py` (momentum breakout)
- `{ws_env} python3 {scripts}/reversion-scanner.py` (mean reversion)
- `{ws_env} python3 {scripts}/funding-scanner.py` (funding rate arbitrage)

For each scanner: if actionable > 0 and confluence >= threshold for current
aggression level and slots available: enter via create_position on {wallet}.
Alert {tg} with asset, direction, pattern, confluence score, leverage.

### 4. Goal Engine (every 4th wake — adjusts aggression)
Run: `{ws_env} python3 {scripts}/goal-engine.py`
- Compares current balance vs target, adjusts aggression (CONSERVATIVE/NORMAL/ELEVATED/ABORT)
- Recalculates daily rate needed, updates confluence thresholds

### 5. Risk Guardian (every wake)
Run: `{ws_env} python3 {scripts}/risk-guardian.py`
- Enforces daily loss limit, max drawdown, max concurrent positions
- Can halt trading if limits breached
- Alert {tg} on critical risk events

### 6. Exit Checker + DSL (every wake)
Run: `{ws_env} python3 {scripts}/tiger-exit.py`
- Pattern-specific exit logic (each scanner pattern has its own exit rules)
Run: `{ws_env} python3 {scripts}/dsl-v4.py`
- Trailing stop loss management for all active positions
- Alert {tg} on closes with asset, direction, PnL, close reason

### 7. ROAR Analyst (every 4th wake)
Run: `{ws_env} python3 {scripts}/roar-analyst.py`
- Meta-optimizer: analyzes trade history, proposes config changes
- Outputs proposed changeset for review

## Rules
- Calculated entries only — require scanner confluence above aggression-adjusted threshold
- Respect max slots and goal-based aggression from Goal Engine
- Risk Guardian can halt all trading — respect halt state
- If COBRA sends a KILL order, close all positions immediately and announce results
- If COBRA sends a REGIME update, adjust leverage and aggression accordingly
- Always output structured JSON summaries for COBRA to read

## VIPER Token Optimization
- If no scripts produce actionable output, respond with exactly "HEARTBEAT_OK"
- Keep responses minimal when no action is needed
- Only elaborate when entries, exits, or alerts occur
"""


def _compute_leverage(budget, regime="TRENDING"):
    """Compute default leverage scaled by budget and market regime."""
    base = 7 if budget < 5000 else 10
    if regime == "VOLATILE":
        return max(3, base // 2)
    if regime == "RANGING":
        return max(5, base - 2)
    return base


def spawn_wolf(budget, dsl_preset="aggressive", name=None, config=None,
               regime="TRENDING"):
    """Spawn a new WOLF instance via sessions_spawn.

    1. Create strategy wallet via MCP
    2. Fund it
    3. Build subagent task description
    4. Register in COBRA state
    5. Output sessions_spawn instruction for the agent

    Returns dict with instance_id, wallet, subagent_task, or error.
    """
    cfg = config or load_config()
    chat_id = cfg.get("telegramChatId", "")
    mid_model = cfg.get("midModel", "anthropic/claude-sonnet-4-20250514")

    instance_id = _generate_instance_id("wolf")
    if name is None:
        name = f"COBRA-{instance_id}"

    try:
        create_result = mcporter_call("strategy_create_custom_strategy", name=name)
        wallet = create_result.get("wallet", create_result.get("address", ""))
        strategy_uuid = create_result.get("strategyId", create_result.get("id", ""))
    except RuntimeError as e:
        return {"success": False, "error": f"Failed to create strategy: {e}"}

    if not wallet:
        return {"success": False, "error": "No wallet returned from strategy creation"}

    try:
        mcporter_call("strategy_top_up", amount=budget, strategyId=strategy_uuid)
    except RuntimeError as e:
        return {"success": False, "error": f"Failed to fund strategy: {e}",
                "wallet": wallet, "strategyId": strategy_uuid}

    verify_ch = mcporter_call_safe("strategy_get_clearinghouse_state", wallet=wallet)
    if verify_ch:
        actual_value = float(verify_ch.get("accountValue", verify_ch.get("equity", 0)))
        if actual_value < budget * 0.95:
            return {
                "success": False,
                "error": f"Funding verification failed: expected ~${budget:.0f}, "
                         f"got ${actual_value:.0f}",
                "wallet": wallet, "strategyId": strategy_uuid,
            }

    slots = _compute_slots(budget)
    margin_per_slot = round(budget * 0.30, 2)
    default_leverage = _compute_leverage(budget, regime)

    instance_workspace = ensure_instance_workspace(instance_id)
    scripts = _resolve_scripts_dir("wolf")
    tg = f"telegram:{chat_id}" if chat_id else "telegram:0"

    subagent_task = _build_wolf_subagent_task(
        instance_id, scripts, tg, wallet, budget, slots,
        margin_per_slot, default_leverage, dsl_preset,
        instance_workspace,
    )

    # Build the sessions_spawn instruction for the agent to execute
    spawn_instruction = {
        "tool": "sessions_spawn",
        "params": {
            "task": subagent_task,
            "label": instance_id,
            "model": mid_model,
            "thread": True,
            "mode": "session",
            "runTimeoutSeconds": 0,
        },
    }

    # Also generate crons for work that benefits from scheduled execution
    # These crons send messages to the subagent session via sessions_send
    cron_payloads = _build_wolf_wake_crons(instance_id, mid_model)

    instance_data = {
        "type": "wolf",
        "instanceId": instance_id,
        "wallet": wallet,
        "strategyId": strategy_uuid,
        "budget": budget,
        "slots": slots,
        "marginPerSlot": margin_per_slot,
        "defaultLeverage": default_leverage,
        "dslPreset": dsl_preset,
        "status": "active",
        "spawnedAt": utc_now(),
        "subagentLabel": instance_id,
        "cronNames": [c["name"] for c in cron_payloads],
        "spawnedBy": "cobra-brain",
    }
    save_spawned_instance(instance_id, instance_data)

    return {
        "success": True,
        "instanceId": instance_id,
        "wallet": wallet,
        "strategyId": strategy_uuid,
        "budget": budget,
        "slots": slots,
        "spawnInstruction": spawn_instruction,
        "cronPayloads": cron_payloads,
        "cronCount": len(cron_payloads),
    }


def _build_wolf_wake_crons(instance_id, mid_model):
    """Build crons that periodically wake the WOLF subagent.

    Instead of crons doing the work themselves, these crons send a
    'wake and scan' message to the subagent session, keeping all
    context inside the subagent.
    """
    prefix = f"COBRA/{instance_id}"
    crons = []

    # Primary scanning wake (every 90s) — tells subagent to run scanners
    crons.append(generate_cron_payload(
        name=f"{prefix}/Wake-Scan",
        schedule_ms=90000,
        session="main",
        model=None,
        mandate=(
            f"COBRA subagent wake: Use sessions_send to send this message to "
            f"subagent '{instance_id}':\n"
            f'"Run your Emerging Movers scanner and DSL combined check now. '
            f'Report any entries, exits, or alerts. If nothing actionable, '
            f'reply HEARTBEAT_OK."\n'
            f"If the subagent is not running, reply with "
            f'"SUBAGENT_DOWN: {instance_id}" so COBRA Brain can respawn it.'
        ),
        viper_wrap=True,
    ))

    # Secondary checks wake (every 5 min) — SM flip, watchdog, health
    crons.append(generate_cron_payload(
        name=f"{prefix}/Wake-Monitor",
        schedule_ms=300000,
        session="main",
        model=None,
        mandate=(
            f"COBRA subagent wake: Use sessions_send to send this message to "
            f"subagent '{instance_id}':\n"
            f'"Run SM flip check, watchdog, and health check now. '
            f'Report any critical alerts. If nothing actionable, '
            f'reply HEARTBEAT_OK."\n'
            f"If the subagent is not running, reply with "
            f'"SUBAGENT_DOWN: {instance_id}".'
        ),
        viper_wrap=True,
    ))

    # Full cycle wake (every 15 min) — all scripts including scanner
    crons.append(generate_cron_payload(
        name=f"{prefix}/Wake-Full",
        schedule_ms=900000,
        session="main",
        model=None,
        mandate=(
            f"COBRA subagent wake: Use sessions_send to send this message to "
            f"subagent '{instance_id}':\n"
            f'"Run FULL cycle: opportunity scanner, portfolio update, and all '
            f'secondary checks. Send portfolio summary to Telegram. '
            f'Report all findings."\n'
            f"If the subagent is not running, reply with "
            f'"SUBAGENT_DOWN: {instance_id}".'
        ),
        viper_wrap=True,
    ))

    return crons


def spawn_tiger(budget, goal_pct=5, max_slots=3, name=None, config=None,
                regime="TRENDING"):
    """Spawn a new TIGER instance via sessions_spawn."""
    cfg = config or load_config()
    chat_id = cfg.get("telegramChatId", "")
    mid_model = cfg.get("midModel", "anthropic/claude-sonnet-4-20250514")

    instance_id = _generate_instance_id("tiger")
    if name is None:
        name = f"COBRA-{instance_id}"

    try:
        create_result = mcporter_call("strategy_create_custom_strategy", name=name)
        wallet = create_result.get("wallet", create_result.get("address", ""))
        strategy_uuid = create_result.get("strategyId", create_result.get("id", ""))
    except RuntimeError as e:
        return {"success": False, "error": f"Failed to create strategy: {e}"}

    if not wallet:
        return {"success": False, "error": "No wallet returned from strategy creation"}

    try:
        mcporter_call("strategy_top_up", amount=budget, strategyId=strategy_uuid)
    except RuntimeError as e:
        return {"success": False, "error": f"Failed to fund strategy: {e}",
                "wallet": wallet, "strategyId": strategy_uuid}

    verify_ch = mcporter_call_safe("strategy_get_clearinghouse_state", wallet=wallet)
    if verify_ch:
        actual_value = float(verify_ch.get("accountValue", verify_ch.get("equity", 0)))
        if actual_value < budget * 0.95:
            return {
                "success": False,
                "error": f"Funding verification failed: expected ~${budget:.0f}, "
                         f"got ${actual_value:.0f}",
                "wallet": wallet, "strategyId": strategy_uuid,
            }

    instance_workspace = ensure_instance_workspace(instance_id)
    scripts = _resolve_scripts_dir("tiger")
    tg = f"telegram:{chat_id}" if chat_id else "telegram:0"

    subagent_task = _build_tiger_subagent_task(
        instance_id, scripts, tg, wallet, budget, max_slots, goal_pct,
        instance_workspace,
    )

    spawn_instruction = {
        "tool": "sessions_spawn",
        "params": {
            "task": subagent_task,
            "label": instance_id,
            "model": mid_model,
            "thread": True,
            "mode": "session",
            "runTimeoutSeconds": 0,
        },
    }

    cron_payloads = _build_tiger_wake_crons(instance_id, mid_model)

    instance_data = {
        "type": "tiger",
        "instanceId": instance_id,
        "wallet": wallet,
        "strategyId": strategy_uuid,
        "budget": budget,
        "maxSlots": max_slots,
        "goalPct": goal_pct,
        "status": "active",
        "spawnedAt": utc_now(),
        "subagentLabel": instance_id,
        "cronNames": [c["name"] for c in cron_payloads],
        "spawnedBy": "cobra-brain",
    }
    save_spawned_instance(instance_id, instance_data)

    return {
        "success": True,
        "instanceId": instance_id,
        "wallet": wallet,
        "strategyId": strategy_uuid,
        "budget": budget,
        "maxSlots": max_slots,
        "spawnInstruction": spawn_instruction,
        "cronPayloads": cron_payloads,
        "cronCount": len(cron_payloads),
    }


def _build_tiger_wake_crons(instance_id, mid_model):
    """Build crons that periodically wake the TIGER subagent."""
    prefix = f"COBRA/{instance_id}"
    crons = []

    # Scanner + risk + exit wake (every 5 min)
    crons.append(generate_cron_payload(
        name=f"{prefix}/Wake-Scan",
        schedule_ms=300000,
        session="main",
        model=None,
        mandate=(
            f"COBRA subagent wake: Use sessions_send to send this message to "
            f"subagent '{instance_id}':\n"
            f'"Run OI tracker, then scanner battery (compression, correlation, '
            f'momentum, reversion, funding). Run risk guardian, exit checker, '
            f'and DSL. Report any entries, exits, or alerts. If nothing actionable, '
            f'reply HEARTBEAT_OK."\n'
            f"If the subagent is not running, reply with "
            f'"SUBAGENT_DOWN: {instance_id}".'
        ),
        viper_wrap=True,
    ))

    # Prescreener + goal engine + ROAR wake (every 30 min)
    crons.append(generate_cron_payload(
        name=f"{prefix}/Wake-Full",
        schedule_ms=1800000,
        session="main",
        model=None,
        mandate=(
            f"COBRA subagent wake: Use sessions_send to send this message to "
            f"subagent '{instance_id}':\n"
            f'"Run FULL cycle: prescreener, OI tracker, all 5 scanners, '
            f'goal engine, risk guardian, exit checker, DSL, ROAR analyst. '
            f'Send portfolio summary to Telegram. Report all findings."\n'
            f"If the subagent is not running, reply with "
            f'"SUBAGENT_DOWN: {instance_id}".'
        ),
        viper_wrap=True,
    ))

    return crons


def build_kill_message(instance_id, reason):
    """Build a sessions_send message to tell a subagent to self-terminate.

    The subagent closes all its positions and announces results back.
    """
    return {
        "tool": "sessions_send",
        "targetLabel": instance_id,
        "message": (
            f"COBRA KILL ORDER for {instance_id}.\n"
            f"Reason: {reason}\n\n"
            f"IMMEDIATELY:\n"
            f"1. Close ALL open positions on your wallet\n"
            f"2. Set all DSL states to active: false\n"
            f"3. Report final PnL and positions closed\n"
            f"4. Announce your final status back to COBRA\n\n"
            f"This is non-negotiable. Execute now."
        ),
    }


def kill_instance(instance_id, reason="brain_decision"):
    """Kill a spawned instance.

    Closes positions via MCP (as fallback/verification), marks killed,
    and outputs instructions for the agent to:
    1. Send kill order to the subagent via sessions_send
    2. Kill the subagent session via /subagents kill
    3. Delete the wake crons

    Returns dict with realized_pnl, freed_capital, actions for agent.
    """
    spawn_file = os.path.join(SPAWNED_DIR, f"{instance_id}.json")
    instance_data = load_json_safe(spawn_file)
    if not instance_data:
        return {"success": False, "error": f"Instance {instance_id} not found"}

    if instance_data.get("status") == "killed":
        return {"success": False, "error": f"Instance {instance_id} already killed"}

    wallet = instance_data.get("wallet", "")
    itype = instance_data.get("type", "wolf")
    budget = instance_data.get("budget", 0)

    # Close positions via MCP as a direct action (don't rely solely on subagent)
    close_results = []
    state_dir = get_instance_state_dir(instance_id, itype)

    if itype == "wolf":
        dsl_files = glob.glob(os.path.join(state_dir, "dsl-*.json"))
        for dsl_path in dsl_files:
            state = load_json_safe(dsl_path)
            if not state or not state.get("active"):
                continue
            asset = state.get("asset", "")
            direction = state.get("direction", "LONG")
            try:
                close_data = mcporter_call("close_position", wallet=wallet, asset=asset)
                state["active"] = False
                state["closedBy"] = "cobra-kill"
                state["closedAt"] = utc_now()
                atomic_write(dsl_path, state)
                close_results.append({
                    "asset": asset, "direction": direction,
                    "status": "closed", "data": close_data,
                })
            except RuntimeError as e:
                close_results.append({
                    "asset": asset, "direction": direction,
                    "status": "error", "error": str(e),
                })
    elif itype == "tiger":
        ch = mcporter_call_safe("strategy_get_clearinghouse_state", wallet=wallet)
        if ch:
            positions = ch.get("positions", ch.get("assetPositions", []))
            for pos in positions:
                asset = pos.get("coin", pos.get("asset", ""))
                if not asset:
                    continue
                size = abs(float(pos.get("szi", pos.get("size", 0))))
                if size == 0:
                    continue
                try:
                    close_data = mcporter_call("close_position", wallet=wallet, asset=asset)
                    close_results.append({"asset": asset, "status": "closed", "data": close_data})
                except RuntimeError as e:
                    close_results.append({"asset": asset, "status": "error", "error": str(e)})

    final_ch = mcporter_call_safe("strategy_get_clearinghouse_state", wallet=wallet)
    final_value = float(final_ch.get("accountValue", final_ch.get("equity", 0))) if final_ch else 0
    realized_pnl = round(final_value - budget, 2)

    remaining_positions = 0
    if final_ch:
        positions = final_ch.get("positions", final_ch.get("assetPositions", []))
        for pos in positions:
            size = abs(float(pos.get("szi", pos.get("size", 0))))
            if size > 0:
                remaining_positions += 1
    else:
        if itype == "tiger" and not close_results:
            remaining_positions = 1
        elif close_results and any(r["status"] == "error" for r in close_results):
            remaining_positions = len([r for r in close_results if r["status"] == "error"])

    kill_status = "killed"
    if remaining_positions > 0:
        kill_status = "kill_pending"

    instance_data["status"] = kill_status
    instance_data["killedAt"] = utc_now()
    instance_data["killReason"] = reason
    instance_data["finalValue"] = final_value
    instance_data["realizedPnl"] = realized_pnl
    instance_data["closeResults"] = close_results
    instance_data["remainingPositions"] = remaining_positions
    save_spawned_instance(instance_id, instance_data)

    # Build kill message for subagent (graceful shutdown)
    kill_msg = build_kill_message(instance_id, reason)

    return {
        "success": True,
        "instanceId": instance_id,
        "killStatus": kill_status,
        "realizedPnl": realized_pnl,
        "finalValue": final_value,
        "freedCapital": final_value,
        "positionsClosed": len([r for r in close_results if r["status"] == "closed"]),
        "closeErrors": len([r for r in close_results if r["status"] == "error"]),
        "remainingPositions": remaining_positions,
        "cronsToDelete": instance_data.get("cronNames", []),
        "subagentToKill": instance_data.get("subagentLabel", instance_id),
        "killMessage": kill_msg,
        "closeResults": close_results,
    }


def build_regime_update_message(instance_id, regime, allocation):
    """Build a sessions_send message to update a subagent about regime change."""
    leverage_guidance = {
        "TRENDING": "Leverage up to 10x is appropriate for strong trends.",
        "RANGING": "Reduce leverage to 5-7x. Be more selective with entries.",
        "VOLATILE": "MAXIMUM CAUTION — reduce leverage to 3-5x, tighten stops, reduce exposure.",
    }
    return {
        "tool": "sessions_send",
        "targetLabel": instance_id,
        "message": (
            f"COBRA REGIME UPDATE: Market regime is now {regime}.\n"
            f"Allocation targets: WOLF {allocation.get('wolf', 0)}%, "
            f"TIGER {allocation.get('tiger', 0)}%, Reserve {allocation.get('reserve', 0)}%.\n"
            f"{leverage_guidance.get(regime, 'Adjust your aggression accordingly.')}"
        ),
    }


def get_spawn_summary():
    """Get a summary of all spawned instances for brain decisions."""
    instances = load_spawned_instances()
    wolves = {k: v for k, v in instances.items() if v.get("type") == "wolf"}
    tigers = {k: v for k, v in instances.items() if v.get("type") == "tiger"}
    total_allocated = sum(v.get("budget", 0) for v in instances.values())

    return {
        "activeWolves": len(wolves),
        "activeTigers": len(tigers),
        "totalAllocated": total_allocated,
        "instances": {
            k: {
                "type": v.get("type"),
                "budget": v.get("budget", 0),
                "wallet": v.get("wallet", ""),
                "spawnedAt": v.get("spawnedAt", ""),
                "slots": v.get("slots", v.get("maxSlots", 0)),
                "subagentLabel": v.get("subagentLabel", k),
            }
            for k, v in instances.items()
        },
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="COBRA Spawner")
    parser.add_argument("action", choices=["spawn-wolf", "spawn-tiger", "kill", "summary"])
    parser.add_argument("--budget", type=float, default=5000)
    parser.add_argument("--instance-id", help="Instance ID for kill action")
    parser.add_argument("--preset", default="aggressive")
    parser.add_argument("--goal-pct", type=float, default=5)
    parser.add_argument("--reason", default="manual")
    args = parser.parse_args()

    if args.action == "spawn-wolf":
        result = spawn_wolf(budget=args.budget, dsl_preset=args.preset)
    elif args.action == "spawn-tiger":
        result = spawn_tiger(budget=args.budget, goal_pct=args.goal_pct)
    elif args.action == "kill":
        if not args.instance_id:
            result = {"success": False, "error": "Need --instance-id for kill"}
        else:
            result = kill_instance(args.instance_id, reason=args.reason)
    elif args.action == "summary":
        result = get_spawn_summary()

    output(result)
