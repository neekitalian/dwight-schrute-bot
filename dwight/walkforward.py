"""Bounded chronological research with an unconsumed final holdout.

All variants replay the same strategy and costs. A long-only candidate gate is
applied before any next-bar entry, including to the training-label baseline.
No broker, model promotion, remote data access, or final-holdout evaluator lives
here. Test windows are for development; reading their results contaminates them
for later model selection. The separately reserved final period stays unscored.
"""
from collections import Counter
from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import uuid

from vwap_bot.engine import Config
from .connectors.csv import read_bars
from .experiments import (
    CandidateBot, DEFAULTS, FEATURE_VERSION, JSONModel, _PriorVolumeFilter,
    _metrics, _provenance, _write, _private_directory, complete_sessions, fit_model, purge_labels,
)

SAMPLE_KEYS = (
    "min_train_samples", "min_validation_samples", "min_test_samples",
    "min_class_samples", "min_validation_trades",
)
WALKFORWARD_DEFAULTS = {
    "train_sessions": 120, "validation_sessions": 40, "test_sessions": 40,
    "holdout_sessions": 40, "mode": "expanding", "max_windows": 12,
    "long_only": True, "dataset_manifest": None,
    **{key: DEFAULTS[key] for key in SAMPLE_KEYS},
    "thresholds": list(DEFAULTS["thresholds"]),
}


def _options(config, synthetic):
    cfg = dict(config or {})
    settings = Config(**dict(cfg.pop("strategy", {})))
    if set(cfg) - set(WALKFORWARD_DEFAULTS):
        raise ValueError(f"unknown walk-forward settings: {sorted(set(cfg)-set(WALKFORWARD_DEFAULTS))}")
    options = {**WALKFORWARD_DEFAULTS, **cfg}
    for key in (*SAMPLE_KEYS, "train_sessions", "validation_sessions", "test_sessions", "holdout_sessions", "max_windows"):
        if type(options[key]) is not int or options[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if options["max_windows"] > 50:
        raise ValueError("max_windows cannot exceed 50")
    if options["mode"] not in ("expanding", "rolling"):
        raise ValueError("mode must be expanding or rolling")
    if type(options["long_only"]) is not bool:
        raise ValueError("long_only must be a boolean")
    if not synthetic and any(options[key] < DEFAULTS[key] for key in SAMPLE_KEYS):
        raise ValueError("reduced sample requirements are allowed only for synthetic smoke experiments")
    thresholds = options["thresholds"]
    if (not isinstance(thresholds, (list, tuple)) or not 1 <= len(thresholds) <= 21
            or any(type(x) not in (int, float) or not 0 < x < 1 for x in thresholds)):
        raise ValueError("thresholds must contain 1 to 21 probabilities between zero and one")
    options["thresholds"] = sorted(set(thresholds))
    if options["dataset_manifest"] is not None:
        options["dataset_manifest"] = str(options["dataset_manifest"])
    return settings, options


def plan_windows(session_days, options):
    """Reserve the latest whole sessions first; then plan nonoverlapping tests.

    A previous window's test sessions can become observed history in a later
    window's training/validation period. No fold can see its own future test.
    """
    days = sorted(session_days)
    if len(set(days)) != len(days):
        raise ValueError("session days must be unique")
    n_holdout = options["holdout_sessions"]
    development = days[:-n_holdout] if len(days) > n_holdout else []
    holdout = days[-n_holdout:]
    train_size = options["train_sessions"]
    val_size = options["validation_sessions"]
    test_size = options["test_sessions"]
    windows = []
    train_end = train_size
    consumed = 0
    while train_end+val_size+test_size <= len(development) and len(windows) < options["max_windows"]:
        train_start = 0 if options["mode"] == "expanding" else train_end-train_size
        test_start = train_end+val_size
        test_end = test_start+test_size
        windows.append({
            "window": len(windows)+1,
            "train": development[train_start:train_end],
            "validation": development[train_end:test_start],
            "test": development[test_start:test_end],
        })
        consumed = test_end
        train_end += test_size
    return {
        "windows": windows, "final_holdout_sessions": holdout,
        "unused_development_sessions": development[consumed:],
        "requested_holdout_sessions": n_holdout,
        "holdout_reservation_complete": len(holdout) == n_holdout,
    }


class _DirectionFilter:
    def __init__(self, model, long_only):
        self.model, self.long_only = model, long_only

    def predict_probability(self, features):
        if self.long_only and features["direction"] != 1:
            return 0.0
        return self.model.predict_probability(features) if self.model else 1.0


class WalkForwardBot(CandidateBot):
    """Candidate filter whose direction constraint also applies to the baseline."""
    def __init__(self, config=Config(), model=None, threshold=.5, long_only=True):
        if not 0 < threshold < 1:
            raise ValueError("threshold must be between zero and one")
        self.long_only = long_only
        super().__init__(config, _DirectionFilter(model, long_only), threshold)

    def _signal(self, b, prev):
        before = len(self.candidates)
        super()._signal(b, prev)
        if len(self.candidates) > before:
            candidate = self.candidates[-1]
            if self.long_only and candidate["features"]["direction"] != 1:
                candidate["rejection_reason"] = "long_only_policy"


def _replay(bars, settings, long_only, model=None, threshold=.5):
    bot = WalkForwardBot(settings, model, threshold, long_only)
    for bar in bars:
        bot.feed(bar)
    bot.finish()
    if any(trade["exit_reason"] in ("end_of_data", "data_session_end") for trade in bot.trades):
        raise ValueError("unexpected boundary-forced trade; refusing training labels")
    return bot


def _samples(bot, bars):
    rows, purged = purge_labels(bot.candidates, bars[0].timestamp, bars[-1].timestamp+timedelta(minutes=5))
    counts = Counter(row["label"] for row in rows)
    return rows, {
        "labeled": len(rows), "positive": counts[1], "negative": counts[0],
        "purged": purged, "unlabeled": len(bot.candidates)-len(rows)-purged,
        "direction_rejections": sum(row.get("rejection_reason") == "long_only_policy" for row in bot.candidates),
    }


def _sample_blockers(partition, counts, options):
    reasons = []
    if counts["labeled"] < options[f"min_{partition}_samples"]:
        reasons.append(f"{partition} needs at least {options[f'min_{partition}_samples']} labeled trades")
    if min(counts["positive"], counts["negative"]) < options["min_class_samples"]:
        reasons.append(f"{partition} needs at least {options['min_class_samples']} examples of each outcome")
    return reasons


def _save_replay(directory, variant, bot, bars):
    _write(directory/f"test-{variant}-trades.json", bot.trades)
    _write(directory/f"test-{variant}-equity.json", [
        {"available_at": (bar.timestamp+timedelta(minutes=5)).isoformat(), "equity": equity}
        for bar, equity in zip(bars, bot.marked_equity)
    ])


def _run_window(directory, window, sessions, settings, options, provenance, synthetic):
    bars = {name: [bar for day in window[name] for bar in sessions[day]] for name in ("train", "validation", "test")}
    result = {
        **window, "directory": str(directory), "status": "insufficient_data",
        "sample_counts": {}, "test_evaluation": {}, "blocking_reasons": [],
        "selection_uses_test": False, "test_consumed": False,
    }
    rows = {}
    # Only train and validation outcomes may influence fitting/selection.
    for partition in ("train", "validation"):
        baseline = _replay(bars[partition], settings, options["long_only"])
        rows[partition], counts = _samples(baseline, bars[partition])
        result["sample_counts"][partition] = counts
        result["blocking_reasons"].extend(_sample_blockers(partition, counts, options))
    if result["blocking_reasons"]:
        return result

    model = fit_model(rows["train"], synthetic=synthetic)
    trials = []
    for threshold in options["thresholds"]:
        candidate = _replay(bars["validation"], settings, options["long_only"], model, threshold)
        trials.append({"threshold": threshold, **_metrics(candidate)})
    result["validation_threshold_trials"] = trials
    eligible = [trial for trial in trials if trial["trades"] >= options["min_validation_trades"]]
    if not eligible:
        result["status"] = "insufficient_validation_trades"
        result["blocking_reasons"].append("no threshold produced enough validation trades")
        return result
    best = max(eligible, key=lambda trial: (trial["net_pnl"], -trial["max_bar_close_drawdown"], -abs(trial["threshold"]-.5)))
    threshold = best["threshold"]
    result["selected_threshold"] = threshold
    model.artifact.update(
        threshold=threshold, symbol="QQQ", strategy=asdict(settings),
        direction_policy="long_only" if options["long_only"] else "long_and_short",
        train_sessions=window["train"], validation_sessions=window["validation"],
        research_only=True, promotion_eligible=False, **provenance,
    )
    model = JSONModel(model.artifact)
    _write(directory/"model.json", model.artifact)
    result["model_sha256"] = hashlib.sha256((directory/"model.json").read_bytes()).hexdigest()

    # Test outcomes are first evaluated only after the threshold is frozen.
    baseline = _replay(bars["test"], settings, options["long_only"])
    _, counts = _samples(baseline, bars["test"])
    result["test_consumed"] = True
    result["sample_counts"]["test"] = counts
    result["blocking_reasons"].extend(_sample_blockers("test", counts, options))
    if result["blocking_reasons"]:
        result["status"] = "insufficient_test_samples"
        # The test has been inspected, but an underpowered window is not scored
        # or silently excluded from the denominator of reported window counts.
        return result
    variants = {
        "baseline": baseline,
        "simple_volume": _replay(bars["test"], settings, options["long_only"], _PriorVolumeFilter()),
        "filtered": _replay(bars["test"], settings, options["long_only"], model, threshold),
    }
    for name, bot in variants.items():
        result["test_evaluation"][name] = _metrics(bot)
        _save_replay(directory, name, bot, bars["test"])
    result["status"] = "completed_synthetic_smoke" if synthetic else "completed_research"
    result["filtered_minus_baseline_net_pnl"] = variants["filtered"].equity-baseline.equity
    return result


def run_walkforward(data: Path, output: Path, synthetic=False, config: dict | None = None) -> dict:
    """Run fixed rolling/expanding QQQ windows and leave the final holdout unused.

    Defaults require 120 training, 40 validation, 40 test and 40 reserved final
    sessions. Config accepts WALKFORWARD_DEFAULTS plus nested strategy overrides.
    Reduced sample requirements require synthetic=True. No model is deployable
    through this research API; there is intentionally no holdout-scoring flag.
    """
    if type(synthetic) is not bool:
        raise ValueError("synthetic must be a boolean")
    settings, options = _options(config, synthetic)
    data = Path(data)
    raw = data.read_bytes()
    provenance, manifest = _provenance(data, "QQQ", raw, synthetic, options["dataset_manifest"])
    _private_directory(output, exist_ok=True)
    directory = _private_directory(Path(output)/uuid.uuid4().hex)
    # Replay exactly the bytes fingerprinted above, even if a recorder updates
    # the source CSV while this experiment is running.
    snapshot = directory/"input.csv"
    snapshot.write_bytes(raw)
    snapshot.chmod(0o600)
    try:
        sessions, excluded = complete_sessions(list(read_bars(snapshot)))
    except Exception as exc:
        _write(directory/"failed.json", {"error_type": type(exc).__name__})
        raise
    plan = plan_windows(sessions, options)
    if manifest:
        _write(directory/"dataset-manifest.json", manifest)
    source_paths = [Path(__file__), Path(__file__).with_name("experiments.py"),
                    Path(__file__).parent.parent/"vwap_bot"/"engine.py",
                    Path(__file__).parent/"connectors"/"csv.py"]
    source_hashes = {str(path.relative_to(Path(__file__).parent.parent)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}
    report = {
        "schema_version": 1, "kind": "walk_forward_research", "run_id": directory.name,
        "directory": str(directory), "symbol": "QQQ", "synthetic": synthetic,
        "status": "insufficient_data", "promotion_eligible": False,
        "input_sha256": hashlib.sha256(raw).hexdigest(), **provenance,
        "source_hashes": source_hashes,
        "code_sha256": hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest(),
        "feature_version": FEATURE_VERSION, "strategy": asdict(settings),
        "walkforward_settings": options, "complete_sessions": len(sessions),
        "excluded_sessions": excluded, "plan": plan, "windows": [],
        "final_holdout": {"sessions": plan["final_holdout_sessions"], "consumed": False,
                          "status": "reserved_not_evaluated", "evaluation": None},
        "aggregate": {}, "blocking_reasons": [],
        "aggregate_scope": "completed_windows_only; inspect insufficient counts before interpreting",
        "limitations": [
            "Synthetic results test software only; real CSV without a verified manifest is unverified research data.",
            "Final holdout sessions are parsed for input validation and reservation only; never replayed, fitted, or scored.",
            "Previously inspected development test windows must not be presented as a fresh final holdout in later experiments.",
            "Each window starts from the same capital and flat state; aggregate P&L sums independent window replays, not a continuous account.",
            "Earlier test observations may become training or validation history in later chronological windows.",
            "Only complete 78-bar weekday sessions are supported; early closes are excluded and exchange holiday validity needs dataset provenance.",
            "Labels cover baseline-selected closed trades; filtered paths can produce additional candidates.",
            "All fills are OHLC simulations with next-bar entry, fixed slippage and commissions; no queue, latency, spread or partial-fill model.",
            "Direction is matched across variants; other broker cash-loss and order-lifecycle rules are not modeled.",
            "Minimum label counts are engineering gates, not statistical power guarantees; uncertainty and market-regime analysis remain necessary.",
            "No model is promoted or connected to a broker; a separate review must authorize any future final-holdout evaluation.",
        ],
    }
    _write(directory/"plan.json", plan)
    if not plan["windows"]:
        required = sum(options[key] for key in ("train_sessions", "validation_sessions", "test_sessions", "holdout_sessions"))
        report["blocking_reasons"].append(f"needs at least {required} complete sessions for one window and final holdout")
    try:
        for window in plan["windows"]:
            fold_dir = directory/f"window-{window['window']:03d}"
            _private_directory(fold_dir)
            result = _run_window(fold_dir, window, sessions, settings, options, provenance, synthetic)
            _write(fold_dir/"report.json", result)
            report["windows"].append(result)
        complete = [window for window in report["windows"] if window["status"].startswith("completed_")]
        report["window_counts"] = {"planned": len(plan["windows"]), "completed": len(complete),
                                   "insufficient": len(report["windows"])-len(complete)}
        if complete:
            report["status"] = ("completed_synthetic_smoke" if synthetic else "completed_research") if len(complete) == len(report["windows"]) else "partial_synthetic_smoke" if synthetic else "partial_research"
            report["aggregate"] = {
                variant: {
                    "scored_windows": len(complete),
                    "net_pnl_sum_independent_windows": sum(window["test_evaluation"][variant]["net_pnl"] for window in complete),
                    "closed_trades": sum(window["test_evaluation"][variant]["trades"] for window in complete),
                    "worst_window_bar_close_drawdown": max(window["test_evaluation"][variant]["max_bar_close_drawdown"] for window in complete),
                } for variant in ("baseline", "simple_volume", "filtered")
            }
            report["filtered_ahead_windows"] = sum(window["filtered_minus_baseline_net_pnl"] > 0 for window in complete)
        if report["window_counts"]["insufficient"]:
            report["blocking_reasons"].append("one or more planned windows have insufficient samples; see each window's reasons")
        report["blocking_reasons"].append("synthetic data cannot support deployment" if synthetic else "research only; final holdout and deployment review remain unperformed")
        _write(directory/"report.json", report)
        return report
    except Exception as exc:
        _write(directory/"failed.json", {"error_type": type(exc).__name__})
        raise
