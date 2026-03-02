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
    """Resolve a scan file: per-instance workspace first, shared workspace fallback."""
    instance_path = os.path.join(instance_workspace, filename)
    if os.path.exists(instance_path):
        return instance_path
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
    """Analyze signal pressure for a WOLF instance."""
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

    # --- Read emerging-movers-history.json ---
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
                is_explosion = m.get("isContribExplosion")
                if is_first_jump:
                    if hours_ago(scan_time, 1):
                        result["missedFirstJumps1h"] += 1
                    if hours_ago(scan_time, 4):
                        result["missedFirstJumps4h"] += 1

    # --- Read scan-history.json (opportunity scanner) ---
    scan_history_path = _find_scan_file(workspace, "scan-history.json")
    scan_history = load_json_safe(scan_history_path)
    if isinstance(scan_history, list):
        for scan in scan_history:
            scan_time = scan.get("timestamp") or scan.get("time")
            if not scan_time:
                continue
            opportunities = scan.get("opportunities", scan.get("results", []))
            slots_available = scan.get("anySlotsAvailable", True)
            for opp in opportunities:
                score = opp.get("finalScore", opp.get("score", 0))
                if score >= 175 and not slots_available:
                    if hours_ago(scan_time, 1):
                        result["missedOpportunities1h"] += 1
                    if hours_ago(scan_time, 4):
                        result["missedOpportunities4h"] += 1

    # --- Read DSL state files for position quality ---
    dsl_pattern = os.path.join(state_dir, "dsl-*.json")
    dsl_files = glob.glob(dsl_pattern)
    roe_values = []

    for dsl_path in dsl_files:
        state = load_json_safe(dsl_path)
        if not state or not state.get("active"):
            continue
        result["slotsUsed"] += 1

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

    if roe_values:
        result["avgPositionROE"] = round(sum(roe_values) / len(roe_values), 2)

    # --- Compute signal pressure score ---
    pressure = 0
    pressure += result["missedFirstJumps1h"] * 15
    pressure += result["missedOpportunities1h"] * 8
    if result["slotsUsed"] >= result["slotsMax"] and pressure > 0:
        pressure += 10
    result["signalPressure"] = min(100, pressure)

    return result


def analyze_tiger_instance(instance_id, instance_data):
    """Analyze signal pressure for a TIGER instance."""
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
        "recentWinRate": 0,
    }

    # --- Read prescreened.json ---
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
    elif isinstance(prescreened, list):
        result["prescreenerDensity"] = len(prescreened)
        scores = [float(c.get("score", c.get("totalScore", 0)))
                  for c in prescreened if c.get("score") or c.get("totalScore")]
        if scores:
            result["avgPrescreenerScore"] = round(sum(scores) / len(scores), 1)

    # --- Read tiger-state.json ---
    tiger_state_path = _find_scan_file(workspace, "tiger-state.json")
    tiger_state = load_json_safe(tiger_state_path)
    if tiger_state:
        result["slotsUsed"] = tiger_state.get("activePositions", 0)
        result["slotsMax"] = tiger_state.get("maxSlots", result["slotsMax"])
        result["aggression"] = tiger_state.get("aggression", "NORMAL")

    # --- Read trade-log.json for recent win rate ---
    trade_log_path = _find_scan_file(workspace, "trade-log.json")
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

    # --- Count high-confluence scanner outputs ---
    for scanner_file in ["funding-scanner.json", "compression-scanner.json",
                         "momentum-scanner.json", "whale-scanner.json",
                         "volatility-scanner.json"]:
        scanner_path = _find_scan_file(workspace, scanner_file)
        scanner_data = load_json_safe(scanner_path)
        if scanner_data:
            confluence = scanner_data.get("confluence", 0)
            if isinstance(confluence, (int, float)) and confluence >= 0.65:
                result["highConfluenceCount"] += 1

    # --- Compute signal pressure score ---
    pressure = 0
    pressure += result["highConfluenceCount"] * 10
    if result["prescreenerDensity"] >= 25:
        pressure += (result["prescreenerDensity"] - 15) * 5
    if result["slotsUsed"] >= result["slotsMax"] and result["highConfluenceCount"] > 0:
        pressure += 15
    if result["aggression"] in ("ELEVATED", "ABORT"):
        pressure += 10
    result["signalPressure"] = min(100, pressure)

    return result


def run():
    """Main: scan all spawned instances, compute signal pressure, save results."""
    instances = load_spawned_instances()

    if not instances:
        result = {
            "instances": {},
            "globalSignalPressure": 0,
            "marketOpportunityDensity": "NONE",
            "actionable": 0,
            "heartbeat": "HEARTBEAT_OK",
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

    if global_pressure >= 60:
        density = "HIGH"
    elif global_pressure >= 30:
        density = "MODERATE"
    elif global_pressure > 0:
        density = "LOW"
    else:
        density = "NONE"

    result = {
        "instances": results,
        "globalSignalPressure": global_pressure,
        "marketOpportunityDensity": density,
        "updatedAt": utc_now(),
        "actionable": 1 if global_pressure >= 40 else 0,
    }

    atomic_write(SIGNAL_PRESSURE_FILE, result)

    from viper_gate import output_and_track
    output_and_track("COBRA/Signals", result)


if __name__ == "__main__":
    run()
