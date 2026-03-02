#!/usr/bin/env python3
"""
cobra-monitor.py — Performance + Signal Aggregator for COBRA.

Collects performance metrics via MCP and signal data from local files
for all spawned WOLF/TIGER instances. Saves aggregated report to
cobra-performance.json.

Cron: Monitor — runs every 30 min via Mid model (isolated session).
"""

import json, sys, os, glob, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cobra_config import (
    load_config, load_spawned_instances, load_performance, save_performance,
    get_instance_workspace, get_shared_workspace, get_instance_state_dir,
    mcporter_call_safe, load_json_safe, output, utc_now,
    COBRA_STATE_DIR,
)

SIGNAL_PRESSURE_FILE = os.path.join(COBRA_STATE_DIR, "cobra-signals.json")


def fetch_clearinghouse(wallet):
    """Fetch clearinghouse state for a wallet."""
    return mcporter_call_safe(
        "strategy_get_clearinghouse_state",
        wallet=wallet,
    )


def compute_wolf_trade_stats(state_dir):
    """Compute trade statistics from WOLF DSL state files."""
    dsl_files = glob.glob(os.path.join(state_dir, "dsl-*.json"))
    active = 0
    total_roe = 0
    positions = []

    for path in dsl_files:
        state = load_json_safe(path)
        if not state:
            continue
        if state.get("active"):
            active += 1
            entry = float(state.get("entryPrice", 0))
            current = float(state.get("currentPrice", state.get("lastPrice", 0)))
            if current == 0:
                current = float(state.get("highWaterPrice", 0))
            direction = state.get("direction", "LONG")
            if entry > 0 and current > 0:
                roe = ((current - entry) / entry * 100 if direction == "LONG"
                       else (entry - current) / entry * 100)
                total_roe += roe
                tier_idx = state.get("currentTierIndex")
                positions.append({
                    "asset": state.get("asset", "?"),
                    "direction": direction,
                    "roe": round(roe, 2),
                    "tier": f"Tier {tier_idx + 1}" if tier_idx is not None else "Phase 1",
                })

    return {
        "activePositions": active,
        "avgROE": round(total_roe / active, 2) if active > 0 else 0,
        "positions": positions,
    }


def _find_scan_file(instance_workspace, filename):
    """Resolve a scan file: per-instance workspace first, shared workspace fallback."""
    instance_path = os.path.join(instance_workspace, filename)
    if os.path.exists(instance_path):
        return instance_path
    shared_path = os.path.join(get_shared_workspace(), filename)
    if os.path.exists(shared_path):
        return shared_path
    return instance_path


def compute_tiger_trade_stats(workspace):
    """Compute trade statistics from TIGER state files."""
    tiger_state = load_json_safe(_find_scan_file(workspace, "tiger-state.json"))
    trade_log = load_json_safe(_find_scan_file(workspace, "trade-log.json"))

    result = {
        "activePositions": 0,
        "aggression": "NORMAL",
        "dailyRateNeeded": 0,
        "recentWinRate": 0,
        "totalTrades": 0,
    }

    if tiger_state:
        result["activePositions"] = tiger_state.get("activePositions", 0)
        result["aggression"] = tiger_state.get("aggression", "NORMAL")
        result["dailyRateNeeded"] = tiger_state.get("dailyRateNeeded", 0)

    if isinstance(trade_log, list) and trade_log:
        result["totalTrades"] = len(trade_log)
        recent = trade_log[-20:]
        wins = sum(1 for t in recent
                   if float(t.get("pnl", t.get("realizedPnl", 0))) > 0)
        result["recentWinRate"] = round(wins / len(recent), 2)
    elif isinstance(trade_log, dict):
        patterns = trade_log.get("patterns", {})
        total_wins, total_trades = 0, 0
        for pd in patterns.values():
            if isinstance(pd, dict):
                total_wins += pd.get("wins", 0)
                total_trades += pd.get("total", 0)
        result["totalTrades"] = total_trades
        if total_trades > 0:
            result["recentWinRate"] = round(total_wins / total_trades, 2)

    return result


def monitor_instance(instance_id, instance_data):
    """Monitor a single spawned instance: clearinghouse + local stats."""
    itype = instance_data.get("type", "")
    wallet = instance_data.get("wallet", "")
    spawn_budget = instance_data.get("budget", 0)
    spawned_at = instance_data.get("spawnedAt", "")

    metrics = {
        "instanceId": instance_id,
        "type": itype,
        "wallet": wallet,
        "status": "active",
        "spawnBudget": spawn_budget,
        "spawnedAt": spawned_at,
        "accountValue": 0,
        "unrealizedPnl": 0,
        "roeSinceSpawn": 0,
        "utilization": 0,
        "marginUsed": 0,
        "drawdownFromPeak": 0,
        "tradeStats": {},
        "updatedAt": utc_now(),
    }

    # Fetch clearinghouse state via MCP
    ch = fetch_clearinghouse(wallet) if wallet else None
    if ch:
        account_value = float(ch.get("accountValue", ch.get("equity", 0)))
        margin_used = float(ch.get("marginUsed", ch.get("totalMarginUsed", 0)))
        upnl = float(ch.get("unrealizedPnl", ch.get("crossUnrealizedPnl", 0)))

        metrics["accountValue"] = round(account_value, 2)
        metrics["marginUsed"] = round(margin_used, 2)
        metrics["unrealizedPnl"] = round(upnl, 2)

        if spawn_budget > 0:
            metrics["roeSinceSpawn"] = round(
                (account_value - spawn_budget) / spawn_budget * 100, 2)
        if account_value > 0:
            metrics["utilization"] = round(margin_used / account_value * 100, 1)

    # Local trade stats
    workspace = get_instance_workspace(instance_id, itype)
    state_dir = get_instance_state_dir(instance_id, itype)

    if itype == "wolf":
        metrics["tradeStats"] = compute_wolf_trade_stats(state_dir)
    elif itype == "tiger":
        metrics["tradeStats"] = compute_tiger_trade_stats(workspace)

    # Peak tracking for drawdown
    perf = load_performance()
    prev = perf.get("instances", {}).get(instance_id, {})
    peak = max(prev.get("peakValue", spawn_budget), metrics["accountValue"])
    metrics["peakValue"] = round(peak, 2)
    if peak > 0:
        metrics["drawdownFromPeak"] = round((peak - metrics["accountValue"]) / peak * 100, 2)

    return metrics


def run():
    """Main: monitor all instances, aggregate, save."""
    config = load_config()
    instances = load_spawned_instances()

    if not instances:
        result = {
            "instances": {},
            "global": {
                "totalAccountValue": 0,
                "totalUnrealizedPnl": 0,
                "avgUtilization": 0,
                "activeWolves": 0,
                "activeTigers": 0,
            },
            "updatedAt": utc_now(),
            "actionable": 0,
            "heartbeat": "HEARTBEAT_OK",
        }
        save_performance(result)
        from viper_gate import output_and_track
        output_and_track("COBRA/Monitor", result)
        return

    instance_metrics = {}
    total_value = 0
    total_upnl = 0
    total_util = 0
    wolf_count = 0
    tiger_count = 0

    for iid, idata in instances.items():
        metrics = monitor_instance(iid, idata)
        instance_metrics[iid] = metrics
        total_value += metrics["accountValue"]
        total_upnl += metrics["unrealizedPnl"]
        total_util += metrics["utilization"]
        if metrics["type"] == "wolf":
            wolf_count += 1
        elif metrics["type"] == "tiger":
            tiger_count += 1

    count = len(instance_metrics)
    avg_util = round(total_util / count, 1) if count > 0 else 0

    # Load latest signal pressure data
    signal_data = load_json_safe(SIGNAL_PRESSURE_FILE) or {}
    global_signal_pressure = signal_data.get("globalSignalPressure", 0)

    # Detect issues
    alerts = []
    for iid, m in instance_metrics.items():
        if m["drawdownFromPeak"] > config.get("killVsKeep", {}).get("maxDrawdownPct", 20):
            alerts.append(f"{iid}: drawdown {m['drawdownFromPeak']}% exceeds threshold")
        if m["utilization"] == 0 and m["accountValue"] > 100:
            alerts.append(f"{iid}: 0% utilization with ${m['accountValue']} capital")

    # Subagent liveness: flag instances with kill_pending status
    for iid, idata in instances.items():
        if idata.get("status") == "kill_pending":
            remaining = idata.get("remainingPositions", "?")
            alerts.append(
                f"{iid}: kill_pending — {remaining} positions failed to close, "
                f"needs manual intervention or retry"
            )

    # Portfolio-level drawdown alert (against allocated capital, not totalBudget)
    total_allocated = sum(d.get("budget", 0) for d in instances.values())
    if total_allocated > 0 and total_value > 0:
        portfolio_dd = (total_allocated - total_value) / total_allocated * 100
        portfolio_threshold = config.get("killVsKeep", {}).get("portfolioMaxDrawdownPct", 15)
        if portfolio_dd > portfolio_threshold * 0.8:
            alerts.append(
                f"PORTFOLIO WARNING: drawdown {portfolio_dd:.1f}% approaching "
                f"circuit breaker threshold ({portfolio_threshold}%)"
            )

    result = {
        "instances": instance_metrics,
        "global": {
            "totalAccountValue": round(total_value, 2),
            "totalUnrealizedPnl": round(total_upnl, 2),
            "avgUtilization": avg_util,
            "activeWolves": wolf_count,
            "activeTigers": tiger_count,
            "globalSignalPressure": global_signal_pressure,
        },
        "alerts": alerts,
        "updatedAt": utc_now(),
        "actionable": 1 if alerts else 0,
    }

    save_performance(result)

    from viper_gate import output_and_track
    output_and_track("COBRA/Monitor", result)


if __name__ == "__main__":
    run()
