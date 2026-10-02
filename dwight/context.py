"""Validated, point-in-time QQQ context for offline decision experiments.

This module joins already prepared context snapshots; it neither downloads
fundamentals/news nor computes financial facts. ``published_at`` describes
publication and ``available_at`` describes when the feature set could first
have been used. Neither timestamp nor a source label independently proves a
historical vintage. Dataset builders must retain the underlying releases and
their acquisition/processing evidence, including revision history.

All lookups are as of an aware decision timestamp. A newer expired vintage
does not revive an older one. Missing context is represented explicitly, not
inferred from a zero-valued feature.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re


UTC = timezone.utc
MAX_CONTEXT_FEATURES = 64
_FEATURE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_SNAPSHOT_FIELDS = {
    "symbol", "published_at", "available_at", "expires_at", "source", "features",
}
_DOCUMENT_FIELDS = {"schema_version", "symbol", "feature_names", "snapshots"}


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp string")
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timezone missing")
        return result.astimezone(UTC)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp string") from exc


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    """Do not silently discard duplicate object keys while decoding JSON."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON constant is invalid: {value}")


@dataclass(frozen=True)
class _Snapshot:
    published_at: datetime
    available_at: datetime
    expires_at: datetime
    source: str
    values: tuple[float, ...]
    snapshot_id: str | None


class PointInTimeContext:
    """Immutable input copy with exact-schema validation and causal lookups.

    Feature names define the output order and must be unique ASCII identifiers
    of at most 64 characters. At most 64 context features are supported. Empty
    names and snapshots support a technical-only comparison arm. ``age_days``
    measures time since feature availability, not since publication.
    """

    __slots__ = ("_feature_names", "_snapshots", "_available_times")

    def __init__(self, feature_names: tuple[str, ...], snapshots: list[dict]):
        if not isinstance(feature_names, tuple):
            raise ValueError("feature_names must be a tuple")
        if len(feature_names) > MAX_CONTEXT_FEATURES:
            raise ValueError(f"At most {MAX_CONTEXT_FEATURES} context features are allowed")
        if any(not isinstance(name, str) or not _FEATURE_NAME.fullmatch(name)
               for name in feature_names):
            raise ValueError("Feature names must be ASCII identifiers of 1 to 64 characters")
        if len(set(feature_names)) != len(feature_names):
            raise ValueError("Feature names must be unique")
        if not isinstance(snapshots, list):
            raise ValueError("snapshots must be a list")
        if snapshots and not feature_names:
            raise ValueError("Snapshots require at least one feature name")

        self._feature_names = tuple(feature_names)
        expected_features = set(feature_names)
        validated = []
        availability_times = set()
        for row in snapshots:
            if not isinstance(row, dict):
                raise ValueError("Each snapshot must be an object")
            if not _SNAPSHOT_FIELDS <= row.keys() or row.keys() - _SNAPSHOT_FIELDS - {"snapshot_id"}:
                raise ValueError("Snapshot has missing or unknown fields")
            if row["symbol"] != "QQQ":
                raise ValueError("Context snapshots must have symbol QQQ")
            if "snapshot_id" in row and not isinstance(row["snapshot_id"], str):
                raise ValueError("snapshot_id must be a string when supplied")
            source = row["source"]
            if not isinstance(source, str) or not source.strip():
                raise ValueError("Snapshot source must be a nonempty string")
            published = _timestamp(row["published_at"], "published_at")
            available = _timestamp(row["available_at"], "available_at")
            expires = _timestamp(row["expires_at"], "expires_at")
            if not published <= available < expires:
                raise ValueError("Snapshot timestamps must satisfy published_at <= available_at < expires_at")
            if available in availability_times:
                raise ValueError("Snapshot available_at timestamps must be unique")
            availability_times.add(available)

            features = row["features"]
            if not isinstance(features, dict) or features.keys() != expected_features:
                raise ValueError("Snapshot features must exactly match feature_names")
            values = []
            for name in feature_names:
                value = features[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"Feature {name} must be a finite number, not a boolean")
                try:
                    number = float(value)
                except (ValueError, OverflowError) as exc:
                    raise ValueError(f"Feature {name} must be a finite number") from exc
                if not math.isfinite(number):
                    raise ValueError(f"Feature {name} must be a finite number")
                values.append(number)
            validated.append(_Snapshot(published, available, expires, source, tuple(values),
                                       row.get("snapshot_id")))

        self._snapshots = tuple(sorted(validated, key=lambda row: row.available_at))
        self._available_times = tuple(row.available_at for row in self._snapshots)

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self._feature_names

    def to_dict(self) -> dict:
        """Return a detached, normalized input document for run provenance.

        This is the validated snapshot set captured during construction, not a
        new read of the source file. Byte-level file provenance, if needed,
        should additionally retain a checksum of the original JSON bytes.
        """
        snapshots = []
        for snapshot in self._snapshots:
            row = {
                "symbol": "QQQ",
                "published_at": snapshot.published_at.isoformat(),
                "available_at": snapshot.available_at.isoformat(),
                "expires_at": snapshot.expires_at.isoformat(),
                "source": snapshot.source,
                "features": dict(zip(self._feature_names, snapshot.values)),
            }
            if snapshot.snapshot_id is not None:
                row["snapshot_id"] = snapshot.snapshot_id
            snapshots.append(row)
        return {"schema_version": 1, "symbol": "QQQ",
                "feature_names": list(self._feature_names), "snapshots": snapshots}

    @classmethod
    def from_path(cls, path: str | Path) -> PointInTimeContext:
        """Load a version 1 QQQ context document without permissive coercions."""
        document = json.loads(Path(path).read_text(encoding="utf-8"),
                              object_pairs_hook=_json_object,
                              parse_constant=_reject_json_constant)
        if not isinstance(document, dict) or document.keys() != _DOCUMENT_FIELDS:
            raise ValueError("Context document has missing or unknown fields")
        if type(document["schema_version"]) is not int or document["schema_version"] != 1:
            raise ValueError("Context schema_version must be integer 1")
        if document["symbol"] != "QQQ":
            raise ValueError("Context document symbol must be QQQ")
        if not isinstance(document["feature_names"], list):
            raise ValueError("Document feature_names must be a list")
        return cls(tuple(document["feature_names"]), document["snapshots"])

    def lookup(self, at: datetime) -> dict:
        """Return the newest available, unexpired snapshot at ``at``.

        Availability is inclusive; expiry is exclusive. Metadata is UTC ISO
        text. The returned list/dict is detached from this object's state.
        """
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Context lookup requires a timezone-aware datetime")
        try:
            at = at.astimezone(UTC)
        except (ValueError, OverflowError) as exc:
            raise ValueError("Context lookup timestamp cannot be normalized to UTC") from exc
        missing = {
            "available": False,
            "values": [0.0] * len(self._feature_names),
            "available_at": None,
            "published_at": None,
            "expires_at": None,
            "source": None,
            "age_days": 0.0,
        }
        index = bisect_right(self._available_times, at) - 1
        if index < 0:
            return missing
        snapshot = self._snapshots[index]
        if at >= snapshot.expires_at:
            return missing
        return {
            "available": True,
            "values": list(snapshot.values),
            "available_at": snapshot.available_at.isoformat(),
            "published_at": snapshot.published_at.isoformat(),
            "expires_at": snapshot.expires_at.isoformat(),
            "source": snapshot.source,
            "age_days": (at - snapshot.available_at).total_seconds() / 86400.0,
        }
