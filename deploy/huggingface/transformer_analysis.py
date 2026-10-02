"""Evidence and proposed feature connections, never a model inference endpoint.

This registry makes the distinction between running Dwight code and future
research explicit. It imports no model SDK, reads no files and makes no requests.
"""
from copy import deepcopy


SOURCES_VERIFIED_AT = "2026-10-02"
MODEL_PATHS = {
    "price": {
        "title": "Price sequences",
        "status": "Proposed research. Not connected.",
        "connected": False,
        "measured_uplift": None,
        "question": "Does recent price context help Dwight reject weak VWAP setups?",
        "inputs": [
            "Completed QQQ five minute bars from the same licensed feed used for evaluation.",
            "Past price and volume windows, with session boundaries and missing bars recorded.",
            "Only information available when the VWAP candidate becomes actionable.",
        ],
        "output_features": [
            "Forecast median change scaled by the candidate's observed ATR.",
            "Forecast interval width as a possible uncertainty feature.",
            "Alignment between the candidate direction and the forecast median change.",
        ],
        "handoff": (
            "A frozen sequence model would produce a few extra numeric features. "
            "A newly trained Dwight classifier would combine them with its existing "
            "ten features and make the take or skip decision."
        ),
        "baseline_comparison": (
            "Compare against Dwight alone and a cheap lagged return and volatility "
            "feature extension. A larger model must add value beyond extra history."
        ),
        "validation": [
            "Forecast error and interval coverage against a last price forecast on later sessions.",
            "Trade classification, net return and drawdown against the same frozen strategy rules.",
            "Inference time, memory, availability and added cost per completed candidate.",
        ],
        "limitations": [
            "A price forecast is not the probability that a stop and target trade wins.",
            "General forecasting benchmarks do not establish an advantage on QQQ.",
            "Do not join sessions as if the overnight gap were a normal five minute interval.",
            "Pretraining overlap with the evaluation period must be investigated; unknown overlap stays a stated limitation.",
        ],
        "models": [
            {
                "name": "Chronos 2",
                "model_id": "amazon/chronos-2",
                "url": "https://huggingface.co/amazon/chronos-2",
                "source_url": "https://huggingface.co/amazon/chronos-2",
                "license": "Apache-2.0 declared in the model card",
                "license_scope": "model card declaration",
                "scope": "Time series forecasting with quantile outputs and optional covariates.",
                "role": "First price sequence candidate",
            },
            {
                "name": "TimesFM 2.5",
                "model_id": "google/timesfm-2.5-200m-pytorch",
                "url": "https://huggingface.co/google/timesfm-2.5-200m-pytorch",
                "source_url": "https://huggingface.co/google/timesfm-2.5-200m-pytorch",
                "license": "Apache-2.0 declared in the model card",
                "license_scope": "model card declaration",
                "scope": "Time series forecasting with point and quantile outputs.",
                "role": "Alternative price model, tested separately",
            },
        ],
    },
    "news": {
        "title": "News context",
        "status": "Proposed research. Not connected.",
        "connected": False,
        "measured_uplift": None,
        "question": "Does timely news context distinguish similar VWAP setups?",
        "inputs": [
            "Licensed English headlines or text with publication time, first receipt time and source.",
            "A frozen snapshot of the original article, its revisions and duplicate identifiers.",
            "QQQ relevant company mapping using constituents and weights known on that date.",
        ],
        "output_features": [
            "Positive, negative and neutral sentiment scores for eligible text.",
            "Article age, deduplicated news count and a separate missing news indicator.",
            "Time weighted sentiment summaries, optionally weighted by historical QQQ membership.",
        ],
        "handoff": (
            "A frozen text model would summarize sentiment. Dwight would learn whether "
            "those scores help its take or skip decision after accounting for price, "
            "volume and time of day. Text never changes sizing limits or protective exits."
        ),
        "baseline_comparison": (
            "Compare against Dwight alone and an article age and count extension "
            "without sentiment. This tests whether the text model adds information "
            "beyond the existence of news."
        ),
        "validation": [
            "Review entity relevance, duplicates and sentiment on a small labeled text sample.",
            "Compare models on the same candidate cohort, preserving missing news as missing.",
            "Measure ingestion and inference latency, source coverage and licensing cost.",
        ],
        "limitations": [
            "Positive financial language does not mean the next QQQ price move is positive.",
            "FinBERT sentiment is not an earnings surprise or event classification model.",
            "A publication timestamp alone does not prove the article was available to the bot.",
            "QQQ aggregates many companies; current constituents must not be projected into past tests.",
        ],
        "models": [
            {
                "name": "FinBERT",
                "model_id": "ProsusAI/finbert",
                "url": "https://huggingface.co/ProsusAI/finbert",
                "source_url": "https://github.com/ProsusAI/finBERT/blob/master/LICENSE",
                "license": "Upstream code is Apache-2.0; Hub weights license needs confirmation",
                "license_scope": "source code only confirmed; no license declared in the inspected Hub card",
                "scope": "English financial text sentiment with positive, negative and neutral outputs.",
                "role": "News research candidate pending artifact license confirmation",
            },
        ],
    },
}

CURRENT_MODEL = {
    "name": "Dwight take or skip classifier",
    "kind": "Logistic regression",
    "feature_count": 10,
    "status": "Implemented. The public Space evaluates generated data only.",
    "description": (
        "VWAP rules propose a candidate. Ten numeric features describe direction, "
        "distance from VWAP and EMA, volatility, candle body, relative volume, "
        "session time, stop distance and confirmed structure. Logistic regression "
        "estimates the chance of a positive net outcome under the replay assumptions. "
        "The selected threshold decides take or skip."
    ),
    "boundary": (
        "Current artifacts require the exact vwap-candidate-v1 schema. Adding "
        "transformer features requires a new schema, fitted preprocessing, model "
        "artifact and reviewed release. They cannot be appended to today's model."
    ),
}

EXPERIMENT_ARMS = [
    {"id": "dwight", "title": "Dwight alone", "features": "Existing ten features", "status": "Synthetic demonstration available"},
    {"id": "dwight_price", "title": "Dwight plus price context", "features": "Existing features plus sequence outputs", "status": "Not measured"},
    {"id": "dwight_news", "title": "Dwight plus news context", "features": "Existing features plus timed news outputs", "status": "Not measured"},
    {"id": "dwight_both", "title": "Dwight plus both", "features": "Existing features plus both feature groups", "status": "Not measured"},
]

EVALUATION = [
    "Build one chronological candidate dataset with a common availability cutoff. Fit preprocessing and classifiers on training data only.",
    "Choose thresholds and research settings on validation sessions. Purge labels whose exit outcomes cross a split boundary and apply the predeclared embargo for overlapping label horizons.",
    "Use identical held out sessions, capital, feed, execution assumptions and risk rules. Independently replay each policy because its open positions can change later eligible signals.",
    "Report candidate level Brier score and calibration alongside net return after costs, drawdown, trade count, turnover and session coverage.",
    "Compare against simple extra history and news count controls. Estimate uncertainty with paired session or block resampling and show the sample size.",
    "Freeze the evaluation plan before opening a final holdout. If results inspire another design, reserve a new holdout or use nested walk forward validation.",
    "Finish with forward shadow observation from newly received data. A promising backtest cannot promote its own model.",
]

EVIDENCE_GATE = [
    "Pin source code, model revision, tokenizer, transformations, input schema, source snapshots and checksums.",
    "Record model weights, code, news and market data permissions separately before a commercial release.",
    "Measure improvement on real QQQ data and show its uncertainty, operating cost and latency.",
    "If a required model output is late or missing, abstain unless a separately evaluated fallback is explicitly configured.",
    "Keep deterministic risk checks independent of model output. The Space does not send orders or activate connectors.",
]


def connection_analysis(choice: str = "both") -> dict:
    """Return an isolated presentation record; an invalid choice fails closed."""
    aliases = {"price sequences": "price", "news context": "news", "price and news": "both"}
    if not isinstance(choice, str):
        raise ValueError("Choose price, news or both")
    normalized = choice.strip().lower()
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"price", "news", "both"}:
        raise ValueError("Choose price, news or both")
    paths = ["price", "news"] if normalized == "both" else [normalized]
    nodes = [
        {"id": "bars", "label": "Completed bars", "status": "current_synthetic"},
        {"id": "vwap", "label": "VWAP candidates", "status": "implemented"},
        {"id": "features", "label": "Ten current features", "status": "implemented"},
        {"id": "dwight", "label": "Dwight classifier", "status": "implemented"},
        {"id": "risk", "label": "Fixed replay risk rules", "status": "implemented"},
        {"id": "report", "label": "Simulated outcomes", "status": "implemented"},
    ]
    edges = [
        {"source": source, "target": target, "status": "implemented"}
        for source, target in (("bars", "vwap"), ("vwap", "features"), ("features", "dwight"), ("dwight", "risk"), ("risk", "report"))
    ]
    for key in paths:
        nodes.append({"id": key, "label": MODEL_PATHS[key]["title"], "status": "proposed"})
        edges.append({"source": key, "target": "dwight", "status": "proposed_new_schema"})
    return deepcopy({
        "schema_version": 1,
        "sources_verified_at": SOURCES_VERIFIED_AT,
        "choice": normalized,
        "title": "How transformer features could add value",
        "status": "Research plan only. No transformer is connected or evaluated.",
        "connected": False,
        "measured_uplift": None,
        "current_model": CURRENT_MODEL,
        "selected_paths": [{"id": key, **MODEL_PATHS[key]} for key in paths],
        "architecture": {"nodes": nodes, "edges": edges},
        "experiments": EXPERIMENT_ARMS,
        "evaluation": EVALUATION,
        "evidence_gate": EVIDENCE_GATE,
    })


def connection_markdown(choice: str = "both") -> str:
    """Concise, source linked narrative for the Space and exported reports."""
    analysis = connection_analysis(choice)
    paragraphs = [
        "**Transformer contribution: not measured. No transformer is connected.**",
        CURRENT_MODEL["description"],
        CURRENT_MODEL["boundary"],
    ]
    for path in analysis["selected_paths"]:
        paragraphs.extend([
            f"**{path['title']}** · {path['question']}",
            path["handoff"],
            "Proposed inputs: " + " ".join(path["inputs"]),
            "Proposed features: " + " ".join(path["output_features"]),
            path["baseline_comparison"],
        ])
        for model in path["models"]:
            paragraphs.append(
                f"[{model['name']}]({model['url']}): {model['scope']} "
                f"[License evidence]({model['source_url']}): {model['license']}."
            )
        paragraphs.append(" ".join(path["limitations"]))
    paragraphs.extend([
        "**How we establish value**",
        "Compare Dwight alone, Dwight with price features, Dwight with news features "
        "and Dwight with both. No percentage improvement is available until those experiments run.",
        " ".join(EVALUATION),
        "**Before connection**",
        " ".join(EVIDENCE_GATE),
    ])
    return "\n\n".join(paragraphs)
