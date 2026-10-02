"""Fixed synthetic cases for the public research dashboard.

Every plotted portfolio is replayed with its own policy. Inputs, fitted models
and saved evidence are temporary; detached JSON is the only retained state.
There are no uploads, user supplied paths, network calls or broker connectors.
"""
from datetime import timedelta
from functools import lru_cache
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

from dwight.audit import audit_experiment
from dwight.connectors.csv import read_bars
from dwight.experiments import (
    JSONModel, NY, _PriorVolumeFilter, _metrics, _replay, complete_sessions,
    experiment, purge_labels,
)
from examples.make_experiment_demo import generate
from vwap_bot.engine import Config

from .research import DEMO_CONFIG, DEMO_DAYS, _public_report


CASE_IDS = ("seed42", "seed43", "seed44", "coststress")
CASE_LABELS = {
    "seed42": "Synthetic path 42",
    "seed43": "Synthetic path 43",
    "seed44": "Synthetic path 44",
    "coststress": "Path 42 with doubled costs",
}
VARIANT_LABELS = {
    "baseline": "VWAP baseline",
    "simple_volume": "VWAP + simple volume filter",
    "filtered": "VWAP + Dwight classifier",
}


def _case(case_id: str) -> dict:
    if type(case_id) is not str or case_id not in CASE_IDS:
        raise ValueError("Select one of the four fixed synthetic cases")
    return {
        "label": CASE_LABELS[case_id],
        "seed": 42 if case_id == "coststress" else int(case_id[-2:]),
        "cost_multiplier": 2 if case_id == "coststress" else 1,
    }


def _candles(bars, config: Config) -> list:
    result = []
    session, price_volume, volume, ema = None, 0, 0, None
    for bar in bars:
        day = bar.timestamp.astimezone(NY).date().isoformat()
        if day != session:
            session, price_volume, volume, ema = day, 0, 0, None
        price_volume += (bar.high + bar.low + bar.close) / 3 * bar.volume
        volume += bar.volume
        ema = bar.close if ema is None else ema + 2 / (config.ema_period + 1) * (bar.close - ema)
        result.append({
            "timestamp": bar.timestamp.isoformat(),
            "available_at": (bar.timestamp + timedelta(minutes=5)).isoformat(),
            "session": day,
            **{key: getattr(bar, key) for key in ("open", "high", "low", "close", "volume")},
            "vwap": price_volume / volume if volume else None,
            "ema20": ema,
        })
    return result


def _portfolio(bars, config, policy=None, threshold=.5) -> dict:
    bot = _replay(bars, config, policy, threshold)
    # The initial point makes the capital baseline explicit. Every later point
    # is timestamped when that completed bar becomes observable.
    curve = [{"timestamp": bars[0].timestamp.isoformat(), "bar_timestamp": None,
              "equity": config.capital, "realized_equity": config.capital,
              "drawdown_fraction": 0}]
    peak = config.capital
    for bar, equity, realized in zip(bars, bot.marked_equity, bot.curve, strict=True):
        peak = max(peak, equity)
        curve.append({
            "timestamp": (bar.timestamp + timedelta(minutes=5)).isoformat(),
            "bar_timestamp": bar.timestamp.isoformat(),
            "equity": equity,
            "realized_equity": realized["realized_equity"],
            "drawdown_fraction": (peak - equity) / peak,
        })
    return {"metrics": _metrics(bot), "curve": curve,
            "trades": bot.trades, "candidates": bot.candidates}


def _classifier_evidence(rows: list, model: JSONModel, calibration: dict) -> tuple[list, dict]:
    artifact = model.artifact
    predictions, contributions = [], []
    for row in rows:
        terms = []
        for name, mean, scale, coefficient in zip(
                artifact["feature_names"], artifact["mean"], artifact["scale"],
                artifact["coefficients"], strict=True):
            value = row["features"][name]
            standardized = (value - mean) / scale
            terms.append({"feature": name, "value": value,
                          "standardized_value": standardized, "coefficient": coefficient,
                          "contribution": coefficient * standardized})
        contributions.append(terms)
        predictions.append({
            **{key: row[key] for key in (
                "signal_time", "available_at", "label_available_at", "label", "net_pnl", "net_r")},
            "probability": model.predict_probability(row["features"]),
        })
    averaged = [{
        "feature": name, "coefficient": artifact["coefficients"][index],
        "mean_abs_contribution": sum(abs(terms[index]["contribution"]) for terms in contributions) / len(rows),
    } for index, name in enumerate(artifact["feature_names"])] if rows else []
    example = None
    if rows:
        example = {
            "selection": "First labeled baseline candidate in chronological test order",
            "signal_time": rows[0]["signal_time"],
            "probability": predictions[0]["probability"],
            "intercept": artifact["intercept"],
            "logit": math.fsum([artifact["intercept"], *[term["contribution"] for term in contributions[0]]]),
            "contributions": contributions[0],
        }
    return predictions, {
        "kind": "logistic_regression",
        "transformer_active": False,
        "calibration": calibration,
        "feature_contributions": averaged,
        "example": example,
        "interpretation": (
            "Each contribution is the fitted coefficient multiplied by a feature standardized with training statistics. "
            "The chart averages its absolute size over labeled baseline candidates in the test period. "
            "These values explain the logistic score, not causal effects, future importance or trading profit. "
            "Calibration covers baseline candidates; filtering can expose a different sequence of candidates. "
            "No transformer or language model is active in these experiments."
        ),
    }


@lru_cache(maxsize=4)
def _cached_dashboard_json(case_id: str) -> str:
    case = _case(case_id)
    config = json.loads(json.dumps(DEMO_CONFIG))
    if case["cost_multiplier"] == 2:
        config["strategy"] = {"commission": Config().commission * 2, "slippage": Config().slippage * 2}
    with TemporaryDirectory(prefix="dwight-dashboard-") as temporary:
        root = Path(temporary)
        data = root / "synthetic-QQQ-5Min.csv"
        count = generate(data, days=DEMO_DAYS, seed=case["seed"])
        report = experiment(data, "QQQ", root / "experiment", synthetic=True, config=config)
        directory = Path(report["directory"])
        if report["status"] != "completed_synthetic_smoke":
            raise ValueError("This fixed synthetic case did not produce a complete experiment")
        public = _public_report(report, count)
        public["space_demo"].update(
            seed=case["seed"], case_id=case_id,
            cache="At most four fixed results in process memory; reset on process restart",
            model_published=False,
            model_details_exposed=True,
            fixture_note="Invented prices and volumes, reset to 100 each session; weekdays include exchange holidays",
        )
        model = JSONModel.load(directory / "model.json", report["model_sha256"])
        sessions, _ = complete_sessions(list(read_bars(directory / "input.csv")))
        bars = [bar for day in report["partitions"]["test"] for bar in sessions[day]]
        settings = Config(**report["strategy"])
        variants = {
            "baseline": _portfolio(bars, settings),
            "simple_volume": _portfolio(bars, settings, _PriorVolumeFilter()),
            "filtered": _portfolio(bars, settings, model, report["selected_threshold"]),
        }
        for name, portfolio in variants.items():
            if portfolio["metrics"] != report["evaluation"]["test"][name]:
                raise ValueError("Dashboard replay differs from the saved test metrics")
            suffix = "simple-volume" if name == "simple_volume" else name
            if portfolio["trades"] != json.loads((directory / f"test-{suffix}-trades.json").read_text()):
                raise ValueError("Dashboard trades differ from saved test evidence")
        rows, _ = purge_labels(variants["baseline"]["candidates"], bars[0].timestamp,
                               bars[-1].timestamp + timedelta(minutes=5))
        probabilities, classifier = _classifier_evidence(
            rows, model, report["evaluation"]["test"]["probabilities_on_baseline_candidates"])
        audit = audit_experiment(directory)
        result = {
            "schema_version": 1,
            "case_id": case_id,
            "case": case,
            "report": public,
            "model": model.artifact,
            "audit": audit,
            "test": {
                "candles": _candles(bars, settings),
                "variants": variants,
                "probabilities": probabilities,
                "initial_capital": settings.capital,
                "default_session": (variants["filtered"]["trades"][-1]["entry_time"][:10]
                                    if variants["filtered"]["trades"] else report["partitions"]["test"][-1]),
                "bar_timestamp_semantics": "Five minute bar open; its values are available five minutes later",
                "curve_timestamp_semantics": "Initial capital, then equity marked at each completed bar close",
            },
            "classifier": classifier,
            "provenance": {
                "data_kind": "synthetic",
                "scope": "chronological_test_partition",
                "real_market_data": False,
                "broker_orders_submitted": 0,
                "transformer_experiments_run": 0,
                "artifacts_removed_after_run": True,
                "model_promotion_eligible": False,
                "split_method": "First 60 percent of complete sessions train, next 20 percent validate, remainder test",
                "threshold_selection": "Validation net profit, then lower drawdown and proximity to 0.5 for ties",
                "cost_note": "Fixed per share commissions on both sides; adverse entry, stop and session exit slippage; no spread or queue model",
                "curve_note": "Unrealized positions marked at bar close with estimated exit costs; intrabar drawdown can be worse",
                "indicator_note": "Session bar typical price volume weighted VWAP approximation and session reset EMA20",
                "limitations": [
                    "Generated QQQ shaped fixtures do not use QQQ market observations and cannot demonstrate a trading edge.",
                    "Four selected fixture runs are software robustness checks, not confidence intervals or live trading evidence.",
                    "The cost case retrains and retunes under higher costs, so its difference is not pure execution cost attribution.",
                    "Trade times identify five minute bars; an intrabar stop or target does not have an exact observed fill time.",
                ],
            },
        }
    return json.dumps(result, allow_nan=False)


def run_dashboard_experiment(case_id="seed42") -> dict:
    """Run a fixed case once and return a fresh, JSON compatible visitor copy."""
    _case(case_id)
    return json.loads(_cached_dashboard_json(case_id))


def run_robustness_summary() -> dict:
    """Compare all predeclared fixtures without returning their full chart data."""
    rows = []
    for case_id in CASE_IDS:
        result = run_dashboard_experiment(case_id)
        test = result["report"]["evaluation"]["test"]
        rows.append({
            "case_id": case_id, **result["case"],
            "baseline_net_pnl": test["baseline"]["net_pnl"],
            "simple_volume_net_pnl": test["simple_volume"]["net_pnl"],
            "filtered_net_pnl": test["filtered"]["net_pnl"],
            "filtered_minus_baseline": test["filtered"]["net_pnl"] - test["baseline"]["net_pnl"],
            "filtered_trades": test["filtered"]["trades"],
            "filtered_drawdown_fraction": test["filtered"]["max_bar_close_drawdown"],
            "selected_threshold": result["report"]["selected_threshold"],
            "audit_status": result["audit"]["status"],
            "input_sha256": result["report"]["input_sha256"],
        })
    ordinary = [row for row in rows if row["case_id"] != "coststress"]
    return {
        "rows": rows,
        "ordinary_cases": len(ordinary),
        "ordinary_cases_model_ahead": sum(row["filtered_minus_baseline"] > 0 for row in ordinary),
        "interpretation": "Generated fixtures test software robustness only. The cost case shares path 42 and retrains under doubled costs. No case supports model promotion.",
        "synthetic": True,
        "promotion_eligible": False,
    }
