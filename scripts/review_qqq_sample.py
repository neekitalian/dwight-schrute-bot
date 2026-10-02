"""Private end-to-end review of an already downloaded, verified QQQ sample.

No download, account, order, model promotion or public upload action lives here.
The whole sample is exploratory: this report cannot create an untouched holdout.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, replace
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import sys

# Support both `python scripts/review_qqq_sample.py` and an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dwight.connectors.csv import read_bars
from dwight.data import sha256_file
from dwight.enrichment import run_enrichment
from dwight.experiments import (_metrics, _private_directory, _provenance, _write,
                               complete_sessions, experiment)
from dwight.reporting import generate_report
from dwight.walkforward import _replay, _save_replay
from vwap_bot.engine import Config


def _secure_tree(directory):
    """Private artifacts, including charts emitted by the existing reporter."""
    directory.chmod(0o700)
    for path in directory.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)


def _charts(output, bars, baseline, stress):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    background, panel, muted = "#0b101a", "#111a28", "#9eafc3"
    dates = [bar.timestamp.date().isoformat() for bar in bars]
    ticks = [i for i, day in enumerate(dates) if i == 0 or day != dates[i-1]]
    step = max(1, len(ticks)//8)
    ticks = ticks[::step]

    def style(axis):
        axis.set_facecolor(panel)
        axis.tick_params(colors=muted, labelsize=8)
        axis.grid(color="#273346", alpha=.55, linewidth=.5)
        axis.set_xticks(ticks, [dates[i][5:] for i in ticks])
        for border in axis.spines.values():
            border.set_color("#273346")

    fig, ax = plt.subplots(figsize=(12, 3.9), facecolor=background)
    style(ax)
    xs = list(range(len(bars)))
    ax.plot(xs, [bar.close for bar in bars], color="#64a5ff", linewidth=1.1, label="QQQ five-minute close")
    ax.fill_between(xs, [bar.low for bar in bars], [bar.high for bar in bars], color="#64a5ff", alpha=.12)
    locations = {bar.timestamp: i for i, bar in enumerate(bars)}
    for index, trade in enumerate(baseline.trades):
        entry = locations.get(datetime.fromisoformat(trade["entry_time"]))
        exited = locations.get(datetime.fromisoformat(trade["exit_time"]))
        if entry is not None:
            ax.scatter([entry], [trade["entry"]], marker="^", color="#53d5a0", s=55,
                       label="Simulated long entry" if index == 0 else None, zorder=5)
        if exited is not None:
            ax.scatter([exited], [trade["exit"]], marker="v", color="#ff817e", s=55,
                       label="Simulated exit" if index == 0 else None, zorder=5)
    ax.set_title("QQQ · observed sample prices / simulated trade markers", color="white", loc="left", pad=14)
    ax.set_ylabel("USD per share", color=muted)
    ax.set_xlabel("Exchange session date · regular-session bars only", color=muted)
    ax.legend(facecolor=panel, labelcolor="white", edgecolor="#273346", fontsize=8)
    fig.tight_layout()
    price_path = output / "qqq-price.png"
    fig.savefig(price_path, dpi=150, facecolor=background)
    plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(12, 5.8), sharex=True, facecolor=background,
                             gridspec_kw={"height_ratios": [2, 1]})
    for axis in axes:
        style(axis)
    for bot, label, color in ((baseline, "Long-only baseline", "#53d5a0"),
                              (stress, "2× commission and slippage", "#ffbf69")):
        curve, drawdown, peak = bot.marked_equity, [], bot.c.capital
        for equity in curve:
            peak = max(peak, equity)
            drawdown.append(100*(equity/peak-1))
        axes[0].plot(xs, curve, color=color, linewidth=1.4, label=label)
        axes[1].plot(xs, drawdown, color=color, linewidth=1.2)
    axes[0].set_title("Full-sample exploratory replay · no broker orders", color="white", loc="left", pad=14)
    axes[0].set_ylabel("Marked equity · USD", color=muted)
    axes[0].ticklabel_format(axis="y", style="plain", useOffset=False)
    axes[1].set_ylabel("Drawdown · %", color=muted)
    axes[1].set_xlabel("Exchange session date · time compressed between sessions", color=muted)
    axes[0].legend(facecolor=panel, labelcolor="white", edgecolor="#273346", fontsize=8)
    fig.tight_layout()
    equity_path = output / "long-only-equity.png"
    fig.savefig(equity_path, dpi=150, facecolor=background)
    plt.close(fig)
    return {"price": price_path, "equity": equity_path}


def _html(summary, bars, baseline, stress, charts):
    def picture(key, alt):
        encoded = base64.b64encode(charts[key].read_bytes()).decode()
        return f'<img src="data:image/png;base64,{encoded}" alt="{escape(alt)}">'

    def table(headings, rows):
        return '<table><thead><tr>'+''.join(f'<th>{escape(str(x))}</th>' for x in headings)+'</tr></thead><tbody>'+''.join(
            '<tr>'+''.join(f'<td>{escape(str(x))}</td>' for x in row)+'</tr>' for row in rows)+'</tbody></table>'

    b, s = summary["long_only_baseline"], summary["cost_stress"]
    comparisons = table(["Replay", "Trades", "Net P&L", "Marked drawdown", "Commission / share / side", "Slippage / share"], [
        ["Long-only baseline", b["trades"], f'${b["net_pnl"]:,.2f}', f'{b["max_bar_close_drawdown"]:.3%}',
         f'${baseline.c.commission:.3f}', f'${baseline.c.slippage:.3f}'],
        ["Fixed 2× cost stress", s["trades"], f'${s["net_pnl"]:,.2f}', f'{s["max_bar_close_drawdown"]:.3%}',
         f'${stress.c.commission:.3f}', f'${stress.c.slippage:.3f}']])
    ledger = []
    for label, bot in (("Baseline", baseline), ("2× cost stress", stress)):
        for trade in bot.trades:
            ledger.append([label, trade["signal_time"], trade["entry_time"], trade["exit_time"],
                           trade["quantity"], f'{trade["entry"]:.4f}', f'{trade["exit"]:.4f}',
                           f'{trade["fees"]:.2f}', f'{trade["net_pnl"]:.2f}', trade["exit_reason"]])
    trades = table(["Replay", "Signal start", "Entry start", "Exit start", "Shares", "Entry USD", "Exit USD",
                    "Fees USD", "Net USD", "Exit"], ledger) if ledger else '<p>No simulated trades occurred.</p>'
    chronological = summary["chronological_research"]
    counts = table(["Partition", "Labelled trades", "Positive", "Negative"], [
        [name, counts["labeled"], counts["positive"], counts["negative"]]
        for name, counts in chronological["sample_counts"].items()])
    blockers = ''.join(f'<li>{escape(reason)}</li>' for reason in chronological["blocking_reasons"])
    enrichment = summary["enrichment"]
    enrichment_blockers = ''.join(f'<li>{escape(reason)}</li>' for reason in enrichment["blocking_reasons"])
    cards = ''.join(f'<div class="card"><small>{escape(label)}</small><strong>{escape(str(value))}</strong></div>'
                    for label, value in (("Complete sessions", summary["complete_sessions"]),
                                         ("Five-minute bars", summary["bars"]), ("Long-only trades", b["trades"]),
                                         ("Long-only replay P&L", f'${b["net_pnl"]:,.2f}')))
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dwight · private QQQ sample review</title><style>
body{{margin:0;background:#0b101a;color:#d8e2f0;font:15px/1.6 system-ui,sans-serif}}main{{max-width:1180px;margin:auto;padding:38px 28px 70px}}h1{{font-size:32px;line-height:1.2;color:white;margin-bottom:10px}}h2{{font-size:22px;margin-top:34px;color:white}}.badge{{color:#ffbf69;font:600 12px/1.5 system-ui;letter-spacing:1px}}.muted,small{{color:#9eafc3}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:26px 0}}.card{{padding:17px;border:1px solid #273346;border-radius:9px;background:#111a28}}strong{{display:block;font-size:25px;color:white}}img{{width:100%;border:1px solid #273346;border-radius:9px;margin:15px 0}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:12px;background:#111a28}}td,th{{padding:12px;border-bottom:1px solid #273346;text-align:left;white-space:nowrap}}th{{color:#9eafc3}}a{{color:#64a5ff}}code{{overflow-wrap:anywhere;color:#b8cce6}}.note{{padding:15px 18px;border-left:3px solid #ffbf69;background:#171b23}}@media(max-width:700px){{main{{padding:22px 14px}}.cards{{grid-template-columns:repeat(2,1fr)}}h1{{font-size:27px}}}}
</style></head><body><main><div class="badge">PRIVATE · OBSERVED QQQ SAMPLE · SIMULATED EXECUTION</div>
<h1>Dwight / QQQ sample review</h1><p class="muted">{escape(summary['start'])} → {escape(summary['end'])} · {escape(summary['feed'])} · split adjusted</p>
<p>These are real vendor prices and simulated fills. The full sample has been inspected; it is exploratory and is not an untouched final holdout. No broker paper order or live order was submitted.</p>
<div class="cards">{cards}</div>{picture('price', 'QQQ observed five-minute prices with simulated long-only trade markers')}
<h2>Long-only baseline and fixed cost stress</h2><p>Both runs use the same data, signal rules and risk settings. The stress run doubles only commission and slippage. It reruns the portfolio because costs can change sizing and subsequent decisions.</p>
<div class="scroll">{comparisons}</div><p>Stress minus baseline net P&amp;L: <b>${summary['stress_minus_baseline_net_pnl']:,.2f}</b>. This is a sensitivity check, not a forecast or a guarantee that higher costs always produce a lower result.</p>
{picture('equity', 'Independent baseline and cost-stress marked equity and drawdown')}
<h2>Simulated trade ledger</h2><p class="muted">Timestamps label bar starts, not broker timestamps. Five-minute signals become available only at bar completion; entries use the following bar and configured simulated costs.</p><div class="scroll">{trades}</div>
<h2>Model research remains separate</h2><p>The existing chronological pipeline uses its standard long-and-short baseline; it is separate from the long-only results above. Status: <b>{escape(chronological['status'])}</b>. {escape(chronological['model_statement'])}</p>
{counts}<ul>{blockers}</ul><p>Feature-enrichment status: <b>{escape(enrichment['status'])}</b>; completed comparison windows: {enrichment['completed_windows']}. The unchanged default plan requires at least {enrichment['minimum_sessions_for_one_window']} complete sessions: {enrichment['session_requirements']['train_sessions']} training, {enrichment['session_requirements']['validation_sessions']} validation, {enrichment['session_requirements']['test_sessions']} development test and {enrichment['session_requirements']['holdout_sessions']} final reserve, plus the sample gates above. No model is selected or promoted by this review. Its internally reserved dates do not erase the fact that this whole sample was already inspected.</p><ul>{enrichment_blockers}</ul>
<div class="note">This small vendor sample has no executable bid/ask quotes, queue model, partial fills or latency simulation. It cannot establish a stable return distribution or deployment readiness. Training and paper deployment still require substantially more history, matching feed definitions and forward observation.</div>
<h2>Provenance and private use</h2><p>Source: <a href="https://firstratedata.com/i/etf/QQQ">FirstRate Data official QQQ sample</a>. See the <a href="https://firstratedata.com/about/license">vendor data license</a>. Raw prices remain private and are not redistributed by this report workflow. These bars are independently aggregated from the source minute data and are not labelled Alpaca SIP or IEX.</p>
<p>Acquired: {escape(str(summary['retrieved_at']))}<br>Dataset SHA-256: <code>{escape(summary['dataset_sha256'])}</code><br>Source ZIP SHA-256: <code>{escape(summary['raw_zip_sha256'])}</code><br>Five-minute input SHA-256: <code>{escape(summary['input_sha256'])}</code></p>
<p class="muted">Generated {escape(summary['generated_at'])}. Static charts are embedded; this file loads no external scripts and exposes no raw-data export.</p></main></body></html>'''


def review_sample(manifest_path: Path, output: Path):
    manifest_path, output = Path(manifest_path).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Output must be a nonexistent private directory")
    manifest = json.loads(manifest_path.read_bytes())
    name = manifest.get("bars", {}).get("QQQ", {}).get("5Min")
    if not isinstance(name, str):
        raise ValueError("Manifest has no QQQ five-minute input")
    data = (manifest_path.parent / name).resolve()
    if not data.is_relative_to(manifest_path.parent):
        raise ValueError("Dataset input escapes its manifest directory")
    raw = data.read_bytes()
    provenance, checked_manifest = _provenance(data, "QQQ", raw, False, manifest_path)
    if provenance.get("source") != "firstrate" or provenance.get("sample_only") is not True:
        raise ValueError("This review accepts only verified FirstRate QQQ sample provenance")
    if provenance.get("source_acquisition") not in ("https_download", "legacy_recorded_retrieval"):
        raise ValueError("This review requires recorded vendor acquisition; local archives are unverified")
    sessions, excluded = complete_sessions(list(read_bars(data)))
    bars = [bar for day in sorted(sessions) for bar in sessions[day]]
    if not bars:
        raise ValueError("Sample has no complete 78-bar sessions")
    _private_directory(output)
    try:
        baseline_config = Config()
        stress_config = replace(baseline_config, commission=2*baseline_config.commission,
                                slippage=2*baseline_config.slippage)
        baseline = _replay(bars, baseline_config, long_only=True)
        stress = _replay(bars, stress_config, long_only=True)
        baseline_dir = _private_directory(output / "long-only")
        for name, bot in (("baseline", baseline), ("cost-stress", stress)):
            _save_replay(baseline_dir, name, bot, bars)
            _write(baseline_dir / f"{name}-metrics.json", _metrics(bot))
        chronological = experiment(data, "QQQ", output / "chronological", synthetic=False,
                                   config={"dataset_manifest": str(manifest_path)})
        standard_report = generate_report(Path(chronological["directory"]), output / "chronological-charts",
                                          milestone_label="Real QQQ sample · separate long-and-short research")
        enrichment = run_enrichment(data, output / "enrichment", synthetic=False,
                                    config={"dataset_manifest": str(manifest_path)})
        source_root = Path(__file__).resolve().parents[1]
        source_files = [source_root / path for path in (
            "scripts/review_qqq_sample.py", "dwight/walkforward.py", "dwight/experiments.py",
            "dwight/firstrate.py", "dwight/enrichment.py", "dwight/reporting.py", "dwight/data.py",
            "dwight/connectors/csv.py", "vwap_bot/engine.py")]
        zip_hash = next(entry["sha256"] for entry in checked_manifest["files"] if entry["path"] == "raw/sample.zip")
        summary = {
            "schema_version": 1, "symbol": "QQQ", "synthetic": False, **provenance,
            "generated_at": datetime.now(timezone.utc).isoformat(), "retrieved_at": checked_manifest.get("retrieved_at"),
            "start": min(sessions), "end": max(sessions), "complete_sessions": len(sessions),
            "excluded_sessions": excluded, "bars": len(bars), "input_sha256": sha256_file(data), "raw_zip_sha256": zip_hash,
            "global_holdout_status": "full_sample_exploratory_already_inspected",
            "submits_orders": False, "promotion_eligible": False,
            "baseline_config": asdict(baseline_config), "stress_config": asdict(stress_config),
            "long_only_baseline": _metrics(baseline), "cost_stress": _metrics(stress),
            "stress_minus_baseline_net_pnl": stress.equity-baseline.equity,
            "source_hashes": {str(path.relative_to(source_root)): sha256_file(path) for path in source_files},
            "chronological_research": {
                "directory": str(Path(chronological["directory"]).relative_to(output)),
                "direction_policy": "long_and_short", "status": chronological["status"],
                "sample_counts": chronological["sample_counts"], "blocking_reasons": chronological["blocking_reasons"],
                "model_statement": "No model was fitted." if chronological["status"] == "insufficient_data" else "A research model was fitted; it is not approved for deployment.",
                "report_html": str(Path(standard_report["html"]).relative_to(output)),
            },
            "enrichment": {"directory": str(Path(enrichment["directory"]).relative_to(output)),
                           "status": enrichment["status"], "completed_windows": enrichment["window_counts"]["completed"],
                           "session_requirements": {key: enrichment["settings"][key] for key in
                                                    ("train_sessions", "validation_sessions", "test_sessions", "holdout_sessions")},
                           "minimum_sessions_for_one_window": sum(enrichment["settings"][key] for key in
                                                                  ("train_sessions", "validation_sessions", "test_sessions", "holdout_sessions")),
                           "blocking_reasons": enrichment["blocking_reasons"]},
        }
        charts = _charts(output, bars, baseline, stress)
        _write(output / "summary.json", summary)
        (output / "report.html").write_text(_html(summary, bars, baseline, stress, charts))
        _secure_tree(output)
        return {"directory": str(output), "html": str(output / "report.html"),
                "summary": str(output / "summary.json"), "chronological_status": chronological["status"],
                "enrichment_status": enrichment["status"], "baseline_net_pnl": baseline.equity-baseline.c.capital,
                "stress_net_pnl": stress.equity-stress.c.capital, "submits_orders": False}
    except Exception as exc:
        _write(output / "failed.json", {"error_type": type(exc).__name__})
        _secure_tree(output)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(review_sample(args.manifest, args.output), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
