from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dwight.context import PointInTimeContext
from dwight.experiments import CandidateBot, FEATURE_NAMES, _metrics
from dwight.finrl import DecisionReplay, evaluate_policy, make_gym_env, train_ppo
from vwap_bot.engine import Bar, Bot, Config


START = datetime(2026, 9, 28, 9, 30, tzinfo=ZoneInfo("America/New_York"))
HAS_GYM = importlib.util.find_spec("gymnasium") is not None
HAS_FINRL = importlib.util.find_spec("finrl") is not None


def bars(overrides=None):
    overrides = overrides or {}
    result = []
    for i in range(78):
        values = {"open": 100, "high": 100.2, "low": 99.8, "close": 100, "volume": 1000}
        values.update(overrides.get(i, {}))
        result.append(Bar(START + timedelta(minutes=5 * i), **values))
    return result


def signals(schedule):
    """Replace only candidate generation; retain actual fills, risk and exits."""
    def signal(bot, b, prev):
        index = len(bot.bars) - 1
        direction = schedule.get(index)
        if direction is not None:
            bot.pending = {"direction": direction, "stop": 99 if direction == 1 else 101,
                           "signal_time": b.timestamp.isoformat(), "reason": "controlled candidate"}
    return signal


def observed_features(bot, pending):
    # Deliberately depend on observed OHLCV so future perturbations are visible
    # if the adapter ever advances the engine before emitting the observation.
    last = bot.bars[-1]
    values = [pending["direction"], bot.pv / bot.vol, len(bot.bars), last.close - last.open,
              last.volume / 1000, bot.ema, len(bot.bars) / 78,
              pending["direction"] * (last.close - pending["stop"]), last.high, last.low]
    return dict(zip(FEATURE_NAMES, values))


def finish(replay, action=1):
    observation, info = replay.reset()
    rewards = []
    while not replay.terminated:
        observation, reward, terminated, truncated, info = replay.step(action)
        if truncated:
            raise AssertionError("Offline full-session replay must not truncate")
        rewards.append(reward)
    return replay.report(), rewards


def context_snapshot(available_at, expires_at):
    return {"symbol": "QQQ", "published_at": (available_at - timedelta(minutes=1)).isoformat(),
            "available_at": available_at.isoformat(), "expires_at": expires_at.isoformat(),
            "source": "fixture:release", "features": {"macro_score": -0.25}}


class LongOnly:
    def predict_probability(self, features):
        return float(features["direction"] == 1)


class FinRLReplayTests(unittest.TestCase):
    def test_all_take_matches_stateful_long_only_candidate_bot_and_reward(self):
        data = bars({3: {"high": 104}, 9: {"high": 104}, 13: {"high": 104}})
        config = Config(commission=0.025, slippage=0.03)
        baseline = CandidateBot(config, model=LongOnly())
        replay = DecisionReplay(data, config=config)
        with patch.object(Bot, "_signal", signals({2: 1, 5: -1, 8: 1, 12: 1})), \
                patch("dwight.experiments.extract_features", observed_features):
            for row in data:
                baseline.feed(row)
            baseline.finish()
            report, rewards = finish(replay)
        self.assertEqual(report["trades"], baseline.trades)
        self.assertEqual(report["equity"], baseline.curve)
        self.assertEqual(report["metrics"], _metrics(baseline))
        self.assertEqual(len(report["trades"]), 3)
        self.assertEqual(report["metrics"]["candidate_count"], 4)
        self.assertTrue(all(t["direction"] == 1 for t in report["trades"]))
        self.assertTrue(all(d["taken"] for d in report["decisions"]))
        self.assertGreater(sum(t["fees"] for t in report["trades"]), 0)
        self.assertAlmostEqual(sum(rewards), report["metrics"]["return_fraction"], places=14)
        self.assertFalse(report["promotion_eligible"])
        self.assertEqual(report["mode"], "offline_research")

    def test_all_skip_has_no_fills_and_zero_rewards(self):
        replay = DecisionReplay(bars({3: {"high": 104}}))
        with patch.object(Bot, "_signal", signals({2: 1, 3: 1, 5: -1, 8: 1})), \
                patch("dwight.experiments.extract_features", observed_features):
            report, rewards = finish(replay, 0)
        self.assertEqual(report["trades"], [])
        self.assertEqual(report["metrics"]["net_pnl"], 0)
        self.assertEqual(rewards, [0, 0, 0])
        self.assertTrue(all(d["reason"] == "policy_skip" for d in report["decisions"]))
        self.assertEqual(report["metrics"]["taken_candidates"], 0)

    def test_missing_future_and_expired_context_block_take(self):
        decision_at = START + timedelta(minutes=15)
        contexts = {
            "missing": [],
            "future": [context_snapshot(decision_at + timedelta(microseconds=1),
                                        decision_at + timedelta(days=1))],
            "expired": [context_snapshot(decision_at - timedelta(days=1), decision_at)],
        }
        for name, rows in contexts.items():
            context = PointInTimeContext(("macro_score",), rows)
            replay = DecisionReplay(bars({3: {"high": 104}}), context=context)
            with self.subTest(context=name), \
                    patch.object(Bot, "_signal", signals({2: 1})), \
                    patch("dwight.experiments.extract_features", observed_features):
                report, rewards = finish(replay)
                self.assertEqual(report["trades"], [])
                self.assertEqual(rewards, [0])
                decision = report["decisions"][0]
                self.assertEqual(decision["action_requested"], 1)
                self.assertFalse(decision["taken"])
                self.assertEqual(decision["reason"], "context_unavailable")
                self.assertIsNone(decision["context_source"])
                self.assertEqual(report["context_coverage"], 0)

    def test_context_available_at_decision_is_observed_and_take_is_allowed(self):
        at = START + timedelta(minutes=15)
        context = PointInTimeContext(("macro_score",), [context_snapshot(at, at + timedelta(hours=1))])
        replay = DecisionReplay(bars({3: {"high": 104}}), context=context)
        with patch.object(Bot, "_signal", signals({2: 1})), \
                patch("dwight.experiments.extract_features", observed_features):
            observation, info = replay.reset()
            values = dict(zip(replay.observation_names, observation))
            self.assertTrue(info["context_available"])
            self.assertEqual(values["context_available"], 1)
            self.assertEqual(values["context_age_days"], 0)
            self.assertEqual(values["context_macro_score"], -0.25)
            replay.step(1)
        report = replay.report()
        self.assertEqual(len(report["trades"]), 1)
        self.assertEqual(report["context_coverage"], 1)
        self.assertEqual(report["decisions"][0]["context_source"], "fixture:release")

    def test_future_price_and_context_changes_do_not_change_current_observation(self):
        at = START + timedelta(minutes=15)
        data_a = bars({3: {"high": 104, "close": 103}})
        data_b = bars({i: {"open": 200, "high": 210, "low": 198, "close": 209, "volume": 80000}
                       for i in range(3, 78)})
        present = context_snapshot(at, at + timedelta(days=1))
        future = context_snapshot(at + timedelta(minutes=1), at + timedelta(days=1))
        context_a = PointInTimeContext(("macro_score",), [present])
        context_b = PointInTimeContext(("macro_score",), [present, future])
        first, second = DecisionReplay(data_a, context=context_a), DecisionReplay(data_b, context=context_b)
        with patch.object(Bot, "_signal", signals({2: 1})), \
                patch("dwight.experiments.extract_features", observed_features):
            observation_a, info_a = first.reset()
            observation_b, info_b = second.reset()
            self.assertEqual(observation_a, observation_b)
            self.assertEqual(info_a, info_b)
            self.assertEqual(first.cursor, 3)
            self.assertEqual(second.cursor, 3)
            first.step(1)
            second.step(1)
        self.assertNotEqual(first.report()["metrics"]["net_pnl"], second.report()["metrics"]["net_pnl"])
        self.assertEqual(first.report()["decisions"][0]["observation"], observation_a)
        self.assertEqual(second.report()["decisions"][0]["observation"], observation_a)

    def test_stateful_skip_exposes_candidate_absent_from_all_take_path(self):
        data = bars({1: {"low": 98}, 2: {"low": 98}, 3: {"high": 104, "close": 103}})
        all_take, skip_first = DecisionReplay(data), DecisionReplay(data)
        with patch.object(Bot, "_signal", signals({i: 1 for i in range(77)})), \
                patch("dwight.experiments.extract_features", observed_features):
            baseline, _ = finish(all_take)
            skip_first.reset()
            skip_first.step(0)
            while not skip_first.terminated:
                skip_first.step(1)
            filtered = skip_first.report()
        baseline_times = {trade["signal_time"] for trade in baseline["trades"]}
        later = (START + timedelta(minutes=10)).isoformat()
        self.assertNotIn(later, baseline_times)
        self.assertIn(later, {trade["signal_time"] for trade in filtered["trades"]})
        self.assertEqual([t["exit_reason"] for t in baseline["trades"]], ["stop", "stop"])
        self.assertEqual(filtered["trades"][1]["exit_reason"], "target")

    def test_invalid_actions_do_not_advance_or_mutate_active_episode(self):
        replay = DecisionReplay(bars())
        with self.assertRaisesRegex(ValueError, "reset"):
            replay.step(1)
        with patch.object(Bot, "_signal", signals({2: 1})), \
                patch("dwight.experiments.extract_features", observed_features):
            replay.reset()
            for action in (-1, 2, True, False, 1.0, "1", None, [1]):
                with self.subTest(action=action), self.assertRaises(ValueError):
                    replay.step(action)
                self.assertEqual(replay.cursor, 3)
                self.assertEqual(replay.decisions, [])
            with self.assertRaisesRegex(ValueError, "completed"):
                replay.report()
            replay.step(0)
            with self.assertRaisesRegex(ValueError, "not active"):
                replay.step(1)

    def test_reset_discards_episode_state_and_terminal_observation_is_empty(self):
        replay = DecisionReplay(bars({3: {"high": 104}}))
        with patch.object(Bot, "_signal", signals({2: 1})), \
                patch("dwight.experiments.extract_features", observed_features):
            first, rewards = finish(replay)
            self.assertTrue(replay.terminated)
            self.assertEqual(replay.observation, [0] * len(replay.observation_names))
            self.assertTrue(replay.info["episode_complete"])
            second, other_rewards = finish(replay)
        self.assertEqual(first, second)
        self.assertEqual(rewards, other_rewards)
        self.assertEqual(len(second["decisions"]), 1)

    def test_no_candidates_or_only_shorts_terminate_without_executable_action(self):
        for schedule, count in (({}, 0), ({2: -1, 8: -1}, 2)):
            replay = DecisionReplay(bars())
            with self.subTest(schedule=schedule), patch.object(Bot, "_signal", signals(schedule)), \
                    patch("dwight.experiments.extract_features", observed_features):
                report, rewards = finish(replay)
            self.assertEqual(report["trades"], [])
            self.assertEqual(report["decisions"], [])
            self.assertEqual(report["metrics"]["candidate_count"], count)
            self.assertEqual(report["metrics"]["taken_candidates"], 0)
            self.assertEqual(rewards, [])

    def test_malformed_sessions_schema_collisions_and_large_observations_fail(self):
        full = bars()
        for data in ([], full[:42], full[:2] + full[3:], [full[0], *full]):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                DecisionReplay(data)
        for name in ("available", "age_days"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "collide"):
                DecisionReplay(full, context=PointInTimeContext((name,), []))
        with self.assertRaises(ValueError):
            DecisionReplay(full, config={})
        with self.assertRaises(ValueError):
            DecisionReplay(full, context={})
        at = START + timedelta(minutes=15)
        row = context_snapshot(at, at + timedelta(hours=1))
        row["features"]["macro_score"] = 1e31
        replay = DecisionReplay(full, context=PointInTimeContext(("macro_score",), [row]))
        with patch.object(Bot, "_signal", signals({2: 1})), \
                patch("dwight.experiments.extract_features", observed_features), \
                self.assertRaisesRegex(ValueError, "observations must be finite"):
            replay.reset()

    def test_provenance_distinguishes_bar_config_and_context_changes(self):
        first = DecisionReplay(bars()).provenance()
        second = DecisionReplay(bars({77: {"volume": 2000}})).provenance()
        different_context = DecisionReplay(bars(), context=PointInTimeContext(("macro_score",), [])).provenance()
        different_cost = DecisionReplay(bars(), config=Config(commission=0.02)).provenance()
        self.assertNotEqual(first["bars_sha256"], second["bars_sha256"])
        self.assertNotEqual(first["context_sha256"], different_context["context_sha256"])
        self.assertNotEqual(first["strategy"], different_cost["strategy"])
        self.assertFalse(first["requires_context"])
        self.assertTrue(different_context["requires_context"])

    def test_training_api_boundary_matches_pinned_finrl_signature(self):
        """Check adapter arguments and artifacts; this is not an RL training test."""
        calls = {}

        class DummyModel:
            num_timesteps = 64

            def save(self, path):
                Path(path).write_bytes(b"test double: not a trained policy")

        class SignatureCheckedAgent:
            def __init__(self, env):
                calls["env"] = env

            # Exact pinned FinRL public signature: device belongs inside model_kwargs.
            def get_model(self, model_name, policy="MlpPolicy", policy_kwargs=None,
                          model_kwargs=None, verbose=1, seed=None, tensorboard_log=None):
                calls["get_model"] = {"model_name": model_name, "policy": policy,
                                      "policy_kwargs": policy_kwargs, "model_kwargs": model_kwargs,
                                      "verbose": verbose, "seed": seed, "tensorboard_log": tensorboard_log}
                calls["model"] = DummyModel()
                return calls["model"]

            def train_model(self, model, tb_log_name, total_timesteps=5000):
                calls["train_model"] = {"model": model, "tb_log_name": tb_log_name,
                                        "total_timesteps": total_timesteps}
                return model

        modules = {}
        for name in ("finrl", "finrl.agents", "finrl.agents.stablebaselines3",
                     "finrl.agents.stablebaselines3.models"):
            modules[name] = ModuleType(name)
            modules[name].__path__ = []
        modules["finrl.agents.stablebaselines3.models"].DRLAgent = SignatureCheckedAgent
        replay = DecisionReplay(bars())
        env = SimpleNamespace(replay=replay, reset=lambda seed: replay.reset())
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, modules), \
                patch("importlib.metadata.version", side_effect=lambda name: "test-double-" + name), \
                patch.object(Bot, "_signal", signals({2: 1})), \
                patch("dwight.experiments.extract_features", observed_features):
            output = Path(directory) / "training"
            model, report = train_ppo(env, output, total_timesteps=64, seed=7)
            self.assertIs(calls["env"], env)
            self.assertEqual(calls["get_model"], {
                "model_name": "ppo", "policy": "MlpPolicy", "policy_kwargs": None,
                "model_kwargs": {"device": "cpu", "gamma": 1.0, "n_steps": 64, "batch_size": 64},
                "verbose": 0, "seed": 7, "tensorboard_log": None,
            })
            self.assertEqual(calls["train_model"], {"model": model, "tb_log_name": "dwight_vwap",
                                                    "total_timesteps": 64})
            self.assertEqual(report["kind"], "finrl_ppo_research")
            self.assertFalse(report["promotion_eligible"])
            self.assertEqual(report["evaluation"], "not_performed")
            self.assertEqual(report["market_data_provenance"], "caller_supplied_unverified")
            self.assertEqual(report["gamma"], 1.0)
            self.assertEqual(report["actual_timesteps"], 64)
            self.assertEqual(json.loads((output / "training.json").read_text()), report)
            self.assertTrue((output / "policy.zip").exists())
            self.assertFalse((output / "failed.json").exists())


@unittest.skipUnless(HAS_GYM, "optional Gymnasium environment is not installed")
class FinRLGymTests(unittest.TestCase):
    def test_gymnasium_checker_and_numpy_actions(self):
        import numpy as np
        from gymnasium.utils.env_checker import check_env

        replay = DecisionReplay(bars({3: {"high": 104}, 9: {"high": 104}}))
        env = make_gym_env(replay)
        with patch.object(Bot, "_signal", signals({2: 1, 8: 1})), \
                patch("dwight.experiments.extract_features", observed_features):
            check_env(env, skip_render_check=True)
            observation, _ = env.reset(seed=42)
            self.assertEqual(observation.dtype, np.float32)
            self.assertTrue(env.observation_space.contains(observation))
            result = env.step(np.int64(1))
            self.assertTrue(env.observation_space.contains(result[0]))
            self.assertFalse(result[3])
            _, _, terminated, _, _ = env.step(np.array([0], dtype=np.int64))
            self.assertTrue(terminated)

    def test_gymnasium_rejects_no_candidate_episode(self):
        env = make_gym_env(DecisionReplay(bars()))
        with patch.object(Bot, "_signal", signals({})), \
                self.assertRaisesRegex(ValueError, "no eligible long VWAP"):
            env.reset()

    def test_policy_evaluation_is_deterministic_and_rejects_invalid_output(self):
        class Policy:
            calls = []

            def predict(self, observation, *, deterministic):
                self.calls.append(deterministic)
                return 1, None

        replay = DecisionReplay(bars({3: {"high": 104}}))
        policy = Policy()
        with patch.object(Bot, "_signal", signals({2: 1})), \
                patch("dwight.experiments.extract_features", observed_features):
            report = evaluate_policy(replay, policy)
            self.assertEqual(policy.calls, [True])
            self.assertEqual(len(report["trades"]), 1)
            policy.predict = lambda observation, deterministic: (True, None)
            with self.assertRaisesRegex(ValueError, "invalid skip/take action"):
                evaluate_policy(replay, policy)

    @unittest.skipIf(HAS_FINRL, "test requires absent FinRL training dependencies")
    def test_missing_training_dependencies_fail_without_creating_artifacts(self):
        env = make_gym_env(DecisionReplay(bars()))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "training"
            with self.assertRaisesRegex(RuntimeError, "FinRL training dependencies"):
                train_ppo(env, output, total_timesteps=64)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
