"""Private, fixed-recipe QQQ study with no order or forward-start capability.

Prepare once before collecting data. Reuse a completed dataset and evaluation;
never automatically rerun an interrupted evaluation or move a test period.
The final test is consumed by this study, not an untouched reserve afterward.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path

from .data import ALPACA_BARS_URL, SCHEMA_VERSION, download_alpaca_dataset, exchange_sessions, sha256_file
from .experiments import (
    DEFAULTS, JSONModel, _PriorVolumeFilter, _metrics, _private_directory,
    _provenance, _replay, complete_sessions, experiment,
)
from .connectors.csv import read_bars
from .ops import source_sha256
from vwap_bot.engine import Config


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _save(path, value):
    """Write private state atomically, including while the initial file is open."""
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def _validate(recipe):
    allowed = {"schema_version", "symbol", "start", "end", "feed", "adjustment",
               "previously_inspected_periods", "experiment", "cost_stress_multiplier",
               "minimum_filtered_test_trades"}
    if set(recipe) != allowed or recipe["schema_version"] != 1 or recipe["symbol"] != "QQQ":
        raise ValueError("Use the complete QQQ study recipe")
    start, end = date.fromisoformat(recipe["start"]), date.fromisoformat(recipe["end"])
    if start > end or recipe["feed"] not in {"sip", "iex"} or recipe["adjustment"] != "raw":
        raise ValueError("Study requires ordered dates and an explicit raw Alpaca feed")
    for first, last in recipe["previously_inspected_periods"]:
        first, last = date.fromisoformat(first), date.fromisoformat(last)
        if first > last or (start <= last and end >= first):
            raise ValueError("Study range overlaps previously inspected outcomes")
    cfg = recipe["experiment"]
    if cfg.get("long_only") is not True:
        raise ValueError("This study requires explicit long_only=true")
    if set(cfg) - (set(DEFAULTS) | {"long_only", "strategy"}) or any(
            key in cfg for key in ("dataset_manifest", "tracking_uri")):
        raise ValueError("Study recipe contains unsupported experiment fields")
    Config(**cfg.get("strategy", {}))
    for key in ("min_train_samples", "min_validation_samples", "min_test_samples",
                "min_class_samples", "min_validation_trades"):
        value = cfg.get(key, DEFAULTS[key])
        if type(value) is not int or value < DEFAULTS[key]:
            raise ValueError("Real-data sample requirements cannot be reduced")
    train, validation = cfg.get("train_fraction", .6), cfg.get("validation_fraction", .2)
    if (type(train) not in (int, float) or type(validation) not in (int, float)
            or not 0 < train < 1 or not 0 < validation < 1 or train + validation >= 1):
        raise ValueError("Invalid chronological split")
    thresholds = cfg.get("thresholds", DEFAULTS["thresholds"])
    if (not isinstance(thresholds, list) or not 1 <= len(thresholds) <= 21
            or any(type(x) not in (int, float) or not 0 < x < 1 for x in thresholds)):
        raise ValueError("Invalid threshold grid")
    if recipe["cost_stress_multiplier"] != 2:
        raise ValueError("The predeclared stress case doubles commission and slippage")
    minimum = recipe["minimum_filtered_test_trades"]
    if type(minimum) is not int or minimum < DEFAULTS["min_test_samples"]:
        raise ValueError("At least 30 filtered test trades are required for shadow review")


def prepare(workspace, recipe):
    _validate(recipe)
    workspace = _private_directory(workspace)
    _save(workspace / "protocol.json", recipe)
    state = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
             "protocol_sha256": _digest(recipe), "status": "prepared",
             "experiment_started": False, "forward_observation_started": False,
             "submits_orders": False}
    _save(workspace / "state.json", state)
    return state


@contextmanager
def _locked(workspace):
    workspace = Path(workspace).resolve()
    if not workspace.is_dir() or workspace.stat().st_mode & 0o077:
        raise ValueError("Study workspace must exist and be private (0700)")
    fd = os.open(workspace / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another process owns this study") from None
        recipe = json.loads((workspace / "protocol.json").read_text())
        state = json.loads((workspace / "state.json").read_text())
        _validate(recipe)
        if state.get("protocol_sha256") != _digest(recipe):
            raise ValueError("Study recipe changed; do not reuse evaluated outcomes")
        yield workspace, recipe, state


def _manifest(workspace, recipe, state):
    path = (workspace / state["dataset_manifest"]).resolve()
    if not path.is_relative_to(workspace) or sha256_file(path) != state["manifest_sha256"]:
        raise ValueError("Recorded dataset manifest changed")
    manifest = json.loads(path.read_text())
    for key in ("start", "end", "feed", "adjustment"):
        if manifest.get(key) != recipe[key]:
            raise ValueError("Dataset differs from the fixed study recipe: " + key)
    if manifest.get("source") != "alpaca" or manifest.get("symbols") != ["QQQ"]:
        raise ValueError("Study requires an Alpaca QQQ dataset")
    expected = {"schema_version": SCHEMA_VERSION, "endpoint": ALPACA_BARS_URL, "asof": "-",
                "timestamp_convention": "interval_start", "availability": "interval_end",
                "session_policy": "complete_regular_sessions_only", "gap_policy": "reject_no_forward_fill"}
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("Unsupported dataset construction policy")
    entries = manifest.get("files", []) + manifest.get("raw_pages", [])
    names = [entry["path"] for entry in entries]
    if len(set(names)) != len(names) or not manifest.get("raw_pages"):
        raise ValueError("Dataset must retain unique normalized and raw file evidence")
    for entry in entries:
        file = (path.parent / entry["path"]).resolve()
        if not file.is_relative_to(path.parent) or sha256_file(file) != entry["sha256"]:
            raise ValueError("Dataset file integrity check failed")
    if manifest.get("sessions") not in names:
        raise ValueError("Dataset session calendar is not fingerprinted")
    actual_sessions = json.loads((path.parent / manifest["sessions"]).read_text())
    expected_sessions = exchange_sessions(date.fromisoformat(recipe["start"]), date.fromisoformat(recipe["end"]))
    if actual_sessions != [session.as_dict() for session in expected_sessions]:
        raise ValueError("Dataset session calendar differs from the requested range")
    minutes = sum(int((session.close - session.open).total_seconds() / 60) for session in expected_sessions)
    if manifest.get("counts", {}).get("QQQ") != {"1Min": minutes, "5Min": minutes // 5}:
        raise ValueError("Dataset does not cover all requested regular-session bars")
    data = path.parent / manifest["bars"]["QQQ"]["5Min"]
    if not data.resolve().is_relative_to(path.parent):
        raise ValueError("Dataset bar path escapes its private directory")
    _provenance(data, "QQQ", data.read_bytes(), False, path)
    return data, path


def collect(workspace, environ=None):
    with _locked(workspace) as (workspace, recipe, state):
        if "dataset_manifest" in state:
            _manifest(workspace, recipe, state)
            return state
        env = os.environ if environ is None else environ
        key = env.get("APCA_API_KEY_ID") or env.get("ALPACA_API_KEY")
        secret = env.get("APCA_API_SECRET_KEY") or env.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            state.update(status="awaiting_data_credentials")
            _save(workspace / "state.json", state)
            return state
        # Repeating collect after an incomplete download retains that attempt;
        # it never appends unaudited pages or silently accepts a partial range.
        state.update(status="collecting_history")
        _save(workspace / "state.json", state)
        try:
            manifest = download_alpaca_dataset(
                workspace / "datasets", recipe["start"], recipe["end"],
                ("QQQ",), recipe["feed"], "raw", environ=env, max_pages=1000)
            path = Path(manifest["directory"]) / "manifest.json"
            state.update(dataset_manifest=str(path.relative_to(workspace)),
                         manifest_sha256=sha256_file(path), status="history_ready")
            _manifest(workspace, recipe, state)
        except Exception as exc:
            state.update(status="history_failed", error_type=type(exc).__name__)
            _save(workspace / "state.json", state)
            raise
        _save(workspace / "state.json", state)
        return state


def _stress(workspace, report, model, multiplier):
    """Same frozen classifier and threshold in both cost cases; never refit."""
    experiment_dir = Path(report["directory"])
    sessions, _ = complete_sessions(list(read_bars(experiment_dir / "input.csv")))
    days = report["partitions"]["test"]
    bars = [bar for day in days for bar in sessions[day]]
    settings = Config(**report["strategy"])
    results = {}
    for name, settings in (("standard", settings), ("double_cost", replace(
            settings, commission=settings.commission * multiplier,
            slippage=settings.slippage * multiplier))):
        results[name] = {"costs": {"commission": settings.commission, "slippage": settings.slippage},
                         "evaluation": {}}
        for variant, predictor in (("baseline", None), ("simple_volume", _PriorVolumeFilter()),
                                   ("filtered", model)):
            threshold = model.artifact["threshold"] if variant == "filtered" else .5
            bot = _replay(bars, settings, predictor, threshold, long_only=True)
            metrics = _metrics(bot)
            results[name]["evaluation"][variant] = metrics
            _save(workspace / f"{name}-{variant}-trades.json", bot.trades)
            _save(workspace / f"{name}-{variant}-equity.json", [
                {"available_at": (bar.timestamp + timedelta(minutes=5)).isoformat(), "equity": value}
                for bar, value in zip(bars, bot.marked_equity)])
    return {"model_sha256": report["model_sha256"], "threshold": model.artifact["threshold"],
            "refitted": False, "test_sessions": days, "cases": results,
            "interpretation": "Independent simulated portfolios; costs can change sizing and later candidates"}


def _review(report, stress, audit, recipe):
    if set(stress.get("cases", {})) != {"standard", "double_cost"}:
        raise ValueError("Candidate review requires both predeclared cost cases")
    if stress.get("model_sha256") != report.get("model_sha256") or stress.get("refitted") is not False:
        raise ValueError("Cost stress must use the same frozen model without refitting")
    checks = {"audit": audit.get("status") == "passed"}
    for name, case in stress["cases"].items():
        metrics = case["evaluation"]
        if set(metrics) != {"baseline", "simple_volume", "filtered"}:
            raise ValueError("Candidate review requires all three comparator policies")
        filtered, baseline, simple = (metrics[key] for key in ("filtered", "baseline", "simple_volume"))
        checks[name + "_enough_trades"] = filtered["trades"] >= recipe["minimum_filtered_test_trades"]
        checks[name + "_positive_and_better_net_pnl"] = filtered["net_pnl"] > max(
            0, baseline["net_pnl"], simple["net_pnl"])
        checks[name + "_drawdown_not_worse"] = filtered["max_bar_close_drawdown"] <= min(
            baseline["max_bar_close_drawdown"], simple["max_bar_close_drawdown"])
    return {"status": "ready_for_shadow_review" if all(checks.values()) else "keep_research_only",
            "checks": checks, "model_sha256": report["model_sha256"],
            "paper_approved": False, "automatically_frozen": False,
            "meaning": "Predeclared screening gates, not statistical proof or broker approval"}


def evaluate(workspace):
    with _locked(workspace) as (workspace, recipe, state):
        if "dataset_manifest" not in state:
            raise ValueError("Collect verified history before evaluating")
        data, manifest_path = _manifest(workspace, recipe, state)
        if state.get("experiment_started"):
            if state.get("source_sha256") != source_sha256():
                raise ValueError("Research code changed after evaluation began")
            if "artifacts" not in state:
                raise ValueError("Interrupted evaluation requires review; test outcomes may already be consumed")
            for name, digest in state["artifacts"].items():
                path = (workspace / name).resolve()
                if not path.is_relative_to(workspace) or sha256_file(path) != digest:
                    raise ValueError("Saved study artifact changed")
            return state
        state.update(status="evaluating", experiment_started=True, source_sha256=source_sha256(),
                     test_outcomes="possibly_consumed_once_evaluation_begins")
        _save(workspace / "state.json", state)
        try:
            report = experiment(data, "QQQ", workspace / "experiments", synthetic=False,
                                config={**recipe["experiment"], "dataset_manifest": str(manifest_path)})
            state["experiment_directory"] = str(Path(report["directory"]).relative_to(workspace))
            summary = {"status": report["status"], "evidence": "historical_replay",
                       "source": report["source"], "feed": report["feed"], "synthetic": False,
                       "sample_counts": report["sample_counts"], "partitions": report["partitions"],
                       "evaluation": report["evaluation"], "blocking_reasons": report["blocking_reasons"],
                       "forward_observation_started": False, "broker_results": "not_observed"}
            if report["status"] == "completed_research":
                from .audit import audit_experiment
                from .reporting import generate_report
                exp = Path(report["directory"])
                model = JSONModel.load(exp / "model.json", report["model_sha256"])
                audit = audit_experiment(exp)
                stress = _stress(workspace, report, model, recipe["cost_stress_multiplier"])
                review = _review(report, stress, audit, recipe)
                _save(workspace / "cost-stress.json", stress)
                _save(workspace / "candidate-review.json", review)
                generate_report(exp, workspace / "charts", milestone_label="Historical QQQ study", audit=audit)
                summary.update(candidate_review=review, cost_stress=stress,
                               report_html="charts/report.html")
                state["status"] = review["status"]
            else:
                state["status"] = report["status"]
            _save(workspace / "summary.json", summary)
            # Freeze all experiment/report evidence, never credentials or the mutable state file.
            paths = [p for folder in (workspace / "experiments", workspace / "charts")
                     if folder.exists() for p in folder.rglob("*") if p.is_file()]
            paths += [p for p in workspace.glob("*.json") if p.name not in {"state.json", "protocol.json"}]
            state["artifacts"] = {str(p.relative_to(workspace)): sha256_file(p) for p in paths}
            _save(workspace / "state.json", state)
            return state
        except Exception as exc:
            state.update(status="evaluation_requires_review", error_type=type(exc).__name__)
            _save(workspace / "state.json", state)
            raise


def status(workspace):
    with _locked(workspace) as (_, _, state):
        return state
