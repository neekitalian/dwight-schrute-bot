"""Public GET-only discovery. No wallet, credentials, order methods or signing."""
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def discover(limit=10):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    query = urlencode({"limit": limit, "active": "true", "closed": "false"})
    req = Request("https://gamma-api.polymarket.com/markets?"+query,
                  headers={"User-Agent": "dwight-research/0.1", "Accept": "application/json"})
    with urlopen(req, timeout=15) as response:
        raw = response.read(5_000_001)
    if len(raw) > 5_000_000:
        raise ValueError("discovery response exceeds size limit")
    markets = json.loads(raw)
    if not isinstance(markets, list):
        raise ValueError("unexpected Gamma response; review API version")
    return markets
