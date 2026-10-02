# Local validation — 2026-10-03

These checks establish implementation behavior, not investment performance or broker integration readiness. The latest suite and synthetic experiment enforce QQQ-only equity scope; SPY requests, widened release policies and order submissions are rejected.

| Check | Result |
|---|---|
| Package build/install | Editable Dwight 0.2.0 installed successfully on Python 3.11 |
| Full local unit suite | 85 tests passed |
| Synthetic experiment CLI | Completed for QQQ: baseline, volume filter, logistic model, chronological evaluation |
| Training/validation/test labels | 66 / 29 / 33, fabricated prices only |
| MLflow | Local SQLite run completed with artifacts and metrics |
| Release integrity | Model/report/source checksums verified; synthetic live-shadow startup refused |
| Paper recovery | Mock lifecycle: intent, timeout, lookup, restart, deduplication, protected fill, exit, flat reconciliation |
| Shadow lifecycle | Mocked current data: settlement delay, restart deduplication, persistent revision halt, final post-close candle |
| Public Polymarket API | Real GETs: 3 markets, 6 books, 0 errors; TLS verification enabled |
| Credential preflight | Both Alpaca credential entries absent; read-only paper check stops before network |
| Docker/server | Not run: Docker and a target server are not configured |
| GitHub CI | Template prepared; workflow is not active |

Private local evidence (excluded from Git):

- Synthetic experiment: `runs/qqq-scope-smoke/c2c7cf2b4beb4873b3d2c1baf8fb179a/`
- Model SHA-256: `c16094835db28bcb9568c0f68b502605b58351dc4fea54eaa7107dfbfe2ff587`
- MLflow run: `a5032cffcc4d4a399e43dd72d45cf948`
- Dataset SHA-256: `9daa5fd6217eaac76d839386fa94c9bbab35cb0fe92922a3eeaa85605546720c`
- Public snapshot: `runs/public-polymarket/cdf3ec1c89ea4cd1935c986e337dc0dc/`
- Book-file SHA-256: `7d914735a8cfbdfde6d0ca5da35e81ee17cdf011d0becbdc994e683e138d6556`

The synthetic selected threshold was 0.35; promotion remains disabled. A model freeze is an identity check, not authorization for orders. No broker paper or real-money order has been sent. Real equity experiments need authenticated historical data. Unattended paper execution also needs the lifecycle work listed in [the workflow](workflow.md).
