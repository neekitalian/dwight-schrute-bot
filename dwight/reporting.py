"""Portable research reports from verified experiment artifacts.

Charts reconstruct each test portfolio independently and must reproduce the
saved trades and metrics. This module has no market data or broker client and
does not infer live results from a historical replay.
"""
import base64
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re

from vwap_bot.engine import Config
from .connectors.csv import read_bars
from .experiments import JSONModel, NY, _PriorVolumeFilter, _metrics, _replay, complete_sessions, direction_policy, split_sessions

VARIANTS = {
    "baseline": ("VWAP baseline", "baseline", "#579bff"),
    "simple_volume": ("Volume rule", "simple-volume", "#f6b64d"),
    "filtered": ("Model filter", "filtered", "#bb9aff"),
}
MEASUREMENTS = (
    ("elapsed_hours", "Elapsed observation hours"),
    ("observed_sessions", "Exchange sessions observed"),
    ("observed_market_minutes", "Market minutes observed"),
    ("expected_market_minutes", "Market minutes expected"),
    ("missing_market_minutes", "Market minutes missing"),
    ("shadow_decisions", "Forward shadow decisions"),
    ("shadow_takes", "Forward shadow takes"),
    ("stale_decisions", "Stale or catchup decisions"),
    ("error_observations", "Recorded error observations"),
    ("broker_paper_fills", "Broker paper fills"),
    ("last_observation_age_seconds", "Last observation age at window end in seconds"),
)


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False)+"\n")


def _plain(value):
    """Report prose uses words and paragraphs rather than dash punctuation."""
    return re.sub(r"\s+", " ", re.sub(r"[-\u2010-\u2015\u2212_]", " ", str(value))).strip()


def _day(value):
    return datetime.fromisoformat(value).strftime("%B %d, %Y").replace(" 0", " ")


def _window_time(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("campaign window timestamps must include a timezone")
    return moment.astimezone(timezone.utc)


def _readable_time(moment):
    return f"{moment.strftime('%B')} {moment.day}, {moment.year} at {moment.strftime('%H:%M:%S')} UTC"


def _money(value):
    return f"${abs(value):,.2f} {'loss' if value < 0 else 'gain'}"


def _same(actual, expected):
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_same(actual[k], expected[k]) for k in actual)
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(_same(a, b) for a, b in zip(actual, expected))
    if type(actual) in (int, float) and type(expected) in (int, float):
        return math.isfinite(actual) and math.isfinite(expected) and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-8)
    return actual == expected


def _verified_replays(directory, report):
    if report.get("symbol") != "QQQ" or type(report.get("synthetic")) is not bool:
        raise ValueError("report requires QQQ with explicit data provenance")
    if report["synthetic"] != (report.get("source") == "synthetic"):
        raise ValueError("experiment data provenance is inconsistent")
    policy = direction_policy(report)
    long_only = policy == "long_only"
    raw = (directory/"input.csv").read_bytes()
    if hashlib.sha256(raw).hexdigest() != report.get("input_sha256"):
        raise ValueError("experiment input checksum mismatch")
    code = Path(__file__).parent/"experiments.py"
    engine = code.parent.parent/"vwap_bot"/"engine.py"
    if hashlib.sha256(code.read_bytes()+engine.read_bytes()).hexdigest() != report.get("code_sha256"):
        raise ValueError("experiment code changed; reproduce the run with its recorded version before charting")
    sessions, excluded = complete_sessions(list(read_bars(directory/"input.csv")))
    settings_record = report["experiment_settings"]
    configured_long_only = settings_record.get("long_only", False)
    if type(configured_long_only) is not bool or configured_long_only != long_only:
        raise ValueError("experiment direction policy disagrees with settings")
    expected = split_sessions(sessions, settings_record["train_fraction"], settings_record["validation_fraction"])
    if expected != report.get("partitions") or expected != json.loads((directory/"splits.json").read_text()) or excluded != report.get("excluded_sessions"):
        raise ValueError("recorded chronological partitions do not match the dataset")
    days = report.get("partitions", {}).get("test", [])
    if len(days) != len(set(days)) or days != sorted(days) or any(day not in sessions for day in days):
        raise ValueError("invalid recorded test partition")
    bars = [bar for day in days for bar in sessions[day]]
    settings = Config(**report["strategy"])
    evaluation = report.get("evaluation", {}).get("test", {})
    models = {"baseline": None, "simple_volume": _PriorVolumeFilter()}
    if "filtered" in evaluation:
        if not report.get("model_sha256"):
            raise ValueError("filtered experiment has no recorded model checksum")
        model = JSONModel.load(directory/"model.json", report.get("model_sha256"))
        for key in ("input_sha256", "synthetic", "symbol", "strategy", "code_sha256"):
            if model.artifact.get(key) != report.get(key):
                raise ValueError(f"model and experiment disagree on {key}")
        if direction_policy(model.artifact) != policy:
            raise ValueError("model and experiment disagree on direction policy")
        if model.artifact.get("threshold") != report.get("selected_threshold"):
            raise ValueError("model and experiment disagree on threshold")
        models["filtered"] = model
    portfolios = {}
    for variant in VARIANTS:
        if variant not in evaluation:
            continue
        threshold = report["selected_threshold"] if variant == "filtered" else .5
        bot = _replay(bars, settings, models[variant], threshold, long_only=long_only)
        trade_path = directory/f"test-{VARIANTS[variant][1]}-trades.json"
        if not _same(bot.trades, json.loads(trade_path.read_text())):
            raise ValueError(f"saved {variant} trades do not match replay")
        if not _same(_metrics(bot), evaluation[variant]):
            raise ValueError(f"saved {variant} metrics do not match replay")
        portfolios[variant] = bot
    return bars, portfolios


def _source_label(directory, report):
    """A vendor label requires the saved dataset fingerprint and input hash.

    This checks the recorded source chain; it is not independent exchange
    certification and does not grant redistribution or deployment rights.
    """
    if report["synthetic"]:
        return "SYNTHETIC REPLAY"
    if report.get("source") == "alpaca":
        return "HISTORICAL REPLAY"
    if report.get("source") != "firstrate":
        return "UNVERIFIED CSV REPLAY"
    from .firstrate import SAMPLE_URL, LICENSE_URL, sample_acquisition
    manifest = json.loads((directory / "dataset-manifest.json").read_text())
    keys = ("schema_version", "start", "end", "symbols", "feed", "adjustment", "files")
    fingerprint = hashlib.sha256(json.dumps({key: manifest[key] for key in keys}, sort_keys=True).encode()).hexdigest()
    expected_path = manifest.get("bars", {}).get("QQQ", {}).get("5Min")
    recorded = [entry["sha256"] for entry in manifest.get("files", []) if entry.get("path") == expected_path]
    if (manifest.get("schema_version") != "firstrate-sample-rth-bars-v1"
            or manifest.get("synthetic") is not False
            or manifest.get("sample_only") is not True or manifest.get("research_only") is not True
            or manifest.get("live_feed") is not False
            or manifest.get("source_url") != SAMPLE_URL or manifest.get("license_url") != LICENSE_URL
            or manifest.get("source") != "firstrate" or manifest.get("feed") != "firstrate_aggregate"
            or manifest.get("adjustment") != "split" or manifest.get("symbols") != ["QQQ"]
            or not expected_path or recorded != [report["input_sha256"]]
            or fingerprint != manifest.get("dataset_sha256")
            or fingerprint != report.get("dataset_sha256")
            or any(manifest.get(k) != report.get(k) for k in ("source", "feed", "adjustment"))):
        raise ValueError("historical sample provenance mismatch")
    acquisition = sample_acquisition(manifest)
    if report.get("source_acquisition", acquisition) != acquisition:
        raise ValueError("historical sample acquisition mismatch")
    if acquisition == "local_archive":
        return "UNVERIFIED VENDOR CLAIM REPLAY"
    return "HISTORICAL SAMPLE REPLAY"


def _style_axis(axis):
    axis.set_facecolor("#131722")
    axis.tick_params(colors="#aeb9ca", labelsize=8, length=0, pad=8)
    axis.grid(color="#263042", linewidth=.55, alpha=.8)
    axis.set_axisbelow(True)
    for spine in axis.spines.values():
        spine.set_color("#263042")
    axis.yaxis.label.set_color("#aeb9ca")
    axis.xaxis.label.set_color("#aeb9ca")


def _charts(bars, portfolios, report, output, source_label):
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from matplotlib.patches import Rectangle
        from matplotlib.ticker import FuncFormatter
    except ImportError as exc:
        raise RuntimeError("install report chart support: pip install '.[reporting]'") from exc
    chart_paths = {}
    if not bars:
        return chart_paths
    capital = report["strategy"]["capital"]
    fig = Figure(figsize=(12, 6.4), facecolor="#0d111b", layout="constrained")
    FigureCanvasAgg(fig)
    axes = fig.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    for ax in axes:
        _style_axis(ax)
    for variant, bot in portfolios.items():
        label, _, color = VARIANTS[variant]
        equity = [capital, *bot.marked_equity]
        peak = capital
        drawdown = []
        for value in equity:
            peak = max(peak, value)
            drawdown.append((peak-value)/peak*100)
        axes[0].plot(equity, color=color, linewidth=1.45, label=label)
        axes[1].plot(drawdown, color=color, linewidth=1.2)
    axes[0].set_ylabel("Simulated equity · USD")
    axes[0].yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"${x:,.0f}"))
    axes[0].legend(facecolor="#131722", edgecolor="#263042", labelcolor="#dce4f1", loc="upper left", ncol=3)
    axes[1].set_ylabel("Drawdown · %")
    axes[1].invert_yaxis()
    count = min(7, len(bars))
    indices = sorted({round(i*(len(bars)-1)/max(1, count-1)) for i in range(count)})
    axes[1].set_xticks([i+1 for i in indices], [bars[i].timestamp.astimezone(NY).strftime("%b %d %Y") for i in indices])
    axes[1].set_xlabel("Recorded test period · Five minute close marks · New York time")
    fig.suptitle(f"QQQ  |  {source_label}  |  Test portfolios", color="#f0f4fa", fontsize=14, fontweight="bold")
    fig.savefig(output/"performance.png", dpi=135, facecolor=fig.get_facecolor())
    fig.clear()
    chart_paths["performance_chart"] = str(output/"performance.png")

    # A single, explicitly selected session keeps candles readable. Prefer the
    # latest session with a model trade, then a baseline trade, then the last day.
    displayed_variant = "filtered" if "filtered" in portfolios else "baseline"
    trades = portfolios[displayed_variant].trades if displayed_variant in portfolios else []
    session = datetime.fromisoformat(trades[-1]["entry_time"]).astimezone(NY).date() if trades else bars[-1].timestamp.astimezone(NY).date()
    sample = [b for b in bars if b.timestamp.astimezone(NY).date() == session]
    fig = Figure(figsize=(12, 6.1), facecolor="#0d111b", layout="constrained")
    FigureCanvasAgg(fig)
    price, volume = fig.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [4, 1]})
    for ax in (price, volume):
        _style_axis(ax)
    pv = total_volume = 0
    ema = None
    vwap_values, ema_values = [], []
    alpha = 2/(report["strategy"]["ema_period"]+1)
    for index, bar in enumerate(sample):
        color = "#26a69a" if bar.close >= bar.open else "#ef5350"
        price.vlines(index, bar.low, bar.high, color=color, linewidth=1)
        low, high = sorted((bar.open, bar.close))
        price.add_patch(Rectangle((index-.32, low), .64, max(high-low, .001), facecolor=color, edgecolor=color, linewidth=.7))
        volume.bar(index, bar.volume, color=color, width=.64, alpha=.75)
        pv += (bar.high+bar.low+bar.close)/3*bar.volume
        total_volume += bar.volume
        ema = bar.close if ema is None else alpha*bar.close+(1-alpha)*ema
        vwap_values.append(pv/total_volume if total_volume else math.nan)
        ema_values.append(ema)
    price.plot(vwap_values, color="#579bff", linewidth=1.4, label="Session VWAP")
    price.plot(ema_values, color="#f6b64d", linewidth=1.25, label=f"Session EMA {report['strategy']['ema_period']}")
    positions = {bar.timestamp: index for index, bar in enumerate(sample)}
    entry_labeled = exit_labeled = False
    for trade in trades:
        entry, exit_ = datetime.fromisoformat(trade["entry_time"]), datetime.fromisoformat(trade["exit_time"])
        if entry in positions:
            price.scatter(positions[entry], trade["entry"], marker="^" if trade["direction"] == 1 else "v", s=76, facecolor="#f5f6fa", edgecolor="#0d111b", zorder=5, label="Simulated entry" if not entry_labeled else None)
            entry_labeled = True
        if exit_ in positions:
            price.scatter(positions[exit_], trade["exit"], marker="X", s=56, facecolor="#bb9aff", edgecolor="#0d111b", zorder=5, label="Simulated exit" if not exit_labeled else None)
            exit_labeled = True
    price.set_ylabel("Price · USD")
    volume.set_ylabel("Volume")
    volume.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x/1000:.0f}k"))
    volume.set_xticks(list(range(0, len(sample), 12)), [sample[i].timestamp.astimezone(NY).strftime("%H:%M") for i in range(0, len(sample), 12)])
    volume.set_xlabel("Five minute bars · New York time · Markers belong to the displayed portfolio")
    price.legend(facecolor="#131722", edgecolor="#263042", labelcolor="#dce4f1", loc="upper left", ncol=4, fontsize=8)
    price.margins(x=.02)
    fig.suptitle(f"QQQ  |  {source_label}  |  {session.strftime('%B %d, %Y')}\n{VARIANTS[displayed_variant][0]} · Illustrative test session", color="#f0f4fa", fontsize=13, fontweight="bold")
    fig.savefig(output/"price.png", dpi=135, facecolor=fig.get_facecolor())
    fig.clear()
    chart_paths["price_chart"] = str(output/"price.png")
    return chart_paths


def _audit_paragraphs(audit, brief=False):
    if audit is None:
        return ["A strategy audit was not supplied for this report. No compliance conclusion has been recorded."]
    checks = audit.get("checks", [])
    counts = {status: sum(x.get("status") == status for x in checks) for status in ("passed", "failed", "unverified", "not_applicable")}
    paragraphs = [f"The historical replay audit recorded {counts['passed']} passed checks, {counts['failed']} failed checks and {counts['unverified']} unverified checks. This describes the saved research artifacts. It does not certify broker execution."]
    for check in checks:
        if brief and check.get("status") == "passed" and check.get("id") not in ("entry_timing", "position_sizing", "cost_accounting", "stop_target_execution", "daily_loss_limit"):
            continue
        paragraphs.append(f"{_plain(check.get('name', check.get('id', 'Check')))}: {_plain(check.get('status', 'unverified'))}. {_plain(check.get('detail', 'No detail supplied.'))}")
    runtime = audit.get("runtime_skills", {})
    if runtime.get("description"):
        paragraphs.append(_plain(runtime["description"]))
    compatibility = audit.get("paper_compatibility", {})
    if compatibility.get("detail"):
        paragraphs.append(_plain(compatibility["detail"]))
    for limitation in audit.get("limitations", []):
        paragraphs.append(_plain(limitation))
    return paragraphs


def generate_report(experiment_dir: Path, output: Path, *, milestone_label: str = "Initial review", campaign_status: dict | None = None, audit: dict | None = None) -> dict:
    """Create HTML with embedded charts, email prose, PNGs and a JSON summary.

    ``output`` must be a new directory. ``campaign_status`` may supply status,
    blockers, numeric keys in ``MEASUREMENTS``, latest_status, window_start and
    window_end. Observation age is measured at window_end. Absent measurements
    remain unknown. Audit results must belong to this experiment. Delivery is
    intentionally outside this function.
    """
    directory, output = Path(experiment_dir), Path(output)
    report = json.loads((directory/"report.json").read_text())
    bars, portfolios = _verified_replays(directory, report)
    if audit is not None and (audit.get("run_id") != report["run_id"] or audit.get("synthetic") is not report["synthetic"] or audit.get("symbol") != "QQQ"):
        raise ValueError("audit does not belong to the supplied experiment")
    campaign = campaign_status or {}
    for key, _ in MEASUREMENTS:
        if key in campaign and campaign[key] is not None and (type(campaign[key]) not in (int, float) or not math.isfinite(campaign[key]) or campaign[key] < 0):
            raise ValueError(f"invalid campaign measurement: {key}")
    window = {key: _window_time(campaign[key]) if campaign.get(key) is not None else None
              for key in ("window_start", "window_end")}
    if window["window_start"] and window["window_end"] and window["window_start"] > window["window_end"]:
        raise ValueError("campaign window ends before it starts")
    source_label = _source_label(directory, report)
    intro = ("This is a software experiment using generated QQQ data. The prices and profits are simulated. They do not show how the strategy performed in the market." if report["synthetic"] else "This report replays recorded QQQ prices. All fills and profits shown in the charts are simulated. They are not broker paper fills.")
    if source_label == "UNVERIFIED CSV REPLAY":
        intro += " The source of the CSV data has not been verified."
    if source_label == "HISTORICAL SAMPLE REPLAY":
        intro += " Source: FirstRate Data, firstratedata.com. This is a limited vendor sample with split adjusted prices and its own aggregated volume feed. It is not Alpaca SIP data. Keep this report private because it contains licensed price charts."
    if source_label == "UNVERIFIED VENDOR CLAIM REPLAY":
        intro += " This local archive claims to contain a FirstRate Data sample. Its acquisition from the vendor has not been verified. File hashes establish consistency, not vendor authenticity. Keep this report private; the claimed feed uses split adjusted prices and is not Alpaca SIP data."
    period = report.get("partitions", {}).get("test", [])
    narrative = [intro]
    narrative.append(f"Research status: {_plain(report.get('status', 'unknown'))}. The input contains {report.get('complete_sessions', 0)} complete sessions.")
    if report.get("blocking_reasons"):
        narrative.append("Research requirements still unmet: " + "; ".join(_plain(x) for x in report["blocking_reasons"]) + ".")
    for partition, counts in report.get("sample_counts", {}).items():
        narrative.append(f"{partition.title()} contains {counts.get('labeled', 0)} labeled closed trades, including {counts.get('positive', 0)} positive and {counts.get('negative', 0)} negative outcomes.")
    if period:
        narrative.append(f"The recorded test period covers {_day(period[0])} through {_day(period[-1])}, with {len(period)} complete sessions. That is a historical dataset period, not elapsed campaign time.")
    evaluation = report.get("evaluation", {}).get("test", {})
    rows = []
    for variant, (label, _, _) in VARIANTS.items():
        if variant not in evaluation:
            continue
        values = evaluation[variant]
        narrative.append(f"{label} closed {values['trades']} simulated trades with a net {_money(values['net_pnl'])}. Its win rate was {values['win_rate']:.1%}, and its largest drawdown measured at bar closes was {values['max_bar_close_drawdown']:.2%}.")
        rows.append([label, str(values["trades"]), _money(values["net_pnl"]), f"{values['win_rate']:.1%}", f"{values['max_bar_close_drawdown']:.2%}"])
    narrative.append("Each portfolio was replayed independently. Fees and slippage follow the recorded settings. Reusing this test period for improvements would weaken its value as unseen evidence.")
    if "filtered" in evaluation:
        narrative.append("The model was fitted on the training period, and its take threshold was selected on the validation period.")
    else:
        narrative.append("This experiment has no evaluated model filter. The displayed portfolios are the available rule based comparators.")
    if report["synthetic"]:
        narrative.append("No model was approved for deployment. Synthetic results are useful for checking the software and cannot establish a trading edge.")
    else:
        narrative.append("This report does not approve a model for deployment. A separate review and forward observation are required.")
    live = [f"Campaign status: {_plain(campaign.get('status', 'Not supplied'))}."]
    live.append("The campaign status describes its stage. It does not confirm that the worker or data feed is healthy.")
    if window["window_start"] and window["window_end"]:
        live.append(f"This observation window runs from {_readable_time(window['window_start'])} through {_readable_time(window['window_end'])}.")
    else:
        for key, label in (("window_start", "Observation window start"), ("window_end", "Observation window end")):
            live.append(f"{label}: {_readable_time(window[key]) if window[key] else 'not measured'}.")
    for key, label in MEASUREMENTS:
        value = campaign.get(key)
        live.append(f"{label}: {value:g}." if value is not None else f"{label}: not measured.")
    latest = campaign.get("latest_status")
    live.append(f"Most recent worker status: {_plain(latest) if latest else 'not measured'}.")
    if not latest or campaign.get("last_observation_age_seconds") is None:
        live.append("Worker health is not fully measured because its latest status or observation age is missing. The campaign stage alone cannot establish continuous operation.")
    if campaign.get("missing_market_minutes", 0) or campaign.get("error_observations", 0):
        live.append("Forward observation is incomplete or contains recorded errors. Missing market minutes and error observations need review before this window is treated as reliable evidence.")
    if campaign.get("expected_market_minutes") == 0:
        live.append("No eligible market minutes were expected during this window. It provides no new market coverage for judging the strategy.")
    if campaign.get("blockers"):
        live.append("Preparation still needed: " + "; ".join(_plain(x) for x in campaign["blockers"]) + ".")
    live.append("Shadow decisions are observations without orders. They are counted separately from replay trades. No live profit is inferred from the charts.")
    audit_paragraphs = _audit_paragraphs(audit)
    notes = ["VWAP is calculated from each bar's typical price and volume, and resets each session. EMA also resets each session. The highlighted candle chart shows one illustrative session. Equity and drawdown cover the complete recorded test period.", "Entries in the replay use the next bar open with the configured slippage. Stop orders, targets and session exits are simulated from bars. Actual broker execution can differ.", "These charts use the experiment's saved input data. They do not download or reuse TradingView market data."]
    output.mkdir(parents=True, exist_ok=False)
    chart_paths = _charts(bars, portfolios, report, output, source_label)
    title = f"Dwight QQQ report | {_plain(milestone_label)}"
    email = "\n\n".join([title, source_label, *narrative, *live, *_audit_paragraphs(audit, brief=True), *notes]) + "\n"
    (output/"email.txt").write_text(email)
    def paragraphs(values):
        return "".join(f"<p>{escape(_plain(value))}</p>" for value in values)
    def chart(key, alt):
        if key not in chart_paths:
            return "<p>No complete test bars were available for this chart.</p>"
        encoded = base64.b64encode(Path(chart_paths[key]).read_bytes()).decode()
        return f'<img alt="{escape(alt)}" src="data:image/png;base64,{encoded}">'
    table = "<table><thead><tr>"+"".join(f"<th>{x}</th>" for x in ("Portfolio", "Trades", "Net result", "Win rate", "Drawdown"))+"</tr></thead><tbody>"+"".join("<tr>"+"".join(f"<td>{escape(cell)}</td>" for cell in row)+"</tr>" for row in rows)+"</tbody></table>"
    html = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'+f"<title>{escape(title)}</title>"+"""<style>
body{margin:0;background:#0d111b;color:#dce4f1;font:16px/1.65 Arial,sans-serif}main{max-width:1100px;margin:auto;padding:38px 28px 60px}h1{font-size:30px;line-height:1.2;letter-spacing:-.6px;color:#f5f6fa}h2{margin-top:38px;font-size:21px;color:#f0f4fa}p{max-width:950px}.badge{display:inline-block;background:#372b13;color:#ffd481;padding:7px 12px;font-weight:bold;font-size:13px;border:1px solid #695125;border-radius:5px}table{width:100%;border-collapse:collapse;background:#131722;margin:24px 0}th,td{text-align:left;padding:13px;border-bottom:1px solid #263042}th{color:#aeb9ca;font-size:13px}img{width:100%;display:block;margin:24px 0;border:1px solid #263042;border-radius:8px}.meta{color:#91a0b6;font-size:12px;overflow-wrap:anywhere}@media(max-width:650px){main{padding:20px 12px}th,td{padding:8px;font-size:12px}h1{font-size:25px}}@media print{body{background:white;color:#19202c}h1,h2{color:#19202c}table{background:white}img{break-inside:avoid}}
</style><main>"""+f'<div class="badge">{source_label}</div><h1>{escape(title)}</h1>'+paragraphs([intro])+table+chart("performance_chart", f"{source_label} test portfolio equity and drawdown")+paragraphs(narrative[1:])+"<h2>Forward observation</h2>"+paragraphs(live)+chart("price_chart", f"{source_label} QQQ candlestick chart with simulated trade markers")+"<h2>Strategy review</h2>"+paragraphs(audit_paragraphs)+"<h2>How to read this report</h2>"+paragraphs(notes)+f'<p class="meta">Experiment {escape(report["run_id"])}<br>Input SHA256 {escape(report["input_sha256"])}<br>Model SHA256 {escape(report.get("model_sha256", "No fitted model"))}</p></main></html>'
    (output/"report.html").write_text(html)
    summary = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "run_id": report["run_id"], "symbol": "QQQ", "source_label": source_label, "synthetic": report["synthetic"], "milestone_label": milestone_label, "input_sha256": report["input_sha256"], "model_sha256": report.get("model_sha256"), "replay_verified": True, "test_sessions": len(period), "test_metrics": evaluation, "campaign_measurements": {**{key: campaign.get(key) for key, _ in MEASUREMENTS}, "latest_status": latest, **{key: value.isoformat() if value else None for key, value in window.items()}}, "audit_status": audit.get("status") if audit else "not_supplied", "charts": [Path(path).name for path in chart_paths.values()], "email_sent": False}
    _write_json(output/"summary.json", summary)
    return {"directory": str(output), "html": str(output/"report.html"), "email": str(output/"email.txt"), "summary": str(output/"summary.json"), **chart_paths}
