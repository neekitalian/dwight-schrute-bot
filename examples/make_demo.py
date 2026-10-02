"""Synthetic random candles for software smoke testing, never strategy evidence."""
import csv
import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

rng = random.Random(42)
path = Path(__file__).with_name('synthetic.csv')
with path.open('w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(['timestamp','open','high','low','close','volume'])
    day = datetime(2026, 8, 3, 9, 30, tzinfo=ZoneInfo('America/New_York'))
    for offset in range(30):
        start = day+timedelta(days=offset)
        if start.weekday() >= 5:
            continue
        price = 100
        for i in range(78):
            close = price+rng.gauss(.025, .3)
            high = max(price,close)+rng.uniform(.02,.2)
            low = min(price,close)-rng.uniform(.02,.2)
            writer.writerow([(start+timedelta(minutes=5*i)).isoformat(),price,high,low,close,rng.randrange(1000,20000)])
            price = close
print(path)
