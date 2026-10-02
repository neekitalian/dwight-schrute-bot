import unittest
from datetime import datetime, timedelta
from vwap_bot.engine import Bar, Bot, Config

START = datetime.fromisoformat('2026-09-28T09:30:00-04:00')

def bar(i, o=100, h=101, l=99, c=100, volume=1000):
    return Bar(START+timedelta(minutes=5*i), o, h, l, c, volume)

def pending(direction=1):
    return dict(direction=direction, stop=99 if direction == 1 else 101,
                signal_time=START.isoformat(), reason='test')

class EngineTests(unittest.TestCase):
    def test_cost_inclusive_risk_and_stop_first(self):
        b = Bot()
        b.feed(bar(0))
        b.pending = pending()
        b.feed(bar(1, h=104, l=98))
        t = b.trades[0]
        self.assertEqual(t['exit_reason'], 'stop')
        self.assertLessEqual(t['risk'], 25)
        self.assertAlmostEqual(t['net_r'], -1)

    def test_short_target(self):
        b = Bot()
        b.feed(bar(0))
        b.pending = pending(-1)
        b._enter(bar(0))
        b.feed(bar(1, h=100.5, l=96, c=97))
        self.assertEqual(b.trades[0]['exit_reason'], 'target')
        self.assertGreater(b.trades[0]['net_r'], 1.9)

    def test_gap_stop_can_exceed_risk(self):
        b = Bot()
        b.feed(bar(0))
        b.pending = pending()
        b._enter(bar(0))
        b.feed(bar(1, o=97, h=98, l=96, c=97))
        self.assertLess(b.trades[0]['net_r'], -1)

    def test_two_losses_disable_entry(self):
        b = Bot()
        b.feed(bar(0))
        for i in range(1,4):
            b.pending = pending()
            b.feed(bar(i, l=98))
        self.assertEqual(len(b.trades), 2)
        self.assertIsNone(b.position)

    def test_new_session_resets_losses(self):
        b = Bot()
        b.feed(bar(0))
        b.losses = 2
        b.feed(Bar(START+timedelta(days=1),100,101,99,100,1000))
        self.assertEqual(b.losses, 0)

    def test_pivot_requires_right_bars(self):
        b = Bot(Config(swing=2))
        for i, h in enumerate([101,102,105,103]):
            b.feed(bar(i,h=h))
        self.assertEqual(b.highs, [])
        b.feed(bar(4,h=102))
        self.assertEqual(b.highs, [(2,105)])

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            Config(risk_fraction=.02)
        with self.assertRaises(ValueError):
            bar(0, volume=float('nan'))
        b = Bot()
        b.feed(bar(0))
        with self.assertRaises(ValueError):
            b.feed(bar(2))
        with self.assertRaises(ValueError):
            b.feed(bar(0))

    def test_session_close_flattens(self):
        b = Bot()
        b.feed(bar(0))
        b.pending = pending()
        b._enter(bar(77))
        b.last_timestamp = bar(76).timestamp
        b.feed(bar(77, h=100.5,l=99.5))
        self.assertIsNone(b.position)
        self.assertEqual(b.trades[0]['exit_reason'], 'session_end')

    def test_zero_volume_no_signal(self):
        b = Bot()
        for i in range(30):
            b.feed(bar(i, volume=0))
        self.assertIsNone(b.pending)

    def test_rejection_schedules_next_open(self):
        b = Bot(Config(ema_period=2,atr_period=2))
        b.feed(bar(0,h=100.2,l=99.8))
        b.feed(bar(1,h=100.2,l=99.8))
        b.highs = [(0,99),(1,100)]
        b.lows = [(0,97),(1,98)]
        b.setup = dict(direction=1,index=1,extreme=101,level=100,touched=False)
        b.feed(bar(2,o=100,h=101,l=99.9,c=100.9))
        self.assertIsNotNone(b.pending)
        self.assertIsNone(b.position)
        b.feed(bar(3,o=101,h=101.2,l=100.5,c=101))
        self.assertAlmostEqual(b.position['entry'],101.01)

    def test_short_rejection_next_open(self):
        b = Bot(Config(ema_period=2,atr_period=2))
        b.feed(bar(0,h=100.2,l=99.8))
        b.feed(bar(1,h=100.2,l=99.8))
        b.highs = [(0,103),(1,102)]
        b.lows = [(0,101),(1,100)]
        b.setup = dict(direction=-1,index=1,extreme=99,level=100,touched=False)
        b.feed(bar(2,o=100,h=100.1,l=99,c=99.1))
        self.assertIsNotNone(b.pending)
        self.assertIsNone(b.position)
        b.feed(bar(3,o=99,h=99.5,l=98.8,c=99))
        self.assertAlmostEqual(b.position['entry'],98.99)

    def test_future_bars_do_not_change_closed_trades(self):
        import copy
        b = Bot()
        b.feed(bar(0))
        b.pending = pending()
        b.feed(bar(1,l=98))
        before = copy.deepcopy(b.trades)
        for i in range(2,20):
            b.feed(bar(i))
        self.assertEqual(b.trades, before)

if __name__ == '__main__':
    unittest.main()
