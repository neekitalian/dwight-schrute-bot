"""Bounded, synthetic-only experiment exposed by the Hugging Face research UI.

There are no user-supplied inputs, uploads, broker connectors, or network calls.
The core experiment writes into a temporary directory. Only a path-free JSON
report is retained in memory; neither a model nor a release is published.
"""
from functools import lru_cache
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from dwight.experiments import experiment
from examples.make_experiment_demo import generate


DEMO_DAYS = 500
DEMO_SEED = 42
COMPARISON_HEADERS = [
    "Strategy", "Trades", "Net P&L ($, simulated)", "Return (%)",
    "Win rate (%)", "Drawdown at bar close (%)",
]
DEMO_CONFIG = {
    "min_train_samples": 10,
    "min_validation_samples": 4,
    "min_test_samples": 4,
    "min_class_samples": 1,
    "min_validation_trades": 1,
    "train_fraction": 0.6,
    "validation_fraction": 0.2,
    "thresholds": [0.35, 0.5, 0.65],
    "tracking_uri": None,
    "dataset_manifest": None,
}


def _public_report(report: dict, sessions: int) -> dict:
    """Allowlist research values; never return an internal artifact path."""
    if report.get("synthetic") is not True or report.get("promotion_eligible") is not False:
        raise ValueError("The research Space only accepts unapproved synthetic experiments")
    if report.get("symbol") != "QQQ" or report.get("source") != "synthetic":
        raise ValueError("The research Space only accepts synthetic QQQ fixtures")
    fields = (
        "schema_version", "run_id", "status", "symbol", "synthetic", "source",
        "feed", "adjustment", "promotion_eligible", "feature_version",
        "input_sha256", "code_sha256", "strategy", "complete_sessions",
        "excluded_sessions", "partitions", "sample_counts", "evaluation",
        "simple_comparator", "limitations", "blocking_reasons",
        "validation_threshold_trials", "selected_threshold", "model_sha256",
        "heldout_net_pnl_improved",
    )
    result = {key: report[key] for key in fields if key in report}
    result["experiment_settings"] = {
        key: value for key, value in report["experiment_settings"].items()
        if key not in {"tracking_uri", "dataset_manifest"}
    }
    result["space_demo"] = {
        "calendar_days": DEMO_DAYS,
        "seed": DEMO_SEED,
        "generated_weekday_sessions": sessions,
        "real_market_data": False,
        "broker_connected": False,
        "model_published": False,
        "persistent_storage": False,
        "tracking": "disabled",
        "cache": "One result in process memory; reset on process restart",
        "fixture_note": "Invented prices and volumes; weekdays include exchange holidays",
    }
    # Round-trip also detaches every nested value from the internal report.
    return json.loads(json.dumps(result, allow_nan=False))


@lru_cache(maxsize=1)
def _cached_report_json() -> str:
    with TemporaryDirectory(prefix="dwight-research-") as temporary:
        directory = Path(temporary)
        data = directory / "synthetic-QQQ-5Min.csv"
        sessions = generate(data, days=DEMO_DAYS, seed=DEMO_SEED)
        report = experiment(
            data, symbol="QQQ", output=directory / "experiment",
            synthetic=True, config=dict(DEMO_CONFIG),
        )
        public = _public_report(report, sessions)
    # The temporary input, trades and model have been removed before returning.
    return json.dumps(public, allow_nan=False)


def run_synthetic_experiment() -> dict:
    """Compute once per process, returning an independent report per visitor."""
    return json.loads(_cached_report_json())


def present_experiment(report: dict) -> tuple[str, list[list], str, dict]:
    """Present only the final test partition; fit/tuning results stay in JSON."""
    test = report["evaluation"]["test"]
    comparison = []
    for key, title in (
        ("baseline", "VWAP baseline"),
        ("simple_volume", "VWAP + simple volume filter"),
        ("filtered", "VWAP + trained classifier"),
    ):
        if key not in test:
            continue
        metrics = test[key]
        comparison.append([
            title, metrics["trades"], round(metrics["net_pnl"], 2),
            round(100 * metrics["return_fraction"], 2),
            round(100 * metrics["win_rate"], 2),
            round(100 * metrics["max_bar_close_drawdown"], 2),
        ])
    counts = report["sample_counts"]
    count_text = " · ".join(
        f"{name}: **{counts[name]['labeled']}** labeled trades"
        for name in ("train", "validation", "test")
    )
    summary = (
        f"**{report['status'].replace('_', ' ').capitalize()}** · "
        f"{report['complete_sessions']} fabricated sessions\n\n"
        f"{count_text}\n\n"
        "The table shows the final chronological test period. Prices, volume, "
        "fills and returns are simulated. These values check the experiment "
        "pipeline; they do not demonstrate a trading edge. **Deployment eligible: no.**"
    )
    if "model_sha256" in report:
        model = (
            f"**Model:** logistic regression · **Feature version:** `{report['feature_version']}`\n\n"
            f"**Take threshold:** {report['selected_threshold']:g} "
            "(selected using validation data only)\n\n"
            f"**Model SHA-256:** `{report['model_sha256']}`\n\n"
            "The temporary model artifact is deleted after evaluation. "
            "Only this report is cached in memory."
        )
    else:
        model = "**No model produced:** " + "; ".join(report["blocking_reasons"])
    return summary, comparison, model, report
