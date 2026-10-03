"""Public research and read-only connection setup. No account or order endpoints."""
import os
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

import gradio as gr

from .analytics import CASE_IDS, CASE_LABELS, VARIANT_LABELS, run_dashboard_experiment, run_robustness_summary
from .charts import default_session, diagnostics_figure, equity_figure, outcomes_figure, preview_figure, session_figure, sessions
from . import presentation as ui
from . import walkforward_view as walkforward
from . import starter
from . import platforms
from .research import COMPARISON_HEADERS, present_experiment, run_synthetic_experiment


def run_demo():
    """Retain the original four output API for existing research clients."""
    try:
        return present_experiment(run_synthetic_experiment())
    except Exception:
        raise gr.Error("The synthetic experiment could not complete. No trading action was taken.") from None


def load_dashboard(case_id="seed42", variant="filtered"):
    try:
        if variant not in VARIANT_LABELS:
            raise ValueError("Unsupported strategy")
        p = run_dashboard_experiment(case_id)
        day = default_session(p)
        evidence = {"report": p["report"], "model": p["model"], "audit": p["audit"], "provenance": p["provenance"]}
        return (ui.metric_cards(p), ui.performance_note(p), equity_figure(p), ui.comparison_table(p),
                outcomes_figure(p), gr.Dropdown(choices=sessions(p), value=day), session_figure(p, day, variant),
                ui.trade_rows(p, day, variant), ui.diagnostics_note(p), diagnostics_figure(p),
                ui.audit_view(p), ui.method_note(p), evidence, case_id,
                ui.robustness_view(run_robustness_summary()))
    except Exception:
        raise gr.Error("This research view could not complete. Choose a fixed synthetic case and try again.") from None


def inspect_session(case_id, day, variant):
    try:
        p = run_dashboard_experiment(case_id)
        return session_figure(p, day, variant), ui.trade_rows(p, day, variant)
    except Exception:
        raise gr.Error("Select a session and strategy from the current synthetic experiment.") from None


def inspect_connections(choice):
    try:
        return ui.transformer_flow(choice), ui.transformer_details(choice)
    except Exception:
        raise gr.Error("Choose price sequences, news context or both.") from None


def platform_setup(platform, feed):
    try:
        instructions, profile, path = platforms.setup(platform, feed)
        public = platform in platforms.PUBLIC_PLATFORMS
        return (instructions, profile, path,
                gr.Button(value="Check public data" if public else "Check privately using the toolkit", interactive=public),
                {"platform": platform, "status": "not_checked", "account_connected": False})
    except (ValueError, TypeError, KeyError):
        raise gr.Error("Choose a listed platform and feed.") from None


def public_connection_check(platform):
    try:
        return platforms.public_check(platform)
    except Exception:
        raise gr.Error("The public data check could not complete. No account was connected.") from None


def evaluate_walkforward():
    try:
        report = walkforward.run_public_walkforward()
        return (walkforward.summary_html(report), walkforward.timeline_figure(report),
                walkforward.pnl_figure(report), walkforward.window_table(report),
                walkforward.evidence_note(report), report)
    except Exception:
        raise gr.Error("The fixed synthetic walk-forward experiment could not complete. No account or trading action was involved.") from None


def html(value="", **kwargs):
    return gr.HTML(value, apply_default_css=False, **kwargs)


def load_preview():
    return preview_figure(run_dashboard_experiment("seed42"))


def show_performance():
    return gr.Tabs(selected="performance")


def connection_summary(result):
    status = str(result.get("status", "not_checked")).replace("_", " ").capitalize()
    platform_id = result.get("platform", "")
    name = platforms.PLATFORMS.get(platform_id, {}).get("name", "Public check")
    checked = result.get("checked_at")
    caption = "Public data only. No account connected."
    if checked:
        caption += f" Checked {checked}."
    return ui.section(f"{name} · {status}", caption)


def build_app():
    with gr.Blocks(title="Dwight · QQQ Research", analytics_enabled=False) as app:
        html(ui.HERO)
        active_case = gr.State("seed42")
        with gr.Tabs(selected="start") as navigation:
            with gr.Tab("Start here", id="start"):
                with gr.Row(elem_classes="dw-home"):
                    with gr.Column(scale=5, min_width=280, elem_classes="dw-home-copy"):
                        html(f'<div class="dw-home-heading"><span class="dw-wordmark">QQQ RESEARCH TOOLKIT</span><h1>{starter.HOME_TITLE}</h1><p>{starter.HOME_SUBTITLE}</p></div>')
                        with gr.Row(elem_classes="dw-home-actions"):
                            view_results = gr.Button("View results", variant="primary", scale=0, min_width=150)
                            gr.Button("Get toolkit", link=starter.RELEASE, link_target="_blank", scale=0, min_width=150)
                    with gr.Column(scale=6, min_width=300, elem_classes="dw-home-visual"):
                        preview = gr.Plot(show_label=False, container=False)
                        html('<p class="dw-preview-note">Fixed case 42 · Invented QQQ prices · Modeled costs</p>')
                html(starter.FLOW)
                with gr.Accordion("Get started", open=False):
                    starter_choice = gr.Dropdown(choices=list(starter.CHOICES), value=starter.CHOICES[0], label="Choose your next step")
                    starter_steps = gr.Markdown(starter.plan(starter.CHOICES[0]))
                    starter_choice.input(starter.plan, starter_choice, starter_steps, api_name="starter_plan")
                with gr.Accordion("What’s included & current limits", open=False):
                    gr.Markdown(starter.BOUNDARY)
                    gr.Markdown(starter.DOWNLOAD)
            with gr.Tab("Connections", id="connections"):
                html(ui.section("Find your platform.", "Public data checks and private setup profiles. No account or order access here."))
                html(platforms.overview())
                with gr.Row():
                    platform = gr.Dropdown(choices=[(v['name'], k) for k, v in platforms.PLATFORMS.items()],
                                           value="tradingview", label="Platform", scale=3)
                    feed = gr.Dropdown(choices=[("SIP · consolidated", "sip"), ("IEX · one exchange", "iex")],
                                       value="sip", label="Alpaca data feed", scale=2)
                initial_details, initial_profile, initial_path = platforms.setup("tradingview")
                with gr.Row():
                    check_public = gr.Button("Check privately using the toolkit", interactive=False)
                    profile_download = gr.DownloadButton("Download setup profile", value=initial_path)
                status_summary = html(connection_summary({"platform": "tradingview", "status": "not_checked"}))
                with gr.Accordion("Setup instructions", open=False):
                    platform_details = gr.Markdown(initial_details)
                with gr.Accordion("Check details & setup file", open=False):
                    connection_status = gr.JSON(value={"platform": "tradingview", "status": "not_checked", "account_connected": False},
                                                label="Read-only check")
                    gr.Markdown("Checks run from this server. Regional access and account permissions may differ.")
                    connection_profile = gr.Code(value=initial_profile, language="json", interactive=False)
                with gr.Accordion("Supported uses & limits", open=False):
                    gr.Markdown(platforms.INTRO)
                    gr.Markdown(platforms.BOUNDARY)
                setup_outputs = [platform_details, connection_profile, profile_download, check_public, connection_status]
                platform.input(platform_setup, [platform, feed], setup_outputs, api_name="platform_setup").then(
                    connection_summary, connection_status, status_summary, api_name=False)
                feed.input(platform_setup, [platform, feed], setup_outputs, api_name=False).then(
                    connection_summary, connection_status, status_summary, api_name=False)
                check_public.click(public_connection_check, platform, connection_status, api_name="public_connection_check",
                                   concurrency_id="public-data-checks", concurrency_limit=2, trigger_mode="once").then(
                    connection_summary, connection_status, status_summary, api_name=False)
            with gr.Tab("Performance", id="performance"):
                with gr.Row():
                    case = gr.Dropdown(choices=[(CASE_LABELS[k], k) for k in CASE_IDS], value="seed42", label="Research case", scale=4)
                    refresh = gr.Button("Evaluate case", variant="primary", scale=1)
                cards = html('<div class="dw-callout">Preparing synthetic results…</div>')
                note = gr.Markdown()
                equity = gr.Plot(show_label=False)
                html(ui.section("Compare policies", "Same test period. Returns include modeled costs."))
                comparison = html()
                html(ui.section("Across four cases", "Including paths where Dwight falls behind."))
                robustness = html()
                html(ui.section("Trade outcomes", "Simulated risk units and session P&L."))
                outcomes = gr.Plot(show_label=False)
            with gr.Tab("Walk-forward", id="walkforward"):
                html(ui.section("Test on later periods.", "Synthetic data · Long-only · Final holdout unscored"))
                walkforward_run = gr.Button("Evaluate walk-forward", variant="primary")
                walkforward_cards = html('<div class="dw-callout">Select Evaluate walk-forward to begin.</div>')
                walkforward_timeline = gr.Plot(show_label=False)
                walkforward_pnl = gr.Plot(show_label=False)
                html(ui.section("Every test window", "Dates, samples and thresholds. Modeled costs included."))
                walkforward_table = html()
                with gr.Accordion("Method & evaluation notes", open=False):
                    gr.Markdown(walkforward.INTRO)
                    walkforward_note = gr.Markdown()
                    gr.Markdown(walkforward.WORKFLOW_NOTE)
                with gr.Accordion("Inspect evidence", open=False):
                    walkforward_evidence = gr.JSON(label="Synthetic evidence · holdout not evaluated")
                walkforward_run.click(evaluate_walkforward, None,
                                      [walkforward_cards, walkforward_timeline, walkforward_pnl,
                                       walkforward_table, walkforward_note, walkforward_evidence],
                                      api_name="walkforward", concurrency_id="synthetic-research",
                                      concurrency_limit=1, trigger_mode="always_last")
            with gr.Tab("Trade explorer", id="trades"):
                html(ui.section("Look inside a trade.", "Synthetic candles and simulated fills. Chart time: New York."))
                with gr.Row():
                    day = gr.Dropdown(choices=[], label="Test session", interactive=True)
                    variant = gr.Dropdown(choices=[(v, k) for k, v in VARIANT_LABELS.items()], value="filtered", label="Policy", interactive=True)
                price = gr.Plot(show_label=False)
                gr.Markdown("**Circle:** long entry · **Square:** short entry · **Cross:** exit")
                with gr.Accordion("Timing & fill assumptions", open=False):
                    gr.Markdown("Candles are stamped at their opening time; completed values are known five minutes later. Entries fill at the next bar. Exit markers identify the bar; exact intrabar fill times are unknown. VWAP uses bar typical prices.")
                trades = gr.Dataframe(headers=["Entry bar / NY", "Side", "Shares", "Entry", "Stop", "Target", "Exit bar / NY", "Exit", "Reason", "Net P&L / USD", "Net R"],
                                      datatype=["str", "str", "number", "number", "number", "number", "str", "number", "str", "number", "number"],
                                      type="array", interactive=False, label="Simulated trades", wrap=True)
            with gr.Tab("Model analysis", id="model"):
                html(ui.section("What shapes the score?", "Logistic regression · 10 features · Synthetic test data"))
                diagnostic_plot = gr.Plot(show_label=False)
                with gr.Accordion("Model diagnostics", open=False):
                    diagnostics = gr.Markdown()
                with gr.Accordion("Reading the score & feature chart", open=False):
                    gr.Markdown("The score estimates a positive net outcome under this simulator’s rules. It does not guarantee profit. Each policy is replayed independently.\n\nFeatures use training statistics and fitted coefficients. The chart averages absolute contributions across labeled test candidates. This explains the fitted score, not causal importance.")
            with gr.Tab("Transformer connections", id="transformers"):
                html(ui.section("Explore new inputs.", "Proposed research · Not connected · No measured trading value"))
                connection = gr.Radio(choices=[("Both", "both"), ("Price sequences", "price"), ("News context", "news")], value="both", label="Input type")
                flow = html(ui.transformer_flow())
                with gr.Accordion("Models, inputs & licenses", open=False):
                    details = gr.Markdown(ui.transformer_details())
                with gr.Accordion("Compare experiment designs", open=False):
                    html(ui.transformer_comparison())
                with gr.Accordion("How we would measure value", open=False):
                    gr.Markdown(ui.transformer_protocol())
            with gr.Tab("Method & evidence", id="method"):
                html(ui.section("Check the evidence.", "Replay checks verify software behavior. They do not establish a market edge."))
                audit = html()
                with gr.Accordion("Data, rules & account boundaries", open=False):
                    method = gr.Markdown()
                with gr.Accordion("Report, model & audit data", open=False):
                    evidence = gr.JSON(label="Synthetic evidence · no private input paths")
                with gr.Accordion("Original compact experiment API", open=False):
                    gr.Markdown("Seed 42 endpoint for existing research clients.")
                    legacy_run = gr.Button("Run compact experiment")
                    legacy_summary = gr.Markdown()
                    legacy_comparison = gr.Dataframe(headers=COMPARISON_HEADERS, datatype=["str"]+["number"]*5, type="array", interactive=False)
                    legacy_model = gr.Markdown()
                    legacy_report = gr.JSON()
                    legacy_run.click(run_demo, None, [legacy_summary, legacy_comparison, legacy_model, legacy_report], api_name="synthetic_experiment", concurrency_id="synthetic-research", concurrency_limit=1)
        html(ui.FOOTER)
        outputs = [cards, note, equity, comparison, outcomes, day, price, trades, diagnostics, diagnostic_plot, audit, method, evidence, active_case, robustness]
        options = dict(concurrency_id="synthetic-research", concurrency_limit=1, trigger_mode="always_last")
        view_results.click(show_performance, None, navigation, api_name=False, queue=False)
        app.load(load_preview, None, preview, api_name=False, **options)
        app.load(load_dashboard, [case, variant], outputs, api_name=False, **options)
        refresh.click(load_dashboard, [case, variant], outputs, api_name="dashboard", **options)
        case.input(load_dashboard, [case, variant], outputs, api_name=False, **options)
        day.input(inspect_session, [active_case, day, variant], [price, trades], api_name="session", **options)
        variant.input(inspect_session, [active_case, day, variant], [price, trades], api_name=False, **options)
        connection.input(inspect_connections, connection, [flow, details], api_name="connections", concurrency_limit=4)
    return app.queue(max_size=8, default_concurrency_limit=1)


def launch_app(server_name="0.0.0.0", server_port=7860):
    theme = gr.themes.Base(primary_hue="neutral", secondary_hue="neutral", neutral_hue="neutral", font=["Inter", "system-ui", "sans-serif"])
    return build_app().launch(server_name=server_name, server_port=server_port, show_error=False,
                              # Native style rules preserve root media queries;
                              # Gradio scopes its css argument inside .contain.
                              theme=theme, head="<style>" + ui.CSS + "</style>",
                              js="() => document.documentElement.classList.add('dark')",
                              footer_links=[])


if __name__ == "__main__":
    launch_app()
