"""Presentation for the research Space. Performance values come from replay."""
from html import escape
from datetime import datetime
from dwight.experiments import NY
from .analytics import VARIANT_LABELS
from .transformer_analysis import connection_analysis

CSS = """
body, .gradio-container { background:#101318 !important; color:#dce3ee !important; }
.gradio-container { width:100% !important; min-width:0 !important; max-width:1440px !important; box-sizing:border-box; margin:auto; padding:28px 36px !important;
 --body-background-fill:#101318; --block-background-fill:#151a23; --input-background-fill:#1b2230;
 --body-text-color:#dce3ee; --block-label-text-color:#aebaca; --border-color-primary:#2a3443;
 --block-border-color:#2a3443; --input-border-color:#344155; --body-text-color-subdued:#99a8bd;
 --button-secondary-background-fill:#1b2230; --button-secondary-text-color:#dce3ee;
 --button-primary-background-fill:#26caa7; --button-primary-text-color:#08231d;
 --link-text-color:#63b6ff; font-family:Inter,system-ui,sans-serif !important; }
.gradio-container .prose { color:#b5c0d1; line-height:1.7; }
.gradio-container .prose h1,.gradio-container .prose h2,.gradio-container .prose h3,.gradio-container .prose strong { color:#e9eef6; }
.gradio-container input,.gradio-container textarea { color:#dce3ee !important; }
.gradio-container .main, .gradio-container main.contain, .gradio-container .main > .wrap { min-width:0 !important; width:100%; padding:0 !important; }
.gradio-container .row, .gradio-container .column, .gradio-container .tabs, .gradio-container .tab-container { min-width:0 !important; }
.gradio-container .tab-nav { max-width:100%; overflow-x:auto; flex-wrap:nowrap; }
.gradio-container .tab-nav button { white-space:nowrap; }
.gradio-container .tab-nav { border-bottom:1px solid #2a3443 !important; gap:16px; }
.gradio-container .tab-nav button { color:#99a8bd; padding:15px 4px; font-size:14px; }
.gradio-container .tab-nav button.selected { color:#27d9b0; border-color:#27d9b0; }
.dw-hero { display:flex;justify-content:space-between;align-items:center;gap:24px;margin:0 0 24px; }
.dw-wordmark { color:#27d9b0;font-size:12px;letter-spacing:3px;font-weight:700; }
.dw-hero h1 { margin:9px 0;font-weight:600;font-size:36px;letter-spacing:-1.2px;color:#edf3fb; }
.dw-hero p { color:#93a4bb;font-size:14px;margin:0; }
.dw-tag { display:inline-block;padding:6px 10px;border:1px solid #365249;border-radius:5px;color:#79d9b9;background:#142820;font-size:11px;letter-spacing:1px;font-weight:600; }
.dw-hero-aside { text-align:right;flex-shrink:0; }.dw-hero-aside p { margin-top:9px;font-size:12px; }
.dw-section { margin:18px 0 12px; }.dw-section h2 { font-size:20px;letter-spacing:-.3px;font-weight:500;margin:0 0 6px;color:#edf3fb; }
.dw-section p,.dw-note { font-size:13px;color:#99a8bd;line-height:1.65;margin:0; }
.dw-cards { display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0 8px; }
.dw-card { background:#151a23;border:1px solid #293344;border-radius:9px;padding:20px; }
.dw-label { color:#9bacbf;font-size:12px;letter-spacing:.3px; }
.dw-value { color:#ecf2fa;font-variant-numeric:tabular-nums;font-size:29px;letter-spacing:-.8px;margin:12px 0 8px; }
.dw-card small { color:#96a6ba;font-size:12px; }.dw-positive { color:#27d9b0 !important; }.dw-negative { color:#f1667a !important; }
.dw-table-wrap { width:100%;overflow-x:auto;border:1px solid #293344;border-radius:9px;margin:12px 0 20px; }
.dw-table { width:100%;border-collapse:collapse;white-space:nowrap;font-size:13px;background:#151a23; }
.dw-table th { color:#94a6be;font-size:11px;font-weight:500;letter-spacing:.3px;padding:14px 16px;text-align:right;background:#181e28; }
.dw-table th:first-child,.dw-table td:first-child { text-align:left; }
.dw-table td { padding:16px;border-top:1px solid #263040;text-align:right;color:#dce3ee;font-variant-numeric:tabular-nums; }
.dw-table tr.dw-highlight td { background:#142620; }.dw-dot { width:7px;height:7px;display:inline-block;border-radius:50%;margin-right:8px; }
.dw-callout { border-left:3px solid #6599ff;background:#151e2b;padding:16px 20px;margin:12px 0 20px;color:#bfcde0;font-size:14px;line-height:1.7; }
.dw-callout strong { color:#edf3fb; }
.dw-flow { display:flex;gap:8px;align-items:stretch;margin:14px 0 22px; }
.dw-node { background:#15251f;border:1px solid #2c5748;border-radius:8px;padding:15px;flex:1;min-width:0; }
.dw-node b { display:block;color:#d8eee7;font-size:13px;margin:8px 0; }.dw-node small { color:#9bbaaf;font-size:12px;line-height:1.5; }
.dw-node span { color:#65d6b0;font-size:10px;letter-spacing:1px; }.dw-arrow { align-self:center;color:#5a7c6f; }
.dw-branches { display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:12px 0; }
.dw-branch { background:#1c192b;border:1px dashed #65528c;border-radius:9px;padding:20px; }
.dw-branch h3 { color:#e0d4ff;font-size:17px;margin:10px 0; }.dw-branch p { color:#b5a9cd;line-height:1.65;font-size:13px; }
.dw-branch .dw-label { color:#bb9ef1; }.dw-join { text-align:center;color:#b59bdc;font-size:13px;padding:10px 18px 20px;line-height:1.8; }
.dw-footer { border-top:1px solid #293344;margin-top:28px;padding-top:18px;color:#7f90a7;font-size:12px;line-height:1.8; }.dw-footer a { color:#80b5f5; }
@media(max-width:760px) {
 .gradio-container { padding:16px 12px !important; }.dw-hero { align-items:flex-start;gap:16px;flex-direction:column; }
 .gradio-container .row { flex-direction:column !important; }
 .gradio-container .row > * { width:100% !important; min-width:0 !important; flex:auto !important; }
 .dw-hero h1 { font-size:30px; }.dw-hero-aside { text-align:left; }.dw-cards { grid-template-columns:repeat(2,minmax(0,1fr)); }
 .dw-card { padding:14px; }.dw-value { font-size:25px; }.dw-flow { flex-wrap:wrap; }.dw-node { min-width:120px; }
 .dw-arrow { display:none; }.dw-branches { grid-template-columns:1fr; }
}
"""
HERO = """<div class="dw-hero"><div><div class="dw-wordmark">DWIGHT / RESEARCH</div>
<h1>Every decision, examined.</h1><p>QQQ · VWAP strategy · Five minute bars · Model research</p></div>
<div class="dw-hero-aside"><span class="dw-tag">SYNTHETIC REPLAY</span>
<p>Invented prices. No account connected.</p></div></div>"""
FOOTER = """<div class="dw-footer">DWIGHT RESEARCH · Simulated results from generated data. No real QQQ history or broker fills.
<br><a href="https://github.com/neekitalian/dwight-schrute-bot" target="_blank" rel="noopener">Source and methodology ↗</a>
 · The chart design is inspired by financial terminals. This app is not connected to TradingView.</div>"""

def section(title, subtitle=""):
    return f'<div class="dw-section"><h2>{escape(title)}</h2><p>{escape(subtitle)}</p></div>'

def money(value, signed=False):
    return f'{value:+,.2f}' if signed else f'{value:,.2f}'

def color(value):
    return "dw-positive" if value >= 0 else "dw-negative"

def table(headers, rows, highlight=None):
    # Only controlled markup or escaped values enter cells.
    head = "".join(f"<th>{escape(text)}</th>" for text in headers)
    body = "".join(f'<tr class="{"dw-highlight" if index == highlight else ""}">' +
                   "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for index, row in enumerate(rows))
    return f'<div class="dw-table-wrap"><table class="dw-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

def metric_cards(payload):
    test = payload["test"]["variants"]
    m, base = test["filtered"]["metrics"], test["baseline"]["metrics"]
    delta = m["net_pnl"] - base["net_pnl"]
    items = [
        ("Dwight net P&L · USD", money(m["net_pnl"], True), f'{100*m["return_fraction"]:+.2f}% on starting capital', color(m["net_pnl"])),
        ("Difference from VWAP · USD", money(delta, True), "Same later sessions and cost rules", color(delta)),
        ("Maximum drawdown", f'{100*m["max_bar_close_drawdown"]:.2f}%', "Measured at five minute closes", ""),
        ("Completed Dwight trades", str(m["trades"]), f'{100*m["win_rate"]:.1f}% profitable after modeled costs', ""),
    ]
    return '<div class="dw-cards">' + "".join(
        f'<div class="dw-card"><div class="dw-label">{label}</div><div class="dw-value {tone}">{value}</div><small>{note}</small></div>'
        for label, value, note, tone in items) + '</div>'

def comparison_table(payload):
    rows = []
    for (key, label), tone in zip(VARIANT_LABELS.items(), ("#6699ff", "#ecb75f", "#27d9b0")):
        m = payload["test"]["variants"][key]["metrics"]
        rows.append([f'<span class="dw-dot" style="background:{tone}"></span>{escape(label)}', str(m["trades"]),
                     f'<span class="{color(m["net_pnl"])}">{money(m["net_pnl"], True)}</span>',
                     f'{100*m["return_fraction"]:+.2f}%', f'{100*m["win_rate"]:.1f}%',
                     f'{100*m["max_bar_close_drawdown"]:.2f}%', f'{m["expectancy_r"]:+.2f}R'])
    return table(["Independent replay", "Trades", "Net P&L / USD", "Return", "Win rate", "Close drawdown", "Mean net R"], rows, 2)

def performance_note(payload):
    days = payload["report"]["partitions"]["test"]
    return (f'**{escape(payload["case"]["label"])} · test partition {days[0]} to {days[-1]} · '
            f'{len(days)} fabricated sessions.** Starting capital ${payload["test"]["initial_capital"]:,.0f} per strategy. '
            'Each strategy is replayed independently. Every displayed return is simulated. '
            'Drag to zoom, hover to inspect, or select a legend label to hide a series.')

def robustness_view(result):
    rows = []
    for r in result["rows"]:
        delta = r["filtered_minus_baseline"]
        rows.append([escape(r["label"]), money(r["baseline_net_pnl"], True), money(r["simple_volume_net_pnl"], True),
                     money(r["filtered_net_pnl"], True), f'<span class="{color(delta)}">{money(delta, True)}</span>',
                     f'{100*r["filtered_drawdown_fraction"]:.2f}%', escape(r["audit_status"])])
    note = (f'<div class="dw-callout"><strong>Dwight leads on {result["ordinary_cases_model_ahead"]} of '
            f'{result["ordinary_cases"]} ordinary synthetic paths.</strong> These fixtures check software behavior. '
            'They do not establish a market edge or a statistical confidence interval. The fourth case retrains '
            'under doubled costs on path 42; it is not an independent price path or a pure cost attribution.</div>')
    return note + table(["Fixed case", "VWAP / USD", "Volume / USD", "Dwight / USD", "Dwight − VWAP", "Dwight drawdown", "Replay checks"], rows)

def trade_rows(payload, session, variant):
    def clock(value):
        return datetime.fromisoformat(value).astimezone(NY)
    return [[clock(t["entry_time"]).strftime("%H:%M"), "Long" if t["direction"] == 1 else "Short", t["quantity"],
             round(t["entry"], 4), round(t["stop"], 4), round(t["target"], 4), clock(t["exit_time"]).strftime("%H:%M"),
             round(t["exit"], 4), t["exit_reason"].replace("_", " "), round(t["net_pnl"], 2), round(t["net_r"], 3)]
            for t in payload["test"]["variants"][variant]["trades"] if clock(t["entry_time"]).date().isoformat() == session]

def diagnostics_note(payload):
    c, r = payload["classifier"]["calibration"], payload["report"]
    counts = r["sample_counts"]
    return (f'**Current model: logistic regression, 10 features.** Take threshold **{r["selected_threshold"]:g}**, '
            'chosen on validation sessions. The threshold is not a measured win rate.\n\n'
            f'**Labeled baseline trades:** {counts["train"]["labeled"]} train · {counts["validation"]["labeled"]} validation · '
            f'{counts["test"]["labeled"]} test. **Test Brier score:** {c["brier_score"]:.3f} '
            f'(lower is better). **Log loss:** {c["log_loss"]:.3f}.\n\n'
            'The calibration plot compares predicted probabilities with observed outcomes on baseline candidates. '
            'The feature chart shows average absolute contributions to the fitted logistic score. '
            'Neither chart demonstrates causality. Small samples make calibration unstable.')

def audit_view(payload):
    a = payload["audit"]
    rows = [[escape(c["name"]), escape(c["status"]), str(c["evidence_count"]), escape(c["detail"])] for c in a["checks"]]
    return (f'<div class="dw-callout"><strong>Replay audit: {escape(a["status"])}.</strong> '
            'Checks compare saved evidence with the reference engine and stated rules. '
            'A pass does not certify profit, article fidelity or broker behavior. '
            'Runtime agent skills used: none. This experiment runs Python strategy rules and a logistic classifier.</div>' +
            table(["Check", "Result", "Evidence items", "What was checked"], rows))

def method_note(payload):
    r, s = payload["report"], payload["report"]["strategy"]
    partitions = "\n".join(f'- **{name.title()}:** {days[0]} to {days[-1]} · {len(days)} sessions' for name, days in r["partitions"].items())
    return (f'**Data and split**\n\n500 calendar days of generated bars, seed {payload["case"]["seed"]}. '
            'Every session starts around 100. These are QQQ labeled fixtures, not actual QQQ prices. '
            'Weekdays include exchange holidays. Only complete 78 bar sessions enter this experiment.\n\n' + partitions +
            '\n\nFeatures freeze when the signal bar closes. Labels use later completed trade outcomes. '
            'Training fits the scaler and classifier; validation selects the threshold; the later test partition is used for this report. '
            'Minimum sample requirements are deliberately relaxed for a software smoke test.\n\n'
            '**Strategy and simulation**\n\n'
            'Confirmed price structure and VWAP establish direction. After an impulse, a pullback can touch VWAP, structure '
            'or the enabled EMA20. EMA20 is an optional touch level, not a mandatory trend filter. '
            'Both VWAP and EMA reset each session in this reference strategy.\n\n'
            f'Planned risk per trade is {100*s["risk_fraction"]:.2f}% of realized equity, with a price target at {s["reward_r"]:g} times the entry to stop distance '
            f'and a limit of {s["max_losses"]} losing trades per day. This is not a guaranteed cash loss cap. '
            f'Commission is ${s["commission"]:g} per share per side; adverse slippage is ${s["slippage"]:g} where the engine applies it. '
            'Targets fill at their specified price. Net R includes costs and tick rounding, so it differs from the price distance multiplier. Spread, queue position and market impact are not modeled.\n\n'
            'Entries use the next bar open after a completed signal. Opening gaps are handled first. If neither gap rule applies and both stop and target are reached within a bar, '
            'the stop takes precedence. Intrabar fill times are unknown. Close marked equity estimates exit costs; '
            'intrabar drawdown can be worse. The replay permits shorts; the separate paper adapter currently permits long QQQ entries only.\n\n'
            '**What is running**\n\n'
            'This Space trains, independently replays and audits fixed synthetic cases on CPU. It retains bounded JSON results '
            'including model coefficients in memory. Temporary input and artifact files are deleted. No Hub model release is published. '
            'No transformer is running. No broker, TradingView account, email campaign or real market stream is connected here. '
            'The 12 hour, 24 hour, 48 hour and one week forward test clocks have not started.\n\n'
            '**Your TradingView paper account**\n\n'
            'The intended workflow for Paper Trading by TradingView is to review a Dwight proposal, '
            'place any paper order manually, and bring exported results back for analysis. '
            'That proposal and import workflow still needs implementation. This Space currently displays replay results only.\n\n'
            f'**Input SHA256:** {r["input_sha256"]}\n\n**Model SHA256:** {r["model_sha256"]}\n\n'
            f'**Engine and features SHA256:** {r["code_sha256"]}')

def transformer_flow(choice="both"):
    a = connection_analysis(choice)
    nodes = [("OBSERVE", "Completed bars", "Synthetic five minute inputs"),
             ("PROPOSE", "VWAP candidate", "Ten numeric features"), ("FILTER", "Dwight classifier", "Current logistic model"),
             ("CONSTRAIN", "Fixed risk rules", "Sizing, stop, target, daily limit"),
             ("EVALUATE", "Replay outcomes", "Measured synthetic results")]
    flow = '<div class="dw-flow">' + '<div class="dw-arrow">→</div>'.join(
        f'<div class="dw-node"><span>{step}</span><b>{title}</b><small>{subtitle}</small></div>' for step, title, subtitle in nodes) + '</div>'
    branches = '<div class="dw-branches">' + ''.join(
        f'<div class="dw-branch"><div class="dw-label">PROPOSED · NOT CONNECTED</div><h3>{escape(p["title"])}</h3>'
        f'<p>{escape(p["question"])}</p><p><strong>{"Past bars → forecast features" if p["title"] == "Price sequences" else "Timed text → sentiment features"}</strong></p>'
        f'<p>{escape(p["handoff"])}</p></div>' for p in a["selected_paths"]) + '</div>'
    return (section("Current decision path", "Solid green cards are implemented in the synthetic replay.") + flow +
            section("Proposed transformer inputs", "Purple cards are research connections. Their trading contribution has not been measured.") + branches +
            '<div class="dw-join">↓ New feature schema + training only preprocessing + retrained Dwight ↓<br>'
            'Evaluate a new model against Dwight alone before considering a reviewed release.<br>'
            'Transformer outputs never alter the fixed risk policy.</div>')

def transformer_details(choice="both"):
    a = connection_analysis(choice)
    parts = []
    for p in a["selected_paths"]:
        parts.extend([f'### {p["title"]}', '**Inputs we need**\n\n' + '\n'.join(f'- {x}' for x in p["inputs"]),
                      '**What Dwight would receive**\n\n' + '\n'.join(f'- {x}' for x in p["output_features"]),
                      '**What counts as added value**\n\n' + p["baseline_comparison"]])
        for m in p["models"]:
            parts.append(f'**[{m["name"]}]({m["url"]})** · {m["role"]}. {m["scope"]} '
                         f'[License evidence]({m["source_url"]}): {m["license"]}.')
        parts.append('**Limits to test**\n\n' + '\n'.join(f'- {x}' for x in p["limitations"]))
    return '\n\n'.join(parts)

def transformer_comparison():
    a = connection_analysis("both")
    rows = [[escape(r["title"]), escape(r["features"]), escape(r["status"]), "Not measured on real QQQ"] for r in a["experiments"]]
    return table(["Experiment arm", "Information available", "Current evidence", "Marginal value"], rows)

def transformer_protocol():
    a = connection_analysis("both")
    return ('**The test that decides whether it is useful**\n\n' + '\n\n'.join(f'{i}. {x}' for i, x in enumerate(a["evaluation"], 1)) +
            '\n\n**Connection requirements**\n\n' + '\n'.join(f'- {x}' for x in a["evidence_gate"]) +
            '\n\n**Architecture boundary:** ' + a["current_model"]["boundary"])
