#!/usr/bin/env python3
"""
viper_gate.py — VIPER token optimization module for COBRA.

Wraps cron mandates with early-exit prefixes, tracks token usage,
and recommends frequency adjustments. Imported by COBRA scripts.

Usage:
    from viper_gate import wrap_mandate, generate_cron_payload, track_invocation
"""

import json, os, time
from cobra_config import (
    COBRA_STATE_DIR, TOKEN_BUDGET_FILE, atomic_write,
    load_token_budget, save_token_budget, locked_read_modify_write,
    utc_now, utc_today,
)

VIPER_PREFIX = (
    'VIPER: If script output is exactly "HEARTBEAT_OK" or the JSON contains '
    '"actionable": 0 or "signals": 0 or "heartbeat": "HEARTBEAT_OK", '
    'respond with exactly "HEARTBEAT_OK" — no analysis, no summary, '
    "no explanation. Only parse and act if output contains actionable items.\n\n"
)


def wrap_mandate(mandate_text):
    """Prepend VIPER early-exit prefix to a cron mandate."""
    return VIPER_PREFIX + mandate_text


def estimate_tokens(text):
    """Rough token estimate (~4 chars per token for English/JSON)."""
    return max(1, len(text) // 4)


def track_invocation(cron_name, output_size_chars, was_actionable=False):
    """Log a cron invocation to the daily token budget tracker.

    Uses file locking to prevent concurrent read-modify-write races
    between overlapping cron processes.
    """
    estimated_input = 800
    estimated_output = estimate_tokens(str(output_size_chars))
    total = estimated_input + estimated_output

    def _update(budget):
        if budget.get("date") != utc_today():
            budget = {
                "date": utc_today(),
                "totalTokensEstimated": 0,
                "invocations": {},
                "recommendations": [],
            }
        budget["totalTokensEstimated"] = budget.get("totalTokensEstimated", 0) + total
        if cron_name not in budget.get("invocations", {}):
            budget.setdefault("invocations", {})[cron_name] = {
                "count": 0, "actionable": 0, "totalTokens": 0,
            }
        budget["invocations"][cron_name]["count"] += 1
        if was_actionable:
            budget["invocations"][cron_name]["actionable"] += 1
        budget["invocations"][cron_name]["totalTokens"] += total
        return budget

    locked_read_modify_write(TOKEN_BUDGET_FILE, _update)


def get_budget_status(daily_limit=5000000):
    """Check current daily token spend vs limit."""
    budget = load_token_budget()
    spent = budget.get("totalTokensEstimated", 0)
    return {
        "date": budget.get("date", utc_today()),
        "spent": spent,
        "limit": daily_limit,
        "remainingPct": round(max(0, (daily_limit - spent) / daily_limit * 100), 1),
        "overBudget": spent > daily_limit,
    }


def recommend_frequency(cron_name, daily_limit=5000000):
    """Recommend frequency changes based on actionable rate.

    Returns dict with recommendation or None if no change needed.
    """
    budget = load_token_budget()
    inv = budget.get("invocations", {}).get(cron_name)
    if not inv or inv["count"] < 10:
        return None

    actionable_rate = (inv["actionable"] / inv["count"]) * 100

    if actionable_rate < 5:
        return {
            "cron": cron_name,
            "actionableRate": round(actionable_rate, 1),
            "recommendation": "reduce_frequency",
            "reason": f"Only {actionable_rate:.1f}% of invocations are actionable",
        }

    return None


def generate_cron_payload(name, schedule_ms, session, model, mandate,
                          viper_wrap=True, delivery_mode=None):
    """Build an OpenClaw cron JSON payload.

    Args:
        name: Human-readable cron name.
        schedule_ms: Interval in milliseconds.
        session: "main" or "isolated".
        model: Model ID (used for isolated crons).
        mandate: The mandate text.
        viper_wrap: Whether to prepend the VIPER early-exit prefix.
        delivery_mode: Optional delivery mode override.
    """
    if viper_wrap:
        mandate = wrap_mandate(mandate)

    if session == "main":
        payload = {
            "name": name,
            "schedule": {"kind": "every", "everyMs": schedule_ms},
            "sessionTarget": "main",
            "wakeMode": "now",
            "payload": {"kind": "systemEvent", "text": mandate},
        }
    else:
        payload = {
            "name": name,
            "schedule": {"kind": "every", "everyMs": schedule_ms},
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "model": model,
                "message": mandate,
            },
        }

    if delivery_mode:
        payload["delivery"] = {"mode": delivery_mode}

    return payload


def output_and_track(cron_name, data):
    """Output JSON and track the invocation for token budgeting.

    Convenience wrapper for scripts to call instead of raw output().
    """
    from cobra_config import output as _output
    _output(data)
    actionable = data.get("actionable", 0) if isinstance(data, dict) else 0
    output_str = json.dumps(data)
    track_invocation(cron_name, len(output_str), was_actionable=bool(actionable))
