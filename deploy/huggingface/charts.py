"""Interactive charts of the dashboard's saved synthetic evidence only."""
from datetime import datetime
import math

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from dwight.experiments import NY
from .analytics import VARIANT_LABELS


BACKGROUND = "#101318"
PANEL = "#151a23"
TEXT = "#dce3ee"
MUTED = "#8d9aad"
GRID = "#28303d"
MODEL = "#27d9b0"
BASELINE = "#6699ff"
VOLUME = "#ecb75f"
LOSS = "#f1667a"
COLORS = {"baseline": BASELINE, "simple_volume": VOLUME, "filtered": MODEL}
CHART_LABELS = {"baseline": "VWAP", "simple_volume": "Volume filter", "filtered": "Dwight"}
FEATURE_LABELS = {
    "direction": "Direction", "vwap_distance_atr": "VWAP distance",
    "atr_fraction": "ATR / price", "body_atr": "Candle body",
    "relative_volume": "Relative volume", "ema_distance_atr": "EMA distance",
    "session_fraction": "Session time", "signal_stop_distance_atr": "Stop distance",
    "high_structure_atr": "High structure", "low_structure_atr": "Low structure",
}


def _clock(timestamp: str) -> datetime:
    # Plotly receives local wall times with an explicit New York axis label.
    # This prevents the browser's locale from changing which session is shown.
    return datetime.fromisoformat(timestamp).astimezone(NY).replace(tzinfo=None)


def _layout(figure: go.Figure, title: str, height=590) -> go.Figure:
    top, bottom = 120, 60
    plot_height = height - top - bottom
    figure.update_layout(
        title=dict(text=title, font=dict(size=17), x=.02, xanchor="left",
                   y=1 - 16 / height, yanchor="top"),
        paper_bgcolor=BACKGROUND, plot_bgcolor=PANEL,
        font=dict(family="Inter, Arial, sans-serif", color=TEXT, size=12),
        margin=dict(l=60, r=20, t=top, b=bottom),
        height=height,
        hovermode="x unified",
        hoverlabel=dict(bgcolor=PANEL, bordercolor=GRID, font_color=TEXT),
        # Title, legend and subplot headings have distinct vertical bands.
        # Compact policy labels fit a narrow viewport without crossing a chart.
        legend=dict(orientation="h", x=0, xanchor="left", y=1 + 64 / plot_height,
                    yanchor="top", bgcolor="rgba(0,0,0,0)", font_size=10,
                    tracegroupgap=0, itemwidth=30),
        modebar=dict(bgcolor=BACKGROUND, color=MUTED, activecolor=MODEL),
        uirevision=title,
    )
    figure.update_xaxes(gridcolor=GRID, zerolinecolor=GRID, linecolor=GRID)
    figure.update_yaxes(gridcolor=GRID, zerolinecolor=GRID, linecolor=GRID, fixedrange=False)
    figure.update_annotations(font_size=12)
    return figure


def sessions(payload: dict) -> list[str]:
    return sorted({row["session"] for row in payload["test"]["candles"]})


def default_session(payload: dict) -> str:
    available = sessions(payload)
    if not available:
        raise ValueError("This experiment has no test sessions")
    selected = payload["test"].get("default_session")
    return selected if selected in available else available[-1]


def equity_figure(payload: dict) -> go.Figure:
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True,
                           vertical_spacing=.10, row_heights=[.68, .32],
                           subplot_titles=("Bar close equity", "Drawdown from peak"))
    for name, variant in payload["test"]["variants"].items():
        curve = variant["curve"]
        x = [_clock(row["timestamp"]) for row in curve]
        figure.add_trace(go.Scatter(
            x=x, y=[row["equity"] for row in curve],
            name=CHART_LABELS[name], legendgroup=name,
            line=dict(color=COLORS[name], width=2.4 if name == "filtered" else 1.6),
            mode="lines",
            customdata=[[row["realized_equity"]] for row in curve],
            hovertemplate="%{x|%d %b %Y %H:%M}<br>Marked equity $%{y:,.2f}<br>Realized equity $%{customdata[0]:,.2f}<extra>%{fullData.name}</extra>",
        ), row=1, col=1)
        figure.add_trace(go.Scatter(
            x=x, y=[-100 * row["drawdown_fraction"] for row in curve],
            name=CHART_LABELS[name], legendgroup=name, showlegend=False,
            line=dict(color=COLORS[name], width=1.5), mode="lines",
            fill="tozeroy" if name == "filtered" else None,
            fillcolor="rgba(39,217,176,0.09)" if name == "filtered" else None,
            hovertemplate="%{x|%d %b %Y %H:%M}<br>Drawdown %{y:.2f}%<extra>%{fullData.name}</extra>",
        ), row=2, col=1)
    figure.add_hline(y=payload["test"]["initial_capital"], line_dash="dot", line_color=MUTED, row=1, col=1)
    figure.update_yaxes(title_text="USD", tickprefix="$", tickformat=",.0f", row=1, col=1)
    figure.update_yaxes(title_text="Drawdown", ticksuffix="%", row=2, col=1)
    figure.update_xaxes(title_text="New York time", row=2, col=1)
    return _layout(figure, "Synthetic test performance", height=640)


def session_figure(payload: dict, session: str, variant="filtered") -> go.Figure:
    if type(session) is not str or session not in sessions(payload):
        raise ValueError("Select a session from this experiment's test period")
    if type(variant) is not str or variant not in VARIANT_LABELS or variant not in payload["test"]["variants"]:
        raise ValueError("Select an evaluated strategy variant")
    candles = [row for row in payload["test"]["candles"] if row["session"] == session]
    trades = [row for row in payload["test"]["variants"][variant]["trades"]
              if _clock(row["entry_time"]).date().isoformat() == session]
    x = [_clock(row["timestamp"]) for row in candles]
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True,
                           vertical_spacing=.04, row_heights=[.78, .22])
    figure.add_trace(go.Candlestick(
        x=x, open=[row["open"] for row in candles], high=[row["high"] for row in candles],
        low=[row["low"] for row in candles], close=[row["close"] for row in candles],
        increasing_line_color=MODEL, increasing_fillcolor=MODEL,
        decreasing_line_color=LOSS, decreasing_fillcolor=LOSS,
        name="OHLC", whiskerwidth=.35,
    ), row=1, col=1)
    for field, name, color in (("vwap", "VWAP", BASELINE), ("ema20", "EMA20", VOLUME)):
        figure.add_trace(go.Scatter(
            x=x, y=[row[field] for row in candles], name=name,
            line=dict(color=color, width=1.6), mode="lines",
            hovertemplate="%{x|%H:%M} bar<br>$%{y:.4f}<extra>%{fullData.name}</extra>",
        ), row=1, col=1)
    figure.add_trace(go.Bar(
        x=x, y=[row["volume"] for row in candles], name="Generated volume",
        marker_color=["rgba(39,217,176,0.45)" if row["close"] >= row["open"] else "rgba(241,102,122,0.45)" for row in candles],
        hovertemplate="%{x|%H:%M} bar<br>Volume %{y:,.0f}<extra>Generated volume</extra>",
        showlegend=False,
    ), row=2, col=1)
    for direction, name, symbol, color in ((1, "Long entry", "triangle-up", MODEL), (-1, "Short entry", "triangle-down", LOSS)):
        selected = [row for row in trades if row["direction"] == direction]
        if not selected:
            continue
        figure.add_trace(go.Scatter(
            x=[_clock(row["entry_time"]) for row in selected], y=[row["entry"] for row in selected],
            name=name, mode="markers", showlegend=False,
            marker=dict(symbol=symbol, color=color, size=13, line=dict(color=BACKGROUND, width=1.4)),
            customdata=[[row["quantity"], row["stop"], row["target"], row["risk"]] for row in selected],
            hovertemplate="Entry bar %{x|%H:%M}<br>Simulated fill $%{y:.4f}<br>%{customdata[0]} shares<br>Stop $%{customdata[1]:.4f}<br>Target $%{customdata[2]:.4f}<br>Planned risk $%{customdata[3]:.2f}<extra>%{fullData.name}</extra>",
        ), row=1, col=1)
    if trades:
        figure.add_trace(go.Scatter(
            x=[_clock(row["exit_time"]) for row in trades], y=[row["exit"] for row in trades],
            name="Simulated exit", mode="markers", showlegend=False,
            marker=dict(symbol="x", color=[MODEL if row["net_pnl"] > 0 else LOSS for row in trades], size=10, line_width=2),
            customdata=[[row["exit_reason"], row["net_pnl"], row["net_r"]] for row in trades],
            hovertemplate="Exit bar %{x|%H:%M}<br>Simulated fill $%{y:.4f}<br>Reason %{customdata[0]}<br>Net result $%{customdata[1]:.2f}<br>Net R %{customdata[2]:.2f}<br>Exact intrabar fill time is unknown<extra>Simulated exit</extra>",
        ), row=1, col=1)
    else:
        figure.add_annotation(text="No simulated trades for this policy in this session", xref="paper", yref="paper",
                              x=.01, y=.99, showarrow=False, font_color=MUTED, bgcolor=PANEL)
    figure.update_xaxes(rangeslider_visible=False)
    figure.update_xaxes(title_text="Bar open · New York time", tickformat="%H:%M", row=2, col=1)
    figure.update_yaxes(title_text="Price (USD)", tickprefix="$", row=1, col=1)
    figure.update_yaxes(title_text="Volume", row=2, col=1)
    return _layout(figure, f"{session} · {CHART_LABELS[variant]}", height=660)


def diagnostics_figure(payload: dict) -> go.Figure:
    classifier = payload["classifier"]
    figure = make_subplots(rows=2, cols=1, vertical_spacing=.23,
                           row_heights=[.43, .57],
                           subplot_titles=("Probability calibration", "Feature score contributions"))
    figure.add_trace(go.Scatter(
        x=[0, 1], y=[0, 1], mode="lines", name="Perfect calibration reference",
        line=dict(color=MUTED, dash="dot", width=1), hoverinfo="skip", showlegend=False,
    ), row=1, col=1)
    bins = [row for row in classifier["calibration"]["calibration_bins"] if row["count"]]
    figure.add_trace(go.Scatter(
        x=[row["mean_probability"] for row in bins], y=[row["observed_win_rate"] for row in bins],
        mode="markers+lines", name="Labeled baseline candidates", line=dict(color=MODEL, width=1.5),
        marker=dict(color=MODEL, size=[8 + 2 * math.sqrt(row["count"]) for row in bins], line=dict(color=PANEL, width=1)),
        customdata=[[row["count"], row["lower"], row["upper"]] for row in bins],
        hovertemplate="Mean probability %{x:.3f}<br>Observed win share %{y:.3f}<br>%{customdata[0]} candidates<br>Probability bin %{customdata[1]:.1f} to %{customdata[2]:.1f}<extra>Test calibration</extra>",
        showlegend=False,
    ), row=1, col=1)
    terms = sorted(classifier["feature_contributions"], key=lambda row: row["mean_abs_contribution"])
    figure.add_trace(go.Bar(
        x=[row["mean_abs_contribution"] for row in terms],
        y=[FEATURE_LABELS[row["feature"]] for row in terms], orientation="h",
        marker_color=MODEL, name="Logistic score contribution", showlegend=False,
        customdata=[[row["coefficient"], row["feature"]] for row in terms],
        hovertemplate="%{customdata[1]}<br>Mean absolute score term %{x:.4f}<br>Fitted coefficient %{customdata[0]:.4f}<br>Descriptive score contribution, not a causal effect<extra>Test candidates</extra>",
    ), row=2, col=1)
    figure.update_xaxes(title_text="Predicted win probability", range=[0, 1], row=1, col=1)
    figure.update_yaxes(title_text="Observed win share", range=[0, 1], row=1, col=1)
    figure.update_xaxes(title_text="Absolute log odds term", rangemode="tozero", row=2, col=1)
    _layout(figure, "Classifier diagnostics", height=760)
    figure.update_layout(hovermode="closest", margin=dict(l=110, r=20, t=90, b=65))
    return figure


def outcomes_figure(payload: dict) -> go.Figure:
    figure = make_subplots(rows=2, cols=1, vertical_spacing=.22,
                           subplot_titles=("Dwight trades after costs", "Net result by session"))
    trades = payload["test"]["variants"]["filtered"]["trades"]
    figure.add_trace(go.Bar(
        x=list(range(1, len(trades) + 1)), y=[row["net_r"] for row in trades],
        marker_color=[MODEL if row["net_pnl"] > 0 else LOSS for row in trades],
        name="Dwight trade R", showlegend=False,
        customdata=[[_clock(row["exit_time"]).strftime("%d %b %Y %H:%M"), row["net_pnl"], row["exit_reason"]] for row in trades],
        hovertemplate="Trade %{x}<br>Net R %{y:.3f}<br>Net result $%{customdata[1]:.2f}<br>Exit bar %{customdata[0]}<br>%{customdata[2]}<extra>Dwight classifier</extra>",
    ), row=1, col=1)
    days = sessions(payload)
    for name, variant in payload["test"]["variants"].items():
        totals = dict.fromkeys(days, 0.0)
        for trade in variant["trades"]:
            totals[_clock(trade["exit_time"]).date().isoformat()] += trade["net_pnl"]
        figure.add_trace(go.Bar(
            x=days, y=[totals[day] for day in days],
            marker_color=COLORS[name], name=CHART_LABELS[name],
            hovertemplate="%{x|%d %b %Y}<br>Realized net result $%{y:.2f}<extra>%{fullData.name}</extra>",
        ), row=2, col=1)
    figure.update_xaxes(title_text="Executed trade number", dtick=1 if len(trades) < 15 else None, row=1, col=1)
    figure.update_xaxes(title_text="New York session date", row=2, col=1)
    figure.update_yaxes(title_text="Net R", row=1, col=1)
    figure.update_yaxes(title_text="USD", tickprefix="$", row=2, col=1)
    _layout(figure, "Trade contributions", height=640)
    figure.update_layout(barmode="group", bargap=.15, hovermode="closest")
    return figure
