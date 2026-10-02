import argparse
import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from .engine import Bar, Bot, Config


def snapshot(bars, trade, path):
    selected = [b for b in bars if b.timestamp <= datetime.fromisoformat(trade['exit_time'])][-50:]
    lo = min([b.low for b in selected] + [trade['stop'], trade['target']])
    hi = max([b.high for b in selected] + [trade['stop'], trade['target']])
    def y(value):
        return 350 - (value-lo)/max(hi-lo, .01)*300
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="900" height="410" viewBox="0 0 900 410">',
             '<rect width="900" height="410" fill="#101827"/>',
             '<text x="20" y="24" fill="white" font-family="sans-serif">VWAP pullback — trade context (5-minute candles)</text>']
    for i, b in enumerate(selected):
        x = 25+i*14
        color = '#3dd9ab' if b.close >= b.open else '#fb7185'
        parts.append(f'<path d="M{x} {y(b.high)} V{y(b.low)}" stroke="{color}"/>')
        parts.append(f'<rect x="{x-4}" y="{min(y(b.open),y(b.close))}" width="8" height="{max(1,abs(y(b.open)-y(b.close)))}" fill="{color}"/>')
    for key, color in [('entry', '#60a5fa'), ('stop', '#fb7185'), ('target', '#3dd9ab')]:
        parts.append(f'<path d="M20 {y(trade[key])} H740" stroke="{color}" stroke-dasharray="5 4"/><text x="745" y="{y(trade[key])+4}" fill="{color}" font-family="sans-serif">{key} {trade[key]:.2f}</text>')
    parts.append(f'<text x="20" y="390" fill="white" font-family="sans-serif">{trade["entry_time"]} | net {trade["net_r"]:.2f}R | {trade["exit_reason"]}</text></svg>')
    path.write_text('\n'.join(parts))


def main():
    parser = argparse.ArgumentParser(description='VWAP bot: historical replay / simulated paper fills only')
    parser.add_argument('data', type=Path, help='CSV: timestamp,open,high,low,close,volume')
    parser.add_argument('--symbol', required=True, help='One instrument per input file (e.g. QQQ)')
    parser.add_argument('--config', type=Path, help='JSON configuration overrides')
    parser.add_argument('--output', type=Path, default=Path('runs/latest'))
    args = parser.parse_args()
    try:
        config = Config(**(json.loads(args.config.read_text()) if args.config else {}))
        bot = Bot(config)
        bars = []
        with args.data.open(newline='') as f:
            for line, row in enumerate(csv.DictReader(f), 2):
                try:
                    bar = Bar(datetime.fromisoformat(row['timestamp']), *(float(row[k]) for k in ('open', 'high', 'low', 'close', 'volume')))
                    bot.feed(bar)
                    bars.append(bar)
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError(f'CSV line {line}: {exc}') from exc
        if not bars:
            raise ValueError('Input contains no bars')
        bot.finish()
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    args.output.mkdir(parents=True, exist_ok=True)
    for i, trade in enumerate(bot.trades, 1):
        trade['symbol'] = args.symbol
        trade['chart'] = f'trade-{i:04d}.svg'
        snapshot(bars, trade, args.output/trade['chart'])
    fields = list(bot.trades[0]) if bot.trades else ['symbol', 'entry_time', 'entry', 'stop', 'target', 'net_r', 'chart']
    with (args.output/'trades.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(bot.trades)
    with (args.output/'equity.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['timestamp', 'realized_equity'])
        writer.writeheader()
        writer.writerows(bot.curve)
    report = dict(symbol=args.symbol, mode='replay-paper', config=asdict(config), **bot.stats())
    (args.output/'summary.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(bot.stats(), indent=2))
    print(f'Reports: {args.output.resolve()}')


if __name__ == '__main__':
    main()
