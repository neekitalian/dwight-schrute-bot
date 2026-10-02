"""Evidence checks for saved QQQ experiments, without broker or model API calls.

Passing means the saved replay agrees with the current reference engine and the
stated simulation rules. It does not certify a strategy, data licence, future
profit, broker behaviour, or a live deployment.
"""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path

from vwap_bot.engine import Bot, Config
from . import experiments
from .connectors.csv import read_bars
from .experiments import JSONModel, NY, _PriorVolumeFilter, _metrics, _replay


def _same(first, second):
    if type(first) in (int, float) and type(second) in (int, float):
        return math.isfinite(first) and math.isfinite(second) and math.isclose(first, second, rel_tol=1e-10, abs_tol=1e-9)
    if isinstance(first, dict) and isinstance(second, dict):
        return first.keys() == second.keys() and all(_same(first[k], second[k]) for k in first)
    if isinstance(first, list) and isinstance(second, list):
        return len(first) == len(second) and all(_same(a, b) for a, b in zip(first, second))
    return type(first) is type(second) and first == second


def _code_hash():
    engine = Path(experiments.__file__).parent.parent / "vwap_bot" / "engine.py"
    return hashlib.sha256(Path(experiments.__file__).read_bytes() + engine.read_bytes()).hexdigest()


def _exit_event(bar, trade, config):
    """Independently check the first executable exit under the OHLC convention."""
    direction, stop, target = trade["direction"], trade["stop"], trade["target"]
    if direction * (bar.open - stop) <= 0:
        price, reason = bar.open, "gap_stop"
    elif direction * (bar.open - target) >= 0:
        price, reason = target, "target"
    elif (bar.low <= stop if direction == 1 else bar.high >= stop):
        price, reason = stop, "stop"
    elif (bar.high >= target if direction == 1 else bar.low <= target):
        price, reason = target, "target"
    elif bar.timestamp.astimezone(NY).strftime("%H:%M") == "15:55":
        price, reason = bar.close, "session_end"
    else:
        return None
    return reason, price if reason == "target" else price - direction * config.slippage


def _trade_checks(trades, bars, config):
    issues = {name: [] for name in ("entry_timing", "position_sizing", "cost_accounting", "stop_target_execution", "daily_loss_limit")}
    indexed = {bar.timestamp: i for i, bar in enumerate(bars)}
    equity, losses, prior_exit = config.capital, Counter(), None
    for index, trade in enumerate(trades):
        try:
            signal, entry_time, exit_time = (datetime.fromisoformat(trade[key]) for key in ("signal_time", "entry_time", "exit_time"))
            entry_bar = bars[indexed[entry_time]]
            direction, quantity = trade["direction"], trade["quantity"]
            if direction not in (-1, 1) or type(quantity) is not int or quantity < 1:
                raise ValueError("invalid direction or whole share quantity")
            day = entry_time.astimezone(NY).date()
            if (signal not in indexed or entry_time != signal + timedelta(minutes=5)
                    or exit_time < entry_time or exit_time not in indexed
                    or signal.astimezone(NY).date() != day or exit_time.astimezone(NY).date() != day
                    or prior_exit is not None and entry_time <= prior_exit):
                issues["entry_timing"].append(f"trade {index}: invalid signal, entry, exit or overlapping position time")
            expected_entry = entry_bar.open + direction * config.slippage
            distance = direction * (trade["entry"] - trade["stop"])
            unit_risk = (distance + config.slippage) * config.point_value + 2 * config.commission
            if distance <= config.tick or unit_risk <= 0 or direction * (entry_bar.open - trade["stop"]) <= 0:
                issues["position_sizing"].append(f"trade {index}: invalid stop distance")
            else:
                expected_quantity = math.floor(min(equity * config.risk_fraction / unit_risk,
                                                   equity * config.max_leverage / (trade["entry"] * config.point_value)))
                if (quantity != expected_quantity or not _same(trade["risk"], quantity * unit_risk)
                        or quantity * trade["entry"] * config.point_value > equity * config.max_leverage + 1e-8):
                    issues["position_sizing"].append(f"trade {index}: quantity, planned risk or exposure exceeds the configured rule")
            gross = direction * (trade["exit"] - trade["entry"]) * quantity * config.point_value
            fees = 2 * config.commission * quantity
            pnl = gross - fees
            if not all(_same(a, b) for a, b in ((trade["entry"], expected_entry), (trade["gross_pnl"], gross),
                       (trade["fees"], fees), (trade["net_pnl"], pnl), (trade["equity"], equity + pnl),
                       (trade["net_r"], pnl / trade["risk"]))):
                issues["cost_accounting"].append(f"trade {index}: entry slippage, fees or profit ledger does not reconcile")
            raw_target = trade["entry"] + direction * config.reward_r * distance
            target = (math.ceil(raw_target / config.tick) if direction == 1 else math.floor(raw_target / config.tick)) * config.tick
            event = next(((bar.timestamp, *_exit_event(bar, trade, config))
                          for bar in bars[indexed[entry_time]:indexed[exit_time] + 1]
                          if _exit_event(bar, trade, config) is not None), None)
            if (not _same(trade["target"], target) or event is None or event[0] != exit_time
                    or event[1] != trade["exit_reason"] or not _same(event[2], trade["exit"])):
                issues["stop_target_execution"].append(f"trade {index}: target, first exit, stop precedence or exit fill does not match")
            if losses[day] >= config.max_losses:
                issues["daily_loss_limit"].append(f"trade {index}: new entry after the daily losing trade limit")
            losses[day] += int(pnl < 0)
            equity += pnl
            prior_exit = exit_time
        except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
            for entries in issues.values():
                entries.append(f"trade {index}: malformed or unsupported record ({type(exc).__name__})")
    return issues


def audit_experiment(experiment_dir: Path) -> dict:
    """Return JSON compatible audit evidence. Missing artifacts never pass."""
    directory = Path(experiment_dir)
    result = {"schema_version": 1, "scope": "historical_replay", "status": "incomplete", "checks": [],
              "runtime_skills": {"used": [], "description": "This replay invokes coded VWAP rules and an optional logistic model. It does not invoke agent skill tools or an LLM."},
              "paper_compatibility": {"status": "not_verified", "short_trades": 0,
                  "detail": "Replay permits long and short positions. The paper adapter permits long QQQ entries only. No broker fills are audited here."},
              "limitations": [
                  "Passing checks concern recorded historical simulation only. They do not approve paper or live deployment.",
                  "Signal parity checks the reference implementation, not an independent proof that an external article was implemented correctly.",
                  "EMA is an optional pullback touch level. It is not a mandatory trend filter in this strategy.",
                  "Five minute bars cannot prove intrabar order or actual fill availability. Spread, queue position and market impact are not measured.",
                  "The daily limit counts losing trades. It is not a guaranteed maximum cash loss, and gaps can exceed planned stop risk.",
                  "Model training is not repeated by this audit. Training isolation and model selection beyond saved evidence need separate review.",
              ]}

    def check(identifier, name, status, detail, count=0, issues=None):
        result["checks"].append({"id": identifier, "name": name, "status": status, "detail": detail,
                                 "evidence_count": count, "issues": (issues or [])[:20]})

    def finish():
        statuses = {item["status"] for item in result["checks"]}
        result["status"] = "failed" if "failed" in statuses else "incomplete" if "unverified" in statuses else "passed"
        result["check_counts"] = dict(Counter(item["status"] for item in result["checks"]))
        return result

    def read(name):
        return json.loads((directory / name).read_text())

    try:
        report = read("report.json")
        result.update(run_id=report.get("run_id"), symbol=report.get("symbol"), synthetic=report.get("synthetic"))
        check("qqq_scope", "QQQ scope", "passed" if report.get("symbol") == "QQQ" else "failed", "The experiment declares QQQ as its instrument.", 1)
        raw = (directory / "input.csv").read_bytes()
        checksum_matches = hashlib.sha256(raw).hexdigest() == report.get("input_sha256")
        check("input_integrity", "Input snapshot", "passed" if checksum_matches else "failed", "The input checksum is compared with the saved experiment.", 1)
        code_matches = _code_hash() == report.get("code_sha256")
        check("reference_code", "Reference engine version", "passed" if code_matches else "failed", "The current engine and feature code must match the recorded code checksum.", 1)
        settings = Config(**report["strategy"])
        sessions, excluded = experiments.complete_sessions(list(read_bars(directory / "input.csv")))
        splits = read("splits.json")
        expected = experiments.split_sessions(sessions, report["experiment_settings"]["train_fraction"], report["experiment_settings"]["validation_fraction"])
        splits_match = splits == expected == report.get("partitions") and excluded == report.get("excluded_sessions")
        check("chronological_partitions", "Session separation", "passed" if splits_match else "failed", "Training, validation and test use the expected distinct chronological full sessions.", len(sessions))
        if not checksum_matches or not code_matches or not splits_match:
            check("replay_evidence", "Replay evidence", "unverified", "Replay was withheld because input, code or partition identity differs.")
            return finish()
    except FileNotFoundError:
        check("required_artifacts", "Required artifacts", "unverified", "A required report, input snapshot or partition file is missing.")
        return finish()
    except (ValueError, KeyError, TypeError) as exc:
        check("required_artifacts", "Required artifacts", "failed", f"An artifact cannot be validated ({type(exc).__name__}).")
        return finish()

    model = None
    if report.get("model_sha256"):
        try:
            model = JSONModel.load(directory / "model.json", report["model_sha256"])
            coherent = all(_same(model.artifact.get(key), report.get(key)) for key in
                           ("symbol", "strategy", "synthetic", "input_sha256", "code_sha256"))
            coherent = coherent and _same(model.artifact.get("threshold"), report.get("selected_threshold"))
            check("model_integrity", "Frozen model", "passed" if coherent else "failed", "Model checksum, strategy, input, code, provenance flag and decision threshold agree.", 1)
            if not coherent:
                model = None
        except (ValueError, OSError):
            check("model_integrity", "Frozen model", "failed", "The frozen model is missing, invalid or has a different checksum.")
    else:
        check("model_integrity", "Frozen model", "unverified", "This experiment did not produce a frozen model.")

    try:
        saved_candidates = read("candidates.json")
    except (OSError, ValueError):
        saved_candidates = {}
        check("candidate_artifacts", "Candidate evidence", "unverified", "The baseline candidate artifact is missing or invalid.")
    combined = {name: [] for name in ("entry_timing", "position_sizing", "cost_accounting", "stop_target_execution", "daily_loss_limit")}
    trade_count = 0
    for partition in ("train", "validation", "test"):
        bars = [bar for day in splits[partition] for bar in sessions[day]]
        variants = [("baseline", "baseline", None), ("simple_volume", "simple-volume", _PriorVolumeFilter())]
        if model:
            variants.append(("filtered", "filtered", model))
        elif "filtered" in report.get("evaluation", {}).get(partition, {}):
            check(f"{partition}_filtered", f"{partition} model replay", "unverified", "The saved filtered result needs a verified frozen model.")
        for variant, filename, policy in variants:
            prefix = f"{partition}_{variant}"
            try:
                bot = _replay(bars, settings, policy, report.get("selected_threshold", .5) if variant == "filtered" else .5)
                saved_trades = read(f"{partition}-{filename}-trades.json")
                trade_match = _same(saved_trades, bot.trades)
                check(prefix + "_trades", f"{partition} {variant} trade replay", "passed" if trade_match else "failed", "Saved trades are compared with an independent replay of the same frozen inputs.", len(saved_trades))
                metric_match = _same(report["evaluation"][partition][variant], _metrics(bot))
                check(prefix + "_metrics", f"{partition} {variant} metrics", "passed" if metric_match else "failed", "Reported metrics are recomputed from the complete replay path.", len(bars))
                for name, violations in _trade_checks(saved_trades, bars, settings).items():
                    combined[name].extend(f"{partition} {variant}: {message}" for message in violations)
                trade_count += len(saved_trades)
                result["paper_compatibility"]["short_trades"] += sum(trade.get("direction") == -1 for trade in saved_trades)
                if variant == "baseline":
                    reference = Bot(settings)
                    for bar in bars:
                        reference.feed(bar)
                    reference.finish()
                    check(prefix + "_engine", f"{partition} baseline strategy", "passed" if _same(reference.trades, bot.trades) else "failed", "Candidate capture preserves trades from the unchanged reference strategy.", len(bot.trades))
                    candidates = saved_candidates.get(partition)
                elif variant == "filtered":
                    candidates = read(f"{partition}-filtered-candidates.json")
                else:
                    candidates = None
                if variant != "simple_volume":
                    check(prefix + "_candidates", f"{partition} {variant} decisions", "unverified" if candidates is None else "passed" if _same(candidates, bot.candidates) else "failed", "Feature values, completed bar availability, take or skip decisions and later labels are reproduced from sequential bars.", len(bot.candidates))
            except FileNotFoundError:
                check(prefix + "_artifacts", f"{partition} {variant} evidence", "unverified", "A saved trade or candidate artifact is missing.")
            except (ValueError, KeyError, TypeError) as exc:
                check(prefix + "_artifacts", f"{partition} {variant} evidence", "failed", f"Evidence is malformed or replay failed ({type(exc).__name__}).")

    descriptions = {
        "entry_timing": ("Trade timing", "Entries occur at the next candle open after the completed signal candle. Positions do not overlap or cross sessions."),
        "position_sizing": ("Position sizing", "Whole share quantity obeys the prior equity, planned risk budget and gross exposure cap."),
        "cost_accounting": ("Costs and equity", "Entry slippage, round trip fees, net results, R multiples and the equity ledger reconcile."),
        "stop_target_execution": ("Stops and targets", "Rounded targets, first eligible exit, adverse stop slippage and stop priority on ambiguous candles are checked."),
        "daily_loss_limit": ("Daily losing trade limit", "No new entry follows the configured number of closed losing trades in that session."),
    }
    for name, violations in combined.items():
        label, detail = descriptions[name]
        check(name, label, "failed" if violations else "passed" if trade_count else "unverified", detail, trade_count, violations)
    result["audited_trade_records"] = trade_count
    result["paper_compatibility"]["short_trade_count_scope"] = "Sum across all historical partitions and strategy variants, not unique market trades."
    return finish()
