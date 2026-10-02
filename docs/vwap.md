# VWAP + market-structure pullback bot

A Python 3.11+ **historical replay / paper-fill bot**, implementing the supplied strategy for one instrument on 5-minute candles. No dependencies, API keys, broker connection, or real-money orders. This version is a runnable research baseline, not a continuously connected broker paper-trading service.

## Run

From this repository:

```sh
python3 examples/make_demo.py
python3 -m vwap_bot examples/synthetic.csv --symbol SYNTHETIC --output runs/demo
python3 -m unittest discover -s tests -v
```

Use your own data:

```sh
python3 -m vwap_bot path/to/qqq.csv --symbol QQQ --config examples/config.json --output runs/qqq
```

Data must contain one instrument and these columns:

```csv
timestamp,open,high,low,close,volume
2026-09-28T09:30:00-04:00,100,100.20,99.80,100.10,12000
2026-09-28T09:35:00-04:00,100.10,100.30,99.90,100.20,13500
```

Timestamps label candle **start**, include an explicit timezone, and must increase strictly. Feed only completed bars. Input is restricted to weekday US regular-session 09:30–15:55 starts, converted to America/New_York with daylight-saving handling. Missing intraday bars cause an error. Partial first/last sessions are accepted. The caller must supply valid exchange sessions: holiday and early-close calendars are not bundled. ETFs such as QQQ/SPY fit the default share units; raw cash index values have no directly tradable share volume. Futures require appropriate point value, tick, cost, leverage configuration, and separate margin/session modeling before use.

## Exact mechanical interpretation

These definitions replace the discretionary parts of the original description. They are research parameters, not established optimal settings.

1. **Indicators:** session VWAP uses typical price `(high+low+close)/3` weighted by bar volume. EMA(20), ATR (simple mean of the latest 14 true ranges), structure, and setup state reset each session. Warm up for at least 20 bars and two confirmed highs and lows.
2. **Structure:** a swing high/low must be strictly above/below the two candles on each side. It becomes available only after the two right-hand candles close. Long requires the latest two confirmed highs and lows both rising, plus close above VWAP; short is symmetric.
3. **Impulse:** a directional candle with range at least 1.5 ATR closes through the last confirmed swing high/low. This arms a setup for up to six subsequent candles.
4. **Pullback:** a subsequent candle intersects VWAP, optional EMA, or the broken swing level, allowing 0.15 ATR tolerance. The bot tracks the most adverse price after the impulse. A trend change or setup timeout cancels the setup.
5. **Rejection:** after a touch (including on the same candle), a bullish candle closes above the preceding high for long; a bearish candle closes below the preceding low for short. Entry fills at the **following candle's open**, including adverse slippage. No signal-close fills or future pivot information are used.
6. **Stop/target:** stop is one tick beyond the tracked pullback extreme, rounded outward; it never moves. Target is 2 times the entry-to-stop price distance, rounded outward. An entry opening beyond its stop is cancelled.
7. **Sizing:** default planned loss is at most 0.25% of realized equity, including estimated exit slippage and round-trip commissions. Only whole shares/contracts; default notional cap is 1× equity. Configured risk cannot exceed 0.5%. Quantity can be zero. Gaps can cause losses beyond planned risk.
8. **Execution:** one position at a time. Opening gaps are processed first; otherwise when a candle touches both stop and target, the stop wins. Target limit fills receive no favorable gap improvement. Stop/market fills include slippage. Two net losing trades halt entries for the session (not necessarily consecutive losses).
9. **Flat overnight:** positions close at the 15:55 candle close. Truncated sessions flatten at their final supplied close on session transition/end of data; these exits are explicitly labelled. This is a replay boundary convention, not a live execution guarantee.

`Bot.feed(completed_bar)` is the incremental engine API; `finish()` closes at the data boundary. Historical bars must be replayed to reconstruct state after a restart. There is no persistent live service or market-data adapter in this version.

## Costs, journal, and statistics

All money values use the instrument/account quote currency; no EUR/USD conversion is performed. Default commission is 0.005 currency units **per share per side**; slippage is 0.01 **price units per market fill**, and point value is 1. Configure realistic values for your venue.

Each run writes:

- `trades.csv`: side (`direction`: +1 long / −1 short), signal/entry/exit times, entry, stop, target, size, reason, gross/net P&L, fees, planned risk, net R, equity, and chart path.
- `trade-NNNN.svg`: a static candle snapshot with entry, stop, and target lines.
- `equity.csv`: realized balance at each candle; this is not marked-to-market equity.
- `summary.json`: the exact configuration, trade count, win rate, average winning/losing net R, expectancy, P&L, and ending equity.

Net R divides after-cost P&L by the planned, cost-inclusive initial risk. Thus a 2R price target produces slightly less than +2 net R. Expectancy is the mean net R, equivalent to win fraction × average win R − loss fraction × average loss magnitude R (zero outcomes contribute zero). Summary statistics for an empty run are zero, not evidence of performance.

The generated demo is **synthetic random data solely for software verification**. It is not a backtest of a tradable edge. Document 100–200 real-data trades, inspect the chart snapshots and fill assumptions, and evaluate a separate out-of-sample period before considering deployment. Positive historical expectancy alone cannot establish future profitability. Short borrow, liquidity/partial fills, exchange calendars, futures margin, and live broker behavior are not simulated.

## Four-week learning workflow

1. Mark real charts and compare confirmed structure with the bot's mechanical rules.
2. Replay historical candles with realistic costs and inspect every trade.
3. Continue collecting new completed bars for paper replay, keeping parameters fixed.
4. Review 100–200 trades, net expectancy, sample uncertainty, and out-of-sample results. Keep research/trading capital separate from long-term investments.
