#!/usr/bin/env python3
"""
Tests for COBRA critical logic: circuit breaker, kill-vs-keep,
signal pressure, leverage governance, and kill_pending retry.

Run: python3 -m pytest tests/test_cobra.py -v
  or: python3 tests/test_cobra.py
"""

import json, os, sys, tempfile, shutil, unittest, importlib.util
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

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
        """Tier 2+ positions should always KEEP (trailing stop protecting gains)."""
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

    def test_detects_missed_spawn(self):
        brain = self._load_brain()
        state = {"pendingActions": {"spawns": ["wolf-abc"], "kills": []}}
        instances = {}
        warnings, re_kills = brain._verify_pending_actions(state, instances)
        self.assertEqual(len(warnings), 1)
        self.assertIn("wolf-abc", warnings[0])

    def test_reissues_missed_kill(self):
        brain = self._load_brain()
        state = {"pendingActions": {"spawns": [], "kills": ["wolf-def"]}}
        instances = {"wolf-def": {"status": "active", "budget": 5000}}
        warnings, re_kills = brain._verify_pending_actions(state, instances)
        self.assertEqual(len(re_kills), 1)
        self.assertEqual(re_kills[0]["instanceId"], "wolf-def")

    def test_no_warnings_when_clean(self):
        brain = self._load_brain()
        state = {"pendingActions": {"spawns": [], "kills": []}}
        warnings, re_kills = brain._verify_pending_actions(state, {})
        self.assertEqual(len(warnings), 0)
        self.assertEqual(len(re_kills), 0)


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
        """Each missed FIRST_JUMP adds 15 to signal pressure."""
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_json("emerging-movers-history.json", [
            {"timestamp": now_ts, "markets": [
                {"isFirstJump": True}, {"isFirstJump": True}, {"isFirstJump": True}
            ]}
        ])
        with patch.object(self.signals, "get_instance_workspace", return_value=self.tmpdir), \
             patch.object(self.signals, "get_instance_state_dir", return_value=self.tmpdir):
            result = self.signals.analyze_wolf_instance("wolf-1", {"slots": 3})
        self.assertEqual(result["missedFirstJumps1h"], 3)
        self.assertEqual(result["signalPressure"], 45)

    def test_wolf_slots_full_bonus(self):
        """Slots full adds +10 when there's existing pressure."""
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
        self.assertEqual(result["signalPressure"], 25)

    def test_tiger_high_confluence(self):
        """Each scanner with confluence >= 0.65 adds 10 to pressure."""
        for scanner in ("funding-scanner.json", "compression-scanner.json",
                        "momentum-scanner.json"):
            self._write_json(scanner, {"confluence": 0.75})
        for scanner in ("whale-scanner.json", "volatility-scanner.json"):
            self._write_json(scanner, {"confluence": 0.3})
        with patch.object(self.signals, "get_instance_workspace", return_value=self.tmpdir), \
             patch.object(self.signals, "get_instance_state_dir", return_value=self.tmpdir):
            result = self.signals.analyze_tiger_instance("tiger-1", {"maxSlots": 3})
        self.assertEqual(result["highConfluenceCount"], 3)
        self.assertEqual(result["signalPressure"], 30)

    def test_tiger_prescreener_density_bonus(self):
        """Density >= 25 adds (density - 15) * 5 to pressure."""
        candidates = [{"score": 70} for _ in range(30)]
        self._write_json("prescreened.json", {"candidates": candidates})
        with patch.object(self.signals, "get_instance_workspace", return_value=self.tmpdir), \
             patch.object(self.signals, "get_instance_state_dir", return_value=self.tmpdir):
            result = self.signals.analyze_tiger_instance("tiger-1", {"maxSlots": 3})
        self.assertEqual(result["prescreenerDensity"], 30)
        self.assertEqual(result["signalPressure"], 75)

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
            with patch.object(self.spawner, "mcporter_call_safe", return_value=None):
                result = self.spawner.kill_instance("tiger-killtest", reason="test")
            self.assertTrue(result["success"])
            self.assertEqual(result["killStatus"], "kill_pending")
        finally:
            self._cleanup_instance("tiger-killtest")

    def test_wolf_kill_mcp_down_uses_local_dsl(self):
        """WOLF kill works without MCP because positions come from local DSL files."""
        self._write_instance("wolf-killtest", {
            "type": "wolf", "instanceId": "wolf-killtest",
            "wallet": "0xfake", "budget": 3000, "status": "active",
            "spawnedAt": "2026-02-28T10:00:00Z", "cronNames": [],
        })
        try:
            with patch.object(self.spawner, "mcporter_call_safe", return_value=None):
                result = self.spawner.kill_instance("wolf-killtest", reason="test")
            self.assertTrue(result["success"])
            self.assertEqual(result["killStatus"], "killed")
        finally:
            self._cleanup_instance("wolf-killtest")

    def test_kill_with_close_errors_sets_kill_pending(self):
        """When some position closes fail, kill_pending is set for retry."""
        self._write_instance("tiger-closefail", {
            "type": "tiger", "instanceId": "tiger-closefail",
            "wallet": "0xfake", "budget": 3000, "status": "active",
            "spawnedAt": "2026-02-28T10:00:00Z", "cronNames": [],
        })
        fake_ch = {
            "accountValue": 2900,
            "positions": [
                {"coin": "ETH", "szi": "1.5"},
                {"coin": "SOL", "szi": "10"},
            ],
        }
        call_count = {"n": 0}

        def mock_safe(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 1:
                return fake_ch
            return None

        def mock_call(*args, **kwargs):
            raise RuntimeError("MCP close failed")

        try:
            with patch.object(self.spawner, "mcporter_call_safe", side_effect=mock_safe), \
                 patch.object(self.spawner, "mcporter_call", side_effect=mock_call):
                result = self.spawner.kill_instance("tiger-closefail", reason="test")
            self.assertTrue(result["success"])
            self.assertEqual(result["killStatus"], "kill_pending")
        finally:
            self._cleanup_instance("tiger-closefail")


if __name__ == "__main__":
    unittest.main()
