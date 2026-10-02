"""Private, dependency-free visuals for ManualPaperJournal evidence snapshots.

This renderer does not retrieve account records, place orders, infer missing
fees, send email, or independently authenticate user-supplied paper exports.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib
from html import escape
import json
import os
from pathlib import Path

from .manual import ACCOUNT, PROPOSAL_FIELDS, _accounting, _fill, _proposal, _stamp


def _snapshot(report):
    """Freeze input and verify the accounting used by every displayed metric."""
    try:
        raw = (json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True,
                          allow_nan=False) + "\n").encode("utf-8")
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("manual report must be finite JSON evidence") from exc
    if not isinstance(value, dict):
        raise ValueError("manual report must be an object")
    expected = {"schema_version": 1, "account": ACCOUNT, "mode": "manual_evidence",
                "performance_scope": "imported_fills_only", "currency": "USD"}
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise ValueError("unsupported manual report schema or evidence scope")
    if type(value["schema_version"]) is not int:
        raise ValueError("unsupported manual report schema")
    if value.get("submits_orders") is not False or value.get("broker_verified") is not False:
        raise ValueError("manual report cannot claim order submission or broker verification")
    if any(key not in value or value[key] is not None for key in
           ("account_equity", "account_return_pct", "unrealized_pnl")):
        raise ValueError("manual report account equity, return and unrealized PnL must remain unknown")
    try:
        as_of = _stamp(value["as_of"], "as_of")
        fills, proposals, audits = (value[key] for key in ("fills", "proposals", "fill_audit"))
        if not all(isinstance(rows, list) for rows in (fills, proposals, audits)):
            raise ValueError("manual report collections must be lists")
        if any(_fill(fill) != fill for fill in fills):
            raise ValueError("manual report fills must be canonical journal evidence")
        if fills != sorted(fills, key=lambda row: (row["filled_at"], row["sequence"])):
            raise ValueError("manual report fills must be in execution order")
        if len({row["fill_id"] for row in fills}) != len(fills) or len(
                {(row["filled_at"], row["sequence"]) for row in fills}) != len(fills):
            raise ValueError("manual report execution identities must be unique")
        if fills and fills[-1]["filled_at"] > as_of:
            raise ValueError("manual report cannot precede its imported fills")
        kind = fills[0]["data_kind"] if fills else "no_fills"
        if value["data_kind"] != kind or any(row["data_kind"] != kind for row in fills):
            raise ValueError("manual report evidence labels do not match its fills")
        accounting = _accounting(fills)
        if any(value.get(key) != result for key, result in accounting.items()):
            raise ValueError("manual report accounting does not match imported fills")
        if type(value["closed_exit_count"]) is not int:
            raise ValueError("manual report sell execution count must be an integer")
        expected_status = ("unknown_open_positions" if Decimal(accounting["open_position"]["quantity"]) > 0
                           else "unverified_no_account_snapshot")
        if value["reconciliation_status"] != expected_status:
            raise ValueError("manual report reconciliation status is inconsistent")
        by_proposal = {}
        for proposal in proposals:
            canonical = _proposal({key: proposal[key] for key in PROPOSAL_FIELDS})
            if any(canonical[key] != proposal[key] for key in canonical):
                raise ValueError("manual report proposal is not canonical")
            if proposal["proposal_id"] in by_proposal:
                raise ValueError("manual report proposal identities must be unique")
            if proposal["status"] not in {"pending", "confirmed", "skipped", "expired"}:
                raise ValueError("manual report proposal status is invalid")
            by_proposal[proposal["proposal_id"]] = proposal
        if len(audits) != len(fills):
            raise ValueError("manual report needs one audit record per fill")
        for fill, audit in zip(fills, audits):
            if (audit["fill_id"] != fill["fill_id"] or audit["proposal_id"] != fill["proposal_id"]
                    or (fill["proposal_id"] is not None and fill["proposal_id"] not in by_proposal)):
                raise ValueError("manual report fill audit links are inconsistent")
            if not isinstance(audit["issues"], list) or any(not isinstance(issue, str) for issue in audit["issues"]):
                raise ValueError("manual report audit issues must be a text list")
            for key in ("human_confirmation_at", "confirmation_timing", "proposed_entry_quantity",
                        "linked_entry_quantity_to_date", "entry_quantity_excess", "entry_price_difference"):
                if audit[key] is not None and not isinstance(audit[key], str):
                    raise ValueError("manual report audit values must be text or unknown")
        if not isinstance(value["limitations"], list) or any(not isinstance(row, str) for row in value["limitations"]):
            raise ValueError("manual report limitations must be a text list")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("manual report is missing required journal evidence") from exc
    return value, raw


def _e(value):
    return escape(str(value), quote=True)


def _number(value):
    """Preserve journal precision; add readable grouping without float money."""
    return format(Decimal(value), ",f")


def _money(value):
    return _number(value) + " USD"


def _chart(curve):
    if not curve:
        return ('<div class="empty-chart">No sell executions have been imported. '
                'There is no realized PnL series to plot.</div>')
    values = [Decimal(row["cumulative_realized_pnl"]) for row in curve]
    lower, upper = min(Decimal(0), *values), max(Decimal(0), *values)
    if lower == upper:
        lower, upper = Decimal(-1), Decimal(1)
    padding = (upper - lower) / 10
    lower, upper = lower - padding, upper + padding
    left, top, width, height = 100, 30, 880, 260
    def y(value):
        return top + float((upper - value) / (upper - lower)) * height
    indices = list(range(len(values)))
    sampling = ""
    if len(values) > 1000:
        # Keep both extremes of each consecutive bucket, preserving its order.
        # The scale still uses the complete series, including every loss.
        selected = {0, len(values)-1}
        bucket_size = (len(values) + 499) // 500
        for first in range(0, len(values), bucket_size):
            bucket = range(first, min(first + bucket_size, len(values)))
            selected.add(min(bucket, key=values.__getitem__))
            selected.add(max(bucket, key=values.__getitem__))
        indices = sorted(selected)
        sampling = (f'<p class="small muted">Chart display: {len(indices)} selected points from '
                    f'{len(values)} sell executions, retaining each consecutive bucket\'s minimum and maximum. '
                    'The full series is preserved in report.json.</p>')
    points = [(left + (width * i / (len(values)-1) if len(values) > 1 else width/2), y(values[i]))
              for i in indices]
    result = ['<svg viewBox="0 0 1020 370" role="img" aria-labelledby="curve-title curve-description">',
              '<title id="curve-title">Cumulative net realized PnL from imported sell executions</title>',
              '<desc id="curve-description">USD after allocated entry and exit fees. The horizontal axis '
              'is execution order, not elapsed time. This is not account equity or total return.</desc>']
    for amount in (upper, (upper + lower)/2, lower):
        line_y = y(amount)
        result.append(f'<line class="grid" x1="{left}" x2="{left+width}" y1="{line_y:.2f}" y2="{line_y:.2f}"/>')
        result.append(f'<text class="axis" x="{left-10}" y="{line_y+4:.2f}" text-anchor="end">{_e(format(amount, ",.2f"))}</text>')
    result.append(f'<line class="zero" x1="{left}" x2="{left+width}" y1="{y(Decimal(0)):.2f}" y2="{y(Decimal(0)):.2f}"/>')
    if len(points) > 1:
        coordinates = " ".join(f"{x:.2f},{pos_y:.2f}" for x, pos_y in points)
        result.append(f'<polyline class="pnl-line" points="{coordinates}"/>')
    for (x, pos_y), i in zip(points, indices):
        row = curve[i]
        result.append(f'<circle class="pnl-point" cx="{x:.2f}" cy="{pos_y:.2f}" r="4"><title>'
                      f'{_e(row["fill_id"])} | {_e(row["filled_at"])} | {_e(_money(row["cumulative_realized_pnl"]))}'
                      '</title></circle>')
    result.append(f'<text class="axis" x="{left}" y="323">First exit: {_e(curve[0]["filled_at"])}</text>')
    if len(curve) > 1:
        result.append(f'<text class="axis" x="{left+width}" y="343" text-anchor="end">Last exit: {_e(curve[-1]["filled_at"])}</text>')
    result.append('<text class="axis" x="22" y="20">USD</text></svg>')
    return "".join(result) + sampling


def _table(headers, rows, empty):
    notice = (f'<p class="small muted">Showing the last 250 of {len(rows)} rows. '
              'The complete records are preserved in report.json.</p>') if len(rows) > 250 else ""
    head = "".join(f"<th>{_e(header)}</th>" for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{_e(cell)}</td>" for cell in row) + "</tr>" for row in rows[-250:])
    if not body:
        body = f'<tr><td colspan="{len(headers)}" class="muted">{_e(empty)}</td></tr>'
    return notice + f'<div class="table-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _html(report, digest):
    kind = report["data_kind"]
    labels = {"synthetic": ("SYNTHETIC ACCOUNTING EXAMPLE", "Invented fills. These numbers are not trading performance or a TradingView account statement."),
              "paper_export": ("USER-SUPPLIED PAPER EVIDENCE", "Imported paper execution records. Their authenticity and completeness have not been independently verified."),
              "no_fills": ("NO IMPORTED FILLS", "No execution evidence has been imported. Account results and experiment activity are unknown.")}
    label, explanation = labels[kind]
    has_fills = bool(report["fills"])
    position = report["open_position"]
    facts = [("Net realized PnL", _money(report["net_realized_pnl"]) if has_fills else "Not measured"),
             ("All imported fees", _money(report["fees_imported"]) if has_fills else "Not measured"),
             ("QQQ inventory in journal", _number(position["quantity"]) + " shares" if has_fills else "No imported inventory"),
             ("Remaining FIFO cost", _money(position["cost_including_remaining_entry_fees"]) if has_fills else "Not measured")]
    cards = "".join(f'<div class="card"><span>{_e(name)}</span><strong>{_e(value)}</strong></div>' for name, value in facts)
    fills_table = _table(["Filled at (UTC)", "Sequence", "Fill ID", "Side", "Quantity", "Price (USD)", "Fee (USD)", "Proposal ID"],
                        [[row["filled_at"], row["sequence"], row["fill_id"], row["side"], _number(row["quantity"]),
                          _number(row["price"]), _number(row["fee"]), row["proposal_id"] or "Unlinked"] for row in report["fills"]],
                        "No execution evidence imported.")
    audit_table = _table(["Fill ID", "Proposal ID", "Recorded flags", "Acknowledgment timing", "Acknowledged at", "Linked buy quantity", "Proposed quantity", "Excess quantity", "Entry price difference (USD)"],
                        [[row["fill_id"], row["proposal_id"] or "Unlinked", "; ".join(row["issues"]) or "No listed flags",
                          row["confirmation_timing"] or "Not applicable", row["human_confirmation_at"] or "Not recorded",
                          row["linked_entry_quantity_to_date"] if row["linked_entry_quantity_to_date"] is not None else "Not applicable",
                          row["proposed_entry_quantity"] if row["proposed_entry_quantity"] is not None else "Not applicable",
                          row["entry_quantity_excess"] if row["entry_quantity_excess"] is not None else "Not applicable",
                          row["entry_price_difference"] if row["entry_price_difference"] is not None else "Not applicable"] for row in report["fill_audit"]],
                        "No imported fills to compare with proposals.")
    proposals_table = _table(["Proposal ID", "Status", "Quantity", "Planned entry", "Planned stop", "Planned target", "Strategy source", "Model label", "Version label", "Available", "Expires"],
                            [[row[key] for key in ("proposal_id", "status", "quantity", "entry", "stop", "target", "source", "model", "version", "available_at", "expires_at")]
                             for row in report["proposals"]], "No proposals recorded.")
    lots_table = _table(["Entry fill ID", "Remaining QQQ quantity", "Entry price (USD)", "Remaining entry fee (USD)"],
                       [[row["entry_fill_id"], _number(row["quantity"]), _number(row["entry_price"]), _number(row["remaining_entry_fee"])]
                        for row in position["lots"]], "No remaining lots in the imported history. This does not verify account holdings.")
    limitations = "".join(f"<li>{_e(item)}</li>" for item in report["limitations"])
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'">
<title>Dwight | QQQ manual evidence review</title><style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:#0b1016;color:#e8edf4;font:15px/1.6 system-ui,sans-serif}}main{{max-width:1400px;margin:auto;padding:36px 24px 60px}}h1{{font-size:clamp(28px,4vw,42px);line-height:1.2;margin:10px 0}}h2{{font-size:20px;margin:0 0 12px}}p{{margin:10px 0}}.eyebrow{{font:700 12px/1.4 system-ui;letter-spacing:.16em;color:#72d6ca}}.muted,.note{{color:#a5b3c4}}.badge{{display:inline-block;color:#f8ce88;background:#31291d;border:1px solid #665136;border-radius:6px;padding:5px 10px;font-weight:650;font-size:12px}}.banner{{padding:18px 22px;background:#171f29;border:1px solid #364254;border-radius:10px;margin:24px 0}}.cards{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}}.card,section{{background:#111922;border:1px solid #283443;border-radius:10px}}.card{{padding:18px}}.card span{{display:block;color:#a5b3c4;font-size:13px}}.card strong{{display:block;margin-top:8px;font-size:23px;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}}section{{margin-top:22px;padding:22px}}svg{{width:100%;display:block}}.grid{{stroke:#25303e;stroke-width:1}}.zero{{stroke:#8b98aa;stroke-width:1;stroke-dasharray:5 5}}.axis{{fill:#a5b3c4;font:12px system-ui}}.pnl-line{{fill:none;stroke:#2ac4b3;stroke-width:3;stroke-linejoin:round}}.pnl-point{{fill:#2ac4b3;stroke:#111922;stroke-width:2}}.empty-chart{{padding:65px 20px;text-align:center;color:#a5b3c4;border:1px dashed #364254;border-radius:6px}}.table-scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}}th,td{{text-align:left;padding:12px;border-bottom:1px solid #283443;vertical-align:top}}th{{font-weight:600;color:#a5b3c4;white-space:nowrap}}td{{overflow-wrap:anywhere;min-width:85px}}code{{overflow-wrap:anywhere;color:#72d6ca}}a{{color:#72d6ca}}.small{{font-size:13px}}footer{{margin-top:26px;color:#a5b3c4}}@media(max-width:800px){{.cards{{grid-template-columns:repeat(2,minmax(0,1fr))}}main{{padding:24px 14px}}section{{padding:16px}}}}@media(max-width:440px){{.cards{{grid-template-columns:1fr}}}}@media print{{body{{background:#fff;color:#111}}.card,section,.banner{{background:#fff;color:#111;border-color:#999}}.muted,.note,th,footer{{color:#444}}}}
</style></head><body><main>
<header><div class="eyebrow">DWIGHT / PRIVATE EVIDENCE REVIEW</div><h1>QQQ manual paper journal</h1><p class="muted">Snapshot as of {_e(report['as_of'])}</p></header>
<div class="banner"><span class="badge">{_e(label)}</span><p>{_e(explanation)}</p><p class="small muted">The journal describes Paper Trading by TradingView. This report has no account connection and submits no orders.</p></div>
<div class="cards">{cards}</div>
<section><h2>Cumulative net realized PnL</h2><p class="note">Each point is one imported sell execution, ordered by execution timestamp and sequence. Values include allocated entry and exit fees. This is not an account equity curve. Hover a point to inspect its record.</p>{_chart(report['realized_pnl_curve'])}<p class="small muted">Imported fills: {len(report['fills'])}. Sell executions: {report['closed_exit_count']}. Partial exits are separate executions, not independent strategy trades. No win rate is calculated.</p></section>
<section><h2>What remains unknown</h2><p><strong>Account equity: unknown. Account return: unknown. Unrealized PnL: unknown. Account drawdown: unknown.</strong></p><p class="note">No account balance, cash flows, current quote or complete account history has been verified. A flat journal does not prove a flat account. Imported fees include fees still allocated to open lots, so subtracting all fees again from net realized PnL would double count costs.</p><p class="small muted">Recorded reconciliation status: {_e(report['reconciliation_status'])}.</p></section>
<section><h2>Imported executions</h2><p class="note">These records retain the supplied prices, fees and execution order. No missing cost or fill is estimated.</p>{fills_table}</section>
<section><h2>Proposal comparison</h2><p class="note">Flags describe the journal's comparisons only. No listed flags does not establish strategy or risk-rule compliance. Human acknowledgment is not an order acknowledgment. Confirmation timing is descriptive; an acknowledgment after a fill is not automatically a violation. Planned stops are not enforced by this journal.</p>{audit_table}</section>
<section><h2>Recorded proposals</h2><p class="note">Source, model and version are supplied labels, not verified artifact identities. A confirmed proposal is not proof of a submitted order or fill.</p>{proposals_table}</section>
<section><h2>Remaining FIFO inventory</h2><p class="note">Cost includes remaining allocated entry fees. Neither a market value nor an unrealized gain is inferred.</p>{lots_table}</section>
<section><h2>Evidence and limits</h2><p>Source: one frozen <code>ManualPaperJournal.report()</code> snapshot. Its FIFO accounting was recomputed from the imported executions before rendering. This verifies arithmetic consistency, not the authenticity of an account export.</p><p>Evidence label: <code>{_e(kind)}</code>. Broker verified: <code>false</code>. Performance scope: <code>imported_fills_only</code>.</p><p><a href="report.json" download>Frozen report JSON</a></p><p class="small">SHA256 of the exact saved JSON bytes:<br><code>{digest}</code></p><ul>{limitations}</ul></section>
<footer class="small">Private local report. No remote scripts, uploads or email delivery. Keep this report and its JSON outside public repositories and public Spaces.</footer>
</main></body></html>'''


def render_manual_report(report: dict, output: Path) -> dict:
    """Write a private HTML/JSON snapshot in a new directory.

    Pass a report obtained from ``ManualPaperJournal.report()``. The output's
    parent must already exist. Existing paths and symbolic-link traversal are
    rejected; new directory/file permissions are 0700/0600 on supported POSIX
    hosts. Nothing is uploaded or sent. The returned hash covers report.json's
    exact bytes, and the HTML names the same digest.
    """
    snapshot, raw = _snapshot(report)
    digest = hashlib.sha256(raw).hexdigest()
    html = _html(snapshot, digest).encode("utf-8")
    target = Path(output).expanduser()
    if ".." in target.parts:
        raise ValueError("parent traversal is not allowed in manual report paths")
    target = target.absolute()
    if any(part.is_symlink() for part in (target, *target.parents)):
        raise ValueError("manual report paths must not contain symbolic links")
    if not target.parent.is_dir():
        raise ValueError("manual report parent directory must already exist")
    if target.exists():
        raise ValueError("manual report output already exists; choose a new directory")
    # Pin each ancestor using descriptors and O_NOFOLLOW. A path substitution
    # after validation cannot redirect writes through a symbolic link.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(target.anchor, flags)
    directory_fd = None
    try:
        for component in target.parent.parts[1:]:
            child_fd = os.open(component, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        os.mkdir(target.name, mode=0o700, dir_fd=parent_fd)
        directory_fd = os.open(target.name, flags, dir_fd=parent_fd)
        os.fchmod(directory_fd, 0o700)
        for name, content in (("report.json", raw), ("report.html", html)):
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(parent_fd)
    return {"html": str(target / "report.html"), "json": str(target / "report.json"),
            "report_sha256": digest, "data_kind": snapshot["data_kind"],
            "broker_verified": False, "submits_orders": False, "email_sent": False}
