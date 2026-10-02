"""Chronological VWAP model experiments; no broker or remote training access.

Features are frozen when a completed bar creates a candidate. Labels only come
from subsequent, closed trades including the engine's fees and slippage. Model
evaluation replays the strategy with the filter installed: it never just deletes
losing trades from an already executed baseline portfolio.

The pinned engine assumes a 16:00 close. Until it has exchange-calendar-aware
exits, only complete 78-bar sessions are supported here. Early-close and partial
sessions are explicitly excluded, never force-closed into training labels.
"""
from collections import Counter
from dataclasses import asdict
from datetime import datetime, time, timedelta
import hashlib
import json
import math
from pathlib import Path
import uuid
import warnings
from time import perf_counter
from zoneinfo import ZoneInfo

from vwap_bot.engine import Bot, Config
from .connectors.csv import read_bars

NY = ZoneInfo("America/New_York")
FEATURE_VERSION = "vwap-candidate-v1"
FEATURE_NAMES = (
    "direction", "vwap_distance_atr", "atr_fraction", "body_atr",
    "relative_volume", "ema_distance_atr", "session_fraction",
    "signal_stop_distance_atr", "high_structure_atr", "low_structure_atr",
)
DEFAULTS = {
    "train_fraction": .6, "validation_fraction": .2,
    "min_train_samples": 100, "min_validation_samples": 30,
    "min_test_samples": 30, "min_class_samples": 10,
    "min_validation_trades": 5, "thresholds": [.35, .5, .65],
    "tracking_uri": None, "dataset_manifest": None,
}


def extract_features(bot: Bot, pending: dict) -> dict:
    """Features from the bot's observed bars only, after _signal has run."""
    if not bot.bars or not bot.vol or len(bot.trs) < bot.c.atr_period:
        raise ValueError("candidate has insufficient observed feature history")
    b = bot.bars[-1]
    atr = sum(bot.trs[-bot.c.atr_period:]) / bot.c.atr_period
    if atr <= 0 or len(bot.highs) < 2 or len(bot.lows) < 2:
        raise ValueError("candidate has invalid ATR or confirmed structure")
    previous = bot.bars[max(0, len(bot.bars)-21):-1]
    volume_mean = sum(x.volume for x in previous) / len(previous) if previous else 0
    if volume_mean <= 0:
        raise ValueError("candidate has no positive prior volume reference")
    local = b.timestamp.astimezone(NY)
    values = (
        pending["direction"], (b.close-bot.pv/bot.vol)/atr, atr/b.close,
        (b.close-b.open)/atr, b.volume/volume_mean, (b.close-bot.ema)/atr,
        ((local.hour*60+local.minute)-(9*60+30)+5)/390,
        pending["direction"]*(b.close-pending["stop"])/atr,
        (bot.highs[-1][1]-bot.highs[-2][1])/atr,
        (bot.lows[-1][1]-bot.lows[-2][1])/atr,
    )
    if not all(math.isfinite(float(x)) for x in values):
        raise ValueError("nonfinite candidate features")
    return dict(zip(FEATURE_NAMES, values))


class JSONModel:
    """Inspectable logistic-regression inference without pickle or sklearn."""
    def __init__(self, artifact: dict):
        if artifact.get("schema_version") != 1 or artifact.get("kind") != "logistic_regression":
            raise ValueError("unsupported model schema")
        if artifact.get("feature_version") != FEATURE_VERSION:
            raise ValueError("feature version mismatch")
        if artifact.get("feature_names") != list(FEATURE_NAMES):
            raise ValueError("feature names/order mismatch")
        for name in ("mean", "scale", "coefficients"):
            vector = artifact.get(name)
            if not isinstance(vector, list) or len(vector) != len(FEATURE_NAMES):
                raise ValueError(f"invalid model {name}")
            if any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
                raise ValueError(f"nonfinite model {name}")
        if any(x <= 0 for x in artifact["scale"]):
            raise ValueError("model scale must be positive")
        if type(artifact.get("intercept")) not in (int, float) or not math.isfinite(artifact["intercept"]):
            raise ValueError("invalid model intercept")
        if type(artifact.get("synthetic")) is not bool:
            raise ValueError("model must declare synthetic provenance")
        if "threshold" in artifact and (type(artifact["threshold"]) not in (int, float) or not 0 < artifact["threshold"] < 1):
            raise ValueError("invalid model threshold")
        if "symbol" in artifact and (not isinstance(artifact["symbol"], str) or not artifact["symbol"].strip()):
            raise ValueError("invalid model symbol")
        if "strategy" in artifact:
            Config(**artifact["strategy"])
        if artifact["synthetic"] and any(artifact.get(key, "synthetic") != "synthetic" for key in ("source", "feed")):
            raise ValueError("synthetic model provenance mismatch")
        self.artifact = json.loads(json.dumps(artifact, allow_nan=False))

    @classmethod
    def load(cls, path: Path, expected_sha256: str | None = None):
        raw = Path(path).read_bytes()
        if expected_sha256 and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("model checksum mismatch")
        return cls(json.loads(raw))

    def predict_probability(self, features: dict) -> float:
        if set(features) != set(FEATURE_NAMES):
            raise ValueError("feature names mismatch")
        values = [features[name] for name in FEATURE_NAMES]
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in values):
            raise ValueError("invalid feature value")
        a = self.artifact
        terms = [coefficient*(value-mean)/scale
                 for value, mean, scale, coefficient in zip(values, a["mean"], a["scale"], a["coefficients"])]
        if not all(math.isfinite(x) for x in terms):
            raise ValueError("model inference overflow")
        try:
            score = math.fsum([a["intercept"], *terms])
        except OverflowError as exc:
            raise ValueError("model inference overflow") from exc
        if not math.isfinite(score):
            raise ValueError("model inference overflow")
        if score >= 0:
            return 1/(1+math.exp(-score))
        exp_score = math.exp(score)
        return exp_score/(1+exp_score)


class CandidateBot(Bot):
    """Replay engine with candidate capture and an optional take/skip filter."""
    def __init__(self, config=Config(), model=None, threshold=.5):
        super().__init__(config)
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be a probability")
        self.model, self.threshold = model, threshold
        self.candidates = []
        self._candidate_by_time = {}
        self.marked_equity = []

    def _signal(self, b, prev):
        super()._signal(b, prev)
        if not self.pending:
            return
        features = extract_features(self, self.pending)
        candidate = {
            "signal_time": self.pending["signal_time"],
            "available_at": (b.timestamp+timedelta(minutes=5)).isoformat(),
            "session": self.day.isoformat(), "feature_version": FEATURE_VERSION,
            "features": features, "taken": True,
        }
        if self.model:
            candidate["probability"] = self.model.predict_probability(features)
            candidate["taken"] = candidate["probability"] >= self.threshold
        self.candidates.append(candidate)
        self._candidate_by_time[candidate["signal_time"]] = candidate
        if not candidate["taken"]:
            self.pending = None

    def _exit(self, price, timestamp, reason):
        super()._exit(price, timestamp, reason)
        trade = self.trades[-1]
        candidate = self._candidate_by_time.get(trade["signal_time"])
        if candidate and reason not in ("end_of_data", "data_session_end"):
            candidate.update(label=int(trade["net_pnl"] > 0), net_pnl=trade["net_pnl"],
                             net_r=trade["net_r"], label_exit_time=trade["exit_time"],
                             label_available_at=(timestamp+timedelta(minutes=5)).isoformat())

    def feed(self, b):
        super().feed(b)
        marked = self.equity
        if self.position:
            p = self.position
            # Conservative close mark including estimated adverse exit and both fees.
            marked += p["direction"]*(b.close-p["entry"])*p["quantity"]*self.c.point_value
            marked -= (self.c.slippage*self.c.point_value+2*self.c.commission)*p["quantity"]
        self.marked_equity.append(marked)


def complete_sessions(bars: list) -> tuple[dict, list]:
    """Keep full sessions only; fail on malformed order, duplicates, or gaps."""
    sessions = {}
    previous = None
    for b in bars:
        local = b.timestamp.astimezone(NY)
        if previous and b.timestamp <= previous:
            raise ValueError("bars must be strictly increasing")
        if local.weekday() >= 5 or not time(9, 30) <= local.time() <= time(15, 55):
            raise ValueError("bars must be regular-session weekdays")
        if local.minute % 5 or local.second or local.microsecond:
            raise ValueError("expected start-labelled five-minute bars")
        key = local.date().isoformat()
        group = sessions.setdefault(key, [])
        if group and b.timestamp-group[-1].timestamp != timedelta(minutes=5):
            raise ValueError(f"intraday data gap in {key}")
        group.append(b)
        previous = b.timestamp
    kept, excluded = {}, []
    for key, group in sessions.items():
        if len(group) == 78 and group[0].timestamp.astimezone(NY).time() == time(9, 30) and group[-1].timestamp.astimezone(NY).time() == time(15, 55):
            kept[key] = group
        else:
            excluded.append({"session": key, "bars": len(group),
                             "reason": "partial_or_early_close_session_not_supported"})
    return kept, excluded


def split_sessions(sessions: dict, train_fraction=.6, validation_fraction=.2) -> dict:
    days = sorted(sessions)
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1 or train_fraction+validation_fraction >= 1:
        raise ValueError("invalid chronological split fractions")
    first = int(len(days)*train_fraction)
    second = int(len(days)*(train_fraction+validation_fraction))
    return {"train": days[:first], "validation": days[first:second], "test": days[second:]}


def purge_labels(candidates: list, partition_start: datetime, partition_end: datetime) -> tuple[list, int]:
    """Exclude labels whose feature/outcome interval crosses partition bounds."""
    kept = [x for x in candidates if "label" in x and
            partition_start <= datetime.fromisoformat(x["available_at"]) < partition_end and
            datetime.fromisoformat(x["available_at"]) <= datetime.fromisoformat(x["label_available_at"]) <= partition_end]
    return kept, sum("label" in x for x in candidates)-len(kept)


def fit_model(rows: list, synthetic=False) -> JSONModel:
    """Fit training rows only; scaling and coefficients are persisted as JSON."""
    try:
        import sklearn
        from sklearn.exceptions import ConvergenceWarning
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise RuntimeError("install the research extra to train: pip install '.[research]'") from exc
    if len(rows) < 2 or set(x["label"] for x in rows) != {0, 1}:
        raise ValueError("training requires both classes")
    values = [[x["features"][name] for name in FEATURE_NAMES] for x in rows]
    scaler = StandardScaler().fit(values)
    model = LogisticRegression(C=1.0, solver="liblinear", random_state=0, max_iter=1000)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        model.fit(scaler.transform(values), [x["label"] for x in rows])
    return JSONModel({
        "schema_version": 1, "kind": "logistic_regression", "feature_version": FEATURE_VERSION,
        "feature_names": list(FEATURE_NAMES), "mean": scaler.mean_.tolist(),
        "scale": scaler.scale_.tolist(), "coefficients": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]), "positive_class": "net_trade_pnl_above_zero",
        "synthetic": bool(synthetic), "sklearn_version": sklearn.__version__,
        "training_samples": len(rows), "promotion_eligible": False,
    })


def probability_metrics(rows: list, model: JSONModel) -> dict:
    started = perf_counter()
    predictions = [(model.predict_probability(x["features"]), x["label"]) for x in rows]
    elapsed = perf_counter()-started
    n = len(predictions)
    bins = []
    for i in range(5):
        values = [(p, y) for p, y in predictions if i/5 <= p < (i+1)/5 or i == 4 and p == 1]
        bins.append({"lower": i/5, "upper": (i+1)/5, "count": len(values),
                     "mean_probability": sum(p for p, _ in values)/len(values) if values else None,
                     "observed_win_rate": sum(y for _, y in values)/len(values) if values else None})
    return {"samples": n, "brier_score": sum((p-y)**2 for p, y in predictions)/n if n else None,
            "log_loss": -sum(y*math.log(max(1e-15, p))+(1-y)*math.log(max(1e-15, 1-p)) for p, y in predictions)/n if n else None,
            "mean_local_inference_ms": 1000*elapsed/n if n else None,
            "model_api_cost_usd": 0,
            "calibration_bins": bins}


class _PriorVolumeFilter:
    """Fixed comparator, selected before examining any outcomes."""
    def predict_probability(self, features):
        return float(features["relative_volume"] >= 1)


def _replay(bars, settings, model=None, threshold=.5):
    bot = CandidateBot(settings, model, threshold)
    for b in bars:
        bot.feed(b)
    bot.finish()
    if any(x["exit_reason"] in ("end_of_data", "data_session_end") for x in bot.trades):
        raise ValueError("unexpected boundary-forced trade; refusing training labels")
    return bot


def _metrics(bot):
    peak, drawdown = bot.c.capital, 0
    for equity in bot.marked_equity:
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak-equity)/peak)
    return {**bot.stats(), "return_fraction": bot.equity/bot.c.capital-1,
            "max_bar_close_drawdown": drawdown, "candidate_count": len(bot.candidates),
            "taken_candidates": sum(x["taken"] for x in bot.candidates),
            "turnover_fraction": sum((abs(t["entry"])+abs(t["exit"]))*t["quantity"]*bot.c.point_value for t in bot.trades)/bot.c.capital}


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False)+"\n")


def _provenance(data: Path, symbol: str, raw: bytes, synthetic: bool, manifest_path=None):
    if synthetic:
        return {"source": "synthetic", "feed": "synthetic", "adjustment": "synthetic"}, None
    path = Path(manifest_path) if manifest_path else data.parent/"manifest.json"
    if not path.exists():
        if manifest_path:
            raise ValueError("dataset manifest does not exist")
        return {"source": "unverified_csv", "feed": "unknown", "adjustment": "unknown"}, None
    manifest = json.loads(path.read_bytes())
    expected_path = manifest.get("bars", {}).get(symbol, {}).get("5Min")
    if not expected_path or (path.parent/expected_path).resolve() != data.resolve():
        raise ValueError("input is not the manifest's five-minute dataset for this symbol")
    hashes = [x["sha256"] for x in manifest.get("files", []) if x.get("path") == expected_path]
    if hashes != [hashlib.sha256(raw).hexdigest()]:
        raise ValueError("input checksum does not match dataset manifest")
    keys = ("schema_version", "start", "end", "symbols", "feed", "adjustment", "files")
    try:
        fingerprint = {key: manifest[key] for key in keys}
    except KeyError as exc:
        raise ValueError("incomplete dataset provenance") from exc
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    if digest != manifest.get("dataset_sha256"):
        raise ValueError("dataset fingerprint mismatch")
    if manifest.get("source") != "alpaca" or manifest.get("feed") not in ("sip", "iex"):
        raise ValueError("unsupported verified dataset source/feed")
    return {key: manifest[key] for key in ("source", "feed", "adjustment", "dataset_sha256")}, manifest


def _log_mlflow(directory: Path, report: dict, tracking_uri: str):
    from urllib.parse import urlparse
    parsed = urlparse(tracking_uri)
    if parsed.scheme == "sqlite":
        if not tracking_uri.startswith("sqlite:///") or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("MLflow SQLite URI must be a local database path")
    elif parsed.scheme == "file":
        if parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("MLflow file URI must be local")
    elif parsed.scheme or tracking_uri.startswith("//"):
        raise ValueError("only local MLflow tracking is supported")
    try:
        from mlflow import MlflowClient
    except ImportError as exc:
        raise RuntimeError("install the tracking extra to log MLflow runs") from exc
    client = MlflowClient(tracking_uri=tracking_uri)
    name = "dwight-vwap-research"
    experiment = client.get_experiment_by_name(name)
    if experiment:
        location = urlparse(experiment.artifact_location)
        if location.scheme not in ("", "file") or location.netloc:
            raise ValueError("existing MLflow experiment must store artifacts locally")
    eid = experiment.experiment_id if experiment else client.create_experiment(
        name, artifact_location=(directory.parent/"mlflow-artifacts").resolve().as_uri())
    run = client.create_run(eid, tags={"dwight.run_id": report["run_id"],
                                     "dwight.synthetic": str(report["synthetic"]),
                                     "dwight.status": report["status"]})
    rid = run.info.run_id
    for key in ("symbol", "input_sha256", "code_sha256", "feature_version"):
        client.log_param(rid, key, report[key])
    for partition, values in report.get("evaluation", {}).items():
        for variant in ("baseline", "simple_volume", "filtered"):
            for key, value in values.get(variant, {}).items():
                if isinstance(value, (int, float)):
                    client.log_metric(rid, f"{partition}.{variant}.{key}", value)
        for key, value in values.get("probabilities_on_baseline_candidates", {}).items():
            if isinstance(value, (int, float)):
                client.log_metric(rid, f"{partition}.prediction.{key}", value)
    report["mlflow_run_id"] = rid
    report["tracking_status"] = "completed"
    _write(directory/"report.json", report)
    for path in sorted(directory.glob("*.json")):
        client.log_artifact(rid, str(path))
    client.set_terminated(rid)


def experiment(data: Path, symbol: str, output: Path, synthetic=False, config: dict | None = None) -> dict:
    """Run baseline, fit a classifier, select threshold, then test once.

    config accepts {'strategy': Config overrides, **DEFAULTS overrides}; flat
    legacy Config overrides also work. Small minimums are accepted only when
    synthetic=True. Every completed experiment remains research-only: promotion
    requires a separate release review and successful live shadow validation.
    """
    if symbol != "QQQ":
        raise ValueError("Dwight equity experiments are restricted to QQQ")
    cfg = dict(config or {})
    strategy = dict(cfg.pop("strategy", {}))
    for key in set(cfg) & set(Config.__dataclass_fields__):
        strategy[key] = cfg.pop(key)
    if set(cfg)-set(DEFAULTS):
        raise ValueError(f"unknown experiment settings: {sorted(set(cfg)-set(DEFAULTS))}")
    options = {**DEFAULTS, **cfg}
    for key in ("min_train_samples", "min_validation_samples", "min_test_samples", "min_class_samples", "min_validation_trades"):
        if type(options[key]) is not int or options[key] < 1:
            raise ValueError(f"{key} must be positive")
        if not synthetic and options[key] < DEFAULTS[key]:
            raise ValueError("reduced sample requirements are allowed only for synthetic smoke experiments")
    if options["dataset_manifest"] is not None:
        options["dataset_manifest"] = str(options["dataset_manifest"])
    if not options["thresholds"] or any(type(x) not in (int, float) or not 0 < x < 1 for x in options["thresholds"]):
        raise ValueError("thresholds must be probabilities between zero and one")
    settings = Config(**strategy)
    raw = Path(data).read_bytes()
    provenance, dataset_manifest = _provenance(Path(data), symbol, raw, synthetic, options["dataset_manifest"])
    directory = Path(output)/uuid.uuid4().hex
    directory.mkdir(parents=True)
    (directory/"input.csv").write_bytes(raw)
    if dataset_manifest:
        _write(directory/"dataset-manifest.json", dataset_manifest)
    try:
        sessions, excluded = complete_sessions(list(read_bars(directory/"input.csv")))
        partitions = split_sessions(sessions, options["train_fraction"], options["validation_fraction"])
        code_paths = [Path(__file__), Path(__file__).parent.parent/"vwap_bot"/"engine.py"]
        report = {
            "schema_version": 1, "run_id": directory.name, "directory": str(directory),
            "status": "insufficient_data", "symbol": symbol, "synthetic": bool(synthetic),
            **provenance,
            "promotion_eligible": False, "feature_version": FEATURE_VERSION,
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "code_sha256": hashlib.sha256(b"".join(p.read_bytes() for p in code_paths)).hexdigest(),
            "strategy": asdict(settings), "experiment_settings": options,
            "complete_sessions": len(sessions), "excluded_sessions": excluded,
            "partitions": partitions, "sample_counts": {}, "evaluation": {},
            "simple_comparator": "take when current volume >= mean volume of up to 20 previous session bars; no tuning",
            "limitations": [
                "Research only; no model is automatically approved for paper deployment.",
                "Training labels cover baseline-selected, closed trades; filtered paths may expose new candidates.",
                "Only full 78-bar sessions are supported; early-close and partial sessions are excluded.",
                "OHLC fills are simulated; costs are fixed assumptions and quote/spread data is not used.",
                "Drawdown uses five-minute close marks, not worst intrabar equity.",
                "Repeated experiments on the same holdout contaminate it; reserve a new final period before promotion.",
            ],
            "blocking_reasons": [],
        }
        bars_by_partition, rows = {}, {}
        candidates = {}
        for name, days in partitions.items():
            bars = [b for day in days for b in sessions[day]]
            bars_by_partition[name] = bars
            baseline = _replay(bars, settings)
            candidates[name] = baseline.candidates
            labeled, purged = purge_labels(baseline.candidates, bars[0].timestamp, bars[-1].timestamp+timedelta(minutes=5)) if bars else ([], 0)
            rows[name] = labeled
            counts = Counter(x["label"] for x in labeled)
            report["sample_counts"][name] = {"labeled": len(labeled), "positive": counts[1], "negative": counts[0],
                                             "purged": purged, "unlabeled": len(baseline.candidates)-len(labeled)-purged}
            report["evaluation"][name] = {"baseline": _metrics(baseline)}
            _write(directory/f"{name}-baseline-trades.json", baseline.trades)
            simple = _replay(bars, settings, _PriorVolumeFilter())
            report["evaluation"][name]["simple_volume"] = _metrics(simple)
            _write(directory/f"{name}-simple-volume-trades.json", simple.trades)
            if not days or len(labeled) < options[f"min_{name}_samples"]:
                report["blocking_reasons"].append(f"{name} needs at least {options[f'min_{name}_samples']} labeled trades")
            if min(counts[0], counts[1]) < options["min_class_samples"]:
                report["blocking_reasons"].append(f"{name} needs at least {options['min_class_samples']} examples of each outcome")
        _write(directory/"candidates.json", candidates)
        _write(directory/"splits.json", partitions)
        if not report["blocking_reasons"]:
            model = fit_model(rows["train"], synthetic)
            trials = []
            for threshold in sorted(set(options["thresholds"])):
                candidate = _replay(bars_by_partition["validation"], settings, model, threshold)
                trials.append({"threshold": threshold, **_metrics(candidate)})
            report["validation_threshold_trials"] = trials
            eligible = [x for x in trials if x["trades"] >= options["min_validation_trades"]]
            if not eligible:
                report["status"] = "insufficient_validation_trades"
                report["blocking_reasons"].append("no threshold produced enough validation trades")
            else:
                # Only validation net P&L chooses the threshold; ties prefer lower drawdown then 0.5.
                best = max(eligible, key=lambda x: (x["net_pnl"], -x["max_bar_close_drawdown"], -abs(x["threshold"]-.5)))
                threshold = best["threshold"]
                report["selected_threshold"] = threshold
                model.artifact.update(threshold=threshold, symbol=symbol,
                                      **provenance,
                                      input_sha256=report["input_sha256"],
                                      code_sha256=report["code_sha256"],
                                      strategy=asdict(settings))
                model = JSONModel(model.artifact)
                for name in partitions:
                    filtered = _replay(bars_by_partition[name], settings, model, threshold)
                    report["evaluation"][name]["filtered"] = _metrics(filtered)
                    report["evaluation"][name]["probabilities_on_baseline_candidates"] = probability_metrics(rows[name], model)
                    _write(directory/f"{name}-filtered-trades.json", filtered.trades)
                    _write(directory/f"{name}-filtered-candidates.json", filtered.candidates)
                report["status"] = "completed_synthetic_smoke" if synthetic else "completed_research"
                test = report["evaluation"]["test"]
                report["heldout_net_pnl_improved"] = test["filtered"]["net_pnl"] > test["baseline"]["net_pnl"]
                report["blocking_reasons"] = ["synthetic data cannot support deployment" if synthetic else "requires release review and live shadow validation"]
                _write(directory/"model.json", model.artifact)
                report["model_sha256"] = hashlib.sha256((directory/"model.json").read_bytes()).hexdigest()
        _write(directory/"report.json", report)
        if options["tracking_uri"]:
            try:
                _log_mlflow(directory, report, options["tracking_uri"])
            except Exception as exc:
                report["tracking_status"] = "failed"
                report["tracking_error_type"] = type(exc).__name__
                _write(directory/"report.json", report)
                raise
        return report
    except Exception as exc:
        _write(directory/"failed.json", {"error_type": type(exc).__name__})
        raise
