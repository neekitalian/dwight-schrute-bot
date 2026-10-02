"""Hugging Face Space: synthetic QQQ research, with no trading capability."""
import os

os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

import gradio as gr

from deploy.huggingface.research import (
    COMPARISON_HEADERS, present_experiment, run_synthetic_experiment,
)


WORKFLOW_HTML = """
<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:stretch;margin:16px 0">
  <div style="flex:1;min-width:150px;border:1px solid #94a3b8;border-radius:12px;padding:18px">
    <strong>1 · Generate</strong><br>Invented QQQ-labelled five-minute bars<br><small>Fixed seed 42</small>
  </div>
  <div style="flex:1;min-width:150px;border:1px solid #94a3b8;border-radius:12px;padding:18px">
    <strong>2 · Split</strong><br>Earlier → later sessions<br><small>60% train / 20% validate / 20% test</small>
  </div>
  <div style="flex:1;min-width:150px;border:1px solid #94a3b8;border-radius:12px;padding:18px">
    <strong>3 · Learn</strong><br>VWAP candidate → take / skip<br><small>Train model; select threshold on validation</small>
  </div>
  <div style="flex:1;min-width:150px;border:1px solid #94a3b8;border-radius:12px;padding:18px">
    <strong>4 · Compare</strong><br>Baseline vs volume filter vs model<br><small>Final test-period simulated results</small>
  </div>
</div>
"""


def run_demo():
    try:
        return present_experiment(run_synthetic_experiment())
    except Exception:
        # Do not expose server paths or internal exception messages in the UI.
        raise gr.Error("The synthetic experiment could not complete. No trading action was taken.") from None


def build_app():
    with gr.Blocks(title="Dwight · QQQ Research Lab", analytics_enabled=False) as app:
        gr.Markdown(
            "# Dwight · QQQ Research Lab\n"
            "**A small model, a testable decision.** Explore our VWAP take/skip experiment.\n\n"
            "**SYNTHETIC DATA ONLY · NO BROKER CONNECTION · NO ORDERS**"
        )
        with gr.Tab("Experiment"):
            gr.Markdown(
                "Run the actual Dwight training and replay pipeline on a fixed, fabricated dataset. "
                "The first run trains a CPU classifier; later requests reuse the same in-memory result. "
                "No API key, account or upload is needed.\n\n"
                "**Fixture:** 500 calendar days · seed 42 · 5-minute bars · QQQ label. "
                "Invented prices and volume do not represent QQQ history."
            )
            run = gr.Button("Run synthetic experiment", variant="primary")
            summary = gr.Markdown("Run the experiment to see the final test-period comparison.")
            comparison = gr.Dataframe(
                headers=COMPARISON_HEADERS,
                datatype=["str", "number", "number", "number", "number", "number"],
                type="array", interactive=False, label="Final test period · simulated results",
            )
            gr.Markdown(
                "Net P&L includes the engine's fixed commission and slippage assumptions. "
                "Drawdown uses five-minute closing marks, so it misses intrabar extremes."
            )
            model = gr.Markdown()
            with gr.Accordion("Inspect the experiment report", open=False):
                report = gr.JSON(label="Research report · temporary file paths omitted")
            run.click(
                fn=run_demo, inputs=None, outputs=[summary, comparison, model, report],
                api_name="synthetic_experiment", concurrency_limit=1,
                concurrency_id="synthetic-research", trigger_mode="once",
            )
        with gr.Tab("Workflow & status"):
            gr.HTML(WORKFLOW_HTML)
            gr.Markdown(
                "### What this Space does\n"
                "It generates a reproducible fixture, fits the existing logistic regression model, "
                "and runs the original baseline, simple volume filter and model filter. "
                "The model learns from completed baseline trades; its features were frozen when "
                "each candidate became available. Filtered strategies are replayed independently.\n\n"
                "### How this becomes paper trading\n"
                "Real QQQ market data → chronological experiments → reviewed model release → "
                "live shadow observation → fixed risk checks → broker paper execution.\n\n"
                "That deployment path belongs to the separate Dwight service. This research Space "
                "has no broker credentials, market-data connection, order endpoint or promotion action.\n\n"
                "### Current boundaries\n"
                "- Synthetic results validate software behavior; real QQQ evaluation is still required.\n"
                "- Small sample requirements are relaxed only for this synthetic smoke test.\n"
                "- Fixture weekdays include exchange holidays; this is not a market calendar simulation.\n"
                "- Input data, trade files and model artifacts are deleted after each uncached run.\n"
                "- The report cache resets with the process; MLflow logging is disabled here.\n"
                "- No result can approve itself for paper or live deployment.\n\n"
                "Training and model selection remain separate from the running trading service. "
                "TradingView integration is a later monitoring or optional signal step."
            )
    return app.queue(max_size=8, default_concurrency_limit=1)


if __name__ == "__main__":
    build_app().launch(server_name="0.0.0.0", server_port=7860, show_error=False)
