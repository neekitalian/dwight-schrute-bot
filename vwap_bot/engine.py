from dataclasses import dataclass, asdict
from datetime import datetime, time
from zoneinfo import ZoneInfo
import math


@dataclass(frozen=True)
class Config:
    capital: float = 10000
    risk_fraction: float = .0025
    reward_r: float = 2
    max_losses: int = 2
    swing: int = 2
    ema_period: int = 20
    atr_period: int = 14
    impulse_atr: float = 1.5
    touch_atr: float = .15
    max_pullback_bars: int = 6
    tick: float = .01
    slippage: float = .01
    commission: float = .005
    point_value: float = 1
    max_leverage: float = 1
    use_ema: bool = True

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not isinstance(value, bool) and not math.isfinite(value):
                raise ValueError(f'{name} must be finite')
        if not 0 < self.risk_fraction <= .005:
            raise ValueError('risk_fraction must be > 0 and <= 0.005')
        for name in ('capital', 'reward_r', 'tick', 'point_value', 'max_leverage', 'impulse_atr'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive')
        for name in ('max_losses', 'swing', 'ema_period', 'atr_period', 'max_pullback_bars'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f'{name} must be a positive integer')
        if min(self.slippage, self.commission, self.touch_atr) < 0:
            raise ValueError('Costs and touch tolerance cannot be negative')


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            raise ValueError('Timestamp must include a timezone')
        if not all(math.isfinite(x) for x in (self.open, self.high, self.low, self.close, self.volume)):
            raise ValueError('OHLCV must be finite')
        if self.low <= 0 or self.volume < 0 or not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError('Invalid OHLCV')


class Bot:
    """Feed completed, start-labelled 5-minute US regular-session bars in order."""
    def __init__(self, config=Config()):
        self.c = config
        self.equity = config.capital
        self.trades = []
        self.position = self.pending = None
        self.last_timestamp = None
        self.day = None
        self.curve = []
        self._reset()

    def _reset(self):
        self.bars = []
        self.highs, self.lows, self.trs = [], [], []
        self.pv = self.vol = 0
        self.ema = None
        self.setup = None
        self.losses = 0

    def feed(self, b):
        local = b.timestamp.astimezone(ZoneInfo('America/New_York'))
        if local.weekday() >= 5 or not time(9, 30) <= local.time() <= time(15, 55):
            raise ValueError('Input must contain regular-session bars only')
        if local.minute % 5 or local.second or local.microsecond:
            raise ValueError('Expected start-labelled 5-minute bars')
        if self.last_timestamp and b.timestamp <= self.last_timestamp:
            raise ValueError('Bars must be strictly increasing')
        if local.date() != self.day:
            if self.position:
                self._exit(self.bars[-1].close, self.bars[-1].timestamp, 'data_session_end')
            self.pending = None
            self._reset()
            self.day = local.date()
        elif (b.timestamp - self.last_timestamp).total_seconds() != 300:
            raise ValueError('Missing intraday bars; refuse ambiguous fills')
        self.last_timestamp = b.timestamp
        if self.pending:
            self._enter(b)
        if self.position:
            p = self.position
            d = p['direction']
            # Opening gaps precede intrabar touches. If both levels touch, stop wins.
            if d * (b.open - p['stop']) <= 0:
                self._exit(b.open, b.timestamp, 'gap_stop')
            elif d * (b.open - p['target']) >= 0:
                self._exit(p['target'], b.timestamp, 'target')
            elif (b.low <= p['stop'] if d == 1 else b.high >= p['stop']):
                self._exit(p['stop'], b.timestamp, 'stop')
            elif (b.high >= p['target'] if d == 1 else b.low <= p['target']):
                self._exit(p['target'], b.timestamp, 'target')
        prev = self.bars[-1] if self.bars else b
        self.trs.append(max(b.high-b.low, abs(b.high-prev.close), abs(b.low-prev.close)))
        self.bars.append(b)
        self.pv += (b.high + b.low + b.close) / 3 * b.volume
        self.vol += b.volume
        self.ema = b.close if self.ema is None else self.ema + 2 / (self.c.ema_period+1) * (b.close-self.ema)
        self._pivots()
        if local.time() == time(15, 55):
            if self.position:
                self._exit(b.close, b.timestamp, 'session_end')
            self.pending = self.setup = None
        elif not self.position and self.losses < self.c.max_losses and self.equity > 0:
            self._signal(b, prev)
        self.curve.append({'timestamp': b.timestamp.isoformat(), 'realized_equity': self.equity})

    def _pivots(self):
        w = self.c.swing
        if len(self.bars) < 2*w+1:
            return
        window = self.bars[-(2*w+1):]
        pivot = window[w]
        others = window[:w] + window[w+1:]
        idx = len(self.bars)-w-1
        if all(pivot.high > x.high for x in others):
            self.highs.append((idx, pivot.high))
        if all(pivot.low < x.low for x in others):
            self.lows.append((idx, pivot.low))

    def _signal(self, b, prev):
        if not self.vol or len(self.trs) < max(self.c.atr_period, self.c.ema_period) or min(len(self.highs), len(self.lows)) < 2:
            return
        vwap = self.pv/self.vol
        atr = sum(self.trs[-self.c.atr_period:])/self.c.atr_period
        h0, h1 = self.highs[-2][1], self.highs[-1][1]
        l0, l1 = self.lows[-2][1], self.lows[-1][1]
        d = 1 if h1 > h0 and l1 > l0 and b.close > vwap else -1 if h1 < h0 and l1 < l0 and b.close < vwap else 0
        i = len(self.bars)-1
        if self.setup and (self.setup['direction'] != d or i-self.setup['index'] > self.c.max_pullback_bars):
            self.setup = None
        if not d:
            return
        if self.setup:
            s = self.setup
            s['extreme'] = min(s['extreme'], b.low) if d == 1 else max(s['extreme'], b.high)
            levels = [vwap, s['level']]
            if self.c.use_ema:
                levels.append(self.ema)
            touched = any(b.low-self.c.touch_atr*atr <= x <= b.high+self.c.touch_atr*atr for x in levels)
            s['touched'] |= touched
            rejection = b.close > b.open and b.close > prev.high if d == 1 else b.close < b.open and b.close < prev.low
            if s['touched'] and rejection:
                stop = s['extreme'] - d*self.c.tick
                stop = (math.floor(stop/self.c.tick) if d == 1 else math.ceil(stop/self.c.tick))*self.c.tick
                self.pending = {'direction': d, 'stop': stop, 'signal_time': b.timestamp.isoformat(),
                                'reason': 'HH/HL above VWAP; impulse, level touch, bullish rejection' if d == 1 else 'LH/LL below VWAP; impulse, level touch, bearish rejection'}
                self.setup = None
            return
        # Impulse is a wide trend candle breaking the last confirmed swing.
        if b.high-b.low >= self.c.impulse_atr*atr and d*(b.close-b.open) > 0 and (b.close > h1 if d == 1 else b.close < l1):
            self.setup = {'direction': d, 'index': i, 'extreme': b.close,
                          'level': h1 if d == 1 else l1, 'touched': False}

    def _enter(self, b):
        p, self.pending = self.pending, None
        c, d = self.c, p['direction']
        entry = b.open + d*c.slippage
        distance = d*(entry-p['stop'])
        if distance <= c.tick or d*(b.open-p['stop']) <= 0 or self.losses >= c.max_losses:
            return
        unit_risk = (distance+c.slippage)*c.point_value + 2*c.commission
        quantity = math.floor(min(self.equity*c.risk_fraction/unit_risk,
                                  self.equity*c.max_leverage/(entry*c.point_value)))
        if quantity < 1:
            return
        target = entry + d*c.reward_r*distance
        target = (math.ceil(target/c.tick) if d == 1 else math.floor(target/c.tick))*c.tick
        self.position = dict(p, entry=entry, target=target, quantity=quantity,
                             entry_time=b.timestamp.isoformat(), risk=quantity*unit_risk)

    def _exit(self, price, timestamp, reason):
        p, self.position = self.position, None
        # Targets are limit fills; stops and session exits suffer adverse slippage.
        fill = price if reason == 'target' else price-p['direction']*self.c.slippage
        gross = p['direction']*(fill-p['entry'])*p['quantity']*self.c.point_value
        fees = 2*self.c.commission*p['quantity']
        pnl = gross-fees
        self.equity += pnl
        self.losses += int(pnl < 0)
        self.trades.append(dict(p, exit=fill, exit_time=timestamp.isoformat(), exit_reason=reason,
                                gross_pnl=gross, fees=fees, net_pnl=pnl, net_r=pnl/p['risk'], equity=self.equity))
        self.setup = None

    def finish(self):
        if self.position:
            self._exit(self.bars[-1].close, self.bars[-1].timestamp, 'end_of_data')
        self.pending = None
        if self.curve:
            self.curve[-1]['realized_equity'] = self.equity

    def stats(self):
        rs = [t['net_r'] for t in self.trades]
        wins, losses = [r for r in rs if r > 0], [r for r in rs if r < 0]
        return {'trades': len(rs), 'win_rate': len(wins)/len(rs) if rs else 0,
                'average_win_r': sum(wins)/len(wins) if wins else 0,
                'average_loss_r': -sum(losses)/len(losses) if losses else 0,
                'expectancy_r': sum(rs)/len(rs) if rs else 0,
                'net_pnl': self.equity-self.c.capital, 'ending_equity': self.equity}
