#!/usr/bin/env python3
"""
cobra_config.py — Shared config loader for COBRA strategy.

Provides centralized MCP helpers, state file management, atomic writes,
and config loading used by all COBRA scripts.

Usage:
    from cobra_config import (
        load_config, load_spawned_instances, atomic_write,
        mcporter_call, mcporter_call_safe, WORKSPACE, COBRA_STATE_DIR
    )
"""

import json, os, sys, glob, subprocess, time, tempfile, shlex, fcntl
from datetime import datetime, timezone

WORKSPACE = os.environ.get("COBRA_WORKSPACE",
    os.environ.get("OPENCLAW_WORKSPACE", "/data/workspace"))
CONFIG_FILE = os.path.join(WORKSPACE, "cobra-config.json")
COBRA_STATE_DIR = os.path.join(WORKSPACE, "state", "cobra")
SPAWNED_DIR = os.path.join(COBRA_STATE_DIR, "spawned")
STATE_FILE = os.path.join(COBRA_STATE_DIR, "cobra-state.json")
PERFORMANCE_FILE = os.path.join(COBRA_STATE_DIR, "cobra-performance.json")
TOKEN_BUDGET_FILE = os.path.join(COBRA_STATE_DIR, "cobra-token-budget.json")

VERBOSE = os.environ.get("COBRA_VERBOSE") == "1"
DRY_RUN = os.environ.get("COBRA_DRY_RUN") == "1"

DEFAULTS = {
    "version": 1,
    "totalBudget": 10000,
    "reservePct": 15,
    "maxWolves": 2,
    "maxTigers": 1,
    "minSpawnBudget": 500,
    "telegramChatId": "",
    "midModel": "anthropic/claude-sonnet-4-20250514",
    "budgetModel": "anthropic/claude-haiku-4-5",
    "regime": {
        "adxTrendingThreshold": 25,
        "adxRangingThreshold": 20,
        "atrVolatileMultiplier": 2.0,
        "volatileDropPct": 5,
        "volatileDrop1hPct": 3.5,
    },
    "killVsKeep": {
        "signalPressureKillThreshold": 60,
        "signalPressureSpawnThreshold": 50,
        "idleHoursBeforeKill": 2,
        "avgFeePerTrade": 32,
        "maxDrawdownPct": 20,
        "portfolioMaxDrawdownPct": 15,
        "killPendingRetryMinutes": 5,
        "killPendingMaxRetries": 3,
    },
    "tokenBudget": {
        "dailyLimitTokens": 5000000,
        "lowActionableRateThreshold": 5,
    },
}


def deep_merge(base, override):
    """Recursively merge override into base. Preserves nested defaults."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config():
    """Load COBRA config with defaults."""
    try:
        with open(CONFIG_FILE) as f:
            user_config = json.load(f)
        return deep_merge(DEFAULTS, user_config)
    except FileNotFoundError:
        return dict(DEFAULTS)


def load_state():
    """Load COBRA orchestrator state."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "version": 1,
            "regime": "UNKNOWN",
            "regimeConfidence": 0,
            "regimeChangedAt": None,
            "spawnedInstances": {},
            "totalAllocated": 0,
            "lastBrainRun": None,
            "lastDecision": None,
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
        }


def save_state(state):
    """Save COBRA orchestrator state atomically."""
    state["updatedAt"] = utc_now()
    atomic_write(STATE_FILE, state)


def load_performance():
    """Load aggregated performance data."""
    try:
        with open(PERFORMANCE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"instances": {}, "global": {}, "updatedAt": None}


def save_performance(perf):
    """Save performance data atomically."""
    perf["updatedAt"] = utc_now()
    atomic_write(PERFORMANCE_FILE, perf)


def load_token_budget():
    """Load token budget tracking."""
    try:
        with open(TOKEN_BUDGET_FILE) as f:
            data = json.load(f)
        if data.get("date") != utc_today():
            return _new_token_budget()
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return _new_token_budget()


def save_token_budget(budget):
    atomic_write(TOKEN_BUDGET_FILE, budget)


def _new_token_budget():
    return {
        "date": utc_today(),
        "totalTokensEstimated": 0,
        "invocations": {},
        "recommendations": [],
    }


def load_spawned_instances(type_filter=None, include_statuses=None):
    """Load spawned instance configs from state/cobra/spawned/.

    Args:
        type_filter: "wolf" or "tiger" to filter. None for all.
        include_statuses: Set of statuses to include. None means all except
            "killed". Pass {"active", "pending_spawn", "kill_pending"} to
            be explicit.

    Returns:
        Dict of instance_id -> spawn config.
    """
    exclude = {"killed"}
    instances = {}
    if not os.path.isdir(SPAWNED_DIR):
        return instances
    for fname in os.listdir(SPAWNED_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(SPAWNED_DIR, fname)) as f:
                data = json.load(f)
            status = data.get("status", "")
            if include_statuses is not None:
                if status not in include_statuses:
                    continue
            elif status in exclude:
                continue
            itype = data.get("type", "")
            if type_filter and itype != type_filter:
                continue
            instance_id = fname.replace(".json", "")
            instances[instance_id] = data
        except (json.JSONDecodeError, IOError):
            continue
    return instances


def load_killed_instances():
    """Load killed instance configs that still have unrecovered funds."""
    instances = {}
    if not os.path.isdir(SPAWNED_DIR):
        return instances
    for fname in os.listdir(SPAWNED_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(SPAWNED_DIR, fname)) as f:
                data = json.load(f)
            if data.get("status") == "killed" and not data.get("fundsRecovered"):
                instance_id = fname.replace(".json", "")
                instances[instance_id] = data
        except (json.JSONDecodeError, IOError):
            continue
    return instances


def save_spawned_instance(instance_id, data):
    """Save a spawned instance config."""
    os.makedirs(SPAWNED_DIR, exist_ok=True)
    atomic_write(os.path.join(SPAWNED_DIR, f"{instance_id}.json"), data)


def get_instance_workspace(instance_id, instance_type):
    """Get the per-instance workspace path for a spawned WOLF or TIGER instance."""
    return os.path.join(WORKSPACE, "instances", instance_id)


def get_shared_workspace():
    """Get the shared workspace root (fallback for legacy instances)."""
    return WORKSPACE


def ensure_instance_workspace(instance_id):
    """Create the per-instance workspace directory. Returns the path."""
    ws = get_instance_workspace(instance_id, None)
    os.makedirs(ws, exist_ok=True)
    return ws


def get_instance_state_dir(instance_id, instance_type):
    """Get the state directory for a spawned instance's strategy."""
    return os.path.join(WORKSPACE, "state", instance_id)


# --- MCP helpers (from GUIDE.md pattern) ---

def mcporter_call(tool, retries=3, timeout=30, **kwargs):
    """Call a Senpi MCP tool via mcporter. Returns the `data` portion of the response."""
    if DRY_RUN:
        _log(f"[DRY_RUN] mcporter_call({tool}, {kwargs})")
        return {}

    args = []
    for k, v in kwargs.items():
        if v is None:
            continue
        if isinstance(v, (list, dict, bool)):
            args.append(f"{k}={json.dumps(v)}")
        else:
            args.append(f"{k}={v}")

    mcporter_bin = os.environ.get("MCPORTER_CMD", "mcporter")
    cmd = [mcporter_bin, "call", f"senpi.{tool}"] + args
    last_error = None
    last_stderr = None

    for attempt in range(retries):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            d = json.loads(proc.stdout)
            if d.get("success"):
                return d.get("data", {})
            last_error = d.get("error", d)
            last_stderr = proc.stderr.strip() or None
        except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError) as e:
            last_error = str(e)
            try:
                last_stderr = proc.stderr.strip() or None
            except (UnboundLocalError, AttributeError):
                pass
        if attempt < retries - 1:
            time.sleep(3)

    err_msg = f"mcporter {tool} failed after {retries} attempts: {last_error}"
    if last_stderr:
        err_msg += f" | stderr: {last_stderr[:500]}"
    raise RuntimeError(err_msg)


def mcporter_call_safe(tool, retries=3, timeout=30, **kwargs):
    """Like mcporter_call but returns None instead of raising on failure."""
    try:
        return mcporter_call(tool, retries=retries, timeout=timeout, **kwargs)
    except RuntimeError:
        return None


def get_clearinghouse_state(wallet):
    """Fetch clearinghouse state for a strategy wallet via MCP.

    Uses strategy_wallet param (Senpi MCP convention). Returns None on failure.
    """
    return mcporter_call_safe(
        "strategy_get_clearinghouse_state", strategy_wallet=wallet)


def parse_clearinghouse(ch):
    """Extract margin summary and positions from a clearinghouse response.

    Handles both nested (main.marginSummary) and flat response formats
    so callers don't need to know the MCP response shape.

    Returns:
        (margin_dict, positions_list) — margin_dict has accountValue,
        totalMarginUsed, etc. positions_list has unwrapped position dicts.
    """
    if not ch:
        return {}, []
    main = ch.get("main", ch)
    ms = main.get("marginSummary", main)
    positions = main.get("assetPositions", main.get("positions", []))
    unwrapped = []
    for p in positions:
        if isinstance(p, dict) and "position" in p:
            unwrapped.append(p["position"])
        else:
            unwrapped.append(p)
    return ms, unwrapped


# --- Utility functions ---

def atomic_write(path, data):
    """Atomically write JSON data to a file.

    Uses a unique temp file in the same directory so concurrent writers
    don't clobber each other's .tmp files before os.replace.
    """
    dirpath = os.path.dirname(path)
    os.makedirs(dirpath, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirpath, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def output(data):
    """Print JSON output for the cron mandate to parse."""
    print(json.dumps(data, indent=2 if VERBOSE else None))


def load_json_safe(path):
    """Load a JSON file, returning None on any error."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def locked_read_modify_write(path, modify_fn):
    """Atomically read-modify-write a JSON file under an exclusive lock.

    ``modify_fn(data)`` receives the current data (or {} if file is missing)
    and must return the updated data to save.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_path = path + ".lock"
    fd_lock = open(lock_path, "w")
    try:
        fcntl.flock(fd_lock, fcntl.LOCK_EX)
        data = load_json_safe(path) or {}
        updated = modify_fn(data)
        atomic_write(path, updated)
    finally:
        fcntl.flock(fd_lock, fcntl.LOCK_UN)
        fd_lock.close()


def minutes_since(iso_timestamp):
    """Return minutes elapsed since an ISO timestamp, or 9999 if unparseable."""
    if not iso_timestamp:
        return 9999
    try:
        ts = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 60
    except Exception:
        return 9999


def _log(msg):
    """Debug log to stderr (doesn't pollute JSON stdout)."""
    if VERBOSE or DRY_RUN:
        import sys as _sys
        print(f"[COBRA] {msg}", file=_sys.stderr)
