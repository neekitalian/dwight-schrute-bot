"""Bounded, GET-only Polymarket snapshot recording. No orders or wallets."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import uuid


def get_json(url):
    request=Request(url,headers={'Accept':'application/json','User-Agent':'dwight-research/0.2'})
    with urlopen(request,timeout=20) as response:
        raw=response.read(5_000_001)
    if len(raw)>5_000_000:
        raise ValueError('response too large')
    return json.loads(raw)


def record_books(output, limit=5):
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError('limit must be 1..100')
    run_dir=Path(output)/uuid.uuid4().hex
    run_dir.mkdir(parents=True,exist_ok=False)
    markets=get_json('https://gamma-api.polymarket.com/markets?'+urlencode(
        {'limit':limit,'active':'true','closed':'false'}))
    if (not isinstance(markets,list) or len(markets)>limit
            or any(not isinstance(market,dict) for market in markets)):
        raise ValueError('unexpected or oversized discovery response')
    (run_dir/'markets.json').write_text(json.dumps(markets,indent=2,allow_nan=False)+'\n')
    results=[]
    with (run_dir/'books.jsonl').open('x') as stream:
        for market in markets:
            if market.get('closed') or market.get('enableOrderBook') is False: continue
            tokens=market.get('clobTokenIds',[])
            if isinstance(tokens,str): tokens=json.loads(tokens)
            if not isinstance(tokens,list) or len(tokens)>20: raise ValueError('invalid token list')
            for token in tokens:
                if not isinstance(token,str) or not token.isdigit(): raise ValueError('invalid token ID')
                row={'observed_at':datetime.now(timezone.utc).isoformat(),
                     'market_id':market.get('id'),'condition_id':market.get('conditionId'),'token_id':token}
                try:
                    book=get_json('https://clob.polymarket.com/book?'+urlencode({'token_id':token}))
                    if not isinstance(book,dict) or not isinstance(book.get('bids'),list) or not isinstance(book.get('asks'),list):
                        raise ValueError('invalid book payload')
                    if str(book.get('asset_id')) != token: raise ValueError('book token mismatch')
                    row.update(status='recorded',book=book)
                except HTTPError as exc:
                    row.update(status='http_error',code=exc.code)
                except (OSError,ValueError) as exc:
                    row.update(status='error',error_type=type(exc).__name__)
                # Availability is after the HTTP response, not request start.
                row['received_at']=datetime.now(timezone.utc).isoformat()
                stream.write(json.dumps(row,allow_nan=False)+'\n')
                results.append(row['status'])
    manifest={'mode':'public_snapshot_recording','recorded':results.count('recorded'),
              'errors':len(results)-results.count('recorded'), 'directory':str(run_dir),
              'not_a_tick_archive':True,
              'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in run_dir.iterdir()}}
    (run_dir/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest
