"""Public synthetic research dashboard. No data uploads or execution endpoints."""
import os
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

import gradio as gr

from .analytics import CASE_IDS, CASE_LABELS, VARIANT_LABELS, run_dashboard_experiment, run_robustness_summary
from .charts import default_session, diagnostics_figure, equity_figure, outcomes_figure, session_figure, sessions
from . import presentation as ui
from . import walkforward_view as walkforward
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


def build_app():
    with gr.Blocks(title="Dwight · QQQ Research", analytics_enabled=False) as app:
        html(ui.HERO)
        active_case = gr.State("seed42")
        with gr.Row():
            case = gr.Dropdown(choices=[(CASE_LABELS[k], k) for k in CASE_IDS], value="seed42", label="Fixed research case", scale=3)
            gr.Markdown("**Four reproducible tests.** The first visit trains and audits all four cases on CPU. Later visits reuse this process's results.", elem_classes="dw-note")
            refresh = gr.Button("Evaluate case", variant="primary", scale=1)
        with gr.Tab("Performance"):
            cards = html('<div class="dw-callout">Computing the fixed synthetic experiments…</div>')
            note = gr.Markdown()
            equity = gr.Plot(show_label=False)
            html(ui.section("Three policies. The same test period.", "Net P&L includes modeled commissions and slippage. Lower drawdown is better."))
            comparison = html()
            html(ui.section("Does it hold across different paths?", "All four declared cases are shown, including cases where the classifier falls behind."))
            robustness = html()
            html(ui.section("What produced the return?", "Trade outcomes in planned risk units and session P&L. All values are simulated."))
            outcomes = gr.Plot(show_label=False)
        with gr.Tab("Walk-forward"):
            html(ui.section("Test the process across later windows", "One fixed synthetic path. Long-only policies. A final period left untouched."))
            gr.Markdown(walkforward.INTRO)
            walkforward_run = gr.Button("Evaluate walk-forward", variant="primary")
            walkforward_cards = html('<div class="dw-callout">Not evaluated yet. Select Evaluate walk-forward to run the fixed experiment on CPU.</div>')
            walkforward_timeline = gr.Plot(show_label=False)
            walkforward_pnl = gr.Plot(show_label=False)
            html(ui.section("Every declared window", "Dates, sample counts, selected thresholds and incomplete windows remain visible. Amounts include modeled costs."))
            walkforward_table = html()
            walkforward_note = gr.Markdown()
            with gr.Accordion("Inspect synthetic walk-forward evidence", open=False):
                walkforward_evidence = gr.JSON(label="Synthetic evidence · holdout not evaluated")
            gr.Markdown(walkforward.WORKFLOW_NOTE)
            walkforward_run.click(evaluate_walkforward, None,
                                  [walkforward_cards, walkforward_timeline, walkforward_pnl,
                                   walkforward_table, walkforward_note, walkforward_evidence],
                                  api_name="walkforward", concurrency_id="synthetic-research",
                                  concurrency_limit=1, trigger_mode="always_last")
        with gr.Tab("Trade explorer"):
            html(ui.section("Read the trade in context", "Select a fabricated session and inspect its bars, indicators and simulated fills."))
            with gr.Row():
                day = gr.Dropdown(choices=[], label="Test session · New York date", interactive=True)
                variant = gr.Dropdown(choices=[(v, k) for k, v in VARIANT_LABELS.items()], value="filtered", label="Policy", interactive=True)
            price = gr.Plot(show_label=False)
            gr.Markdown("**Chart time: New York.** Candles are stamped at their opening time; completed values are known five minutes later. Up triangles mark long entries, down triangles mark short entries, and crosses mark exits. Entry fills are simulated at the next bar. Exit markers identify the bar; exact intrabar fill times are unknown. VWAP is computed from bar typical prices.")
            trades = gr.Dataframe(headers=["Entry bar / NY", "Side", "Shares", "Entry", "Stop", "Target", "Exit bar / NY", "Exit", "Reason", "Net P&L / USD", "Net R"],
                                  datatype=["str", "str", "number", "number", "number", "number", "str", "number", "str", "number", "number"],
                                  type="array", interactive=False, label="Simulated trade ledger · selected session", wrap=True)
        with gr.Tab("Model analysis"):
            html(ui.section("Why Dwight takes or skips", "The active research model is logistic regression. It is not a transformer."))
            diagnostics = gr.Markdown()
            diagnostic_plot = gr.Plot(show_label=False)
            gr.Markdown("**What the score means.** The model estimates a positive net outcome under this simulator's rules. It does not predict guaranteed profit. Filtering can change which later candidates are available, so each policy is replayed independently.\n\n**What the explanation means.** Each feature is standardized with training statistics, multiplied by its fitted coefficient, and combined with the intercept. The chart averages absolute contributions across labeled baseline candidates in the test period. This explains the fitted score; it is not causal feature importance.")
        with gr.Tab("Transformer connections"):
            gr.Markdown("**Price sequences and news, evaluated separately and together.** These are proposed feature inputs for a future Dwight model. None is connected; there are no measured transformer trading results yet.")
            connection = gr.Radio(choices=[("Both", "both"), ("Price sequences", "price"), ("News context", "news")], value="both", label="Explore a connection")
            flow = html(ui.transformer_flow())
            details = gr.Markdown(ui.transformer_details())
            html(ui.section("Measure the added value", "Keep the data, costs and strategy rules comparable. Adding a larger model is an experiment, not an automatic improvement."))
            html(ui.transformer_comparison())
            gr.Markdown(ui.transformer_protocol())
        with gr.Tab("Method & evidence"):
            html(ui.section("Trace the result back to its rules", "Replay checks, data identity and the boundaries of this experiment."))
            audit = html()
            method = gr.Markdown()
            with gr.Accordion("Inspect report, model coefficients and audit evidence", open=False):
                evidence = gr.JSON(label="Synthetic evidence · no private input paths")
            with gr.Accordion("Original compact experiment API", open=False):
                gr.Markdown("The original seed 42 endpoint remains available for existing research clients. It computes the same synthetic pipeline and returns its compact report.")
                legacy_run = gr.Button("Run compact experiment")
                legacy_summary = gr.Markdown()
                legacy_comparison = gr.Dataframe(headers=COMPARISON_HEADERS, datatype=["str"]+["number"]*5, type="array", interactive=False)
                legacy_model = gr.Markdown()
                legacy_report = gr.JSON()
                legacy_run.click(run_demo, None, [legacy_summary, legacy_comparison, legacy_model, legacy_report], api_name="synthetic_experiment", concurrency_id="synthetic-research", concurrency_limit=1)
        html(ui.FOOTER)
        outputs = [cards, note, equity, comparison, outcomes, day, price, trades, diagnostics, diagnostic_plot, audit, method, evidence, active_case, robustness]
        options = dict(concurrency_id="synthetic-research", concurrency_limit=1, trigger_mode="always_last")
        app.load(load_dashboard, [case, variant], outputs, api_name=False, **options)
        refresh.click(load_dashboard, [case, variant], outputs, api_name="dashboard", **options)
        case.input(load_dashboard, [case, variant], outputs, api_name=False, **options)
        day.input(inspect_session, [active_case, day, variant], [price, trades], api_name="session", **options)
        variant.input(inspect_session, [active_case, day, variant], [price, trades], api_name=False, **options)
        connection.input(inspect_connections, connection, [flow, details], api_name="connections", concurrency_limit=4)
    return app.queue(max_size=8, default_concurrency_limit=1)


def launch_app(server_name="0.0.0.0", server_port=7860):
    theme = gr.themes.Base(primary_hue="teal", secondary_hue="blue", neutral_hue="slate", font=["Inter", "system-ui", "sans-serif"])
    return build_app().launch(server_name=server_name, server_port=server_port, show_error=False,
                              # Native style rules preserve root media queries;
                              # Gradio scopes its css argument inside .contain.
                              theme=theme, head="<style>" + ui.CSS + "</style>",
                              js="() => document.documentElement.classList.add('dark')",
                              footer_links=[])


if __name__ == "__main__":
    launch_app()
