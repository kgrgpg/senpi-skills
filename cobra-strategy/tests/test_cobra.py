#!/usr/bin/env python3
"""
Tests for COBRA critical logic: circuit breaker, kill-vs-keep,
signal pressure, leverage governance, and kill_pending retry.

Run: python3 -m pytest tests/test_cobra.py -v
  or: python3 tests/test_cobra.py
"""

import json, os, sys, tempfile, shutil, unittest, importlib.util
from contextlib import contextmanager
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta


@contextmanager
def ExitStack_cm(*cms):
    """Combine multiple patch context managers into one."""
    if not cms:
        yield
        return
    with cms[0]:
        with ExitStack_cm(*cms[1:]):
            yield

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

_TEST_WORKSPACE = tempfile.mkdtemp(prefix="cobra-test-")
os.environ["COBRA_DRY_RUN"] = "1"
os.environ["COBRA_WORKSPACE"] = _TEST_WORKSPACE


def _import_hyphenated(name, filepath):
    """Import a module whose filename contains hyphens."""
    spec = importlib.util.spec_from_file_location(name, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Pre-register hyphenated modules so tests don't depend on execution order.
_import_hyphenated("cobra_spawner", os.path.join(SCRIPTS_DIR, "cobra-spawner.py"))
_import_hyphenated("cobra_regime", os.path.join(SCRIPTS_DIR, "cobra-regime.py"))
_import_hyphenated("cobra_signals", os.path.join(SCRIPTS_DIR, "cobra-signals.py"))


class TestSpawnerImport(unittest.TestCase):
    """Verify brain can load the hyphenated cobra-spawner.py."""

    def test_get_spawner_returns_module(self):
        brain = _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))
        # Clear the cache to force a fresh load
        brain._spawner_cache = None
        spawner = brain._get_spawner()
        self.assertTrue(hasattr(spawner, "spawn_wolf"))
        self.assertTrue(hasattr(spawner, "kill_instance"))
        self.assertTrue(hasattr(spawner, "build_kill_message"))

    def test_get_spawner_caches(self):
        brain = _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))
        brain._spawner_cache = None
        s1 = brain._get_spawner()
        s2 = brain._get_spawner()
        self.assertIs(s1, s2)


class TestCircuitBreaker(unittest.TestCase):
    """Circuit breaker should compare against allocated capital, not totalBudget."""

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def test_no_false_trip_when_partially_deployed(self):
        """With 50% deployed at par, breaker must NOT trip (no actual loss)."""
        brain = self._load_brain()
        config = {"totalBudget": 10000, "killVsKeep": {"portfolioMaxDrawdownPct": 15}}
        instances = {
            "wolf-1": {"budget": 3000},
            "tiger-1": {"budget": 2000},
        }
        perf_data = {"instances": {
            "wolf-1": {"accountValue": 3000},
            "tiger-1": {"accountValue": 2000},
        }}
        self.assertFalse(
            brain._check_portfolio_circuit_breaker(config, perf_data, instances))

    def test_trips_on_real_drawdown(self):
        """With 20% actual loss on deployed capital, breaker trips at 15% threshold."""
        brain = self._load_brain()
        config = {"totalBudget": 10000, "killVsKeep": {"portfolioMaxDrawdownPct": 15}}
        instances = {
            "wolf-1": {"budget": 5000},
            "wolf-2": {"budget": 5000},
        }
        perf_data = {"instances": {
            "wolf-1": {"accountValue": 4000},
            "wolf-2": {"accountValue": 4000},
        }}
        self.assertTrue(
            brain._check_portfolio_circuit_breaker(config, perf_data, instances))

    def test_no_trip_on_minor_loss(self):
        """5% loss should NOT trip the 15% breaker."""
        brain = self._load_brain()
        config = {"totalBudget": 10000, "killVsKeep": {"portfolioMaxDrawdownPct": 15}}
        instances = {"wolf-1": {"budget": 8000}}
        perf_data = {"instances": {"wolf-1": {"accountValue": 7600}}}
        self.assertFalse(
            brain._check_portfolio_circuit_breaker(config, perf_data, instances))

    def test_no_instances_returns_false(self):
        """Zero allocated = no breaker, not a 100% drawdown."""
        brain = self._load_brain()
        config = {"totalBudget": 10000, "killVsKeep": {"portfolioMaxDrawdownPct": 15}}
        self.assertFalse(
            brain._check_portfolio_circuit_breaker(config, {}, {}))


class TestKillVsKeep(unittest.TestCase):

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def test_keep_on_tier2_positions(self):
        """Tier 2+ positions should strongly favor KEEP via weighted scoring."""
        brain = self._load_brain()
        config = {"killVsKeep": {
            "signalPressureKillThreshold": 60, "idleHoursBeforeKill": 2,
            "avgFeePerTrade": 32, "maxDrawdownPct": 20,
        }}
        signal_data = {"instances": {"wolf-1": {
            "signalPressure": 90,
            "positionQuality": {"phase1": 0, "tier1": 0, "tier2plus": 2},
        }}}
        perf_data = {"instances": {"wolf-1": {
            "accountValue": 5000, "unrealizedPnl": 200, "utilization": 50,
            "drawdownFromPeak": 0, "tradeStats": {"activePositions": 2},
        }}}
        result = brain._evaluate_kill_vs_keep(
            "wolf-1", {"type": "wolf", "budget": 5000, "spawnedAt": ""},
            signal_data, perf_data, config)
        self.assertEqual(result["decision"], "KEEP")

    def test_tier2_can_be_overridden_by_extreme_drawdown(self):
        """Even with Tier 2+, extreme drawdown + high signal pressure can force KILL."""
        brain = self._load_brain()
        config = {"killVsKeep": {
            "signalPressureKillThreshold": 60, "idleHoursBeforeKill": 2,
            "avgFeePerTrade": 32, "maxDrawdownPct": 20,
        }}
        signal_data = {"instances": {"wolf-1": {
            "signalPressure": 90,
            "positionQuality": {"phase1": 2, "tier1": 0, "tier2plus": 1},
            "avgPositionROE": -15,
            "missedFirstJumps1h": 5, "missedOpportunities1h": 3,
            "highConfluenceCount": 0, "recentWinRate": 0.6,
        }}}
        perf_data = {"instances": {"wolf-1": {
            "accountValue": 3800, "unrealizedPnl": -200, "utilization": 80,
            "drawdownFromPeak": 25, "tradeStats": {"activePositions": 3},
        }}}
        result = brain._evaluate_kill_vs_keep(
            "wolf-1", {"type": "wolf", "budget": 5000,
                       "spawnedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            signal_data, perf_data, config)
        self.assertIn(result["decision"], ("KILL", "WAIT"))

    def test_kill_idle_instance(self):
        """Idle for 3+ hours with 0 positions and high signal pressure = KILL."""
        brain = self._load_brain()
        config = {"killVsKeep": {
            "signalPressureKillThreshold": 60, "idleHoursBeforeKill": 2,
            "avgFeePerTrade": 32, "maxDrawdownPct": 20,
        }}
        old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        signal_data = {"instances": {"wolf-1": {
            "signalPressure": 70,
            "positionQuality": {"phase1": 0, "tier1": 0, "tier2plus": 0},
        }}}
        perf_data = {"instances": {"wolf-1": {
            "accountValue": 5000, "unrealizedPnl": 0, "utilization": 0,
            "drawdownFromPeak": 0, "tradeStats": {"activePositions": 0},
        }}}
        result = brain._evaluate_kill_vs_keep(
            "wolf-1", {"type": "wolf", "budget": 5000, "spawnedAt": old_time},
            signal_data, perf_data, config)
        self.assertEqual(result["decision"], "KILL")

    def test_wait_on_moderate_signals(self):
        """Moderate kill score (25-49) should WAIT, not KILL."""
        brain = self._load_brain()
        config = {"killVsKeep": {
            "signalPressureKillThreshold": 60, "idleHoursBeforeKill": 2,
            "avgFeePerTrade": 32, "maxDrawdownPct": 20,
        }}
        signal_data = {"instances": {"wolf-1": {
            "signalPressure": 70,
            "positionQuality": {"phase1": 1, "tier1": 0, "tier2plus": 0},
            "avgPositionROE": 2.0,
        }}}
        perf_data = {"instances": {"wolf-1": {
            "accountValue": 5000, "unrealizedPnl": 100, "utilization": 30,
            "drawdownFromPeak": 5, "tradeStats": {"activePositions": 1},
        }}}
        result = brain._evaluate_kill_vs_keep(
            "wolf-1", {"type": "wolf", "budget": 5000,
                       "spawnedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            signal_data, perf_data, config)
        self.assertIn(result["decision"], ("KEEP", "WAIT"))


class TestLeverage(unittest.TestCase):

    def _load_spawner(self):
        return _import_hyphenated(
            "cobra_spawner", os.path.join(SCRIPTS_DIR, "cobra-spawner.py"))

    def test_trending_full_leverage(self):
        spawner = self._load_spawner()
        self.assertEqual(spawner._compute_leverage(3000, "TRENDING"), 7)
        self.assertEqual(spawner._compute_leverage(10000, "TRENDING"), 10)

    def test_volatile_halved(self):
        spawner = self._load_spawner()
        lev = spawner._compute_leverage(10000, "VOLATILE")
        self.assertLessEqual(lev, 5)
        self.assertGreaterEqual(lev, 3)

    def test_ranging_reduced(self):
        spawner = self._load_spawner()
        lev = spawner._compute_leverage(10000, "RANGING")
        self.assertLess(lev, 10)
        self.assertGreaterEqual(lev, 5)

class TestWorkspaceIsolation(unittest.TestCase):

    def test_instance_workspace_is_per_instance(self):
        import cobra_config
        ws1 = cobra_config.get_instance_workspace("wolf-abc", "wolf")
        ws2 = cobra_config.get_instance_workspace("wolf-def", "wolf")
        self.assertNotEqual(ws1, ws2)
        self.assertIn("wolf-abc", ws1)
        self.assertIn("wolf-def", ws2)

    def test_shared_workspace_is_root(self):
        import cobra_config
        shared = cobra_config.get_shared_workspace()
        self.assertEqual(shared, cobra_config.WORKSPACE)

    def test_find_scan_file_prefers_instance(self):
        """_find_scan_file should prefer per-instance file over shared."""
        import cobra_config
        signals = _import_hyphenated(
            "cobra_signals", os.path.join(SCRIPTS_DIR, "cobra-signals.py"))

        tmpdir = tempfile.mkdtemp()
        try:
            instance_ws = os.path.join(tmpdir, "instances", "wolf-1")
            os.makedirs(instance_ws)
            with open(os.path.join(instance_ws, "test.json"), "w") as f:
                f.write("{}")

            shared_ws = tmpdir
            with open(os.path.join(shared_ws, "test.json"), "w") as f:
                f.write('{"shared": true}')

            # File that only exists in shared workspace (not in instance)
            with open(os.path.join(shared_ws, "shared-only.json"), "w") as f:
                f.write('{"shared": true}')

            with patch.object(cobra_config, "WORKSPACE", tmpdir):
                # Prefers instance file when it exists
                result = signals._find_scan_file(instance_ws, "test.json")
                self.assertEqual(result, os.path.join(instance_ws, "test.json"))

                # Falls back to shared when instance file doesn't exist
                result2 = signals._find_scan_file(instance_ws, "shared-only.json")
                self.assertEqual(result2, os.path.join(shared_ws, "shared-only.json"))
        finally:
            shutil.rmtree(tmpdir)


class TestKillPendingRetry(unittest.TestCase):

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def test_skip_recently_pending(self):
        """kill_pending < retryMinutes should RETRY_WAIT, not retry."""
        brain = self._load_brain()
        config = {"killVsKeep": {"killPendingRetryMinutes": 5, "killPendingMaxRetries": 3}}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        instances = {"wolf-1": {"status": "kill_pending", "killedAt": now, "budget": 5000}}
        results, ids = brain._retry_kill_pending(instances, config)
        self.assertIn("wolf-1", ids)
        self.assertEqual(results[0]["action"], "RETRY_WAIT")

    def test_stuck_after_max_retries(self):
        """After max retries, should mark STUCK."""
        brain = self._load_brain()
        config = {"killVsKeep": {"killPendingRetryMinutes": 0, "killPendingMaxRetries": 3}}
        old_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        instances = {"wolf-1": {
            "status": "kill_pending", "killedAt": old_time,
            "killRetries": 3, "budget": 5000,
        }}
        results, ids = brain._retry_kill_pending(instances, config)
        self.assertIn("wolf-1", ids)
        self.assertEqual(results[0]["action"], "STUCK")


class TestPendingActionVerification(unittest.TestCase):

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def test_detects_stale_pending_spawn(self):
        """pending_spawn with no activity after threshold triggers warning."""
        brain = self._load_brain()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=45)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        state = {"pendingActions": {"spawns": [], "kills": []}}
        instances = {"wolf-abc": {
            "status": "pending_spawn", "spawnedAt": old_time,
            "type": "wolf", "wallet": "0xfake", "budget": 5000,
        }}
        warnings, re_kills, promote_ids = brain._verify_pending_actions(state, instances)
        self.assertEqual(len(warnings), 1)
        self.assertIn("wolf-abc", warnings[0])
        self.assertIn("pending_spawn", warnings[0])

    def test_reissues_missed_kill(self):
        brain = self._load_brain()
        state = {"pendingActions": {"spawns": [], "kills": ["wolf-def"]}}
        instances = {"wolf-def": {"status": "active", "budget": 5000}}
        warnings, re_kills, promote_ids = brain._verify_pending_actions(state, instances)
        self.assertEqual(len(re_kills), 1)
        self.assertEqual(re_kills[0]["instanceId"], "wolf-def")

    def test_no_warnings_when_clean(self):
        brain = self._load_brain()
        state = {"pendingActions": {"spawns": [], "kills": []}}
        warnings, re_kills, promote_ids = brain._verify_pending_actions(state, {})
        self.assertEqual(len(warnings), 0)
        self.assertEqual(len(re_kills), 0)

    def test_funded_but_idle_not_promoted(self):
        """pending_spawn with accountValue > 0 but utilization == 0 must NOT promote."""
        brain = self._load_brain()
        perf_dir = os.path.join(_TEST_WORKSPACE, "state", "cobra")
        os.makedirs(perf_dir, exist_ok=True)
        perf_path = os.path.join(perf_dir, "cobra-performance.json")
        with open(perf_path, "w") as f:
            json.dump({"instances": {"wolf-funded": {
                "accountValue": 3000, "utilization": 0,
            }}}, f)
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=45)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        state = {"pendingActions": {"spawns": [], "kills": []}}
        instances = {"wolf-funded": {
            "status": "pending_spawn", "spawnedAt": old_time,
            "type": "wolf", "wallet": "0xfake", "budget": 3000,
        }}
        try:
            warnings, re_kills, promote_ids = brain._verify_pending_actions(state, instances)
            self.assertNotIn("wolf-funded", promote_ids)
            self.assertEqual(len(warnings), 1)
            self.assertIn("wolf-funded", warnings[0])
        finally:
            if os.path.exists(perf_path):
                os.unlink(perf_path)


class TestSpawnDecisions(unittest.TestCase):

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def _base_config(self, **overrides):
        cfg = {
            "totalBudget": 10000, "minSpawnBudget": 500,
            "maxWolves": 2, "maxTigers": 1, "reservePct": 15,
            "killVsKeep": {"signalPressureSpawnThreshold": 50},
        }
        cfg.update(overrides)
        return cfg

    def test_no_spawn_at_max_wolves(self):
        """No WOLF spawn when already at maxWolves limit."""
        brain = self._load_brain()
        regime = {"regime": "TRENDING", "allocation": {"wolf": 60, "tiger": 25, "reserve": 15}}
        signal = {"globalSignalPressure": 70}
        instances = {"wolf-1": {"type": "wolf", "budget": 5000}}
        spawns = brain._decide_spawns(
            regime, signal, {}, self._base_config(maxWolves=1, maxTigers=0), instances)
        wolf_spawns = [s for s in spawns if s["type"] == "wolf"]
        self.assertEqual(len(wolf_spawns), 0)

    def test_no_spawn_low_pressure_with_existing_instances(self):
        """Low signal pressure + existing instances = no new spawns."""
        brain = self._load_brain()
        regime = {"regime": "TRENDING", "allocation": {"wolf": 60, "tiger": 25, "reserve": 15}}
        signal = {"globalSignalPressure": 30}
        instances = {"wolf-1": {"type": "wolf", "budget": 3000}}
        spawns = brain._decide_spawns(regime, signal, {}, self._base_config(), instances)
        self.assertEqual(spawns, [])

    def test_spawns_when_no_instances(self):
        """With zero instances, spawn regardless of signal pressure."""
        brain = self._load_brain()
        regime = {"regime": "TRENDING", "allocation": {"wolf": 60, "tiger": 25, "reserve": 15}}
        signal = {"globalSignalPressure": 10}
        spawns = brain._decide_spawns(regime, signal, {}, self._base_config(), {})
        self.assertGreater(len(spawns), 0)

    def test_no_spawn_insufficient_capital(self):
        """No spawn when idle capital below minSpawnBudget."""
        brain = self._load_brain()
        regime = {"regime": "TRENDING", "allocation": {"wolf": 60, "tiger": 25, "reserve": 15}}
        signal = {"globalSignalPressure": 70}
        instances = {
            "wolf-1": {"type": "wolf", "budget": 5000},
            "wolf-2": {"type": "wolf", "budget": 3200},
        }
        spawns = brain._decide_spawns(regime, signal, {}, self._base_config(), instances)
        self.assertEqual(spawns, [])

    def test_spawn_respects_regime_allocation(self):
        """In RANGING, tiger should get larger allocation than wolf."""
        brain = self._load_brain()
        regime = {"regime": "RANGING", "allocation": {"wolf": 20, "tiger": 50, "reserve": 30}}
        signal = {"globalSignalPressure": 10}
        spawns = brain._decide_spawns(regime, signal, {}, self._base_config(), {})
        wolves = [s for s in spawns if s["type"] == "wolf"]
        tigers = [s for s in spawns if s["type"] == "tiger"]
        if wolves and tigers:
            self.assertGreater(tigers[0]["budget"], wolves[0]["budget"])

    def test_kill_pending_excluded_from_allocation(self):
        """kill_pending instances must NOT count toward per-type allocation,
        so freed capital can be redeployed via new spawns."""
        brain = self._load_brain()
        regime = {"regime": "TRENDING", "allocation": {"wolf": 60, "tiger": 25, "reserve": 15}}
        signal = {"globalSignalPressure": 70}
        instances = {
            "wolf-1": {"type": "wolf", "budget": 3000, "status": "active"},
            "wolf-2": {"type": "wolf", "budget": 3000, "status": "kill_pending"},
        }
        spawns = brain._decide_spawns(
            regime, signal, {}, self._base_config(maxWolves=2), instances)
        wolf_spawns = [s for s in spawns if s["type"] == "wolf"]
        self.assertGreater(len(wolf_spawns), 0,
                           "Should spawn wolf since kill_pending freed a slot")


class TestSpawnPriority(unittest.TestCase):
    """Regime-based spawn priority: higher allocation spawns first."""

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def _base_config(self, **overrides):
        cfg = {
            "totalBudget": 1000, "minSpawnBudget": 500,
            "maxWolves": 2, "maxTigers": 1, "reservePct": 15,
            "killVsKeep": {"signalPressureSpawnThreshold": 50},
        }
        cfg.update(overrides)
        return cfg

    def test_ranging_spawns_tiger_first_small_budget(self):
        """With $1000 budget in RANGING, tiger (50%) should spawn, not wolf (20%)."""
        brain = self._load_brain()
        regime = {"regime": "RANGING", "allocation": {"wolf": 20, "tiger": 50, "reserve": 30}}
        signal = {"globalSignalPressure": 10}
        spawns = brain._decide_spawns(regime, signal, {}, self._base_config(), {})
        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0]["type"], "tiger")

    def test_trending_spawns_wolf_first_small_budget(self):
        """With $1000 budget in TRENDING, wolf (60%) should spawn, not tiger (25%)."""
        brain = self._load_brain()
        regime = {"regime": "TRENDING", "allocation": {"wolf": 60, "tiger": 25, "reserve": 15}}
        signal = {"globalSignalPressure": 10}
        spawns = brain._decide_spawns(regime, signal, {}, self._base_config(), {})
        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0]["type"], "wolf")

    def test_large_budget_spawns_both(self):
        """With sufficient capital, both types should spawn."""
        brain = self._load_brain()
        regime = {"regime": "TRENDING", "allocation": {"wolf": 60, "tiger": 25, "reserve": 15}}
        signal = {"globalSignalPressure": 10}
        spawns = brain._decide_spawns(
            regime, signal, {}, self._base_config(totalBudget=10000), {})
        types = {s["type"] for s in spawns}
        self.assertIn("wolf", types)
        self.assertIn("tiger", types)


class TestSlotExpansion(unittest.TestCase):
    """Dynamic slot expansion when signal-rich and slot-capped."""

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def _base_config(self, **overrides):
        cfg = {
            "totalBudget": 10000, "minSpawnBudget": 500,
            "maxWolves": 2, "maxTigers": 1, "reservePct": 15,
            "killVsKeep": {"signalPressureSpawnThreshold": 50},
        }
        cfg.update(overrides)
        return cfg

    def test_expands_slot_capped_high_pressure(self):
        """Instance at max slots with pressure > 60 should get expansion."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "wolf-exp1": {
                    "signalPressure": 75, "slotsUsed": 2, "slotsMax": 2,
                },
            },
        }
        perf = {
            "instances": {
                "wolf-exp1": {"accountValue": 500, "utilization": 60},
            },
        }
        instances = {
            "wolf-exp1": {"type": "wolf", "status": "active", "budget": 500, "slots": 2},
        }
        expansions = brain._decide_expansions(signal, perf, self._base_config(), instances)
        self.assertEqual(len(expansions), 1)
        self.assertEqual(expansions[0]["newSlots"], 3)
        self.assertGreater(expansions[0]["newMarginPerSlot"], 0)

    def test_no_expand_low_pressure(self):
        """Pressure < 40 should not trigger expansion."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "wolf-lo": {"signalPressure": 30, "slotsUsed": 2, "slotsMax": 2},
            },
        }
        perf = {"instances": {"wolf-lo": {"accountValue": 500, "utilization": 50}}}
        instances = {"wolf-lo": {"type": "wolf", "status": "active", "budget": 500, "slots": 2}}
        expansions = brain._decide_expansions(signal, perf, self._base_config(), instances)
        self.assertEqual(len(expansions), 0)

    def test_no_expand_slots_available(self):
        """If slots not full, no expansion needed."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "wolf-avail": {"signalPressure": 80, "slotsUsed": 1, "slotsMax": 2},
            },
        }
        perf = {"instances": {"wolf-avail": {"accountValue": 500, "utilization": 30}}}
        instances = {"wolf-avail": {"type": "wolf", "status": "active", "budget": 500, "slots": 2}}
        expansions = brain._decide_expansions(signal, perf, self._base_config(), instances)
        self.assertEqual(len(expansions), 0)

    def test_no_expand_high_utilization(self):
        """Utilization > 70% blocks expansion (insufficient margin headroom)."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "wolf-util": {"signalPressure": 80, "slotsUsed": 2, "slotsMax": 2},
            },
        }
        perf = {"instances": {"wolf-util": {"accountValue": 500, "utilization": 75}}}
        instances = {"wolf-util": {"type": "wolf", "status": "active", "budget": 500, "slots": 2}}
        expansions = brain._decide_expansions(signal, perf, self._base_config(), instances)
        self.assertEqual(len(expansions), 0)

    def test_no_expand_drawdown(self):
        """Account value < 95% of budget blocks expansion."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "wolf-dd": {"signalPressure": 80, "slotsUsed": 2, "slotsMax": 2},
            },
        }
        perf = {"instances": {"wolf-dd": {"accountValue": 400, "utilization": 50}}}
        instances = {"wolf-dd": {"type": "wolf", "status": "active", "budget": 500, "slots": 2}}
        expansions = brain._decide_expansions(signal, perf, self._base_config(), instances)
        self.assertEqual(len(expansions), 0)

    def test_max_slot_cap(self):
        """Cannot expand beyond 5 slots."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "wolf-cap": {"signalPressure": 90, "slotsUsed": 5, "slotsMax": 5},
            },
        }
        perf = {"instances": {"wolf-cap": {"accountValue": 10000, "utilization": 40}}}
        instances = {"wolf-cap": {"type": "wolf", "status": "active", "budget": 10000, "slots": 5}}
        expansions = brain._decide_expansions(signal, perf, self._base_config(), instances)
        self.assertEqual(len(expansions), 0)

    def test_margin_recalculation(self):
        """New margin per slot = accountValue * 0.30 / newSlots."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "wolf-mrg": {"signalPressure": 70, "slotsUsed": 3, "slotsMax": 3},
            },
        }
        perf = {"instances": {"wolf-mrg": {"accountValue": 6000, "utilization": 50}}}
        instances = {"wolf-mrg": {"type": "wolf", "status": "active", "budget": 5000, "slots": 3}}
        expansions = brain._decide_expansions(signal, perf, self._base_config(), instances)
        self.assertEqual(len(expansions), 1)
        self.assertEqual(expansions[0]["newSlots"], 4)
        expected_margin = round(6000 * 0.30 / 4, 2)
        self.assertEqual(expansions[0]["newMarginPerSlot"], expected_margin)


    def test_aggressive_expansion_two_slots(self):
        """Pressure >= 80 should expand by +2 slots."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "wolf-agg": {"signalPressure": 85, "slotsUsed": 2, "slotsMax": 2},
            },
        }
        perf = {"instances": {"wolf-agg": {"accountValue": 500, "utilization": 50}}}
        instances = {"wolf-agg": {"type": "wolf", "status": "active", "budget": 500, "slots": 2}}
        expansions = brain._decide_expansions(signal, perf, self._base_config(), instances)
        self.assertEqual(len(expansions), 1)
        self.assertEqual(expansions[0]["newSlots"], 4)

    def test_monitor_position_count_fallback(self):
        """When signal slotsUsed=0 but monitor shows 2 positions, use monitor data."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "wolf-mon": {"signalPressure": 50, "slotsUsed": 0, "slotsMax": 2},
            },
        }
        perf = {"instances": {
            "wolf-mon": {"accountValue": 500, "utilization": 60, "positionCount": 2},
        }}
        instances = {"wolf-mon": {"type": "wolf", "status": "active", "budget": 500, "slots": 2}}
        expansions = brain._decide_expansions(signal, perf, self._base_config(), instances)
        self.assertEqual(len(expansions), 1)

    def test_moderate_pressure_triggers_expansion(self):
        """Pressure 40-59 should trigger expansion (lowered threshold)."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "wolf-mod": {"signalPressure": 45, "slotsUsed": 2, "slotsMax": 2},
            },
        }
        perf = {"instances": {"wolf-mod": {"accountValue": 500, "utilization": 50}}}
        instances = {"wolf-mod": {"type": "wolf", "status": "active", "budget": 500, "slots": 2}}
        expansions = brain._decide_expansions(signal, perf, self._base_config(), instances)
        self.assertEqual(len(expansions), 1)
        self.assertEqual(expansions[0]["newSlots"], 3)


class TestRebalanceDecisions(unittest.TestCase):
    """Rebalance idle instances to active strategy types."""

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def _base_config(self, **overrides):
        cfg = {
            "totalBudget": 10000, "minSpawnBudget": 500,
            "maxWolves": 2, "maxTigers": 1, "reservePct": 15,
            "killVsKeep": {"signalPressureSpawnThreshold": 50},
        }
        cfg.update(overrides)
        return cfg

    def test_rebalance_idle_tiger_to_wolf(self):
        """Idle tiger (0% util, 3h) with wolf pressure > 50 triggers rebalance."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "tiger-idle": {"signalPressure": 10},
                "wolf-busy": {"signalPressure": 70},
            },
        }
        perf = {
            "instances": {
                "tiger-idle": {"accountValue": 1000, "utilization": 0},
                "wolf-busy": {"accountValue": 500, "utilization": 60},
            },
        }
        instances = {
            "tiger-idle": {
                "type": "tiger", "status": "active", "budget": 1000,
                "spawnedAt": "2020-01-01T00:00:00Z",
            },
            "wolf-busy": {
                "type": "wolf", "status": "active", "budget": 500,
                "spawnedAt": "2020-01-01T00:00:00Z",
            },
        }
        rebs = brain._decide_rebalances(signal, perf, self._base_config(), instances)
        self.assertEqual(len(rebs), 1)
        self.assertEqual(rebs[0]["fromInstance"], "tiger-idle")
        self.assertEqual(rebs[0]["toType"], "wolf")
        self.assertEqual(rebs[0]["amount"], 500.0)

    def test_no_rebalance_if_other_type_low_pressure(self):
        """No rebalance if the other strategy type has low pressure."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "tiger-idle": {"signalPressure": 5},
                "wolf-quiet": {"signalPressure": 20},
            },
        }
        perf = {
            "instances": {
                "tiger-idle": {"accountValue": 1000, "utilization": 0},
                "wolf-quiet": {"accountValue": 500, "utilization": 10},
            },
        }
        instances = {
            "tiger-idle": {
                "type": "tiger", "status": "active", "budget": 1000,
                "spawnedAt": "2020-01-01T00:00:00Z",
            },
            "wolf-quiet": {
                "type": "wolf", "status": "active", "budget": 500,
                "spawnedAt": "2020-01-01T00:00:00Z",
            },
        }
        rebs = brain._decide_rebalances(signal, perf, self._base_config(), instances)
        self.assertEqual(len(rebs), 0)

    def test_no_rebalance_recently_spawned(self):
        """Instance alive < 1 hour should not be rebalanced."""
        brain = self._load_brain()
        from cobra_config import utc_now
        signal = {
            "instances": {
                "tiger-new": {"signalPressure": 0},
                "wolf-hot": {"signalPressure": 80},
            },
        }
        perf = {
            "instances": {
                "tiger-new": {"accountValue": 500, "utilization": 0},
                "wolf-hot": {"accountValue": 500, "utilization": 60},
            },
        }
        instances = {
            "tiger-new": {
                "type": "tiger", "status": "active", "budget": 500,
                "spawnedAt": utc_now(),
            },
            "wolf-hot": {
                "type": "wolf", "status": "active", "budget": 500,
                "spawnedAt": "2020-01-01T00:00:00Z",
            },
        }
        rebs = brain._decide_rebalances(signal, perf, self._base_config(), instances)
        self.assertEqual(len(rebs), 0)

    def test_rebalance_respects_min_remaining(self):
        """Rebalance amount must leave at least $400 in the source instance."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "tiger-small": {"signalPressure": 0},
                "wolf-hot": {"signalPressure": 80},
            },
        }
        perf = {
            "instances": {
                "tiger-small": {"accountValue": 800, "utilization": 0},
                "wolf-hot": {"accountValue": 500, "utilization": 60},
            },
        }
        instances = {
            "tiger-small": {
                "type": "tiger", "status": "active", "budget": 800,
                "spawnedAt": "2020-01-01T00:00:00Z",
            },
            "wolf-hot": {
                "type": "wolf", "status": "active", "budget": 500,
                "spawnedAt": "2020-01-01T00:00:00Z",
            },
        }
        rebs = brain._decide_rebalances(
            signal, perf, self._base_config(minSpawnBudget=500), instances)
        if rebs:
            self.assertLessEqual(rebs[0]["amount"], 400)


class TestSaturationPressure(unittest.TestCase):
    """Slot saturation generates signal pressure even without scan history files."""

    def _load_signals(self):
        return _import_hyphenated(
            "cobra_signals", os.path.join(SCRIPTS_DIR, "cobra-signals.py"))

    def test_full_slots_generate_base_pressure(self):
        """2/2 slots used should produce pressure >= 25 even with no history files."""
        signals = self._load_signals()
        tmpdir = tempfile.mkdtemp()
        instance_id = "wolf-sat1"
        instance_data = {"type": "wolf", "slots": 2}

        os.makedirs(tmpdir, exist_ok=True)
        for asset in ("ZEC", "ENA"):
            with open(os.path.join(tmpdir, f"dsl-{asset}.json"), "w") as f:
                json.dump({"asset": asset, "active": True, "entryPrice": 100,
                           "currentPrice": 101, "direction": "SHORT"}, f)

        try:
            with patch.object(signals, "SIGNAL_PRESSURE_FILE",
                              os.path.join(tmpdir, "signals.json")), \
                 patch.object(signals, "get_instance_workspace", return_value=tmpdir), \
                 patch.object(signals, "get_instance_state_dir", return_value=tmpdir):
                result = signals.analyze_wolf_instance(instance_id, instance_data)
            self.assertEqual(result["slotsUsed"], 2)
            self.assertGreaterEqual(result["signalPressure"], 25)
        finally:
            shutil.rmtree(tmpdir)

    def test_saturation_cycles_escalate(self):
        """Consecutive full-slot cycles should increase pressure."""
        signals = self._load_signals()
        tmpdir = tempfile.mkdtemp()
        instance_id = "wolf-sat2"
        instance_data = {"type": "wolf", "slots": 2}

        os.makedirs(tmpdir, exist_ok=True)
        for asset in ("ZEC", "ENA"):
            with open(os.path.join(tmpdir, f"dsl-{asset}.json"), "w") as f:
                json.dump({"asset": asset, "active": True, "entryPrice": 100,
                           "currentPrice": 101, "direction": "SHORT"}, f)

        prev_signals = {
            "instances": {instance_id: {"_saturatedCycles": 4}},
        }
        prev_file = os.path.join(tmpdir, "signals.json")
        with open(prev_file, "w") as f:
            json.dump(prev_signals, f)

        try:
            with patch.object(signals, "SIGNAL_PRESSURE_FILE", prev_file), \
                 patch.object(signals, "get_instance_workspace", return_value=tmpdir), \
                 patch.object(signals, "get_instance_state_dir", return_value=tmpdir):
                result = signals.analyze_wolf_instance(instance_id, instance_data)
            self.assertEqual(result["_saturatedCycles"], 5)
            self.assertGreaterEqual(result["signalPressure"], 45)
        finally:
            shutil.rmtree(tmpdir)

    def test_empty_slots_reset_saturation(self):
        """When slots free up, saturation cycle counter resets to 0."""
        signals = self._load_signals()
        tmpdir = tempfile.mkdtemp()
        instance_id = "wolf-sat3"
        instance_data = {"type": "wolf", "slots": 2}

        os.makedirs(tmpdir, exist_ok=True)
        with open(os.path.join(tmpdir, "dsl-ZEC.json"), "w") as f:
            json.dump({"asset": "ZEC", "active": True, "entryPrice": 100,
                       "currentPrice": 101, "direction": "SHORT"}, f)

        prev_signals = {
            "instances": {instance_id: {"_saturatedCycles": 6}},
        }
        prev_file = os.path.join(tmpdir, "signals.json")
        with open(prev_file, "w") as f:
            json.dump(prev_signals, f)

        try:
            with patch.object(signals, "SIGNAL_PRESSURE_FILE", prev_file), \
                 patch.object(signals, "get_instance_workspace", return_value=tmpdir), \
                 patch.object(signals, "get_instance_state_dir", return_value=tmpdir):
                result = signals.analyze_wolf_instance(instance_id, instance_data)
            self.assertEqual(result["slotsUsed"], 1)
            self.assertEqual(result["_saturatedCycles"], 0)
            self.assertEqual(result["signalPressure"], 0)
        finally:
            shutil.rmtree(tmpdir)


class TestExpansionConfigWrite(unittest.TestCase):
    """expand_instance_slots must update wolf-strategies.json / tiger-config.json."""

    def _load_spawner(self):
        return _import_hyphenated(
            "cobra_spawner", os.path.join(SCRIPTS_DIR, "cobra-spawner.py"))

    def _patch_workspace(self, spawner, cobra_cfg, tmpdir, spawned_dir):
        """Context manager to patch all workspace-derived paths consistently."""
        return ExitStack_cm(
            patch.object(spawner, "SPAWNED_DIR", spawned_dir),
            patch.object(cobra_cfg, "WORKSPACE", tmpdir),
            patch.object(cobra_cfg, "SPAWNED_DIR", spawned_dir),
        )

    def test_wolf_strategies_json_updated(self):
        import cobra_config
        spawner = self._load_spawner()
        tmpdir = tempfile.mkdtemp()
        spawned_dir = os.path.join(tmpdir, "state", "cobra", "spawned")
        os.makedirs(spawned_dir)
        instance_ws = os.path.join(tmpdir, "instances", "wolf-expand1")
        os.makedirs(instance_ws, exist_ok=True)

        with open(os.path.join(spawned_dir, "wolf-expand1.json"), "w") as f:
            json.dump({
                "type": "wolf", "instanceId": "wolf-expand1",
                "wallet": "0xtest", "strategyId": "strat-1",
                "budget": 500, "slots": 2, "marginPerSlot": 150,
                "status": "active", "subagentLabel": "wolf-expand1",
                "defaultLeverage": 7,
            }, f)

        wolf_strat = {"strategies": {"wolf-expand1": {
            "name": "test", "wallet": "0xtest", "slots": 2,
            "marginPerSlot": 150, "enabled": True,
        }}}
        with open(os.path.join(instance_ws, "wolf-strategies.json"), "w") as f:
            json.dump(wolf_strat, f)

        try:
            with self._patch_workspace(spawner, cobra_config, tmpdir, spawned_dir):
                result = spawner.expand_instance_slots("wolf-expand1", 3, 50.0)
            self.assertTrue(result["success"])
            self.assertEqual(result["newSlots"], 3)

            with open(os.path.join(instance_ws, "wolf-strategies.json")) as f:
                updated = json.load(f)
            strat = updated["strategies"]["wolf-expand1"]
            self.assertEqual(strat["slots"], 3)
            self.assertEqual(strat["marginPerSlot"], 50.0)
        finally:
            shutil.rmtree(tmpdir)

    def test_tiger_config_json_updated(self):
        import cobra_config
        spawner = self._load_spawner()
        tmpdir = tempfile.mkdtemp()
        spawned_dir = os.path.join(tmpdir, "state", "cobra", "spawned")
        os.makedirs(spawned_dir)
        instance_ws = os.path.join(tmpdir, "instances", "tiger-expand1")
        os.makedirs(instance_ws, exist_ok=True)

        with open(os.path.join(spawned_dir, "tiger-expand1.json"), "w") as f:
            json.dump({
                "type": "tiger", "instanceId": "tiger-expand1",
                "wallet": "0xtest", "maxSlots": 2,
                "budget": 500, "status": "active",
            }, f)

        with open(os.path.join(instance_ws, "tiger-config.json"), "w") as f:
            json.dump({"maxSlots": 2, "strategyId": "x"}, f)

        try:
            with self._patch_workspace(spawner, cobra_config, tmpdir, spawned_dir):
                result = spawner.expand_instance_slots("tiger-expand1", 4, 37.5)
            self.assertTrue(result["success"])
            self.assertEqual(result["newSlots"], 4)

            with open(os.path.join(instance_ws, "tiger-config.json")) as f:
                updated = json.load(f)
            self.assertEqual(updated["maxSlots"], 4)
        finally:
            shutil.rmtree(tmpdir)

    def test_expansion_message_includes_session_key(self):
        spawner = self._load_spawner()
        msg = spawner.build_expansion_message("wolf-1", 2, 3, 50.0,
                                              session_key="agent:main:sub:abc")
        self.assertEqual(msg["params"]["sessionKey"], "agent:main:sub:abc")
        self.assertIn("3", msg["params"]["message"])

    def test_expansion_preserves_original_slots(self):
        """First expansion should record originalSlots for later reference."""
        import cobra_config
        spawner = self._load_spawner()
        tmpdir = tempfile.mkdtemp()
        spawned_dir = os.path.join(tmpdir, "state", "cobra", "spawned")
        os.makedirs(spawned_dir)
        instance_ws = os.path.join(tmpdir, "instances", "wolf-orig")
        os.makedirs(instance_ws, exist_ok=True)

        with open(os.path.join(spawned_dir, "wolf-orig.json"), "w") as f:
            json.dump({
                "type": "wolf", "instanceId": "wolf-orig",
                "wallet": "0xtest", "budget": 500, "slots": 2,
                "marginPerSlot": 150, "status": "active",
                "subagentLabel": "wolf-orig", "defaultLeverage": 7,
            }, f)

        try:
            with self._patch_workspace(spawner, cobra_config, tmpdir, spawned_dir):
                spawner.expand_instance_slots("wolf-orig", 3, 50.0)

            with open(os.path.join(spawned_dir, "wolf-orig.json")) as f:
                data = json.load(f)
            self.assertEqual(data["originalSlots"], 2)
            self.assertEqual(data["slots"], 3)
        finally:
            shutil.rmtree(tmpdir)


class TestKillVsKeepNetScore(unittest.TestCase):
    """Net score should be returned for debugging transparency."""

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def test_net_score_in_result(self):
        brain = self._load_brain()
        config = {"killVsKeep": {
            "signalPressureKillThreshold": 60, "idleHoursBeforeKill": 2,
            "avgFeePerTrade": 32, "maxDrawdownPct": 20,
        }}
        signal_data = {"instances": {"wolf-1": {
            "signalPressure": 20,
            "positionQuality": {"phase1": 0, "tier1": 0, "tier2plus": 0},
        }}}
        perf_data = {"instances": {"wolf-1": {
            "accountValue": 5000, "unrealizedPnl": 0, "utilization": 50,
            "drawdownFromPeak": 0, "tradeStats": {"activePositions": 1},
        }}}
        result = brain._evaluate_kill_vs_keep(
            "wolf-1", {"type": "wolf", "budget": 5000,
                       "spawnedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            signal_data, perf_data, config)
        self.assertIn("netScore", result)
        self.assertIn("keepScore", result)
        self.assertEqual(result["netScore"], result["score"] - result["keepScore"])


class TestKillROEThreshold(unittest.TestCase):
    """Kill-vs-keep should only kill at -3% ROE, not at minor fluctuation."""

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def test_no_kill_at_minor_negative_roe(self):
        """Avg ROE -0.4% should NOT trigger a kill (normal fluctuation)."""
        brain = self._load_brain()
        config = {"killVsKeep": {
            "signalPressureKillThreshold": 60, "idleHoursBeforeKill": 2,
            "avgFeePerTrade": 32, "maxDrawdownPct": 20,
        }}
        signal_data = {"instances": {"wolf-x": {
            "signalPressure": 20,
            "positionQuality": {"phase1": 2, "tier1": 0, "tier2plus": 0},
            "avgPositionROE": -0.4,
            "slotsUsed": 2,
        }}}
        perf_data = {"instances": {"wolf-x": {
            "accountValue": 490, "unrealizedPnl": -10, "utilization": 60,
            "drawdownFromPeak": 2, "tradeStats": {"activePositions": 2},
        }}}
        result = brain._evaluate_kill_vs_keep(
            "wolf-x", {"type": "wolf", "budget": 500,
                        "spawnedAt": "2020-01-01T00:00:00Z"},
            signal_data, perf_data, config)
        self.assertNotEqual(result["decision"], "KILL")

    def test_kills_at_significant_negative_roe(self):
        """Avg ROE -5% with all Phase 1 should trigger kill score boost."""
        brain = self._load_brain()
        config = {"killVsKeep": {
            "signalPressureKillThreshold": 60, "idleHoursBeforeKill": 2,
            "avgFeePerTrade": 32, "maxDrawdownPct": 20,
        }}
        signal_data = {"instances": {"wolf-y": {
            "signalPressure": 70,
            "positionQuality": {"phase1": 2, "tier1": 0, "tier2plus": 0},
            "avgPositionROE": -5.0,
            "slotsUsed": 2,
        }}}
        perf_data = {"instances": {"wolf-y": {
            "accountValue": 450, "unrealizedPnl": -50, "utilization": 60,
            "drawdownFromPeak": 10, "tradeStats": {"activePositions": 2},
        }}}
        result = brain._evaluate_kill_vs_keep(
            "wolf-y", {"type": "wolf", "budget": 500,
                        "spawnedAt": "2020-01-01T00:00:00Z"},
            signal_data, perf_data, config)
        kill_reasons = [r for r in result["reasons"] if "Phase 1" in r]
        self.assertTrue(len(kill_reasons) > 0)


class TestRebalanceHaltedInstance(unittest.TestCase):
    """Halted instances should be rebalanced immediately regardless of age."""

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def _base_config(self, **overrides):
        cfg = {
            "totalBudget": 10000, "minSpawnBudget": 500,
            "maxWolves": 2, "maxTigers": 1, "reservePct": 15,
            "killVsKeep": {"signalPressureSpawnThreshold": 50},
        }
        cfg.update(overrides)
        return cfg

    def test_halted_tiger_rebalanced_immediately(self):
        """Tiger that halted itself should be rebalanced without waiting for idle hours."""
        brain = self._load_brain()
        from cobra_config import utc_now
        signal = {
            "instances": {
                "tiger-halted": {"signalPressure": 0, "halted": True,
                                 "haltReason": "Target requires 999%/day"},
                "wolf-ok": {"signalPressure": 25},
            },
        }
        perf = {
            "instances": {
                "tiger-halted": {"accountValue": 500, "utilization": 0},
                "wolf-ok": {"accountValue": 500, "utilization": 60},
            },
        }
        instances = {
            "tiger-halted": {
                "type": "tiger", "status": "active", "budget": 500,
                "spawnedAt": utc_now(),
            },
            "wolf-ok": {
                "type": "wolf", "status": "active", "budget": 500,
                "spawnedAt": "2020-01-01T00:00:00Z",
            },
        }
        rebs = brain._decide_rebalances(signal, perf, self._base_config(), instances)
        self.assertEqual(len(rebs), 1)
        self.assertEqual(rebs[0]["fromInstance"], "tiger-halted")
        self.assertIn("HALTED", rebs[0]["reason"])

    def test_non_halted_still_needs_pressure(self):
        """Non-halted idle instance still requires cross-type pressure >= 35."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "tiger-idle": {"signalPressure": 0, "halted": False},
                "wolf-quiet": {"signalPressure": 20},
            },
        }
        perf = {
            "instances": {
                "tiger-idle": {"accountValue": 1000, "utilization": 0},
                "wolf-quiet": {"accountValue": 500, "utilization": 10},
            },
        }
        instances = {
            "tiger-idle": {
                "type": "tiger", "status": "active", "budget": 1000,
                "spawnedAt": "2020-01-01T00:00:00Z",
            },
            "wolf-quiet": {
                "type": "wolf", "status": "active", "budget": 500,
                "spawnedAt": "2020-01-01T00:00:00Z",
            },
        }
        rebs = brain._decide_rebalances(signal, perf, self._base_config(), instances)
        self.assertEqual(len(rebs), 0)

    def test_rebalance_at_pressure_35(self):
        """Cross-type pressure >= 35 should trigger rebalance (lowered from 50)."""
        brain = self._load_brain()
        signal = {
            "instances": {
                "tiger-idle": {"signalPressure": 0},
                "wolf-hot": {"signalPressure": 40},
            },
        }
        perf = {
            "instances": {
                "tiger-idle": {"accountValue": 1000, "utilization": 0},
                "wolf-hot": {"accountValue": 500, "utilization": 60},
            },
        }
        instances = {
            "tiger-idle": {
                "type": "tiger", "status": "active", "budget": 1000,
                "spawnedAt": "2020-01-01T00:00:00Z",
            },
            "wolf-hot": {
                "type": "wolf", "status": "active", "budget": 500,
                "spawnedAt": "2020-01-01T00:00:00Z",
            },
        }
        rebs = brain._decide_rebalances(signal, perf, self._base_config(), instances)
        self.assertEqual(len(rebs), 1)


class TestSignalPressureScoring(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cobra-signal-test-")
        self.signals = _import_hyphenated(
            "cobra_signals", os.path.join(SCRIPTS_DIR, "cobra-signals.py"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_json(self, filename, data):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w") as f:
            json.dump(data, f)

    def test_wolf_missed_first_jumps(self):
        """Each missed FIRST_JUMP (not currently traded) adds 15 to signal pressure."""
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_json("emerging-movers-history.json", [
            {"timestamp": now_ts, "markets": [
                {"isFirstJump": True, "asset": "HYPE"},
                {"isFirstJump": True, "asset": "SOL"},
                {"isFirstJump": True, "asset": "WIF"},
            ]}
        ])
        with patch.object(self.signals, "get_instance_workspace", return_value=self.tmpdir), \
             patch.object(self.signals, "get_instance_state_dir", return_value=self.tmpdir):
            result = self.signals.analyze_wolf_instance("wolf-1", {"slots": 3})
        self.assertEqual(result["missedFirstJumps1h"], 3)
        self.assertEqual(result["signalPressure"], 45)

    def test_wolf_active_positions_not_counted_as_missed(self):
        """Only active DSL positions are excluded from missed signal counts."""
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_json("emerging-movers-history.json", [
            {"timestamp": now_ts, "markets": [
                {"isFirstJump": True, "asset": "HYPE"},
                {"isFirstJump": True, "asset": "SOL"},
                {"isFirstJump": True, "asset": "ZEC"},
            ]}
        ])
        self._write_json("dsl-HYPE.json", {
            "active": True, "asset": "HYPE", "direction": "LONG",
            "entryPrice": 100, "currentPrice": 105, "phase": 1,
        })
        self._write_json("dsl-ZEC.json", {
            "active": False, "asset": "ZEC", "direction": "SHORT",
            "entryPrice": 200, "currentPrice": 201, "closedBy": "cobra-kill",
        })
        with patch.object(self.signals, "get_instance_workspace", return_value=self.tmpdir), \
             patch.object(self.signals, "get_instance_state_dir", return_value=self.tmpdir):
            result = self.signals.analyze_wolf_instance("wolf-1", {"slots": 3})
        # HYPE excluded (active), ZEC counted (closed), SOL counted
        self.assertEqual(result["missedFirstJumps1h"], 2)
        self.assertGreaterEqual(result["signalPressure"], 30)

    def test_wolf_slots_full_bonus(self):
        """Slots full adds base +25 plus saturation escalation on top of FIRST_JUMP pressure."""
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_json("emerging-movers-history.json", [
            {"timestamp": now_ts, "markets": [{"isFirstJump": True}]}
        ])
        for asset in ("ETH", "SOL"):
            self._write_json(f"dsl-{asset}.json", {
                "active": True, "asset": asset, "direction": "LONG",
                "entryPrice": 100, "currentPrice": 105,
                "phase": 1, "currentTierIndex": None,
            })
        with patch.object(self.signals, "get_instance_workspace", return_value=self.tmpdir), \
             patch.object(self.signals, "get_instance_state_dir", return_value=self.tmpdir):
            result = self.signals.analyze_wolf_instance("wolf-1", {"slots": 2})
        self.assertEqual(result["slotsUsed"], 2)
        # 15 (FIRST_JUMP) + 25 (slot sat base) + 5 (1 cycle * 5) = 45
        self.assertEqual(result["signalPressure"], 45)

    def test_tiger_high_confluence(self):
        """Prescreened candidates with score >= 65 count as high-confluence."""
        candidates = [
            {"score": 75}, {"score": 80}, {"score": 70},
            {"score": 40}, {"score": 55},
        ]
        self._write_json("prescreened.json", {"candidates": candidates})
        with patch.object(self.signals, "get_instance_workspace", return_value=self.tmpdir), \
             patch.object(self.signals, "get_instance_state_dir", return_value=self.tmpdir):
            result = self.signals.analyze_tiger_instance("tiger-1", {"maxSlots": 3})
        self.assertEqual(result["highConfluenceCount"], 3)
        self.assertEqual(result["signalPressure"], 30)

    def test_tiger_prescreener_density_bonus(self):
        """Density >= 35 adds (density - 25) * 3 to pressure."""
        candidates = [{"score": 50} for _ in range(40)]
        self._write_json("prescreened.json", {"candidates": candidates})
        with patch.object(self.signals, "get_instance_workspace", return_value=self.tmpdir), \
             patch.object(self.signals, "get_instance_state_dir", return_value=self.tmpdir):
            result = self.signals.analyze_tiger_instance("tiger-1", {"maxSlots": 3})
        self.assertEqual(result["prescreenerDensity"], 40)
        self.assertEqual(result["highConfluenceCount"], 0)
        # (40 - 25) * 3 = 45
        self.assertEqual(result["signalPressure"], 45)

    def test_tiger_normal_density_no_bonus(self):
        """Density of 30 (normal) should NOT trigger density bonus."""
        candidates = [{"score": 50} for _ in range(30)]
        self._write_json("prescreened.json", {"candidates": candidates})
        with patch.object(self.signals, "get_instance_workspace", return_value=self.tmpdir), \
             patch.object(self.signals, "get_instance_state_dir", return_value=self.tmpdir):
            result = self.signals.analyze_tiger_instance("tiger-1", {"maxSlots": 3})
        self.assertEqual(result["prescreenerDensity"], 30)
        self.assertEqual(result["signalPressure"], 0)

    def test_pressure_capped_at_100(self):
        """Signal pressure never exceeds 100."""
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        markets = [{"isFirstJump": True} for _ in range(10)]
        self._write_json("emerging-movers-history.json", [
            {"timestamp": now_ts, "markets": markets}
        ])
        with patch.object(self.signals, "get_instance_workspace", return_value=self.tmpdir), \
             patch.object(self.signals, "get_instance_state_dir", return_value=self.tmpdir):
            result = self.signals.analyze_wolf_instance("wolf-1", {"slots": 3})
        self.assertEqual(result["signalPressure"], 100)


class TestBootstrapSignalScan(unittest.TestCase):
    """Bootstrap scanning reads shared workspace when no instances exist."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cobra-bootstrap-test-")
        self.signals = _import_hyphenated(
            "cobra_signals", os.path.join(SCRIPTS_DIR, "cobra-signals.py"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_json(self, filename, data):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w") as f:
            json.dump(data, f)

    def test_empty_workspace_returns_zero(self):
        with patch.object(self.signals, "get_shared_workspace", return_value=self.tmpdir):
            result = self.signals._bootstrap_signal_scan()
        self.assertEqual(result["signalPressure"], 0)
        self.assertEqual(result["type"], "bootstrap")

    def test_reads_emerging_movers_from_shared(self):
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_json("emerging-movers-history.json", [
            {"timestamp": now_ts, "markets": [
                {"isFirstJump": True, "asset": "HYPE"},
                {"isFirstJump": True, "asset": "SOL"},
            ]}
        ])
        with patch.object(self.signals, "get_shared_workspace", return_value=self.tmpdir):
            result = self.signals._bootstrap_signal_scan()
        self.assertEqual(result["missedFirstJumps1h"], 2)
        self.assertEqual(result["signalPressure"], 30)

    def test_reads_prescreened_from_shared(self):
        candidates = [{"score": 75}, {"score": 80}, {"score": 50}]
        self._write_json("prescreened.json", {"candidates": candidates})
        with patch.object(self.signals, "get_shared_workspace", return_value=self.tmpdir):
            result = self.signals._bootstrap_signal_scan()
        self.assertEqual(result["highConfluenceCount"], 2)
        self.assertEqual(result["signalPressure"], 20)

    def test_run_bootstrap_mode_output(self):
        """run() in bootstrap mode sets bootstrapMode flag."""
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_json("emerging-movers-history.json", [
            {"timestamp": now_ts, "markets": [{"isFirstJump": True}]}
        ])
        with patch.object(self.signals, "load_spawned_instances", return_value={}), \
             patch.object(self.signals, "get_shared_workspace", return_value=self.tmpdir), \
             patch.object(self.signals, "SIGNAL_PRESSURE_FILE",
                          os.path.join(self.tmpdir, "cobra-signals.json")), \
             patch("viper_gate.output_and_track"):
            self.signals._run_inner()
        result = json.load(open(os.path.join(self.tmpdir, "cobra-signals.json")))
        self.assertTrue(result.get("bootstrapMode"))
        self.assertIn("_bootstrap", result["instances"])

    def test_run_error_handling(self):
        """run() should catch exceptions and output error JSON."""
        captured = {}

        def fake_output(data):
            captured["data"] = data

        with patch.object(self.signals, "load_spawned_instances",
                          side_effect=RuntimeError("test error")), \
             patch("cobra_config.output", fake_output), \
             patch.object(self.signals, "SIGNAL_PRESSURE_FILE",
                          os.path.join(self.tmpdir, "cobra-signals.json")):
            self.signals.run()
        self.assertIn("error", captured["data"])
        self.assertEqual(captured["data"]["globalSignalPressure"], 0)


class TestRegimeClassification(unittest.TestCase):
    """Regime classifier: ADX/ATR/price-change based market regime detection."""

    def _load_regime(self):
        return _import_hyphenated(
            "cobra_regime", os.path.join(SCRIPTS_DIR, "cobra-regime.py"))

    def _make_candles(self, n, base_close=50000, trend_pct=0, volatility=0):
        """Generate synthetic candles with optional trend and volatility."""
        candles = []
        for i in range(n):
            c = base_close * (1 + trend_pct * i / n)
            spread = base_close * volatility / 100
            candles.append({
                "h": c + spread, "l": c - spread,
                "c": c, "o": c - spread * 0.5,
            })
        return candles

    def test_strong_trending(self):
        """High ADX + expanding ATR = TRENDING."""
        regime = self._load_regime()
        candles = self._make_candles(60, trend_pct=0.10, volatility=2)
        result = regime.classify_regime(candles, [], 0)
        self.assertIn(result["regime"], ("TRENDING", "VOLATILE"))

    def test_ranging_low_adx(self):
        """Flat market with very low movement = RANGING."""
        regime = self._load_regime()
        candles = self._make_candles(60, trend_pct=0.0, volatility=0.1)
        result = regime.classify_regime(candles, [], 0)
        self.assertEqual(result["regime"], "RANGING")

    def test_volatile_big_price_drop(self):
        """Large single-candle drop triggers VOLATILE."""
        regime = self._load_regime()
        candles = self._make_candles(59, volatility=1)
        candles.append({"h": 50000, "l": 46000, "c": 46500, "o": 50000})
        candles_1h = [{"h": 50000, "l": 46000, "c": 46500, "o": 50000},
                      {"h": 50000, "l": 50000, "c": 50000, "o": 50000}]
        result = regime.classify_regime(candles, candles_1h, 0)
        self.assertEqual(result["regime"], "VOLATILE")

    def test_extreme_funding_boosts_volatile_confidence(self):
        """Extreme funding rate should boost VOLATILE confidence."""
        regime = self._load_regime()
        candles = self._make_candles(59, volatility=1)
        candles.append({"h": 50000, "l": 46000, "c": 46500, "o": 50000})
        result = regime.classify_regime(candles, [], 0.08)
        self.assertEqual(result["regime"], "VOLATILE")
        self.assertGreater(result["confidence"], 0.7)

    def test_allocation_trending(self):
        regime = self._load_regime()
        alloc = regime.get_allocation("TRENDING")
        self.assertEqual(alloc["wolf"], 60)
        self.assertEqual(alloc["tiger"], 25)
        self.assertEqual(alloc["reserve"], 15)

    def test_allocation_ranging(self):
        regime = self._load_regime()
        alloc = regime.get_allocation("RANGING")
        self.assertEqual(alloc["wolf"], 20)
        self.assertEqual(alloc["tiger"], 50)

    def test_allocation_unknown(self):
        regime = self._load_regime()
        alloc = regime.get_allocation("UNKNOWN")
        self.assertEqual(alloc["reserve"], 50)

    def test_zero_candles_returns_zero(self):
        regime = self._load_regime()
        self.assertEqual(regime.compute_adx([]), 0)
        self.assertEqual(regime.compute_atr([]), (0, 0))
        self.assertEqual(regime.compute_btc_change([]), 0)


class TestRegimeHysteresis(unittest.TestCase):
    """Regime hysteresis prevents flapping on low-confidence transitions."""

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def test_high_confidence_switches_immediately(self):
        brain = self._load_brain()
        state = {"regime": "RANGING"}
        regime, conf = brain._apply_regime_hysteresis("TRENDING", 0.80, state)
        self.assertEqual(regime, "TRENDING")

    def test_low_confidence_defers_first_time(self):
        brain = self._load_brain()
        state = {"regime": "RANGING"}
        regime, conf = brain._apply_regime_hysteresis("TRENDING", 0.50, state)
        self.assertEqual(regime, "RANGING")
        self.assertEqual(state["_pendingRegime"], "TRENDING")

    def test_low_confidence_confirms_on_second_reading(self):
        brain = self._load_brain()
        state = {"regime": "RANGING", "_pendingRegime": "TRENDING"}
        regime, conf = brain._apply_regime_hysteresis("TRENDING", 0.50, state)
        self.assertEqual(regime, "TRENDING")

    def test_same_regime_no_change(self):
        brain = self._load_brain()
        state = {"regime": "TRENDING"}
        regime, conf = brain._apply_regime_hysteresis("TRENDING", 0.40, state)
        self.assertEqual(regime, "TRENDING")

    def test_same_regime_clears_stale_pending(self):
        """When market confirms current regime, stale _pendingRegime must be cleared."""
        brain = self._load_brain()
        state = {"regime": "TRENDING", "_pendingRegime": "MEAN_REVERTING"}
        regime, conf = brain._apply_regime_hysteresis("TRENDING", 0.50, state)
        self.assertEqual(regime, "TRENDING")
        self.assertNotIn("_pendingRegime", state)

    def test_stale_pending_does_not_cause_false_switch(self):
        """Low-confidence signal matching stale pending must not switch after revert."""
        brain = self._load_brain()
        state = {"regime": "TRENDING"}
        regime, _ = brain._apply_regime_hysteresis("MEAN_REVERTING", 0.40, state)
        self.assertEqual(regime, "TRENDING")
        self.assertEqual(state["_pendingRegime"], "MEAN_REVERTING")
        regime, _ = brain._apply_regime_hysteresis("TRENDING", 0.50, state)
        self.assertEqual(regime, "TRENDING")
        self.assertNotIn("_pendingRegime", state)
        regime, _ = brain._apply_regime_hysteresis("MEAN_REVERTING", 0.40, state)
        self.assertEqual(regime, "TRENDING")


class TestTrappedCapitalAccounting(unittest.TestCase):
    """Spawn decisions must account for capital trapped in killed wallets."""

    def _load_brain(self):
        return _import_hyphenated(
            "cobra_brain", os.path.join(SCRIPTS_DIR, "cobra-brain.py"))

    def _base_config(self, **overrides):
        cfg = {
            "totalBudget": 10000, "minSpawnBudget": 500,
            "maxWolves": 2, "maxTigers": 1, "reservePct": 15,
            "killVsKeep": {"signalPressureSpawnThreshold": 50},
        }
        cfg.update(overrides)
        return cfg

    def test_trapped_capital_reduces_idle(self):
        """Killed instances with unrecovered funds reduce available capital."""
        brain = self._load_brain()
        import cobra_config

        killed_dir = os.path.join(_TEST_WORKSPACE, "state", "cobra", "spawned")
        os.makedirs(killed_dir, exist_ok=True)
        killed_path = os.path.join(killed_dir, "wolf-killed.json")
        with open(killed_path, "w") as f:
            json.dump({"status": "killed", "type": "wolf", "budget": 4000,
                       "finalValue": 3800, "fundsRecovered": False}, f)

        try:
            regime = {"regime": "TRENDING", "allocation": {"wolf": 60, "tiger": 25, "reserve": 15}}
            signal = {"globalSignalPressure": 10}
            spawns = brain._decide_spawns(regime, signal, {}, self._base_config(), {})
            total_budget = sum(s["budget"] for s in spawns)
            self.assertLess(total_budget, 5000)
        finally:
            os.unlink(killed_path)


class TestParseClearinghouse(unittest.TestCase):
    """parse_clearinghouse handles both flat and nested response formats."""

    def test_nested_format(self):
        import cobra_config
        ch = {"main": {
            "marginSummary": {"accountValue": "1000", "totalMarginUsed": "200"},
            "assetPositions": [{"coin": "BTC", "szi": "0.01"}],
        }}
        ms, positions = cobra_config.parse_clearinghouse(ch)
        self.assertEqual(ms["accountValue"], "1000")
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["coin"], "BTC")

    def test_flat_format(self):
        import cobra_config
        ch = {"accountValue": 500, "positions": [{"coin": "ETH", "szi": "1"}]}
        ms, positions = cobra_config.parse_clearinghouse(ch)
        self.assertEqual(ms["accountValue"], 500)
        self.assertEqual(len(positions), 1)

    def test_unwraps_position_objects(self):
        import cobra_config
        ch = {"main": {
            "marginSummary": {"accountValue": "1000"},
            "assetPositions": [
                {"type": "oneWay", "position": {"coin": "SOL", "szi": "5"}},
            ],
        }}
        ms, positions = cobra_config.parse_clearinghouse(ch)
        self.assertEqual(positions[0]["coin"], "SOL")

    def test_none_input(self):
        import cobra_config
        ms, positions = cobra_config.parse_clearinghouse(None)
        self.assertEqual(ms, {})
        self.assertEqual(positions, [])

    def test_empty_input(self):
        import cobra_config
        ms, positions = cobra_config.parse_clearinghouse({})
        self.assertEqual(positions, [])


class TestTigerScriptsResolution(unittest.TestCase):
    """Tiger scripts dir should resolve tiger-strategy before tiger."""

    def test_tiger_strategy_path_tried_first(self):
        spawner = _import_hyphenated(
            "cobra_spawner", os.path.join(SCRIPTS_DIR, "cobra-spawner.py"))
        tmpdir = tempfile.mkdtemp()
        try:
            ts_dir = os.path.join(tmpdir, "skills", "tiger-strategy", "scripts")
            os.makedirs(ts_dir)
            with patch.object(spawner, "WORKSPACE", tmpdir):
                result = spawner._resolve_scripts_dir("tiger")
            self.assertIn("tiger-strategy", result)
        finally:
            shutil.rmtree(tmpdir)


class TestInstanceStateDir(unittest.TestCase):
    """get_instance_state_dir must resolve inside instance workspace."""

    def test_wolf_state_dir_by_instance_id(self):
        """Wolf state dir is {instance_workspace}/state/{instance_id}/."""
        import cobra_config
        tmpdir = tempfile.mkdtemp()
        try:
            iid = "wolf-abc123"
            state_path = os.path.join(tmpdir, "instances", iid, "state", iid)
            os.makedirs(state_path)
            with patch.object(cobra_config, "WORKSPACE", tmpdir):
                result = cobra_config.get_instance_state_dir(iid, "wolf")
            self.assertEqual(result, state_path)
        finally:
            shutil.rmtree(tmpdir)

    def test_tiger_state_dir_by_strategy_id(self):
        """Tiger state dir resolves to first subdir under state/ (strategyId)."""
        import cobra_config
        tmpdir = tempfile.mkdtemp()
        try:
            iid = "tiger-def456"
            strategy_uuid = "def456-1234-5678-9abc-ffffffffffff"
            state_path = os.path.join(tmpdir, "instances", iid, "state", strategy_uuid)
            os.makedirs(state_path)
            with patch.object(cobra_config, "WORKSPACE", tmpdir):
                result = cobra_config.get_instance_state_dir(iid, "tiger")
            self.assertEqual(result, state_path)
        finally:
            shutil.rmtree(tmpdir)

    def test_fallback_when_no_state_subdir(self):
        """Falls back to state/{instance_id} when no subdirectory exists."""
        import cobra_config
        tmpdir = tempfile.mkdtemp()
        try:
            iid = "wolf-nostate"
            with patch.object(cobra_config, "WORKSPACE", tmpdir):
                result = cobra_config.get_instance_state_dir(iid, "wolf")
            self.assertTrue(result.endswith(os.path.join("state", iid)))
        finally:
            shutil.rmtree(tmpdir)


class TestFindScanFile(unittest.TestCase):
    """_find_scan_file must check history/ subdirectory for wolf scan data."""

    def _load_signals(self):
        return _import_hyphenated(
            "cobra_signals", os.path.join(SCRIPTS_DIR, "cobra-signals.py"))

    def test_finds_file_at_workspace_root(self):
        signals = self._load_signals()
        tmpdir = tempfile.mkdtemp()
        try:
            fpath = os.path.join(tmpdir, "scan-history.json")
            with open(fpath, "w") as f:
                json.dump([], f)
            result = signals._find_scan_file(tmpdir, "scan-history.json")
            self.assertEqual(result, fpath)
        finally:
            shutil.rmtree(tmpdir)

    def test_finds_file_in_history_subdir(self):
        """Wolf puts scan-history.json in history/ — must find it."""
        signals = self._load_signals()
        tmpdir = tempfile.mkdtemp()
        try:
            hist_dir = os.path.join(tmpdir, "history")
            os.makedirs(hist_dir)
            fpath = os.path.join(hist_dir, "scan-history.json")
            with open(fpath, "w") as f:
                json.dump([], f)
            result = signals._find_scan_file(tmpdir, "scan-history.json")
            self.assertEqual(result, fpath)
        finally:
            shutil.rmtree(tmpdir)

    def test_falls_back_to_shared_workspace(self):
        signals = self._load_signals()
        tmpdir = tempfile.mkdtemp()
        shared = tempfile.mkdtemp()
        try:
            fpath = os.path.join(shared, "emerging-movers-history.json")
            with open(fpath, "w") as f:
                json.dump([], f)
            with patch.object(signals, "get_shared_workspace", return_value=shared):
                result = signals._find_scan_file(tmpdir, "emerging-movers-history.json")
            self.assertEqual(result, fpath)
        finally:
            shutil.rmtree(tmpdir)
            shutil.rmtree(shared)


class TestCreateAndFundWallet(unittest.TestCase):
    """Wallet creation must unwrap MCP response wrapper and handle async status."""

    def _load_spawner(self):
        return _import_hyphenated(
            "cobra_spawner", os.path.join(SCRIPTS_DIR, "cobra-spawner.py"))

    def test_unwraps_strategy_wrapper(self):
        """MCP returns {strategy: {strategyWalletAddress, id, ...}} — must unwrap."""
        spawner = self._load_spawner()
        create_resp = {
            "strategy": {
                "strategyWalletAddress": "0xWALLET_ABC",
                "id": "uuid-123",
                "status": "ACTIVE",
            }
        }
        ch_resp = {"main": {"marginSummary": {"accountValue": "5000"}}}
        with patch.object(spawner, "mcporter_call", return_value=create_resp), \
             patch.object(spawner, "mcporter_call_safe", return_value=None), \
             patch.object(spawner, "get_clearinghouse_state", return_value=ch_resp), \
             patch.object(spawner, "parse_clearinghouse",
                          return_value=({"accountValue": "5000"}, [])), \
             patch.object(spawner, "time") as mock_time:
            mock_time.sleep = MagicMock()
            result = spawner._create_and_fund_wallet(5000, "test-strat")
        self.assertIsInstance(result, tuple)
        wallet, sid = result
        self.assertEqual(wallet, "0xWALLET_ABC")
        self.assertEqual(sid, "uuid-123")

    def test_polls_on_create_wallet_status(self):
        """When MCP returns status=CREATE_WALLET, must poll until ACTIVE."""
        spawner = self._load_spawner()
        create_resp = {
            "strategy": {
                "strategyWalletAddress": "",
                "id": "uuid-456",
                "status": "CREATE_WALLET",
            }
        }
        ch_resp = {"main": {"marginSummary": {"accountValue": "3000"}}}

        with patch.object(spawner, "mcporter_call", return_value=create_resp), \
             patch.object(spawner, "mcporter_call_safe", side_effect=[
                 {"strategies": [{"id": "uuid-456", "status": "CREATE_WALLET"}]},
                 {"strategies": [{"id": "uuid-456", "status": "ACTIVE",
                                  "strategyWalletAddress": "0xPOLLED"}]},
             ]), \
             patch.object(spawner, "get_clearinghouse_state", return_value=ch_resp), \
             patch.object(spawner, "parse_clearinghouse",
                          return_value=({"accountValue": "3000"}, [])), \
             patch.object(spawner, "time") as mock_time, \
             patch.object(spawner, "_STRATEGY_POLL_MAX_WAIT", 10):
            mock_time.time = MagicMock(side_effect=[0, 1, 2])
            mock_time.sleep = MagicMock()
            result = spawner._create_and_fund_wallet(3000, "test-poll")
        self.assertIsInstance(result, tuple)
        self.assertEqual(result[0], "0xPOLLED")

    def test_poll_detects_failed_status(self):
        """When strategy creation fails, poll returns None."""
        spawner = self._load_spawner()
        list_resp = {"strategies": [{"id": "uuid-789", "status": "FAILED"}]}
        with patch.object(spawner, "mcporter_call_safe", return_value=list_resp), \
             patch.object(spawner, "time") as mock_time, \
             patch.object(spawner, "_STRATEGY_POLL_MAX_WAIT", 5):
            mock_time.time = MagicMock(side_effect=[0, 1])
            mock_time.sleep = MagicMock()
            result = spawner._poll_strategy_wallet("uuid-789")
        self.assertIsNone(result)

    def test_verification_uses_50pct_threshold(self):
        """Wallet with >50% of budget funded passes verification."""
        spawner = self._load_spawner()
        create_resp = {
            "strategy": {
                "strategyWalletAddress": "0xOK",
                "id": "uuid-thr",
                "status": "ACTIVE",
            }
        }
        with patch.object(spawner, "mcporter_call", return_value=create_resp), \
             patch.object(spawner, "mcporter_call_safe", return_value=None), \
             patch.object(spawner, "get_clearinghouse_state", return_value={}), \
             patch.object(spawner, "parse_clearinghouse",
                          return_value=({"accountValue": "2600"}, [])), \
             patch.object(spawner, "time") as mock_time:
            mock_time.sleep = MagicMock()
            result = spawner._create_and_fund_wallet(5000, "test-thr")
        self.assertIsInstance(result, tuple)

    def test_verification_fails_below_50pct(self):
        """Wallet with <50% of budget funded must fail and cleanup."""
        spawner = self._load_spawner()
        create_resp = {
            "strategy": {
                "strategyWalletAddress": "0xLOW",
                "id": "uuid-low",
                "status": "ACTIVE",
            }
        }
        call_n = {"n": 0}

        def mock_parse(ch):
            call_n["n"] += 1
            if call_n["n"] <= 1:
                return {"accountValue": "100"}, []
            return {"accountValue": "200"}, []

        with patch.object(spawner, "mcporter_call", return_value=create_resp), \
             patch.object(spawner, "mcporter_call_safe", return_value=None), \
             patch.object(spawner, "get_clearinghouse_state", return_value={}), \
             patch.object(spawner, "parse_clearinghouse", side_effect=mock_parse), \
             patch.object(spawner, "time") as mock_time:
            mock_time.sleep = MagicMock()
            result = spawner._create_and_fund_wallet(5000, "test-low")
        self.assertIsInstance(result, dict)
        self.assertFalse(result["success"])
        self.assertIn("Funding verification failed", result["error"])


class TestKillFlowMCPFailure(unittest.TestCase):
    """TIGER kill with MCP failure must set kill_pending, not killed."""

    def setUp(self):
        self.spawner = _import_hyphenated(
            "cobra_spawner", os.path.join(SCRIPTS_DIR, "cobra-spawner.py"))
        spawned_dir = os.path.join(_TEST_WORKSPACE, "state", "cobra", "spawned")
        os.makedirs(spawned_dir, exist_ok=True)

    def _write_instance(self, instance_id, data):
        spawned_dir = os.path.join(_TEST_WORKSPACE, "state", "cobra", "spawned")
        path = os.path.join(spawned_dir, f"{instance_id}.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def _cleanup_instance(self, instance_id):
        spawned_dir = os.path.join(_TEST_WORKSPACE, "state", "cobra", "spawned")
        path = os.path.join(spawned_dir, f"{instance_id}.json")
        if os.path.exists(path):
            os.unlink(path)

    def test_tiger_kill_mcp_down_sets_kill_pending(self):
        """When MCP is unreachable during TIGER kill, status must be kill_pending."""
        self._write_instance("tiger-killtest", {
            "type": "tiger", "instanceId": "tiger-killtest",
            "wallet": "0xfake", "budget": 3000, "status": "active",
            "spawnedAt": "2026-02-28T10:00:00Z", "cronNames": [],
        })
        try:
            with patch.object(self.spawner, "get_clearinghouse_state", return_value=None), \
                 patch.object(self.spawner, "mcporter_call_safe", return_value=None):
                result = self.spawner.kill_instance("tiger-killtest", reason="test")
            self.assertTrue(result["success"])
            self.assertEqual(result["killStatus"], "kill_pending")
        finally:
            self._cleanup_instance("tiger-killtest")

    def test_wolf_kill_mcp_down_sets_kill_pending(self):
        """WOLF kill now uses clearinghouse too — MCP down = kill_pending."""
        self._write_instance("wolf-killtest", {
            "type": "wolf", "instanceId": "wolf-killtest",
            "wallet": "0xfake", "budget": 3000, "status": "active",
            "spawnedAt": "2026-02-28T10:00:00Z", "cronNames": [],
        })
        try:
            with patch.object(self.spawner, "get_clearinghouse_state", return_value=None), \
                 patch.object(self.spawner, "mcporter_call_safe", return_value=None):
                result = self.spawner.kill_instance("wolf-killtest", reason="test")
            self.assertTrue(result["success"])
            self.assertEqual(result["killStatus"], "kill_pending")
        finally:
            self._cleanup_instance("wolf-killtest")

    def test_clearinghouse_down_preserves_dsl_files(self):
        """When clearinghouse is unavailable, DSL files must NOT be deactivated."""
        state_dir = os.path.join(_TEST_WORKSPACE, "state", "tiger-chtest")
        os.makedirs(state_dir, exist_ok=True)
        dsl_path = os.path.join(state_dir, "dsl-ETH.json")
        with open(dsl_path, "w") as f:
            json.dump({"active": True, "asset": "ETH", "direction": "LONG"}, f)
        try:
            with patch.object(self.spawner, "get_clearinghouse_state", return_value=None), \
                 patch.object(self.spawner, "get_instance_state_dir", return_value=state_dir):
                results, ch = self.spawner._close_positions_via_clearinghouse(
                    "0xfake", "tiger-chtest", "tiger")
            self.assertIsNone(ch)
            self.assertEqual(results, [])
            with open(dsl_path) as f:
                state = json.load(f)
            self.assertTrue(state["active"])
        finally:
            shutil.rmtree(state_dir, ignore_errors=True)

    def test_kill_with_close_errors_sets_kill_pending(self):
        """When some position closes fail, kill_pending is set for retry."""
        self._write_instance("tiger-closefail", {
            "type": "tiger", "instanceId": "tiger-closefail",
            "wallet": "0xfake", "budget": 3000, "status": "active",
            "spawnedAt": "2026-02-28T10:00:00Z", "cronNames": [],
        })
        fake_ch = {
            "main": {
                "marginSummary": {"accountValue": "2900"},
                "assetPositions": [
                    {"coin": "ETH", "szi": "1.5"},
                    {"coin": "SOL", "szi": "10"},
                ],
            }
        }
        call_count = {"n": 0}

        def mock_ch(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 1:
                return fake_ch
            return None

        def mock_call(*args, **kwargs):
            raise RuntimeError("MCP close failed")

        try:
            with patch.object(self.spawner, "get_clearinghouse_state", side_effect=mock_ch), \
                 patch.object(self.spawner, "mcporter_call", side_effect=mock_call), \
                 patch.object(self.spawner, "mcporter_call_safe", return_value=None):
                result = self.spawner.kill_instance("tiger-closefail", reason="test")
            self.assertTrue(result["success"])
            self.assertEqual(result["killStatus"], "kill_pending")
        finally:
            self._cleanup_instance("tiger-closefail")


class TestSessionKeyInMessages(unittest.TestCase):
    """sessions_send messages should include sessionKey when available."""

    def _load_spawner(self):
        return _import_hyphenated(
            "cobra_spawner", os.path.join(SCRIPTS_DIR, "cobra-spawner.py"))

    def test_kill_message_includes_session_key(self):
        spawner = self._load_spawner()
        msg = spawner.build_kill_message("wolf-1", "test", session_key="agent:main:subagent:abc123")
        self.assertEqual(msg["params"]["sessionKey"], "agent:main:subagent:abc123")
        self.assertNotIn("_note", msg)

    def test_kill_message_fallback_without_session_key(self):
        spawner = self._load_spawner()
        msg = spawner.build_kill_message("wolf-1", "test")
        self.assertNotIn("sessionKey", msg["params"])
        self.assertIn("_note", msg)
        self.assertIn("sessions_list", msg["_note"])

    def test_regime_message_includes_session_key(self):
        spawner = self._load_spawner()
        msg = spawner.build_regime_update_message(
            "wolf-1", "VOLATILE", {"wolf": 40, "tiger": 30, "reserve": 30},
            session_key="agent:main:subagent:def456")
        self.assertEqual(msg["params"]["sessionKey"], "agent:main:subagent:def456")

    def test_regime_message_fallback_without_session_key(self):
        spawner = self._load_spawner()
        msg = spawner.build_regime_update_message(
            "wolf-1", "TRENDING", {"wolf": 60, "tiger": 25, "reserve": 15})
        self.assertNotIn("sessionKey", msg["params"])
        self.assertIn("_note", msg)


if __name__ == "__main__":
    unittest.main()
