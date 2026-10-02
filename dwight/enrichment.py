"""Predeclared causal-feature ablation for QQQ, separate from deployable v1.

This module compares two fixed regularized logistic models on the SAME labels,
windows, costs and direction policy. It adds no brokerage path, model promotion,
or final-holdout evaluator. Existing v1 feature/model artifacts are unchanged.
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import uuid
import warnings

from vwap_bot.engine import Bot, Config
from .connectors.csv import read_bars
from .experiments import (
    CandidateBot, FEATURE_NAMES, FEATURE_VERSION, _PriorVolumeFilter,
    _metrics, _provenance, _write, _private_directory, complete_sessions, extract_features,
    probability_metrics,
)
from .walkforward import _options, _sample_blockers, _samples, _save_replay, plan_windows

ENRICHED_VERSION = "vwap-candidate-v2-session-context-research"
EXTRA_FEATURES = (
    "session_return_atr", "observed_session_range_atr",
    "realized_log_volatility_12", "vwap_change_1_atr",
)
ENRICHED_NAMES = (*FEATURE_NAMES, *EXTRA_FEATURES)
FEATURE_DEFINITIONS = {
    "session_return_atr": "(current close - current session first observed open) / current trailing ATR",
    "observed_session_range_atr": "(max observed session high - min observed session low) / current trailing ATR",
    "realized_log_volatility_12": "RMS of up to 12 most recent close-to-close log returns within the current session",
    "vwap_change_1_atr": "(current cumulative session bar-derived VWAP - cumulative VWAP before current bar) / current trailing ATR",
}
ARMS = ("technical_v1", "session_context_v2")


def enriched_features(bot, pending):
    """Only observed session bars; no future extrema, returns or normalization."""
    features = extract_features(bot, pending)
    bars = bot.bars
    current = bars[-1]
    atr = sum(bot.trs[-bot.c.atr_period:])/bot.c.atr_period
    closes = [bar.close for bar in bars[-13:]]
    returns = [math.log(end/start) for start, end in zip(closes, closes[1:])]
    prior_volume = sum(bar.volume for bar in bars[:-1])
    if prior_volume <= 0 or not returns:
        raise ValueError("insufficient causal feature history")
    prior_vwap = sum((bar.high+bar.low+bar.close)/3*bar.volume for bar in bars[:-1])/prior_volume
    features.update(
        session_return_atr=(current.close-bars[0].open)/atr,
        observed_session_range_atr=(max(bar.high for bar in bars)-min(bar.low for bar in bars))/atr,
        realized_log_volatility_12=math.sqrt(sum(value*value for value in returns)/len(returns)),
        vwap_change_1_atr=(bot.pv/bot.vol-prior_vwap)/atr,
    )
    if not all(math.isfinite(value) for value in features.values()):
        raise ValueError("nonfinite enriched feature")
    return features


class EnrichedBot(CandidateBot):
    """Same engine fills/risk as v1, with an explicitly different feature hook."""
    def __init__(self, config=Config(), model=None, threshold=.5, long_only=True):
        super().__init__(config, model, threshold)
        self.long_only = long_only

    def _signal(self, b, prev):
        # Call the original signal engine exactly once; do not invoke v1's
        # classifier hook and then overwrite its decision using v2 features.
        Bot._signal(self, b, prev)
        if not self.pending:
            return
        features = enriched_features(self, self.pending)
        candidate = {
            "signal_time": self.pending["signal_time"],
            "available_at": (b.timestamp+timedelta(minutes=5)).isoformat(),
            "session": self.day.isoformat(), "feature_version": ENRICHED_VERSION,
            "features": features, "taken": True,
        }
        if self.long_only and features["direction"] != 1:
            candidate.update(taken=False, rejection_reason="long_only_policy")
        elif self.model:
            candidate["probability"] = self.model.predict_probability(features)
            candidate["taken"] = candidate["probability"] >= self.threshold
        self.candidates.append(candidate)
        self._candidate_by_time[candidate["signal_time"]] = candidate
        if not candidate["taken"]:
            self.pending = None


class ResearchModel:
    """Explicitly non-deployable JSON coefficient artifact; no pickle loading."""
    def __init__(self, artifact):
        if artifact.get("kind") != "feature_ablation_research_model" or artifact.get("schema_version") != 1:
            raise ValueError("unsupported research model schema")
        arm = artifact.get("arm")
        if arm not in ARMS:
            raise ValueError("unknown research model arm")
        names = FEATURE_NAMES if arm == "technical_v1" else ENRICHED_NAMES
        version = FEATURE_VERSION if arm == "technical_v1" else ENRICHED_VERSION
        if artifact.get("feature_names") != list(names) or artifact.get("feature_version") != version:
            raise ValueError("research feature schema mismatch")
        for key in ("mean", "scale", "coefficients"):
            vector = artifact.get(key)
            if not isinstance(vector, list) or len(vector) != len(names):
                raise ValueError("invalid research model vector")
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
                raise ValueError("nonfinite research model vector")
        if any(value <= 0 for value in artifact["scale"]):
            raise ValueError("model scale must be positive")
        if type(artifact.get("intercept")) not in (int, float) or not math.isfinite(artifact["intercept"]):
            raise ValueError("invalid model intercept")
        if type(artifact.get("synthetic")) is not bool or artifact.get("research_only") is not True or artifact.get("promotion_eligible") is not False:
            raise ValueError("research model cannot enable promotion")
        self.artifact = json.loads(json.dumps(artifact, allow_nan=False))

    def predict_probability(self, features):
        if set(features) != set(ENRICHED_NAMES):
            raise ValueError("enriched feature schema mismatch")
        a = self.artifact
        values = [features[name] for name in a["feature_names"]]
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            raise ValueError("invalid enriched feature value")
        terms = [(value-mean)/scale*coefficient for value, mean, scale, coefficient in
                 zip(values, a["mean"], a["scale"], a["coefficients"])]
        if not all(math.isfinite(value) for value in terms):
            raise ValueError("inference overflow")
        try:
            score = math.fsum([a["intercept"], *terms])
        except OverflowError as exc:
            raise ValueError("inference overflow") from exc
        if not math.isfinite(score):
            raise ValueError("inference overflow")
        if score >= 0:
            return 1/(1+math.exp(-score))
        exp_score = math.exp(score)
        return exp_score/(1+exp_score)


def _fit(rows, arm, synthetic):
    import sklearn
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    if arm not in ARMS:
        raise ValueError("unknown predeclared comparison arm")
    names = FEATURE_NAMES if arm == "technical_v1" else ENRICHED_NAMES
    values = [[row["features"][name] for name in names] for row in rows]
    scaler = StandardScaler().fit(values)
    model = LogisticRegression(C=1.0, solver="liblinear", random_state=0, max_iter=1000)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        model.fit(scaler.transform(values), [row["label"] for row in rows])
    return ResearchModel({
        "schema_version": 1, "kind": "feature_ablation_research_model",
        "arm": arm, "feature_version": FEATURE_VERSION if arm == "technical_v1" else ENRICHED_VERSION,
        "feature_names": list(names), "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
        "coefficients": model.coef_[0].tolist(), "intercept": float(model.intercept_[0]),
        "synthetic": synthetic, "research_only": True, "promotion_eligible": False,
        "sklearn_version": sklearn.__version__, "training_samples": len(rows),
        "positive_class": "net_trade_pnl_above_zero",
    })


def _replay(bars, settings, long_only, model=None, threshold=.5):
    bot = EnrichedBot(settings, model, threshold, long_only)
    for bar in bars:
        bot.feed(bar)
    bot.finish()
    if any(trade["exit_reason"] in ("end_of_data", "data_session_end") for trade in bot.trades):
        raise ValueError("boundary-forced trade is not an eligible label")
    return bot


def _run_window(directory, window, sessions, settings, options, synthetic, recipe_sha256):
    bars = {name: [bar for day in window[name] for bar in sessions[day]] for name in ("train", "validation", "test")}
    result = {**window, "status": "insufficient_data", "sample_counts": {},
              "arms": {}, "test_evaluation": {}, "blocking_reasons": [],
              "selection_uses_test": False, "test_consumed": False}
    rows = {}
    for partition in ("train", "validation"):
        baseline = _replay(bars[partition], settings, options["long_only"])
        rows[partition], counts = _samples(baseline, bars[partition])
        result["sample_counts"][partition] = counts
        result["blocking_reasons"].extend(_sample_blockers(partition, counts, options))
        _write(directory/f"{partition}-candidates.json", baseline.candidates)
    if result["blocking_reasons"]:
        return result

    models = {}
    # Freeze BOTH arms on the same train/validation observations before either
    # arm is allowed to inspect its development test. No test-selected winner.
    for arm in ARMS:
        model = _fit(rows["train"], arm, synthetic)
        trials = []
        for threshold in options["thresholds"]:
            replayed = _replay(bars["validation"], settings, options["long_only"], model, threshold)
            trials.append({"threshold": threshold, **_metrics(replayed)})
        result["arms"][arm] = {"validation_threshold_trials": trials}
        eligible = [trial for trial in trials if trial["trades"] >= options["min_validation_trades"]]
        if not eligible:
            result["blocking_reasons"].append(f"{arm}: no threshold produced enough validation trades")
            continue
        best = max(eligible, key=lambda trial: (trial["net_pnl"], -trial["max_bar_close_drawdown"], -abs(trial["threshold"]-.5)))
        threshold = best["threshold"]
        model.artifact.update(threshold=threshold, recipe_sha256=recipe_sha256,
                              symbol="QQQ", strategy=asdict(settings),
                              direction_policy="long_only" if options["long_only"] else "long_and_short",
                              train_sessions=window["train"], validation_sessions=window["validation"])
        _write(directory/f"{arm}-model.json", model.artifact)
        result["arms"][arm].update(selected_threshold=threshold,
                                   model_sha256=hashlib.sha256((directory/f"{arm}-model.json").read_bytes()).hexdigest())
        models[arm] = model
    if result["blocking_reasons"]:
        result["status"] = "insufficient_validation_trades"
        return result

    baseline = _replay(bars["test"], settings, options["long_only"])
    test_rows, counts = _samples(baseline, bars["test"])
    result["sample_counts"]["test"] = counts
    result["test_consumed"] = True
    result["blocking_reasons"].extend(_sample_blockers("test", counts, options))
    if result["blocking_reasons"]:
        result["status"] = "insufficient_test_samples"
        return result
    variants = {"baseline": baseline,
                "simple_volume": _replay(bars["test"], settings, options["long_only"], _PriorVolumeFilter())}
    for arm, model in models.items():
        variants[arm] = _replay(bars["test"], settings, options["long_only"], model,
                                result["arms"][arm]["selected_threshold"])
        result["arms"][arm]["probabilities_on_baseline_candidates"] = probability_metrics(test_rows, model)
    for variant, bot in variants.items():
        result["test_evaluation"][variant] = _metrics(bot)
        _save_replay(directory, variant, bot, bars["test"])
    result["enriched_minus_v1_net_pnl"] = variants["session_context_v2"].equity-variants["technical_v1"].equity
    result["status"] = "completed_synthetic_smoke" if synthetic else "completed_research"
    return result


def run_enrichment(data: Path, output: Path, synthetic=False, config: dict | None = None):
    """Compare fixed feature sets on development windows; leave holdout untouched.

    Uses the same config contract and real sample gates as run_walkforward.
    Two fixed model arms and four additional features are predeclared in code.
    No tree/model/hyperparameter sweep and no final-holdout scoring option exist.
    """
    if type(synthetic) is not bool:
        raise ValueError("synthetic must be a boolean")
    settings, options = _options(config, synthetic)
    data = Path(data)
    raw = data.read_bytes()
    provenance, manifest = _provenance(data, "QQQ", raw, synthetic, options["dataset_manifest"])
    _private_directory(output, exist_ok=True)
    directory = _private_directory(Path(output)/uuid.uuid4().hex)
    snapshot = directory/"input.csv"
    snapshot.write_bytes(raw)
    snapshot.chmod(0o600)
    try:
        sessions, excluded = complete_sessions(list(read_bars(snapshot)))
        plan = plan_windows(sessions, options)
        if manifest:
            _write(directory/"dataset-manifest.json", manifest)
        paths = [Path(__file__), Path(__file__).with_name("walkforward.py"),
                 Path(__file__).with_name("experiments.py"),
                 Path(__file__).parent/"connectors"/"csv.py",
                 Path(__file__).parent.parent/"vwap_bot"/"engine.py"]
        source_hashes = {str(path.relative_to(Path(__file__).parent.parent)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        recipe = {
            "schema_version": 1, "kind": "predeclared_feature_ablation", "symbol": "QQQ",
            "input_sha256": hashlib.sha256(raw).hexdigest(), **provenance,
            "source_hashes": source_hashes, "synthetic": synthetic,
            "strategy": asdict(settings), "settings": options, "plan": plan,
            "arms": {"technical_v1": list(FEATURE_NAMES), "session_context_v2": list(ENRICHED_NAMES)},
            "extra_feature_definitions": FEATURE_DEFINITIONS,
            "learner": {"kind": "logistic_regression", "C": 1.0, "solver": "liblinear", "random_state": 0, "max_iter": 1000},
            "threshold_selection": "validation net P&L, then lower drawdown, then closest to 0.5",
            "test_selects_variant": False, "final_holdout_evaluator": False,
        }
        # Persist the exact recipe before creating labels or fitting any model.
        _write(directory/"recipe.json", recipe)
        recipe_sha256 = hashlib.sha256((directory/"recipe.json").read_bytes()).hexdigest()
        _write(directory/"plan.json", plan)
        report = {
            "schema_version": 1, "kind": "feature_ablation_research", "run_id": directory.name,
            "directory": str(directory), "created_at": datetime.now(timezone.utc).isoformat(),
            "recipe_sha256": recipe_sha256, "input_sha256": recipe["input_sha256"],
            "source_hashes": source_hashes, **provenance, "symbol": "QQQ", "synthetic": synthetic,
            "status": "insufficient_data", "promotion_eligible": False, "selected_variant": None,
            "feature_versions": [FEATURE_VERSION, ENRICHED_VERSION],
            "complete_sessions": len(sessions), "excluded_sessions": excluded,
            "settings": options, "strategy": asdict(settings), "plan": plan, "windows": [],
            "final_holdout": {"sessions": plan["final_holdout_sessions"], "consumed": False,
                              "requested_sessions": plan["requested_holdout_sessions"],
                              "reservation_complete": plan["holdout_reservation_complete"],
                              "status": "reserved_not_evaluated", "evaluation": None},
            "aggregate": {}, "blocking_reasons": [],
            "limitations": [
                "This tests four technical features, not new fundamental facts or an LLM's opinions.",
                "Local recipe persistence is not an external preregistration or proof a dataset was never inspected before.",
                "Final holdout is parsed for integrity only; it is never replayed or scored by this API.",
                "Do not move an existing reserved final period forward and reuse it for development when adding new data.",
                "Development tests are consumed research observations; neither arm is automatically selected or promoted.",
                "Only full 78-bar sessions are supported; early closes are explicitly excluded.",
                "Training labels cover baseline-selected trades; a filtered portfolio can encounter additional candidates.",
                "Next-bar OHLC fills and fixed costs do not model spreads, queue priority, latency, partial fills or market impact.",
                "Each fold starts flat at the same capital; summed P&L represents independent windows, not a continuous account.",
                "Minimum sample gates are not statistical power or evidence of profitability.",
            ],
        }
        if not plan["windows"]:
            report["blocking_reasons"].append("insufficient complete sessions for requested windows and final holdout")
        for window in plan["windows"]:
            fold = directory/f"window-{window['window']:03d}"
            _private_directory(fold)
            result = _run_window(fold, window, sessions, settings, options, synthetic, recipe_sha256)
            _write(fold/"report.json", result)
            report["windows"].append(result)
        complete = [window for window in report["windows"] if window["status"].startswith("completed_")]
        report["window_counts"] = {"planned": len(plan["windows"]), "completed": len(complete),
                                   "insufficient": len(plan["windows"])-len(complete)}
        if complete:
            prefix = "completed" if len(complete) == len(plan["windows"]) else "partial"
            report["status"] = prefix+("_synthetic_smoke" if synthetic else "_research")
            report["aggregate"] = {
                arm: {"scored_windows": len(complete),
                      "net_pnl_sum_independent_windows": sum(window["test_evaluation"][arm]["net_pnl"] for window in complete),
                      "closed_trades": sum(window["test_evaluation"][arm]["trades"] for window in complete),
                      "worst_window_bar_close_drawdown": max(window["test_evaluation"][arm]["max_bar_close_drawdown"] for window in complete)}
                for arm in ("baseline", "simple_volume", *ARMS)
            }
            report["enriched_ahead_of_v1_windows"] = sum(window["enriched_minus_v1_net_pnl"] > 0 for window in complete)
        if report["window_counts"]["insufficient"]:
            report["blocking_reasons"].append("planned windows with insufficient samples remain in the denominator")
        report["blocking_reasons"].append("synthetic data is not market evidence" if synthetic else "research only; final holdout and deployment review unperformed")
        _write(directory/"report.json", report)
        return report
    except Exception as exc:
        _write(directory/"failed.json", {"error_type": type(exc).__name__})
        raise
