"""Presentation for the research Space. Performance values come from replay."""
from html import escape
from datetime import datetime
from dwight.experiments import NY
from .analytics import VARIANT_LABELS
from .transformer_analysis import connection_analysis

CSS = """
:root { color-scheme:dark; }
body, .gradio-container { background:#0a0a0a !important; color:#ededed !important; }
.gradio-container { width:100% !important; min-width:0 !important; max-width:1440px !important;
 box-sizing:border-box; margin:auto; padding:30px 48px !important;
 --body-background-fill:#0a0a0a; --background-fill-primary:#0a0a0a; --background-fill-secondary:#141414;
 --block-background-fill:#141414; --input-background-fill:#191919;
 --body-text-color:#ededed; --block-label-text-color:#a3a3a3; --border-color-primary:#2b2b2b;
 --border-color-accent:#595959; --block-border-color:#2b2b2b; --input-border-color:#333333;
 --body-text-color-subdued:#969696; --block-title-text-color:#ededed;
 --button-secondary-background-fill:#191919; --button-secondary-background-fill-hover:#252525;
 --button-secondary-border-color:#333333; --button-secondary-text-color:#ededed;
 --button-primary-background-fill:#fafafa; --button-primary-background-fill-hover:#dedede;
 --button-primary-text-color:#111111; --button-primary-border-color:#fafafa;
 --input-placeholder-color:#858585; --link-text-color:#ededed; --link-text-color-hover:#ffffff;
 --checkbox-background-color:#191919; --checkbox-border-color:#595959;
 --checkbox-label-background-fill:#191919; --checkbox-label-background-fill-hover:#252525;
 --checkbox-label-border-color:#333333; --checkbox-label-border-color-selected:#595959;
 --checkbox-background-color-selected:#ededed; --checkbox-border-color-selected:#ededed;
 --checkbox-label-background-fill-selected:#252525; --checkbox-label-text-color-selected:#fafafa;
 --slider-color:#bdbdbd; --color-accent:#bdbdbd; --color-accent-soft:#252525;
 font-family:Inter,system-ui,sans-serif !important; -webkit-font-smoothing:antialiased; }
.gradio-container .prose { color:#a3a3a3; line-height:1.75; font-size:14px; }
.gradio-container .prose h1,.gradio-container .prose h2,.gradio-container .prose h3 { color:#f5f5f5; font-weight:400; letter-spacing:-.5px; }
.gradio-container .prose h2 { font-size:28px; line-height:1.3; margin:20px 0 12px; }
.gradio-container .prose h3 { font-size:22px; line-height:1.35; margin:16px 0 12px; }
.gradio-container .prose strong { color:#e6e6e6; font-weight:500; }
.gradio-container .prose a { color:#e6e6e6; text-underline-offset:4px; }
.gradio-container input,.gradio-container textarea { color:#ededed !important; }
.gradio-container button.primary { background:#fafafa !important; color:#111111 !important; border:1px solid #fafafa !important; border-radius:999px !important; font-weight:500; box-shadow:none !important; }
.gradio-container button.primary:hover { background:#dedede !important; border-color:#dedede !important; }
.gradio-container button.secondary,.gradio-container a.secondary { border-radius:999px !important; }
.gradio-container button:focus-visible,.gradio-container a:focus-visible { outline:2px solid #a3a3a3; outline-offset:4px; }
.gradio-container .main,.gradio-container main.contain,.gradio-container .main > .wrap { min-width:0 !important; width:100%; padding:0 !important; }
.gradio-container .row,.gradio-container .column,.gradio-container .tabs,.gradio-container .tab-container { min-width:0 !important; }
.gradio-container [role="tablist"] { max-width:100%; overflow-x:auto; flex-wrap:nowrap; border-bottom:1px solid #2b2b2b !important; gap:24px; margin:18px 0 12px; }
.gradio-container [role="tablist"] button { white-space:nowrap; color:#969696; padding:16px 0; font-size:13px; font-weight:400; }
.gradio-container [role="tablist"] [role="tab"][aria-selected="true"] { color:#fafafa; border-color:#fafafa; background:transparent; }
.gradio-container [role="tabpanel"] { padding:20px 0 !important; border:0 !important; }
.gradio-container .block { border-radius:12px; }
.dw-masthead { flex-wrap:wrap; display:flex; justify-content:flex-start; align-items:center; gap:20px; padding:0 0 10px; }
.dw-brand { display:flex; align-items:center; gap:10px; color:#fafafa; font-size:20px; letter-spacing:-.8px; font-weight:500; }
.dw-brand-mark { width:21px; height:21px; border:1px solid #e6e6e6; border-radius:50%; display:inline-block; box-shadow:inset 5px 0 0 #0a0a0a,inset 6px 0 0 #e6e6e6; }
.dw-masthead-meta { display:flex; gap:24px; align-items:center; color:#969696; font-size:12px; }
.dw-masthead-meta a { color:#d4d4d4; text-decoration:none; }
.dw-hero { display:flex; justify-content:space-between; align-items:flex-end; gap:28px; margin:12px 0 28px; padding:0 0 36px; border-bottom:1px solid #242424; }
.dw-wordmark { color:#969696; font-size:11px; letter-spacing:1.2px; font-weight:400; }
.dw-hero h1 { margin:15px 0 12px; font-weight:400; font-size:44px; line-height:1.12; letter-spacing:-1.8px; color:#fafafa; }
.dw-hero p { color:#969696; font-size:15px; margin:0; line-height:1.65; }
.dw-tag { display:inline-block; padding:6px 11px; border:1px solid #363636; border-radius:999px; color:#c7c7c7; background:#141414; font-size:10px; letter-spacing:.8px; font-weight:400; }
.dw-hero-aside { text-align:right; flex-shrink:0; }.dw-hero-aside p { margin-top:10px; font-size:12px; }
.dw-section { margin:28px 0 16px; }.dw-section h2 { font-size:24px; letter-spacing:-.6px; font-weight:400; margin:0 0 8px; color:#fafafa; }
.dw-section p,.dw-note { font-size:13px; color:#969696; line-height:1.75; margin:0; }
.dw-cards { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:16px 0 12px; }
.dw-card { background:#141414; border:1px solid #2b2b2b; border-radius:14px; padding:24px; }
.dw-label { color:#a3a3a3; font-size:12px; letter-spacing:.1px; }
.dw-value { color:#fafafa; font-variant-numeric:tabular-nums; font-size:30px; font-weight:400; letter-spacing:-1px; margin:18px 0 10px; }
.dw-card small { color:#969696; font-size:12px; line-height:1.6; }.dw-positive { color:#86a995 !important; }.dw-negative { color:#c58c8c !important; }
.dw-table-wrap { width:100%; overflow-x:auto; border:1px solid #2b2b2b; border-radius:14px; margin:16px 0 24px; }
.dw-table { width:100%; border-collapse:collapse; white-space:nowrap; font-size:13px; background:#101010; }
.dw-table th { color:#969696; font-size:11px; font-weight:400; letter-spacing:.2px; padding:16px 20px; text-align:right; background:#141414; }
.dw-table th:first-child,.dw-table td:first-child { text-align:left; }
.dw-table td { padding:18px 20px; border-top:1px solid #282828; text-align:right; color:#dedede; font-variant-numeric:tabular-nums; }
.dw-table tr.dw-highlight td { background:#1b1b1b; }.dw-dot { width:7px; height:7px; display:inline-block; border-radius:50%; margin-right:9px; }
.dw-callout { border:1px solid #333333; border-radius:12px; background:#141414; padding:20px 24px; margin:16px 0 24px; color:#a3a3a3; font-size:14px; line-height:1.75; }
.dw-callout strong { color:#ededed; font-weight:500; }
.dw-flow { display:flex; gap:12px; align-items:stretch; margin:24px 0 32px; }
.dw-node { background:#141414; border:1px solid #2b2b2b; border-radius:14px; padding:24px; flex:1; min-width:0; }
.dw-node b { display:block; color:#ededed; font-size:15px; font-weight:400; margin:18px 0 10px; letter-spacing:-.2px; }
.dw-node small { display:block; color:#969696; font-size:12px; line-height:1.75; }
.dw-node span { color:#a3a3a3; font-size:10px; letter-spacing:1px; }
.dw-platform-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:24px 0; }
.dw-branches { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin:16px 0; }
.dw-branch { background:#101010; border:1px dashed #454545; border-radius:14px; padding:28px; }
.dw-branch h3 { color:#ededed; font-size:22px; font-weight:400; letter-spacing:-.4px; margin:16px 0; }
.dw-branch p { color:#969696; line-height:1.75; font-size:13px; }.dw-branch strong { color:#d4d4d4; font-weight:500; }
.dw-branch .dw-label { color:#a3a3a3; font-size:10px; letter-spacing:1px; }
.dw-join { color:#a3a3a3; border:1px solid #2b2b2b; border-radius:14px; background:#141414; font-size:13px; padding:24px 28px; margin:20px 0; line-height:1.9; }
.dw-join strong { display:block; color:#ededed; font-size:15px; font-weight:400; margin-bottom:8px; }
.dw-footer { display:flex; justify-content:space-between; flex-wrap:wrap; gap:12px; border-top:1px solid #282828; margin-top:40px; padding-top:24px; color:#858585; font-size:12px; line-height:1.9; }.dw-footer a { color:#c7c7c7; text-underline-offset:4px; }
@media(max-width:1000px) { .dw-flow { flex-wrap:wrap; }.dw-node { flex-basis:180px; }.dw-platform-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
@media(max-width:760px) {
 .gradio-container { padding:22px 18px !important; }.dw-masthead { padding-bottom:24px; }.dw-masthead-meta span { display:none; }
 .dw-hero { align-items:flex-start; gap:24px; flex-direction:column; margin-top:0; padding-bottom:28px; }
 .gradio-container .row { flex-direction:column !important; }
 .gradio-container .row > * { width:100% !important; min-width:0 !important; flex:auto !important; }
 .dw-hero h1 { font-size:36px; letter-spacing:-1.2px; }.dw-hero p { font-size:14px; }.dw-hero-aside { text-align:left; }
 .dw-cards { grid-template-columns:repeat(2,minmax(0,1fr)); }.dw-card { padding:18px; }.dw-value { font-size:25px; }
 .dw-node { padding:20px; flex-basis:140px; }.dw-branches { grid-template-columns:1fr; }.dw-branch { padding:24px; }
 .dw-section h2 { font-size:22px; }.gradio-container [role="tablist"] { gap:20px; }
}

.dw-demo-status { margin:0; color:#a3a3a3; font-size:11px; }
.gradio-container .dw-home { align-items:center; gap:48px; padding:36px 0 20px; }
.dw-home-copy { gap:24px !important; }
.dw-home-heading h1 { color:#fafafa; font-size:56px; font-weight:400; letter-spacing:-2.3px; line-height:1.08; margin:24px 0 20px; max-width:450px; }
.dw-home-heading p { color:#969696; font-size:17px; line-height:1.6; max-width:360px; margin:0; }
.gradio-container .dw-home-actions { gap:12px; }
.gradio-container .dw-home-actions button,.gradio-container .dw-home-actions a { min-height:44px; font-size:13px; }
.dw-home-visual { border:1px solid #282828; border-radius:18px; padding:20px 8px 16px; background:#0a0a0a; gap:0 !important; }
.dw-preview-note { color:#969696; font-size:11px; text-align:center; margin:4px 12px; line-height:1.7; }
.gradio-container .dw-home-visual .block { border:0 !important; background:transparent !important; }
.gradio-container [role="tabpanel"] > .gap { gap:16px; }
.gradio-container details { border-radius:10px !important; }
@media(max-width:760px) {
 .dw-masthead { gap:12px; }.dw-masthead-meta { width:100%; justify-content:space-between; }
 .gradio-container .dw-home { padding:12px 0 8px; gap:28px; }
 .dw-home-heading h1 { font-size:42px; letter-spacing:-1.6px; max-width:330px; margin:18px 0; }
 .dw-home-heading p { font-size:15px; }
 .gradio-container .dw-home-actions { flex-direction:row !important; }
 .gradio-container .dw-home-actions > * { flex:1 !important; width:auto !important; }
 .dw-home-visual { padding:12px 0; }
}

@media(prefers-reduced-motion:reduce) { .gradio-container * { scroll-behavior:auto !important; transition:none !important; } }
"""
HERO = """<div class="dw-masthead"><div class="dw-brand"><span class="dw-brand-mark" aria-hidden="true"></span>Dwight</div>
<div class="dw-masthead-meta"><p class="dw-demo-status">Synthetic demo · No account connected</p></div></div>"""
FOOTER = """<div class="dw-footer"><span>Dwight · Research preview 0.6.0</span>
<a href="https://github.com/neekitalian/dwight-schrute-bot" target="_blank" rel="noopener">Source &amp; methodology</a></div>"""

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
    for (key, label), tone in zip(VARIANT_LABELS.items(), ("#e8e8e8", "#b6a88c", "#839aa8")):
        m = payload["test"]["variants"][key]["metrics"]
        rows.append([f'<span class="dw-dot" style="background:{tone}"></span>{escape(label)}', str(m["trades"]),
                     f'<span class="{color(m["net_pnl"])}">{money(m["net_pnl"], True)}</span>',
                     f'{100*m["return_fraction"]:+.2f}%', f'{100*m["win_rate"]:.1f}%',
                     f'{100*m["max_bar_close_drawdown"]:.2f}%', f'{m["expectancy_r"]:+.2f}R'])
    return table(["Independent replay", "Trades", "Net P&L / USD", "Return", "Win rate", "Close drawdown", "Mean net R"], rows, 2)

def performance_note(payload):
    days = payload["report"]["partitions"]["test"]
    return (f'**{escape(payload["case"]["label"])} · test partition {days[0]} to {days[-1]} · '
            f'{len(days)} synthetic sessions.** ${payload["test"]["initial_capital"]:,.0f} starting capital per policy. Modeled costs included.')

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
            'The local toolkit records proposals and normalized fill imports. Its native TradingView export mapping remains unverified. This Space displays replay results only.\n\n'
            f'**Input SHA256:** {r["input_sha256"]}\n\n**Model SHA256:** {r["model_sha256"]}\n\n'
            f'**Engine and features SHA256:** {r["code_sha256"]}')

def transformer_flow(choice="both"):
    a = connection_analysis(choice)
    nodes = [("OBSERVE", "Completed bars", "Synthetic five minute inputs"),
             ("PROPOSE", "VWAP candidate", "Ten numeric features"), ("FILTER", "Dwight classifier", "Current logistic model"),
             ("CONSTRAIN", "Fixed risk rules", "Sizing, stop, target, daily limit"),
             ("EVALUATE", "Replay outcomes", "Measured synthetic results")]
    flow = '<div class="dw-flow">' + ''.join(
        f'<div class="dw-node"><span>{index:02d} / {step}</span><b>{title}</b><small>{subtitle}</small></div>' for index, (step, title, subtitle) in enumerate(nodes, 1)) + '</div>'
    branches = '<div class="dw-branches">' + ''.join(
        f'<div class="dw-branch"><div class="dw-label">PROPOSED · NOT CONNECTED</div><h3>{escape(p["title"])}</h3>'
        f'<p>{"Forecast features from past bars." if p["title"] == "Price sequences" else "Sentiment features from timed text."}</p></div>' for p in a["selected_paths"]) + '</div>'
    return (section("Inside Dwight", "Current synthetic replay") + flow +
            section("Proposed transformer inputs", "Their trading contribution has not been measured.") + branches +
            '<div class="dw-join"><strong>Keep what improves the test</strong>Compare each new input with Dwight alone. Transformer outputs never alter the fixed risk policy.</div>')

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
