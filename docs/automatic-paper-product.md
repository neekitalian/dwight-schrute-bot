# Automatic paper trading product

Status: product specification. The continuous broker execution worker, authenticated control console and automatic broker-fill reports described below are not implemented as an integrated product. This document does not enable trading or change an account selection.

## Product choice

Build a private web console backed by a persistent Python service. Start with one deployment, one operator and one dedicated API paper account. Keep the Python core open source and package installation for the operator. Customers of a later hosted edition use the console without installing Python.

| Surface | Purpose | First version |
| --- | --- | --- |
| Private Dwight console | Account setup, strategy settings, session controls and results | Main product interface |
| Python worker | Observe bars, evaluate the strategy, enforce risk, manage orders and reconcile fills | Runs on an always-on server |
| Broker API | Authoritative orders, fills, positions and account equity | Proposed: Alpaca Paper, subject to account selection |
| TradingView | Optional chart display; optional later alert input | Not required to keep the worker running |
| Hugging Face | Public demonstration and research comparisons | No customer account credentials or order controls |
| Developer API | Integration with other products | Later, after the worker lifecycle is verified |

A browser extension is not an execution dependency. Closing the browser must not stop supervision of existing orders. Do not automate clicks in TradingView's native paper account as a substitute for a broker API.

Alpaca documents a paper API with separate credentials and the endpoint `https://paper-api.alpaca.markets`. TradingView documents that Pine strategies cannot place orders in its built-in paper account or directly through the Trading Panel. An external broker API is therefore the proposed execution route; adding Python after a TradingView alert does not remove the native account limitation. [Alpaca paper API](https://docs.alpaca.markets/us/docs/paper-trading), [TradingView strategy limitations](https://www.tradingview.com/pine-script-docs/faq/strategies/#can-i-connect-my-strategies-to-my-paper-trading-account)

## Customer flow

1. **Connect**: choose an API paper account in the private installation. Verify the exact account, permissions and requested market-data feed. Show the paper label and masked account identity persistently.
2. **Choose**: select QQQ VWAP on completed five-minute bars. The first version uses the deterministic baseline. A learned filter stays unavailable until separately evaluated and qualified.
3. **Set limits**: choose the permitted allocation, maximum position, per-trade planned risk, daily loss limit and trading session. Validate against account state. Defaults are not approval for a customer's account.
4. **Start paper trading**: show a concise preflight summary of account, strategy version, data feed and limits. Start only after the user activates that exact configuration and preflight passes.
5. **Review**: watch actual broker fills, current positions, equity, costs and skipped-trade reasons. Reports are generated from reconciled broker evidence without manual CSV conversion.

Keep the console small: **Overview**, **Strategy**, **Activity**, **Settings**. Overview shows account mode, worker/data health, positions, return and drawdown when supported by valid evidence. Activity explains each accepted or skipped setup and its broker outcome. Research and model training remain a separate workflow.

Use two distinct controls: **Pause new entries** leaves supervision and protective exits running; **Close Dwight positions** is a separate explicit action that cancels only owned conflicting orders, waits for confirmed cancellation and closes the reconciled remaining quantity. Never describe a paused worker as a flat account. An exit request is not a confirmed exit fill.

## First execution scope

The proposed first worker supports only QQQ, whole-share long entries, one strategy and one dedicated paper account. No leverage expansion, pyramiding, unrelated positions, account resets or real-money endpoint is part of this version. The user must explicitly select the execution account; existing market-data credentials alone do not authorize order submission.

Use the same declared feed, bar construction and causal strategy version in research and forward evaluation. Keep model inference optional. The first automatic trial tests the VWAP baseline and the order lifecycle, not a claim that a trained model improves returns. TradingView alerts, if added later, enter as untrusted observations or proposals and must pass the same freshness, identity and risk checks.

The worker uses current broker state and executable quotes for sizing and decisions. Historical next-bar-open fills are replay assumptions and must never be recorded as broker fills.

## Existing components and required work

| Component | Available today | Required for automatic operation |
| --- | --- | --- |
| Strategy | Causal baseline replay and completed-bar manual observer | Account-independent candidate interface consumed by a broker worker |
| Broker transport | Paper-only Alpaca client and mock-tested durable entry intents in `dwight/paper.py` | Verified real paper API integration, executable quote ingestion and continuous supervision |
| Risk checks | Symbol, quote, spread, exposure and loss checks on entry | Session-wide enforcement, account-reset detection, expiry and policy identity across restarts |
| Orders | Stable client identifiers, persisted intent and reconciliation | Partial-entry protection, exit ownership, rejected exit recovery and confirmed session-close handling |
| Storage | Private research, manual and paper ledgers | Persisted broker fill identities, positions, equity snapshots and restart recovery |
| Controls | No private control console | Authenticated setup, preflight, start, pause, close and clear status feedback |
| Reports | Replay reports and normalized manual-fill reports | Broker reconciliation, cost accounting, equity coverage and paper-specific milestone evidence |

The current paper library halts new entries when a bracket parent is partially filled. That does not protect the filled shares: bracket exits may not yet be active. Treat the partial-fill path as an implementation blocker. A complete design must account for fills racing with cancellation, verify remaining quantity, avoid overlapping exits and keep emergency handling active when strategy evaluation fails. Inspect `PaperExecutor.reconcile`, `submit` and the broker's current order semantics before extending this module.

## Acceptance before a paper pilot

| Scenario | Required outcome |
| --- | --- |
| Missing credentials, wrong endpoint or changed account | No order request; clear setup failure |
| Repeated signal or webhook | One durable intent; no duplicate entry |
| Submission times out after acceptance | Lookup and reconcile by client order identifier; never blindly resubmit |
| Worker restarts with an open order or position | Recover account and ledger state before accepting a new candidate |
| Entry becomes stale | Cancel owned unfilled remainder; reconcile possible concurrent fills |
| Partial entry or rejected protective exit | Protect or close verified exposure under an explicit tested policy; no unattended unprotected partial position |
| Data or model failure | Block new entries; continue independent order and exit supervision |
| Broker connectivity failure | Preserve unknown state, stop entries and alert; do not claim that positions were closed |
| Daily loss threshold | Block entries and apply the chosen exit policy; never promise a guaranteed maximum loss |
| Session end or early close | Use the exchange calendar; cancel owned entries and confirm exits, or report unresolved exposure |
| Unknown account order or position | Halt and report the conflict; do not cancel unrelated orders |
| Pause requested | No new entries; existing exposure remains supervised |
| Flat requested | Report success only after broker positions and relevant open orders reconcile to flat |
| Report generated | Separate simulated broker PnL from historical replay; identify missing fees or equity evidence |

Exercise these cases with deterministic broker fixtures and fault injection first. Then verify the same paths against the explicitly selected paper account. Passing mocked tests does not establish broker readiness. Preserve the existing native TradingView and shadow campaigns; create a distinct broker-paper campaign only after the execution account and start criteria are verified.

## Delivery order

1. Complete and test the paper worker lifecycle using the existing baseline and broker library.
2. Verify a private single-operator paper pilot and broker-derived reports.
3. Build the small control console on that verified worker, including authentication and credential storage before remote exposure.
4. Package reproducible installation, upgrades, backups and health monitoring for a self-hosted edition.
5. Add a hosted edition with customer isolation, account authorization and independent execution state per customer.
6. Add optional TradingView signal inputs and qualified model versions after the first route works end to end.

For a later hosted edition, Alpaca's OAuth flow can let a user authorize a particular paper account. Application registration, redirect validation, anti-forgery state, backend token storage and any provider approval are additional work. Request paper authorization explicitly and only the scopes used by the product. Do not present OAuth as currently implemented or approved. [Alpaca OAuth documentation](https://docs.alpaca.markets/us/docs/using-oauth2-and-trading-api)

Product descriptions must distinguish a research preview, a verified automatic paper service and any future real-money offering. This specification does not activate any account. The open-source core and market-data rights remain separate.