#!/usr/bin/env python3
"""
cobra-brain.py — Main Orchestrator for COBRA.

The brain. Runs every 15 min on the main session. Combines regime classification,
signal pressure, and performance data to make spawn/kill/keep decisions.

Uses OpenClaw sessions_spawn for subagent-based instance management:
    - Spawn: outputs sessions_spawn instruction for the agent to execute
    - Kill: closes positions via MCP + outputs sessions_send kill order + subagent kill
    - Regime shift: outputs sessions_send regime updates to all subagents

Decision loop:
    1. Classify market regime (inline)
    2. Compute signal pressure (inline)
    3. Load state + performance
    4. For each instance: evaluate Kill vs Keep
    5. Spawn decisions based on idle capital + regime + signal pressure
    6. Execute via cobra-spawner
    7. Output actionable JSON for cron mandate

Output JSON:
    {"regime": "TRENDING", "decisions": [...], "spawns": [...], "kills": [...],
     "subagentSpawns": [...], "subagentKills": [...], "subagentMessages": [...],
     "summary": "...", "actionable": 1}
"""

import json, sys, os, glob, time, importlib.util
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cobra_config import (
    load_config, load_state, save_state, load_performance,
    load_spawned_instances, load_killed_instances,
    save_spawned_instance, get_instance_state_dir,
    mcporter_call_safe, load_json_safe, output, utc_now, minutes_since,
    WORKSPACE, COBRA_STATE_DIR, SPAWNED_DIR,
)

SIGNAL_PRESSURE_FILE = os.path.join(COBRA_STATE_DIR, "cobra-signals.json")
_SIGNAL_STALENESS_MINUTES = 10
_SPAWN_VERIFY_MINUTES = 30

_spawner_cache = None


def _get_spawner():
    """Lazy-load cobra-spawner.py (hyphenated filename requires importlib)."""
    global _spawner_cache
    if _spawner_cache is None:
        spec = importlib.util.spec_from_file_location(
            "cobra_spawner",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "cobra-spawner.py"))
        _spawner_cache = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_spawner_cache)
    return _spawner_cache


def _get_regime():
    """Run regime classification inline."""
    spec = importlib.util.spec_from_file_location(
        "cobra_regime",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "cobra-regime.py"))
    regime_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(regime_mod)

    data_4h = mcporter_call_safe("market_get_asset_data", asset="BTC", interval="4h", lookback=60)
    data_1h = mcporter_call_safe("market_get_asset_data", asset="BTC", interval="1h", lookback=30)

    candles_4h = data_4h.get("candles", data_4h.get("data", [])) if data_4h else []
    candles_1h = data_1h.get("candles", data_1h.get("data", [])) if data_1h else []
    funding = float(data_4h.get("funding", {}).get("rate", 0)) if data_4h else 0

    result = regime_mod.classify_regime(candles_4h, candles_1h, funding)
    result["allocation"] = regime_mod.get_allocation(result["regime"])
    return result


def _apply_regime_hysteresis(new_regime, new_confidence, state):
    """Prevent regime flapping by requiring consecutive confirmation.

    If the regime changed but confidence is < 0.65, keep the old regime
    until a second consecutive reading confirms the switch.
    """
    prev_regime = state.get("regime", "UNKNOWN")
    if prev_regime == "UNKNOWN" or new_regime == prev_regime:
        state.pop("_pendingRegime", None)
        return new_regime, new_confidence

    pending = state.get("_pendingRegime")
    if new_confidence >= 0.65:
        return new_regime, new_confidence

    if pending == new_regime:
        return new_regime, new_confidence

    state["_pendingRegime"] = new_regime
    return prev_regime, new_confidence


def _get_signal_pressure():
    """Load latest signal pressure data. Zeros out if stale."""
    default = {
        "instances": {},
        "globalSignalPressure": 0,
        "marketOpportunityDensity": "NONE",
    }
    data = load_json_safe(SIGNAL_PRESSURE_FILE)
    if not data:
        return default

    updated_at = data.get("updatedAt")
    age_min = minutes_since(updated_at)
    if age_min > _SIGNAL_STALENESS_MINUTES:
        data["globalSignalPressure"] = 0
        data["marketOpportunityDensity"] = "STALE"
        data["_staleWarning"] = (
            f"Signal data is {age_min:.0f}min old "
            f"(threshold {_SIGNAL_STALENESS_MINUTES}min), zeroed out"
        )
    return data


def _evaluate_kill_vs_keep(instance_id, instance_data, signal_data, perf_data, config):
    """Evaluate whether to KILL, KEEP, or WAIT for a single instance.

    Returns:
        {"decision": "KEEP"|"KILL"|"WAIT", "reasons": [...], "score": float}
    """
    kvk = config.get("killVsKeep", {})
    pressure_threshold = kvk.get("signalPressureKillThreshold", 60)
    idle_hours = kvk.get("idleHoursBeforeKill", 2)
    avg_fee = kvk.get("avgFeePerTrade", 32)

    itype = instance_data.get("type", "wolf")
    budget = instance_data.get("budget", 0)
    spawned_at = instance_data.get("spawnedAt", "")

    # Signal pressure for this instance
    instance_signals = signal_data.get("instances", {}).get(instance_id, {})
    signal_pressure = instance_signals.get("signalPressure", 0)

    # Performance for this instance
    instance_perf = perf_data.get("instances", {}).get(instance_id, {})
    account_value = instance_perf.get("accountValue", budget)
    upnl = instance_perf.get("unrealizedPnl", 0)
    utilization = instance_perf.get("utilization", 0)
    drawdown = instance_perf.get("drawdownFromPeak", 0)
    trade_stats = instance_perf.get("tradeStats", {})
    active_positions = trade_stats.get("activePositions", 0)

    reasons = []
    decision = "KEEP"
    keep_score = 0

    # --- KEEP conditions ---

    # Tier 2+ positions: strong reason to keep (trailing stop protecting gains),
    # but not absolute — extreme drawdown or circuit breaker can still override.
    if itype == "wolf":
        pos_quality = instance_signals.get("positionQuality", {})
        tier2plus = pos_quality.get("tier2plus", 0)
        if tier2plus > 0:
            keep_score += 40
            reasons.append(f"KEEP: {tier2plus} positions at Tier 2+ (trailing stop protecting gains)")

    # Low signal pressure: not missing much
    if signal_pressure < 40:
        keep_score += 15
        reasons.append(f"KEEP: signal pressure {signal_pressure} < 40 (not much being missed)")

    # Strong positive uPnL trending up
    if upnl > budget * 0.05:
        keep_score += 10
        reasons.append(f"KEEP: uPnL ${upnl:.0f} is +{upnl/budget*100:.1f}% (profitable)")

    # --- KILL conditions ---

    kill_score = 0

    # All positions Phase 1 + significant negative ROE (not just noise)
    if itype == "wolf":
        pos_quality = instance_signals.get("positionQuality", {})
        phase1 = pos_quality.get("phase1", 0)
        tier1 = pos_quality.get("tier1", 0)
        avg_roe = instance_signals.get("avgPositionROE", 0)
        if active_positions > 0 and phase1 == active_positions and avg_roe < -3:
            kill_score += 30
            reasons.append(f"KILL signal: all {phase1} positions Phase 1 with avg ROE {avg_roe}%")

    # High signal pressure
    if signal_pressure > pressure_threshold:
        kill_score += 25
        reasons.append(f"KILL signal: signal pressure {signal_pressure} > {pressure_threshold}")

    # Idle instance -- 0 positions for too long
    if active_positions == 0:
        hours_alive = minutes_since(spawned_at) / 60
        if hours_alive > idle_hours:
            kill_score += 35
            reasons.append(f"KILL signal: idle {hours_alive:.1f}h with 0 positions (threshold {idle_hours}h)")

    # High drawdown
    if drawdown > kvk.get("maxDrawdownPct", 20):
        kill_score += 30
        reasons.append(f"KILL signal: drawdown {drawdown}% exceeds max")

    # --- Opportunity EV calculation ---
    if signal_pressure > 40:
        # Estimate: missed signals x historical win rate x avg win
        missed_signals = (instance_signals.get("missedFirstJumps1h", 0) +
                          instance_signals.get("missedOpportunities1h", 0) +
                          instance_signals.get("highConfluenceCount", 0))
        win_rate = instance_signals.get("recentWinRate", 0.5)
        avg_win = budget * 0.03
        opportunity_ev = missed_signals * win_rate * avg_win

        booking_cost = active_positions * avg_fee
        restart_cost = 50

        if opportunity_ev > (upnl + booking_cost + restart_cost) and opportunity_ev > 0:
            kill_score += 20
            reasons.append(
                f"KILL signal: opportunity EV ${opportunity_ev:.0f} > "
                f"uPnL ${upnl:.0f} + fees ${booking_cost:.0f} + restart ${restart_cost:.0f}"
            )

    # --- Final decision: net score = kill signals minus keep signals ---
    net_score = kill_score - keep_score
    if net_score >= 30:
        decision = "KILL"
    elif net_score >= 0 and kill_score >= 25:
        decision = "WAIT"
        reasons.append(f"WAIT: kill={kill_score} keep={keep_score} net={net_score} (need net 30 to kill)")
    else:
        decision = "KEEP"
        if not reasons:
            reasons.append("KEEP: no kill signals detected")

    return {
        "decision": decision, "reasons": reasons,
        "score": kill_score, "keepScore": keep_score, "netScore": net_score,
    }


def _decide_spawns(regime_data, signal_data, state, config, current_instances):
    """Decide whether to spawn new instances based on regime + idle capital.

    Accounts for trapped capital in killed instances whose funds haven't
    been recovered, preventing over-deployment.
    """
    cfg = config
    allocation = regime_data.get("allocation", {})
    regime = regime_data.get("regime", "UNKNOWN")

    total_budget = cfg.get("totalBudget", 10000)
    min_spawn = cfg.get("minSpawnBudget", 500)
    max_wolves = cfg.get("maxWolves", 2)
    max_tigers = cfg.get("maxTigers", 1)

    # Only count active + pending_spawn instances (not kill_pending).
    # Missing status treated as active for backward compatibility.
    countable = {k: v for k, v in current_instances.items()
                 if v.get("status", "active") not in ("kill_pending", "killed")}
    active_wolves = sum(1 for v in countable.values() if v.get("type") == "wolf")
    active_tigers = sum(1 for v in countable.values() if v.get("type") == "tiger")
    allocated = sum(v.get("budget", 0) for v in countable.values())

    # Subtract capital trapped in killed wallets that haven't been withdrawn
    killed = load_killed_instances()
    trapped_capital = sum(v.get("finalValue", v.get("budget", 0))
                          for v in killed.values())

    reserve_pct = cfg.get("reservePct", 15)
    usable_budget = total_budget * (1 - reserve_pct / 100)
    idle_capital = usable_budget - allocated - trapped_capital

    global_pressure = signal_data.get("globalSignalPressure", 0)
    spawn_pressure_threshold = cfg.get("killVsKeep", {}).get("signalPressureSpawnThreshold", 50)

    spawns = []

    if idle_capital < min_spawn:
        return spawns

    # Only spawn if signal pressure justifies it, or we have no instances at all
    no_instances = (active_wolves + active_tigers) == 0
    pressure_ok = global_pressure >= spawn_pressure_threshold

    if not no_instances and not pressure_ok:
        return spawns

    wolf_target_pct = allocation.get("wolf", 30)
    tiger_target_pct = allocation.get("tiger", 30)

    wolf_target_budget = total_budget * wolf_target_pct / 100
    tiger_target_budget = total_budget * tiger_target_pct / 100

    wolf_allocated = sum(v.get("budget", 0)
                         for v in countable.values() if v.get("type") == "wolf")
    tiger_allocated = sum(v.get("budget", 0)
                          for v in countable.values() if v.get("type") == "tiger")

    # Build spawn candidates, then sort by allocation priority so the
    # regime-favored strategy type gets first claim on limited capital.
    candidates = []

    if (wolf_allocated < wolf_target_budget and
            active_wolves < max_wolves and idle_capital >= min_spawn):
        wolf_budget = min(wolf_target_budget - wolf_allocated, idle_capital * 0.6)
        wolf_budget = max(min_spawn, round(wolf_budget, 2))
        if wolf_budget <= idle_capital:
            preset = "aggressive" if regime == "TRENDING" else "conservative"
            candidates.append({
                "type": "wolf",
                "budget": wolf_budget,
                "dslPreset": preset,
                "allocationPct": wolf_target_pct,
                "reason": f"Wolf under-allocated (${wolf_allocated:.0f}/${wolf_target_budget:.0f}), "
                          f"regime={regime}, signal_pressure={global_pressure}",
            })

    if (tiger_allocated < tiger_target_budget and
            active_tigers < max_tigers and idle_capital >= min_spawn):
        tiger_budget = min(tiger_target_budget - tiger_allocated, idle_capital * 0.7)
        tiger_budget = max(min_spawn, round(tiger_budget, 2))
        if tiger_budget <= idle_capital:
            candidates.append({
                "type": "tiger",
                "budget": tiger_budget,
                "goalPct": 5 if regime in ("TRENDING", "VOLATILE") else 3,
                "allocationPct": tiger_target_pct,
                "reason": f"Tiger under-allocated (${tiger_allocated:.0f}/${tiger_target_budget:.0f}), "
                          f"regime={regime}",
            })

    # Higher allocation % spawns first (e.g. TIGER before WOLF in RANGING)
    candidates.sort(key=lambda c: c["allocationPct"], reverse=True)

    for c in candidates:
        if idle_capital < min_spawn:
            break
        c_budget = min(c["budget"], idle_capital)
        c_budget = max(min_spawn, round(c_budget, 2))
        if c_budget > idle_capital:
            continue
        c.pop("allocationPct", None)
        c["budget"] = c_budget
        spawns.append(c)
        idle_capital -= c_budget

    return spawns


_MAX_EXPANDED_SLOTS = 5
_EXPANSION_SIGNAL_THRESHOLD = 40
_EXPANSION_AGGRESSIVE_THRESHOLD = 80
_EXPANSION_MAX_UTILIZATION = 70
_EXPANSION_MIN_LIQ_BUFFER = 50
_REBALANCE_IDLE_HOURS = 1
_REBALANCE_MIN_UTIL = 5
_REBALANCE_MIN_REMAINING = 400


def _decide_expansions(signal_data, perf_data, config, current_instances):
    """Decide whether to expand slots on signal-rich, slot-capped instances.

    When an instance is at max slots and reporting high signal pressure
    (missed opportunities), increase its slot count so the next scan cycle
    can open additional positions — provided risk gates pass.

    Uses monitor position count (from MCP clearinghouse) as fallback when
    signal module can't read subagent scan files.

    Returns list of {"instanceId", "newSlots", "newMarginPerSlot", "reason"}.
    """
    expansions = []
    for iid, idata in current_instances.items():
        if idata.get("status", "active") not in ("active", "pending_spawn"):
            continue

        itype = idata.get("type", "wolf")
        budget = idata.get("budget", 0)
        current_slots = idata.get("slots", idata.get("maxSlots", 2))

        if current_slots >= _MAX_EXPANDED_SLOTS:
            continue

        inst_signals = signal_data.get("instances", {}).get(iid, {})
        inst_perf = perf_data.get("instances", {}).get(iid, {})

        pressure = inst_signals.get("signalPressure", 0)
        slots_used = inst_signals.get("slotsUsed", 0)
        slots_max = inst_signals.get("slotsMax", current_slots)

        monitor_positions = inst_perf.get("positionCount", 0)
        if monitor_positions > slots_used:
            slots_used = monitor_positions

        if pressure < _EXPANSION_SIGNAL_THRESHOLD:
            continue
        if slots_used < slots_max:
            continue

        account_value = inst_perf.get("accountValue", 0)
        utilization = inst_perf.get("utilization", 0)

        if account_value < budget * 0.95:
            continue
        if utilization > _EXPANSION_MAX_UTILIZATION:
            continue

        increment = 2 if pressure >= _EXPANSION_AGGRESSIVE_THRESHOLD else 1
        new_slots = min(current_slots + increment, _MAX_EXPANDED_SLOTS)
        new_margin = round(account_value * 0.30 / new_slots, 2)

        expansions.append({
            "instanceId": iid,
            "type": itype,
            "currentSlots": current_slots,
            "newSlots": new_slots,
            "newMarginPerSlot": new_margin,
            "reason": (f"Signal pressure {pressure}/100, slots {slots_used}/{slots_max} full, "
                       f"account ${account_value:.0f} healthy, util {utilization:.0f}%"),
        })

    return expansions


def _decide_rebalances(signal_data, perf_data, config, current_instances):
    """Decide whether to kill idle instances and respawn capital elsewhere.

    When an instance has near-zero utilization for an extended period while
    another strategy type has high signal pressure, kill the idle instance
    and let the freed capital flow into a spawn of the active type.

    Returns list of {"fromInstance", "toType", "amount", "reason"}.
    """
    rebalances = []
    instance_list = list(current_instances.items())

    wolf_pressure = max(
        (signal_data.get("instances", {}).get(iid, {}).get("signalPressure", 0)
         for iid, d in instance_list if d.get("type") == "wolf"), default=0)
    tiger_pressure = max(
        (signal_data.get("instances", {}).get(iid, {}).get("signalPressure", 0)
         for iid, d in instance_list if d.get("type") == "tiger"), default=0)

    for iid, idata in instance_list:
        if idata.get("status", "active") not in ("active",):
            continue

        itype = idata.get("type", "wolf")
        budget = idata.get("budget", 0)
        spawned_at = idata.get("spawnedAt", "")

        inst_signals = signal_data.get("instances", {}).get(iid, {})
        inst_perf = perf_data.get("instances", {}).get(iid, {})
        utilization = inst_perf.get("utilization", 0)
        account_value = inst_perf.get("accountValue", budget)

        is_halted = inst_signals.get("halted", False)

        if is_halted:
            pass
        elif utilization >= _REBALANCE_MIN_UTIL:
            continue
        else:
            hours_alive = minutes_since(spawned_at) / 60
            if hours_alive < _REBALANCE_IDLE_HOURS:
                continue

        other_pressure = tiger_pressure if itype == "wolf" else wolf_pressure
        if not is_halted and other_pressure < 35:
            continue

        min_spawn = config.get("minSpawnBudget", 500)
        if is_halted:
            rebalance_amount = round(account_value, 2)
        else:
            rebalance_amount = round(account_value * 0.50, 2)
            if (account_value - rebalance_amount) < _REBALANCE_MIN_REMAINING:
                rebalance_amount = max(0, account_value - _REBALANCE_MIN_REMAINING)
        if rebalance_amount < min_spawn:
            continue

        target_type = "tiger" if itype == "wolf" else "wolf"
        hours_alive = minutes_since(spawned_at) / 60
        if is_halted:
            reason = (f"{iid} HALTED ({inst_signals.get('haltReason', 'unknown')}), "
                      f"rebalancing ${rebalance_amount:.0f} to {target_type}")
        else:
            reason = (f"{iid} idle ({utilization:.0f}% util for {hours_alive:.1f}h), "
                      f"{target_type} pressure={other_pressure}, "
                      f"rebalancing ${rebalance_amount:.0f}")
        rebalances.append({
            "fromInstance": iid,
            "fromType": itype,
            "toType": target_type,
            "amount": rebalance_amount,
            "reason": reason,
        })

    return rebalances


def _check_portfolio_circuit_breaker(config, perf_data, instances):
    """Return True if portfolio-level drawdown exceeds threshold.

    Measures drawdown against allocated capital (sum of instance budgets),
    not totalBudget. Unallocated/reserved cash is not a loss.
    """
    kvk = config.get("killVsKeep", {})
    max_dd = kvk.get("portfolioMaxDrawdownPct", 15)
    total_allocated = sum(idata.get("budget", 0) for idata in instances.values())
    if total_allocated <= 0:
        return False
    total_value = sum(
        perf_data.get("instances", {}).get(iid, {}).get("accountValue", idata.get("budget", 0))
        for iid, idata in instances.items()
    )
    portfolio_dd = (total_allocated - total_value) / total_allocated * 100
    return portfolio_dd > max_dd



def _retry_kill_pending(instances, config):
    """Retry killing instances stuck in kill_pending status.

    Returns (retry_results, retried_ids) where retried_ids are instance IDs
    that should be excluded from normal kill-vs-keep evaluation.
    """
    kvk = config.get("killVsKeep", {})
    retry_minutes = kvk.get("killPendingRetryMinutes", 5)
    max_retries = kvk.get("killPendingMaxRetries", 3)

    results = []
    retried_ids = set()

    for iid, idata in list(instances.items()):
        if idata.get("status") != "kill_pending":
            continue

        retried_ids.add(iid)
        killed_at = idata.get("killedAt", "")
        mins_elapsed = minutes_since(killed_at)
        retries = idata.get("killRetries", 0)

        if mins_elapsed < retry_minutes:
            results.append({
                "instanceId": iid, "action": "RETRY_WAIT",
                "message": f"kill_pending {mins_elapsed:.0f}min, retry at {retry_minutes}min",
            })
            continue

        if retries >= max_retries:
            results.append({
                "instanceId": iid, "action": "STUCK",
                "retries": retries,
                "message": (f"STUCK: {iid} failed {retries} kill attempts — "
                            f"manual intervention required"),
            })
            continue

        _spawner = _get_spawner()
        result = _spawner.kill_instance(iid, reason=f"kill_pending retry #{retries + 1}")

        spawn_file = os.path.join(SPAWNED_DIR, f"{iid}.json")
        updated = load_json_safe(spawn_file)
        if updated:
            updated["killRetries"] = retries + 1
            if not updated.get("firstKillAttemptAt"):
                updated["firstKillAttemptAt"] = killed_at
            save_spawned_instance(iid, updated)

        results.append({
            "instanceId": iid, "action": "RETRY_KILL",
            "attempt": retries + 1, "result": result,
        })

    return results, retried_ids


def _verify_pending_actions(state, instances):
    """Verify that actions requested in the previous brain run were executed.

    Spawn verification: instances start as pending_spawn. If they haven't
    produced any evidence of activity (scan files, performance data) within
    _SPAWN_VERIFY_MINUTES, warn — the cron agent likely failed to execute
    sessions_spawn even though the wallet was funded.

    Returns list of warnings, list of kill dicts to re-issue, and list of
    instance IDs to promote from pending_spawn to active.
    """
    prev = state.get("pendingActions", {})
    warnings = []
    re_kills = []
    promote_ids = []

    # Check for pending_spawn instances that have been waiting too long
    for iid, idata in instances.items():
        if idata.get("status") != "pending_spawn":
            continue
        age = minutes_since(idata.get("spawnedAt"))
        itype = idata.get("type", "wolf")
        instance_ws = os.path.join(WORKSPACE, "instances", iid)

        has_evidence = False
        if os.path.isdir(instance_ws):
            for f in ("emerging-movers-history.json", "scan-history.json",
                      "prescreened.json", "tiger-state.json"):
                if os.path.exists(os.path.join(instance_ws, f)):
                    has_evidence = True
                    break

        perf = load_json_safe(os.path.join(COBRA_STATE_DIR, "cobra-performance.json")) or {}
        if iid in perf.get("instances", {}):
            perf_entry = perf["instances"][iid]
            if perf_entry.get("utilization", 0) > 0:
                has_evidence = True

        if has_evidence:
            promote_ids.append(iid)
        elif age > _SPAWN_VERIFY_MINUTES:
            warnings.append(
                f"WARNING: spawn {iid} has been pending_spawn for {age:.0f}min "
                f"with no activity — subagent may not have started. "
                f"Wallet {idata.get('wallet', '?')} is funded but idle.")

    for kill_id in prev.get("kills", []):
        idata = instances.get(kill_id)
        if idata and idata.get("status") in ("active", "pending_spawn"):
            warnings.append(
                f"WARNING: kill {kill_id} from previous run still active — re-issuing")
            re_kills.append({
                "instanceId": kill_id,
                "reason": "missed kill from previous brain run",
            })

    return warnings, re_kills, promote_ids


def run():
    """Main brain loop."""
    config = load_config()
    state = load_state()

    # Step 1: Regime classification with hysteresis
    regime_data = _get_regime()
    raw_regime = regime_data.get("regime", "UNKNOWN")
    raw_confidence = regime_data.get("confidence", 0)
    regime, confidence = _apply_regime_hysteresis(raw_regime, raw_confidence, state)
    if regime != raw_regime:
        regime_data["regime"] = regime
        regime_data["_rawRegime"] = raw_regime
        spec = importlib.util.spec_from_file_location(
            "cobra_regime_alloc",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "cobra-regime.py"))
        alloc_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(alloc_mod)
        regime_data["allocation"] = alloc_mod.get_allocation(regime)

    # Step 2: Signal pressure
    signal_data = _get_signal_pressure()
    global_pressure = signal_data.get("globalSignalPressure", 0)

    # Step 3: Load current state (single load, reuse throughout)
    perf_data = load_json_safe(
        os.path.join(COBRA_STATE_DIR, "cobra-performance.json")) or {"instances": {}}
    instances = load_spawned_instances()

    # Step 4a: Retry kill_pending instances (deterministic retry before any decisions)
    kill_pending_results, pending_ids = _retry_kill_pending(instances, config)

    # Step 4b: Verify actions from previous brain run + promote confirmed spawns
    verify_warnings, re_kills, promote_ids = _verify_pending_actions(state, instances)
    for pid in promote_ids:
        idata = instances.get(pid)
        if idata:
            idata["status"] = "active"
            save_spawned_instance(pid, idata)

    # Step 4c: Portfolio-level circuit breaker
    portfolio_breaker_tripped = _check_portfolio_circuit_breaker(config, perf_data, instances)

    actions = []
    kills = list(re_kills)
    spawns_planned = []
    expansions_planned = []

    for r in kill_pending_results:
        actions.append(f"KILL_PENDING {r['instanceId']}: {r['action']} — {r.get('message', '')}")
    for w in verify_warnings:
        actions.append(w)

    if portfolio_breaker_tripped:
        kill_reason = (
            f"Portfolio circuit breaker: drawdown exceeds "
            f"{config.get('killVsKeep', {}).get('portfolioMaxDrawdownPct', 15)}%"
        )
        for iid, idata in instances.items():
            kills.append({"instanceId": iid, "reason": kill_reason})
            actions.append(f"KILL {iid}: portfolio circuit breaker")
    else:
        # Step 5: Kill-vs-Keep for each active instance (skip kill_pending)
        for iid, idata in instances.items():
            if iid in pending_ids:
                continue
            kvk_result = _evaluate_kill_vs_keep(
                iid, idata, signal_data, perf_data, config)
            actions.append(
                f"{kvk_result['decision']} {iid}: "
                f"score={kvk_result['score']}, {'; '.join(kvk_result['reasons'][:2])}"
            )
            if kvk_result["decision"] == "KILL":
                kills.append({
                    "instanceId": iid,
                    "reason": "; ".join(kvk_result["reasons"]),
                    "score": kvk_result["score"],
                })

        # Step 5b: Slot expansion on signal-rich, slot-capped instances
        expansions_planned = _decide_expansions(
            signal_data, perf_data, config, instances)
        for exp in expansions_planned:
            actions.append(
                f"EXPAND {exp['instanceId']}: "
                f"{exp['currentSlots']}->{exp['newSlots']} slots, {exp['reason']}")

        # Step 5c: Rebalance idle capital to active strategy types
        rebalances_planned = _decide_rebalances(
            signal_data, perf_data, config, instances)
        for reb in rebalances_planned:
            kills.append({
                "instanceId": reb["fromInstance"],
                "reason": f"rebalance: {reb['reason']}",
            })
            actions.append(f"REBALANCE {reb['fromInstance']} -> {reb['toType']}: {reb['reason']}")

        # Step 6: Spawn decisions — remove killed instances so freed capital is visible
        surviving_instances = {
            iid: idata for iid, idata in instances.items()
            if iid not in {k["instanceId"] for k in kills}
        }

        spawns_planned = _decide_spawns(
            regime_data, signal_data, state, config, surviving_instances)

        # Inject rebalance spawns (target type with freed capital)
        for reb in rebalances_planned:
            spawns_planned.append({
                "type": reb["toType"],
                "budget": reb["amount"],
                "dslPreset": "aggressive" if regime == "TRENDING" else "conservative",
                "goalPct": 5 if regime in ("TRENDING", "VOLATILE") else 3,
                "reason": f"rebalance from {reb['fromInstance']}",
            })

    # Collect kill results from kill_pending retries
    kill_results = [r["result"] for r in kill_pending_results
                    if r.get("action") == "RETRY_KILL" and r.get("result")]

    # Step 7: Execute kills (close positions via MCP + prepare subagent kill orders)
    if kills:
        _spawner = _get_spawner()
        for k in kills:
            result = _spawner.kill_instance(k["instanceId"], reason=k["reason"])
            kill_results.append(result)

    # Step 7b: Execute slot expansions
    expansion_results = []
    if expansions_planned:
        _spawner = _get_spawner()
        for exp in expansions_planned:
            result = _spawner.expand_instance_slots(
                exp["instanceId"], exp["newSlots"], exp["newMarginPerSlot"])
            expansion_results.append(result)

    # Step 8: Execute spawns (create wallets + prepare subagent spawn instructions)
    spawn_results = []
    if spawns_planned:
        _spawner = _get_spawner()
        for sp in spawns_planned:
            if sp["type"] == "wolf":
                result = _spawner.spawn_wolf(
                    budget=sp["budget"],
                    dsl_preset=sp.get("dslPreset", "aggressive"),
                    regime=regime,
                )
            else:
                result = _spawner.spawn_tiger(
                    budget=sp["budget"],
                    goal_pct=sp.get("goalPct", 5),
                    regime=regime,
                )
            spawn_results.append(result)

    # Step 9: Detect regime shift
    prev_regime = state.get("regime", "UNKNOWN")
    regime_shifted = prev_regime != regime and prev_regime != "UNKNOWN"
    if regime_shifted:
        state.pop("_pendingRegime", None)

    # Step 10: Build subagent regime update messages if regime shifted
    # Re-load instances here since kills/spawns may have changed disk state
    current_instances = load_spawned_instances()
    subagent_messages = []
    if regime_shifted and regime != "UNKNOWN":
        _spawner = _get_spawner()
        allocation = regime_data.get("allocation", {})
        for iid, idata in current_instances.items():
            msg = _spawner.build_regime_update_message(
                iid, regime, allocation,
                session_key=idata.get("childSessionKey"))
            subagent_messages.append(msg)

    # Step 11: Update state
    state["regime"] = regime
    state["regimeConfidence"] = confidence
    state["lastBrainRun"] = utc_now()
    state["globalSignalPressure"] = global_pressure
    if regime_shifted:
        state["regimeChangedAt"] = utc_now()
    if portfolio_breaker_tripped:
        state["lastCircuitBreakerAt"] = utc_now()
    state["lastDecision"] = {
        "kills": len(kills),
        "spawns": len(spawns_planned),
        "expansions": len(expansion_results),
        "actions": actions[:10],
        "portfolioBreakerTripped": portfolio_breaker_tripped,
    }
    state["pendingActions"] = {
        "spawns": [s.get("instanceId", "") for s in spawn_results if s.get("success")],
        "kills": [k["instanceId"] for k in kills],
    }
    state["spawnedInstances"] = {
        k: {"type": v.get("type"), "budget": v.get("budget")}
        for k, v in current_instances.items()
    }
    state["totalAllocated"] = sum(
        v.get("budget", 0) for v in current_instances.values())
    save_state(state)

    # Build summary
    summary_parts = [
        f"Regime: {regime} ({confidence:.0%} confidence)",
        f"Signal pressure: {global_pressure}/100 ({signal_data.get('marketOpportunityDensity', 'NONE')})",
        f"Instances: {len(instances)} active",
    ]
    if kills:
        summary_parts.append(f"Killed: {len(kills)} instances")
    if spawn_results:
        successful = [s for s in spawn_results if s.get("success")]
        summary_parts.append(f"Spawned: {len(successful)} new instances")
    if expansion_results:
        expanded = [e for e in expansion_results if e.get("success")]
        summary_parts.append(f"Expanded: {len(expanded)} instances")
    if regime_shifted:
        summary_parts.append(f"REGIME SHIFT: {prev_regime} -> {regime}")

    # Collect expansion messages for subagents
    for er in expansion_results:
        if er.get("success") and er.get("expansionMessage"):
            subagent_messages.append(er["expansionMessage"])

    # Collect sessions_spawn instructions (from spawns)
    subagent_spawns = []
    for sr in spawn_results:
        if sr.get("success") and sr.get("spawnInstruction"):
            subagent_spawns.append(sr["spawnInstruction"])

    # Collect subagent kill orders (from kills)
    subagent_kills = []
    for kr in kill_results:
        if kr.get("success"):
            subagent_kills.append({
                "label": kr.get("subagentToKill", kr["instanceId"]),
                "killMessage": kr.get("killMessage"),
            })

    # Collect cron payloads to create (wake crons for new subagents)
    crons_to_create = []
    for sr in spawn_results:
        if sr.get("success") and sr.get("cronPayloads"):
            crons_to_create.extend(sr["cronPayloads"])

    # Collect crons to delete (from kills)
    crons_to_delete = []
    for kr in kill_results:
        if kr.get("success") and kr.get("cronsToDelete"):
            crons_to_delete.extend(kr["cronsToDelete"])

    # Collect stuck instance alerts (need operator attention)
    stuck_alerts = [r["message"] for r in kill_pending_results
                    if r.get("action") == "STUCK"]

    result = {
        "regime": regime,
        "regimeConfidence": confidence,
        "regimeShifted": regime_shifted,
        "previousRegime": prev_regime if regime_shifted else None,
        "globalSignalPressure": global_pressure,
        "portfolioBreakerTripped": portfolio_breaker_tripped,
        "decisions": actions,
        "kills": kill_results,
        "expansions": expansion_results,
        "spawns": spawn_results,
        "subagentSpawns": subagent_spawns,
        "subagentKills": subagent_kills,
        "subagentMessages": subagent_messages,
        "cronsToCreate": crons_to_create,
        "cronsToDelete": crons_to_delete,
        "stuckInstances": stuck_alerts,
        "verifyWarnings": verify_warnings,
        "summary": " | ".join(summary_parts),
        "actionable": 1 if (kills or spawn_results or expansion_results
                            or regime_shifted or portfolio_breaker_tripped
                            or stuck_alerts or verify_warnings) else 0,
    }

    from viper_gate import output_and_track
    output_and_track("COBRA/Brain", result)


if __name__ == "__main__":
    run()
