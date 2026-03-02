#!/usr/bin/env python3
"""
cobra-signals.py — Signal Pressure Aggregator for COBRA.

Reads screener data from all spawned WOLF and TIGER instances. Pure Python,
no MCP calls needed -- reads local JSON files written by WOLF/TIGER scanners.

Computes a signal pressure score (0-100) per instance and globally,
indicating how many high-quality opportunities are being missed due to
capital constraints.

Output JSON:
    {"instances": {...}, "globalSignalPressure": 58, "marketOpportunityDensity": "HIGH"}

Cron: Signal Scan — runs every 5 min via Budget model (ultra cheap).
"""

import json, sys, os, glob, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cobra_config import (
    load_spawned_instances, get_instance_workspace, get_shared_workspace,
    get_instance_state_dir, load_json_safe, output, WORKSPACE,
    COBRA_STATE_DIR, atomic_write, utc_now,
)

SIGNAL_PRESSURE_FILE = os.path.join(COBRA_STATE_DIR, "cobra-signals.json")


def _find_scan_file(instance_workspace, filename):
    """Resolve a scan file across standard locations.

    Search order: workspace root, history/ subdir (wolf convention),
    then shared workspace fallback.
    """
    instance_path = os.path.join(instance_workspace, filename)
    if os.path.exists(instance_path):
        return instance_path
    history_path = os.path.join(instance_workspace, "history", filename)
    if os.path.exists(history_path):
        return history_path
    shared_path = os.path.join(get_shared_workspace(), filename)
    if os.path.exists(shared_path):
        return shared_path
    return instance_path


def hours_ago(iso_timestamp, hours):
    """Check if a timestamp is within the last N hours."""
    try:
        if isinstance(iso_timestamp, (int, float)):
            ts = datetime.fromtimestamp(iso_timestamp / 1000, tz=timezone.utc)
        else:
            ts = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc).replace(microsecond=0)
        delta = (cutoff - ts).total_seconds() / 3600
        return delta <= hours
    except Exception:
        return False


def analyze_wolf_instance(instance_id, instance_data):
    """Analyze signal pressure for a WOLF instance.

    Cross-references signals with active DSL positions to avoid counting
    signals that were successfully traded as "missed."
    """
    workspace = get_instance_workspace(instance_id, "wolf")
    state_dir = get_instance_state_dir(instance_id, "wolf")

    result = {
        "type": "wolf",
        "signalPressure": 0,
        "missedFirstJumps1h": 0,
        "missedFirstJumps4h": 0,
        "missedOpportunities1h": 0,
        "missedOpportunities4h": 0,
        "slotsUsed": 0,
        "slotsMax": instance_data.get("slots", 3),
        "positionQuality": {"phase1": 0, "tier1": 0, "tier2plus": 0},
        "avgPositionROE": 0,
    }

    # --- Read DSL state files first (need traded assets for cross-reference) ---
    dsl_pattern = os.path.join(state_dir, "dsl-*.json")
    dsl_files = glob.glob(dsl_pattern)
    roe_values = []
    traded_assets = set()

    for dsl_path in dsl_files:
        state = load_json_safe(dsl_path)
        if not state:
            continue
        asset = state.get("asset", "")
        if state.get("active"):
            result["slotsUsed"] += 1
            traded_assets.add(asset.upper())

            tier_idx = state.get("currentTierIndex")
            phase = state.get("phase", 1)
            if phase == 1 or tier_idx is None:
                result["positionQuality"]["phase1"] += 1
            elif tier_idx == 0:
                result["positionQuality"]["tier1"] += 1
            else:
                result["positionQuality"]["tier2plus"] += 1

            entry = float(state.get("entryPrice", 0))
            current = float(state.get("currentPrice", state.get("lastPrice", 0)))
            if current == 0:
                current = float(state.get("highWaterPrice", 0))
            direction = state.get("direction", "LONG")
            if entry > 0 and current > 0:
                if direction == "LONG":
                    roe = (current - entry) / entry * 100
                else:
                    roe = (entry - current) / entry * 100
                roe_values.append(roe)
        elif asset:
            traded_assets.add(asset.upper())

    if roe_values:
        result["avgPositionROE"] = round(sum(roe_values) / len(roe_values), 2)

    # --- Read emerging-movers-history.json ---
    # Exclude signals for assets already being traded (active or recently closed DSL)
    em_history_path = _find_scan_file(workspace, "emerging-movers-history.json")
    em_history = load_json_safe(em_history_path)
    if isinstance(em_history, list):
        for scan in em_history:
            scan_time = scan.get("timestamp") or scan.get("time")
            if not scan_time:
                continue
            markets = scan.get("markets", scan.get("ranked", []))
            for m in markets:
                is_first_jump = m.get("isFirstJump") or m.get("firstJump")
                if not is_first_jump:
                    continue
                asset = (m.get("asset") or m.get("coin") or m.get("symbol") or "").upper()
                if asset in traded_assets:
                    continue
                if hours_ago(scan_time, 1):
                    result["missedFirstJumps1h"] += 1
                if hours_ago(scan_time, 4):
                    result["missedFirstJumps4h"] += 1

    # --- Read scan-history.json (opportunity scanner) ---
    scan_history_path = _find_scan_file(workspace, "scan-history.json")
    scan_history = load_json_safe(scan_history_path)
    if isinstance(scan_history, list):
        seen_assets_1h = set()
        for scan in scan_history:
            scan_time = scan.get("timestamp") or scan.get("time")
            if not scan_time:
                continue
            opportunities = scan.get("opportunities", scan.get("results", []))
            slots_available = scan.get("anySlotsAvailable", True)
            for opp in opportunities:
                score = opp.get("finalScore", opp.get("score", 0))
                asset = (opp.get("asset") or opp.get("coin") or "").upper()
                if score >= 175 and not slots_available and asset not in traded_assets:
                    if hours_ago(scan_time, 1) and asset not in seen_assets_1h:
                        result["missedOpportunities1h"] += 1
                        seen_assets_1h.add(asset)
                    if hours_ago(scan_time, 4):
                        result["missedOpportunities4h"] += 1

    # --- Compute signal pressure score ---
    pressure = 0
    pressure += result["missedFirstJumps1h"] * 15
    pressure += result["missedOpportunities1h"] * 8
    if result["slotsUsed"] >= result["slotsMax"] and pressure > 0:
        pressure += 10
    result["signalPressure"] = min(100, pressure)

    return result


def analyze_tiger_instance(instance_id, instance_data):
    """Analyze signal pressure for a TIGER instance.

    TIGER scanners output to stdout (not to individual JSON files).
    Signal pressure is derived from:
    - prescreened.json: candidate density (market richness)
    - tiger-state.json: active positions, slots, aggression, halt state
    - trade-log.json: per-pattern win rates, recent outcomes
    - dsl-{asset}.json: position quality (same format as WOLF DSL)
    """
    workspace = get_instance_workspace(instance_id, "tiger")
    state_dir = get_instance_state_dir(instance_id, "tiger")

    result = {
        "type": "tiger",
        "signalPressure": 0,
        "prescreenerDensity": 0,
        "avgPrescreenerScore": 0,
        "highConfluenceCount": 0,
        "slotsUsed": 0,
        "slotsMax": instance_data.get("maxSlots", 3),
        "aggression": "NORMAL",
        "halted": False,
        "recentWinRate": 0,
        "positionQuality": {"phase1": 0, "tier1": 0, "tier2plus": 0},
    }

    # --- Read prescreened.json (written by prescreener.py to workspace root) ---
    prescreened_path = _find_scan_file(workspace, "prescreened.json")
    prescreened = load_json_safe(prescreened_path)
    if isinstance(prescreened, dict):
        candidates = prescreened.get("candidates", prescreened.get("results", []))
        if isinstance(candidates, list):
            result["prescreenerDensity"] = len(candidates)
            scores = [float(c.get("score", c.get("totalScore", 0)))
                      for c in candidates if c.get("score") or c.get("totalScore")]
            if scores:
                result["avgPrescreenerScore"] = round(sum(scores) / len(scores), 1)
                result["highConfluenceCount"] = sum(1 for s in scores if s >= 65)
    elif isinstance(prescreened, list):
        result["prescreenerDensity"] = len(prescreened)
        scores = [float(c.get("score", c.get("totalScore", 0)))
                  for c in prescreened if c.get("score") or c.get("totalScore")]
        if scores:
            result["avgPrescreenerScore"] = round(sum(scores) / len(scores), 1)
            result["highConfluenceCount"] = sum(1 for s in scores if s >= 65)

    # --- Read tiger-state.json (written by tiger_config.save_state) ---
    tiger_state_path = _find_scan_file(state_dir, "tiger-state.json")
    tiger_state = load_json_safe(tiger_state_path)
    if tiger_state:
        active_positions = tiger_state.get("activePositions", {})
        if isinstance(active_positions, dict):
            result["slotsUsed"] = len(active_positions)
        elif isinstance(active_positions, (int, float)):
            result["slotsUsed"] = int(active_positions)
        result["aggression"] = tiger_state.get("aggression", "NORMAL")
        safety = tiger_state.get("safety", {})
        result["halted"] = safety.get("halted", False)
        total_trades = tiger_state.get("totalTrades", 0)
        total_wins = tiger_state.get("totalWins", 0)
        if total_trades > 0:
            result["recentWinRate"] = round(total_wins / total_trades, 2)

    # --- Read trade-log.json for per-pattern win rates ---
    trade_log_path = _find_scan_file(state_dir, "trade-log.json")
    trade_log = load_json_safe(trade_log_path)
    if isinstance(trade_log, list) and trade_log:
        recent = trade_log[-20:]
        wins = sum(1 for t in recent
                   if float(t.get("pnl", t.get("realizedPnl", 0))) > 0)
        result["recentWinRate"] = round(wins / len(recent), 2)
    elif isinstance(trade_log, dict):
        patterns = trade_log.get("patterns", trade_log.get("trades", {}))
        if isinstance(patterns, dict):
            total_wins, total_trades = 0, 0
            for pattern_data in patterns.values():
                if isinstance(pattern_data, dict):
                    total_wins += pattern_data.get("wins", 0)
                    total_trades += pattern_data.get("total", 0)
            if total_trades > 0:
                result["recentWinRate"] = round(total_wins / total_trades, 2)

    # --- Read DSL state files for position quality (same format as WOLF) ---
    dsl_pattern = os.path.join(state_dir, "dsl-*.json")
    dsl_files = glob.glob(dsl_pattern)
    for dsl_path in dsl_files:
        state = load_json_safe(dsl_path)
        if not state or not state.get("active"):
            continue
        tier_idx = state.get("currentTierIndex")
        phase = state.get("phase", 1)
        if phase == 1 or tier_idx is None:
            result["positionQuality"]["phase1"] += 1
        elif tier_idx == 0:
            result["positionQuality"]["tier1"] += 1
        else:
            result["positionQuality"]["tier2plus"] += 1

    # --- Compute signal pressure score ---
    # Density bonus uses a higher threshold (35) and gentler multiplier (3)
    # so normal candidate counts (20-30) don't inflate pressure.
    pressure = 0
    pressure += result["highConfluenceCount"] * 10
    if result["prescreenerDensity"] >= 35:
        pressure += (result["prescreenerDensity"] - 25) * 3
    if result["slotsUsed"] >= result["slotsMax"] and result["prescreenerDensity"] > 0:
        pressure += 15
    if result["aggression"] in ("ELEVATED", "ABORT"):
        pressure += 10
    if result["halted"]:
        pressure = max(pressure - 20, 0)
    result["signalPressure"] = min(100, pressure)

    return result


def _bootstrap_signal_scan():
    """Scan shared workspace for signal data when no instances exist yet.

    Reads any emerging-movers-history, scan-history, and prescreened files
    that may exist from manual scans or previous strategies, giving COBRA
    market awareness before the first instance spawns.
    """
    shared_ws = get_shared_workspace()
    result = {
        "type": "bootstrap",
        "signalPressure": 0,
        "missedFirstJumps1h": 0,
        "missedOpportunities1h": 0,
        "highConfluenceCount": 0,
        "prescreenerDensity": 0,
    }

    em_data = load_json_safe(os.path.join(shared_ws, "emerging-movers-history.json"))
    if isinstance(em_data, list):
        for scan in em_data:
            scan_time = scan.get("timestamp") or scan.get("time")
            if not scan_time:
                continue
            markets = scan.get("markets", scan.get("ranked", []))
            for m in markets:
                if (m.get("isFirstJump") or m.get("firstJump")) and hours_ago(scan_time, 1):
                    result["missedFirstJumps1h"] += 1

    scan_data = load_json_safe(os.path.join(shared_ws, "scan-history.json"))
    if isinstance(scan_data, list):
        seen = set()
        for scan in scan_data:
            scan_time = scan.get("timestamp") or scan.get("time")
            if not scan_time or not hours_ago(scan_time, 1):
                continue
            for opp in scan.get("opportunities", scan.get("results", [])):
                score = opp.get("finalScore", opp.get("score", 0))
                asset = (opp.get("asset") or opp.get("coin") or "").upper()
                if score >= 175 and asset not in seen:
                    result["missedOpportunities1h"] += 1
                    seen.add(asset)

    ps_data = load_json_safe(os.path.join(shared_ws, "prescreened.json"))
    candidates = []
    if isinstance(ps_data, dict):
        candidates = ps_data.get("candidates", ps_data.get("results", []))
    elif isinstance(ps_data, list):
        candidates = ps_data
    if isinstance(candidates, list):
        result["prescreenerDensity"] = len(candidates)
        scores = [float(c.get("score", c.get("totalScore", 0)))
                  for c in candidates if c.get("score") or c.get("totalScore")]
        result["highConfluenceCount"] = sum(1 for s in scores if s >= 65)

    pressure = 0
    pressure += result["missedFirstJumps1h"] * 15
    pressure += result["missedOpportunities1h"] * 8
    pressure += result["highConfluenceCount"] * 10
    if result["prescreenerDensity"] >= 35:
        pressure += (result["prescreenerDensity"] - 25) * 3
    result["signalPressure"] = min(100, pressure)
    return result


def _pressure_to_density(pressure):
    if pressure >= 60:
        return "HIGH"
    if pressure >= 30:
        return "MODERATE"
    if pressure > 0:
        return "LOW"
    return "NONE"


def run():
    """Main: scan all spawned instances, compute signal pressure, save results."""
    try:
        _run_inner()
    except Exception as e:
        error_result = {
            "instances": {},
            "globalSignalPressure": 0,
            "marketOpportunityDensity": "ERROR",
            "error": str(e),
            "updatedAt": utc_now(),
            "actionable": 0,
        }
        try:
            atomic_write(SIGNAL_PRESSURE_FILE, error_result)
        except Exception:
            pass
        from cobra_config import output as _output
        _output(error_result)


def _run_inner():
    instances = load_spawned_instances()

    if not instances:
        bootstrap = _bootstrap_signal_scan()
        global_pressure = bootstrap["signalPressure"]
        result = {
            "instances": {"_bootstrap": bootstrap},
            "globalSignalPressure": global_pressure,
            "marketOpportunityDensity": _pressure_to_density(global_pressure),
            "bootstrapMode": True,
            "updatedAt": utc_now(),
            "actionable": 1 if global_pressure >= 40 else 0,
        }
        atomic_write(SIGNAL_PRESSURE_FILE, result)
        from viper_gate import output_and_track
        output_and_track("COBRA/Signals", result)
        return

    results = {}
    all_pressures = []

    for iid, idata in instances.items():
        itype = idata.get("type", "")
        if itype == "wolf":
            analysis = analyze_wolf_instance(iid, idata)
        elif itype == "tiger":
            analysis = analyze_tiger_instance(iid, idata)
        else:
            continue
        results[iid] = analysis
        all_pressures.append(analysis["signalPressure"])

    global_pressure = max(all_pressures) if all_pressures else 0

    result = {
        "instances": results,
        "globalSignalPressure": global_pressure,
        "marketOpportunityDensity": _pressure_to_density(global_pressure),
        "updatedAt": utc_now(),
        "actionable": 1 if global_pressure >= 40 else 0,
    }

    atomic_write(SIGNAL_PRESSURE_FILE, result)

    from viper_gate import output_and_track
    output_and_track("COBRA/Signals", result)


if __name__ == "__main__":
    run()
