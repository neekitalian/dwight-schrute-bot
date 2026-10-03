"""One fixed synthetic walk-forward result for the public research Space.

No private inputs, network, broker connectors, or model promotion. Temporary
research artifacts are removed before a detached, path-free report is cached.
"""
from datetime import datetime, timedelta
from functools import lru_cache
from html import escape
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

import plotly.graph_objects as go

from dwight.walkforward import run_walkforward
from examples.make_experiment_demo import generate
from .charts import _layout, CHART_LABELS, COLORS, GRID, MUTED
from . import presentation as ui


DAYS = 500
SEED = 42
CONFIG = {
    "train_sessions": 80, "validation_sessions": 40, "test_sessions": 40,
    "holdout_sessions": 40, "max_windows": 3, "mode": "expanding", "long_only": True,
    "min_train_samples": 4, "min_validation_samples": 2, "min_test_samples": 2,
    "min_class_samples": 1, "min_validation_trades": 1,
    "thresholds": [0.35, 0.5, 0.65], "dataset_manifest": None,
}
_LOCK = Lock()
VARIANTS = ("baseline", "simple_volume", "filtered")
INTRO = """**One synthetic path, three chronological tests** The window plan is fixed before evaluation.
Each window trains on earlier sessions, chooses its threshold on validation sessions, then evaluates
on a later development test. The latest 40 sessions remain reserved and unscored.

This tab uses invented prices and reduced sample gates to test the research process. It does not
establish an advantage on QQQ. The long-only policy differs from the older Performance tab's
long-and-short experiment, so their results are not interchangeable."""
WORKFLOW_NOTE = """**Your TradingView paper account** Dwight can keep a private proposal queue and
import normalized execution evidence through the local CLI. You review each proposal and place
orders manually in Paper Trading by TradingView. The journal has no order-submission capability;
its CSV schema is not yet a tested native TradingView export adapter. This public Space never
reads your journal, accepts uploads, connects to your account, or sends orders.

**FinRL and fundamental context** The repository has an offline research adapter for testing a
reinforcement-learning take-or-skip policy above VWAP. Fundamental and news inputs require separate
data sources and point-in-time preparation. FinRL is not trained or evaluated in this tab. Its
results and transformer results remain unmeasured; the displayed learned policy is logistic regression.

**Real data and additional features** Private research now supports a validated FirstRate QQQ
sample and a separate comparison of four causal session features against the original classifier.
The available sample is too short to meet the real training requirements. Price data and private
charts stay outside this public Space. The additional features have not established an advantage;
this tab continues to display the original fixed synthetic experiment."""


def _public(report):
    """Copy only research fields, omitting every artifact directory and path."""
    if (report.get("synthetic") is not True or report.get("source") != "synthetic"
            or report.get("symbol") != "QQQ" or report.get("promotion_eligible") is not False):
        raise ValueError("The public walk-forward view requires unapproved synthetic QQQ data")
    holdout = report.get("final_holdout", {})
    if holdout.get("consumed") is not False or holdout.get("evaluation") is not None:
        raise ValueError("The final holdout must remain unscored")
    if report["walkforward_settings"].get("long_only") is not True:
        raise ValueError("The fixed public experiment requires long-only policies")
    final_dates = set(holdout["sessions"])
    seen_tests = set()
    for window in report["windows"]:
        if window.get("selection_uses_test") is not False:
            raise ValueError("Model selection cannot use its test period")
        for part in ("train", "validation", "test"):
            if final_dates.intersection(window[part]):
                raise ValueError("Final holdout dates entered a development window")
        if seen_tests.intersection(window["test"]):
            raise ValueError("Development tests cannot overlap")
        seen_tests.update(window["test"])
        if window["status"].startswith("completed_") and set(window["test_evaluation"]) != set(VARIANTS):
            raise ValueError("Completed windows must report all policies")
    fields = (
        "schema_version", "kind", "status", "symbol", "synthetic", "source", "feed",
        "adjustment", "promotion_eligible", "feature_version", "input_sha256", "code_sha256",
        "strategy", "complete_sessions", "excluded_sessions", "plan", "final_holdout",
        "window_counts", "aggregate", "aggregate_scope", "filtered_ahead_windows",
        "blocking_reasons", "limitations",
    )
    result = {key: report[key] for key in fields if key in report}
    window_fields = (
        "window", "train", "validation", "test", "status", "sample_counts", "test_evaluation",
        "blocking_reasons", "selection_uses_test", "test_consumed", "validation_threshold_trials",
        "selected_threshold", "model_sha256", "filtered_minus_baseline_net_pnl",
    )
    result["windows"] = [{key: window[key] for key in window_fields if key in window} for window in report["windows"]]
    result["walkforward_settings"] = {key: value for key, value in report["walkforward_settings"].items()
                                      if key != "dataset_manifest"}
    result["space_demo"] = {
        "calendar_days": DAYS, "seed": SEED, "real_market_data": False,
        "broker_connected": False, "broker_orders_submitted": 0, "private_data_read": False,
        "model_published": False, "finrl_evaluated": False, "transformer_evaluated": False,
        "artifacts_removed_after_run": True, "persistent_storage": False,
        "cache": "One fixed result in process memory; reset on process restart",
        "fixture_note": "Invented prices and volumes reset each session; weekdays include exchange holidays",
    }
    return json.loads(json.dumps(result, allow_nan=False))


@lru_cache(maxsize=1)
def _cached_walkforward_json():
    with TemporaryDirectory(prefix="dwight-walkforward-public-") as temporary:
        root = Path(temporary)
        data = root / "synthetic-QQQ-5Min.csv"
        generate(data, days=DAYS, seed=SEED)
        report = run_walkforward(data, root / "research", synthetic=True,
                                 config=json.loads(json.dumps(CONFIG)))
        public = _public(report)
    return json.dumps(public, allow_nan=False)


def run_public_walkforward():
    """Inputless, single-flight computation with independent copies per visitor."""
    with _LOCK:
        encoded = _cached_walkforward_json()
    return json.loads(encoded)


def summary_html(report):
    counts = report["window_counts"]
    items = [
        ("Completed windows", f'{counts["completed"]} / {counts["planned"]}', "Insufficient windows remain visible"),
        ("Final holdout", str(len(report["final_holdout"]["sessions"])), "Sessions reserved, never scored"),
        ("Classifier ahead", f'{report.get("filtered_ahead_windows", 0)} / {counts["completed"]}', "Compared with VWAP on completed tests"),
        ("Policy", "Long only", "The same direction rule for every variant"),
    ]
    return '<div class="dw-cards">' + ''.join(
        f'<div class="dw-card"><div class="dw-label">{escape(label)}</div>'
        f'<div class="dw-value">{escape(value)}</div><small>{escape(note)}</small></div>'
        for label, value, note in items) + '</div>'


def _date_range(dates):
    return f"{dates[0]} to {dates[-1]} · {len(dates)} sessions" if dates else "None"


def timeline_figure(report):
    """Calendar spans show exact session ranges; no returns on the holdout row."""
    figure = go.Figure()
    partitions = (
        ("train", "Train", COLORS["baseline"]),
        ("validation", "Validate", COLORS["simple_volume"]),
        ("test", "Test", COLORS["filtered"]),
    )
    for partition, label, color in partitions:
        rows = [(f'Window {w["window"]}', w[partition], w["status"]) for w in report["windows"]]
        _timeline_bars(figure, label, color, rows)
    _timeline_bars(figure, "Unused dev.", MUTED,
                   [("Untouched data", report["plan"]["unused_development_sessions"], "not evaluated")])
    _timeline_bars(figure, "Final holdout", GRID,
                   [("Untouched data", report["final_holdout"]["sessions"], "reserved, not evaluated")])
    rows = [f'Window {w["window"]}' for w in report["windows"]] + ["Untouched data"]
    figure.update_layout(barmode="overlay", bargap=.40)
    figure.update_xaxes(type="date", title_text="Synthetic session date", tickformat="%b %Y")
    figure.update_yaxes(categoryorder="array", categoryarray=rows, autorange="reversed", fixedrange=True)
    figure = _layout(figure, "Train, validate, test, reserve", height=480)
    figure.update_layout(hovermode="closest", margin=dict(l=112, r=20, t=125, b=65),
                         legend=dict(font_size=9, entrywidth=85, entrywidthmode="pixels"))
    return figure


def _timeline_bars(figure, name, color, rows):
    selected = [(label, days, status) for label, days, status in rows if days]
    if not selected:
        return
    figure.add_trace(go.Bar(
        name=name, orientation="h", marker_color=color,
        y=[label for label, _, _ in selected],
        base=[days[0] for _, days, _ in selected],
        x=[(datetime.fromisoformat(days[-1]) + timedelta(days=1) - datetime.fromisoformat(days[0])).total_seconds() * 1000
           for _, days, _ in selected],
        customdata=[[days[0], days[-1], len(days), status] for _, days, status in selected],
        hovertemplate="%{y}<br>%{customdata[0]} to %{customdata[1]}<br>%{customdata[2]} sessions<br>%{customdata[3]}<extra>%{fullData.name}</extra>",
    ))


def pnl_figure(report):
    figure = go.Figure()
    for variant in VARIANTS:
        values, details = [], []
        for window in report["windows"]:
            metrics = window["test_evaluation"].get(variant)
            values.append(metrics["net_pnl"] if metrics else None)
            details.append([window["test"][0], window["test"][-1],
                            metrics["trades"] if metrics else None, window["status"]])
        figure.add_trace(go.Bar(
            name=CHART_LABELS[variant], marker_color=COLORS[variant],
            x=[f'Window {w["window"]}' for w in report["windows"]], y=values,
            customdata=details,
            hovertemplate="%{x}<br>Net P&L $%{y:,.2f}<br>%{customdata[0]} to %{customdata[1]}<br>%{customdata[2]} closed trades<br>%{customdata[3]}<extra>%{fullData.name}</extra>",
        ))
    figure.add_hline(y=0, line_color=MUTED, line_width=1)
    figure.update_layout(barmode="group", bargap=.25)
    figure.update_xaxes(title_text="Independent development test window")
    figure.update_yaxes(title_text="Net simulated P&L / USD", tickprefix="$", zerolinecolor=GRID)
    figure = _layout(figure, "Synthetic test P&L by window", height=480)
    figure.update_layout(hovermode="closest")
    return figure


def window_table(report):
    rows = []
    for window in report["windows"]:
        threshold = window.get("selected_threshold")
        metrics = []
        for variant in VARIANTS:
            value = window["test_evaluation"].get(variant)
            metrics.append(f'{value["net_pnl"]:+,.2f}' if value else "Not scored")
        sample_counts = "; ".join(f'{part}: {counts["labeled"]}' for part, counts in window["sample_counts"].items())
        rows.append([
            f'Window {window["window"]}', escape(window["status"]),
            escape(_date_range(window["train"])), escape(_date_range(window["validation"])),
            escape(_date_range(window["test"])), "Yes" if window["test_consumed"] else "No",
            f"{threshold:.2f}" if threshold is not None else "Not selected",
            *metrics, escape(sample_counts), escape("; ".join(window["blocking_reasons"]) or "None"),
        ])
    return ui.table(["Window", "Status", "Training dates", "Validation dates", "Test dates", "Test inspected",
                     "Threshold", "VWAP / USD", "Volume / USD", "Dwight / USD", "Labeled samples", "Blockers"], rows)


def evidence_note(report):
    counts = report["window_counts"]
    return (
        f'**Run status:** `{report["status"]}`. {counts["completed"]} of {counts["planned"]} windows completed; '
        f'{counts["insufficient"]} remain insufficient.\n\n'
        f'**Final holdout:** {_date_range(report["final_holdout"]["sessions"])}. '
        'Reserved only. No model fitting, threshold tuning or performance score uses these dates.\n\n'
        f'**Unused development dates:** {_date_range(report["plan"]["unused_development_sessions"])}. '
        'The public run stops at three windows.\n\n'
        '**How to read the comparison** Each window starts flat with the same starting capital and cost rules. '
        'The bars compare independent test replays; they are not a continuous account equity curve. '
        'Earlier test dates can become observed training or validation history in a later window. '
        'After you inspect these test outcomes, they belong to development history, not a fresh final test.\n\n'
        '**Limits** Reduced label counts are a software smoke test, not statistical power. '
        'All prices and volumes are invented; the generator resets prices each session and includes weekday holidays. '
        'The replay uses next-bar fills and fixed costs, without real quotes, queues or partial fills. '
        'No result is eligible for deployment.\n\n'
        f'**Input fingerprint:** `{report["input_sha256"]}`. '
        f'**Code fingerprint:** `{report["code_sha256"]}`.'
    )
