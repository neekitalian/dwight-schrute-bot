"""Offline QQQ versus QQQx observations, without orders or inferred fills.

Prices use each instrument's own reported units. The comparison is indicative:
the token's economic conversion/rebase multiplier is not established here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
from statistics import mean, median

from dwight.data import NY, exchange_sessions

UTC = timezone.utc
INTERVAL = timedelta(minutes=5)
REAL_KINDS = {"historical_real", "observed_market", "observed_public_market"}
QUANTITY_UNIT = "Kraken-reported QQQx base quantity"


def _time(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an explicit UTC timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{label} is not a valid UTC timestamp") from None
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must use UTC")
    return stamp.astimezone(UTC)


def _number(value, label, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{label} must be finite") from None
    if not math.isfinite(number) or (number < 0 if zero else number <= 0):
        raise ValueError(f"{label} must be finite and {'nonnegative' if zero else 'positive'}")
    return number


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be explicit")
    return value


def _bars(rows, label):
    if not isinstance(rows, list):
        raise ValueError(f"{label} must be a list")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{label} has an invalid row")
        stamp = _time(row.get("timestamp"), f"{label}.timestamp")
        if stamp.minute % 5 or stamp.second or stamp.microsecond:
            raise ValueError(f"{label} timestamps must be five-minute interval starts")
        if stamp in result:
            raise ValueError(f"{label} has a duplicate timestamp")
        values = {key: _number(row.get(key), f"{label}.{key}", zero=key == "volume")
                  for key in ("open", "high", "low", "close", "volume")}
        if (values["low"] > min(values["open"], values["close"]) or
                values["high"] < max(values["open"], values["close"]) or
                values["low"] > values["high"]):
            raise ValueError(f"{label} has inconsistent OHLC")
        result[stamp] = values
    return dict(sorted(result.items()))


def _kind(value, label):
    if not isinstance(value, str) or value not in REAL_KINDS | {"synthetic"}:
        raise ValueError(f"{label} has an unsupported data_kind")
    return value


def _source(source):
    if not isinstance(source, dict):
        raise ValueError("qqq_source must declare provenance")
    required = {"symbol": "QQQ", "asset_class": "equity_etf", "currency": "USD",
                "interval_minutes": 5, "timestamp_label": "start", "adjustment": "raw"}
    for key, expected in required.items():
        if source.get(key) != expected or isinstance(source.get(key), bool):
            raise ValueError(f"qqq_source.{key} must be {expected}")
    for key in ("provider", "feed"):
        _text(source.get(key), f"qqq_source.{key}")
    _kind(source.get("data_kind"), "qqq_source")
    collected = _time(source.get("collected_at"), "qqq_source.collected_at")
    keys = (*required, "provider", "feed", "data_kind", "collected_at", "source_sha256",
            "dataset_fingerprint", "dataset_sha256", "manifest_sha256", "input_sha256",
            "integrity_statement")
    return {key: source[key] for key in keys if key in source}, collected


def _token(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("token_snapshot must be an explicit observation")
    if snapshot.get("schema_version") != 1 or isinstance(snapshot.get("schema_version"), bool):
        raise ValueError("token_snapshot must declare schema_version 1")
    if snapshot.get("execution_enabled") is not False:
        raise ValueError("token_snapshot must explicitly disable execution")
    _kind(snapshot.get("data_kind"), "token_snapshot")
    if snapshot.get("interval_minutes") != 5 or snapshot.get("timestamp_label") != "start":
        raise ValueError("token_snapshot must use five-minute start-labelled bars")
    instrument = snapshot.get("instrument")
    if not isinstance(instrument, dict):
        raise ValueError("token_snapshot.instrument is required")
    expected = {"symbol": "QQQx", "base": "QQQx", "quote": "USD", "venue": "kraken",
                "asset_class": "tokenized_equity", "pair": "QQQxUSD"}
    for key, value in expected.items():
        if instrument.get(key) != value:
            raise ValueError(f"token_snapshot.instrument.{key} must be {value}")
    native = instrument.get("provider_pair_metadata")
    if native is not None:
        if not isinstance(native, dict):
            raise ValueError("provider_pair_metadata must be an object")
        for key, value in (("base", "QQQx"), ("quote", "ZUSD"), ("altname", "QQQxUSD"),
                           ("aclass_base", "tokenized_asset"), ("aclass_quote", "currency")):
            if native.get(key) != value:
                raise ValueError(f"provider_pair_metadata.{key} contradicts instrument identity")
    collection = snapshot.get("collection", {})
    if not isinstance(collection, dict):
        raise ValueError("token_snapshot.collection must be an object")
    start = _time(collection.get("started_at", snapshot.get("collection_started_at")),
                  "token_snapshot.collection.started_at")
    end = _time(collection.get("finished_at", snapshot.get("collection_finished_at")),
                "token_snapshot.collection.finished_at")
    if end < start:
        raise ValueError("token_snapshot collection timestamps are reversed")
    for key, stamp in (("collection_started_at", start), ("collection_finished_at", end)):
        if key in snapshot and _time(snapshot[key], key) != stamp:
            raise ValueError("token_snapshot collection timestamps conflict")
    clean = {key: instrument[key] for key in (*expected, "issuer", "chain", "contract",
                                             "provider_pair_metadata", "quantity_unit",
                                             "corporate_action_multiplier", "quantity_caveat") if key in instrument}
    return clean, end


def _session(stamp, sessions):
    value = sessions.get(stamp.astimezone(NY).date().isoformat())
    return value if value and value.open <= stamp and stamp + INTERVAL <= value.close else None


def _vwap(rows, sessions):
    """Observed regular-session prefixes only, without filling missing volume."""
    result, totals = {}, {}
    for stamp, bar in rows.items():
        session = _session(stamp, sessions)
        if session is None:
            continue
        weighted, volume, count = totals.get(session.date, (0.0, 0.0, 0))
        typical = bar["high"] / 3 + bar["low"] / 3 + bar["close"] / 3
        weighted += typical * bar["volume"]
        volume += bar["volume"]
        count += 1
        if not math.isfinite(weighted) or not math.isfinite(volume):
            raise ValueError("VWAP arithmetic exceeds finite range")
        expected = int((stamp - session.open).total_seconds() // 300) + 1
        result[stamp] = (weighted / volume if volume else None, count == expected)
        totals[session.date] = (weighted, volume, count)
    return result


def _sweep(levels, units, midpoint):
    requested = Decimal(str(units))
    remaining, gross = requested, Decimal(0)
    for price, quantity in levels:
        take = min(remaining, Decimal(str(quantity)))
        gross += take * Decimal(str(price))
        remaining -= take
        if remaining <= 0:
            break
    filled = requested - remaining
    complete = remaining <= 0
    average = float(gross / filled) if filled else None
    return {"requested_units": units, "quantity_unit": QUANTITY_UNIT,
            "filled_units": float(filled), "complete": complete,
            "status": "snapshot_depth_sufficient" if complete else "insufficient_depth",
            "gross_notional_usd": float(gross) if filled else None,
            "average_price_usd": average,
            "full_fill_average_price_usd": average if complete else None,
            "slippage_vs_mid_bps": (average / midpoint - 1) * 10000 if complete else None,
            "fees_usd": None, "actual_fill": False}


def _book(book, as_of, max_age):
    if book is None:
        return {"status": "unavailable", "observed_at": None, "age_seconds": None,
                "midprice_usd": None, "spread_bps": None, "buy_sweeps": [], "sell_sweeps": []}
    if not isinstance(book, dict) or book.get("quantity_unit") != QUANTITY_UNIT:
        raise ValueError("Order book must declare Kraken-reported QQQx quantity units")
    observed = _time(book.get("observed_at"), "orderbook.observed_at")
    age = (as_of - observed).total_seconds()
    if age < 0:
        raise ValueError("Order book observation is after collection completion")
    sides = {}
    for side in ("bids", "asks"):
        rows = book.get(side)
        if not isinstance(rows, list):
            raise ValueError(f"orderbook.{side} must be a list")
        levels = []
        for level in rows:
            if not isinstance(level, (list, tuple)) or len(level) != 2:
                raise ValueError("Order book levels must contain price and quantity")
            levels.append((_number(level[0], "orderbook.price"), _number(level[1], "orderbook.quantity")))
        prices = [level[0] for level in levels]
        if prices != sorted(prices, reverse=side == "bids") or len(set(prices)) != len(prices):
            raise ValueError("Order book levels must be unique and sorted by best price")
        sides[side] = levels
    if not sides["bids"] or not sides["asks"]:
        return {"status": "insufficient_two_sided_book", "observed_at": observed.isoformat(),
                "age_seconds": age, "midprice_usd": None, "spread_bps": None,
                "buy_sweeps": [], "sell_sweeps": []}
    bid, ask = sides["bids"][0][0], sides["asks"][0][0]
    if ask < bid:
        raise ValueError("Order book is crossed")
    midpoint = bid / 2 + ask / 2
    fresh = age <= max_age
    return {"status": "fresh" if fresh else "stale", "observed_at": observed.isoformat(),
            "as_of": as_of.isoformat(),
            "time_basis": "local_response_receipt",
            "age_seconds": age, "midprice_usd": midpoint, "spread_bps": (ask - bid) / midpoint * 10000,
            "bid_price_usd": bid, "ask_price_usd": ask, "quantity_unit": QUANTITY_UNIT,
            "buy_sweeps": [_sweep(sides["asks"], size, midpoint) for size in (1, 10, 100)] if fresh else [],
            "sell_sweeps": [_sweep(sides["bids"], size, midpoint) for size in (1, 10, 100)] if fresh else [],
            "fill_model": "static_observed_book_only", "fees_usd": None}


def _quote(quote, source, as_of, max_age):
    if quote is None:
        return {"status": "unavailable", "spread_bps": None}
    if not isinstance(quote, dict):
        raise ValueError("qqq_quotes must be a single timestamped bid/ask observation")
    for key, expected in (("symbol", "QQQ"), ("currency", "USD"),
                          ("provider", source["provider"]), ("feed", source["feed"])):
        if quote.get(key) != expected:
            raise ValueError(f"qqq_quotes.{key} does not match the QQQ source")
    observed = _time(quote.get("observed_at"), "qqq_quotes.observed_at")
    received = _time(quote.get("received_at"), "qqq_quotes.received_at")
    if observed > received:
        raise ValueError("QQQ quote observed_at cannot follow received_at")
    bid, ask = _number(quote.get("bid"), "qqq_quotes.bid"), _number(quote.get("ask"), "qqq_quotes.ask")
    if ask < bid:
        raise ValueError("QQQ quote is crossed")
    age = (as_of - observed).total_seconds()
    status = "after_comparison" if received > as_of else "fresh" if 0 <= age <= max_age else "stale"
    return {"status": status, "observed_at": observed.isoformat(), "received_at": received.isoformat(),
            "age_seconds": age, "bid_price_usd": bid, "ask_price_usd": ask,
            "spread_bps": (ask - bid) / (ask / 2 + bid / 2) * 10000 if status == "fresh" else None}


def compare_market_data(qqq_bars, token_snapshot, *, qqq_source, qqq_quotes=None,
                        max_quote_age_seconds=30):
    """Join completed same-UTC intervals during US regular sessions; never fill gaps.

    Requires Dwight's calendar/data extra. Synthetic test fixtures may patch the
    calendar helper, but real reports use its holiday/DST/early-close schedule.
    The optional QQQ quote is one provider/feed-matched observed_at/received_at
    bid/ask record. Historical closes are never substituted for quotes.
    """
    max_age = _number(max_quote_age_seconds, "max_quote_age_seconds")
    source, qqq_collected = _source(qqq_source)
    instrument, token_collected = _token(token_snapshot)
    qqq = _bars(qqq_bars, "qqq_bars")
    token = _bars(token_snapshot.get("bars"), "token_snapshot.bars")
    as_of = min(qqq_collected, token_collected)
    all_stamps = [*qqq, *token]
    sessions = {}
    if all_stamps:
        first = min(all_stamps).astimezone(NY).date()
        last = max(all_stamps).astimezone(NY).date()
        sessions = {value.date: value for value in exchange_sessions(first, last)}
    qqq_complete = {t: b for t, b in qqq.items() if t + INTERVAL <= as_of}
    token_complete = {t: b for t, b in token.items() if t + INTERVAL <= as_of}
    qqq_rth = {t: b for t, b in qqq_complete.items() if _session(t, sessions)}
    token_rth = {t: b for t, b in token_complete.items() if _session(t, sessions)}
    shared = sorted(qqq_rth.keys() & token_rth.keys())
    qv, tv = _vwap(qqq_rth, sessions), _vwap(token_rth, sessions)
    series = []
    for stamp in shared:
        left, right = qqq_rth[stamp], token_rth[stamp]
        delta = (right["close"] / left["close"] - 1) * 10000
        series.append({"timestamp": stamp.isoformat(), "bar_end": (stamp + INTERVAL).isoformat(),
                       "qqq_close": left["close"], "qqqx_close": right["close"], "divergence_bps": delta,
                       "qqq_volume": left["volume"], "qqqx_volume": right["volume"],
                       "qqq_had_trades": left["volume"] > 0, "token_had_trades": right["volume"] > 0,
                       "qqq_vwap": qv[stamp][0], "qqqx_vwap": tv[stamp][0],
                       "qqq_vwap_prefix_complete": qv[stamp][1], "qqqx_vwap_prefix_complete": tv[stamp][1]})
    deltas = [row["divergence_bps"] for row in series]
    absolute = [abs(value) for value in deltas]
    active_deltas = [row["divergence_bps"] for row in series
                     if row["qqq_had_trades"] and row["token_had_trades"]]
    active_absolute = [abs(value) for value in active_deltas]
    synthetic = "synthetic" in (source["data_kind"], token_snapshot["data_kind"])
    result = {"schema_version": 1, "research_only": True, "execution_enabled": False,
              "data_kind": "synthetic" if synthetic else "observed_market_comparison",
              "status": "overlap" if shared else "no_overlap", "as_of": as_of.isoformat(),
              "sources": {"qqq": source, "qqqx": {"instrument": instrument,
                  "data_kind": token_snapshot["data_kind"], "collected_at": token_collected.isoformat()}},
              "coverage": {"qqq_input_bars": len(qqq), "token_input_bars": len(token),
                  "qqq_complete_regular_bars": len(qqq_rth), "token_complete_regular_bars": len(token_rth),
                  "common_bar_count": len(shared), "missing_token_for_qqq": len(qqq_rth) - len(shared),
                  "common_active_bar_count": len(active_deltas),
                  "common_zero_volume_token_bars": sum(not row["token_had_trades"] for row in series),
                  "common_zero_volume_qqq_bars": sum(not row["qqq_had_trades"] for row in series),
                  "missing_qqq_for_token_regular": len(token_rth) - len(shared),
                  "token_bars_outside_qqq_availability": len(token_complete) - len(shared),
                  "token_bars_outside_regular_sessions": len(token_complete) - len(token_rth),
                  "qqq_bars_outside_regular_sessions": len(qqq_complete) - len(qqq_rth),
                  "qqq_incomplete_or_after_cutoff_bars": len(qqq) - len(qqq_complete),
                  "token_incomplete_or_after_cutoff_bars": len(token) - len(token_complete),
                  "qqq_covered_fraction": len(shared) / len(qqq_rth) if qqq_rth else None,
                  "token_regular_covered_fraction": len(shared) / len(token_rth) if token_rth else None},
              "divergence": {"kind": "indicative_unadjusted_price_difference",
                  "formula": "(QQQx close / QQQ close - 1) * 10000", "unit": "basis_points",
                  "economic_conversion": "unverified", "mean_signed_bps": mean(deltas) if deltas else None,
                  "all_reported_close_bar_count": len(deltas),
                  "active_bar_mean_signed_bps": mean(active_deltas) if active_deltas else None,
                  "active_bar_mean_absolute_bps": mean(active_absolute) if active_absolute else None,
                  "active_bar_max_absolute_bps": max(active_absolute) if active_absolute else None,
                  "median_signed_bps": median(deltas) if deltas else None,
                  "mean_absolute_bps": mean(absolute) if absolute else None,
                  "max_absolute_bps": max(absolute) if absolute else None,
                  "min_signed_bps": min(deltas) if deltas else None,
                  "max_signed_bps": max(deltas) if deltas else None},
              "aligned_series": series,
              "token_series": [{"timestamp": t.isoformat(), "bar_end": (t + INTERVAL).isoformat(),
                  "close": b["close"], "volume": b["volume"], "reference_available": t in shared}
                  for t, b in token_complete.items()],
              "orderbook": _book(token_snapshot.get("orderbook"), token_collected, max_age),
              "qqq_quote": _quote(qqq_quotes, source, token_collected, max_age),
              "limitations": [
                  "QQQ and QQQx are different instruments; price differences do not establish arbitrage or profit.",
                  "QQQx economic conversion and corporate-action/rebase multiplier are unverified; Kraken lot_multiplier is not that multiplier.",
                  "Only completed five-minute starts in regular US equity sessions are joined. No forward filling or weekend reference prices.",
                  "Volumes are each source's own reported units; token volume is not consolidated underlying QQQ volume.",
                  "Zero-volume interval closes may be a prior trade carry or provider mark, not a contemporaneous quote. All-close metrics include them; active-bar metrics require both volumes positive.",
                  "had_trades flags mean positive reported bar volume, not separately verified trades or synchronous prices.",
                  "VWAP is a typical-price bar approximation over observed regular-session prefixes. Missing prefixes are marked; zero volume gives unknown VWAP.",
                  "Static book sweeps use QQQx base quantities, not verified share equivalents. Depth can change before execution; no actual fills are known.",
                  "A sampled book cannot reconstruct historical fills. Fees, settlement, latency and eligibility are unverified.",
                  "Order-book freshness measures local response receipt age; the provider supplies no atomic snapshot event timestamp.",
                  "Historical closes are not executable QQQ quotes; absent bid/ask evidence remains unknown.",
              ]}
    if synthetic:
        result["limitations"].insert(0, "SYNTHETIC FIXTURE: invented data, not observed market performance.")
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, OverflowError, TypeError):
        raise ValueError("Comparison output exceeds strict JSON-safe finite range") from None
    return result
