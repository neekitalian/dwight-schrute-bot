"""Research produces proposals, never orders. Independent from strategy execution."""
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Protocol


@dataclass(frozen=True)
class Evidence:
    url: str
    published_at: datetime
    observed_at: datetime
    content_hash: str


@dataclass(frozen=True)
class Assessment:
    category: str
    confidence: float
    model_version: str
    question_version: str

    def usable(self, threshold):
        return (self.category in {"relevant", "irrelevant"}
                and math.isfinite(self.confidence)
                and 0 <= self.confidence <= 1
                and self.confidence >= threshold
                and bool(self.model_version and self.question_version))


class ResearchProvider(Protocol):
    def assess(self, text: str) -> Assessment: ...


def classify(text: str, fast: ResearchProvider, fallback: ResearchProvider | None = None,
             threshold: float = .95):
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("invalid threshold")
    trace = []
    for layer, provider in (("fast", fast), ("fallback", fallback)):
        if provider is None:
            continue
        try:
            answer = provider.assess(text)
            trace.append({"layer": layer, "category": answer.category,
                          "confidence": answer.confidence if math.isfinite(answer.confidence) else None,
                          "model": answer.model_version, "question": answer.question_version})
            if answer.usable(threshold):
                return {"category": answer.category, "route": layer, "trace": trace}
        except Exception as exc:
            trace.append({"layer": layer, "error": type(exc).__name__})
    return {"category": "review", "route": "abstain", "trace": trace}
