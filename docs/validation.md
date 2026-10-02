# Local validation — 2026-10-03

These checks establish implementation behavior, not investment performance or broker integration readiness. The latest suite and synthetic experiment enforce QQQ-only equity scope; SPY requests, widened release policies and order submissions are rejected.

| Check | Result |
|---|---|
| Package build/install | Editable Dwight 0.2.0 installed successfully on Python 3.11 |
| Full local unit suite | 120 tests passed, including audit tampering, report charts, campaign cutoff/recovery/delivery claims, synthetic Space callback and deployment bundle checks |
| Robustness batch | Three fixed synthetic seeds plus a doubled cost case completed with MLflow records; all four audits passed; the model beat the baseline in only one of the three ordinary cases |
| Visual report | Four private HTML reports and PNG chart pairs generated; browser loaded the comparison page and both example charts without horizontal overflow |
| Milestone delivery | Private ledger initialized for 12, 24, 48 and 168 hours; observation clock not started; no emails sent; no mail provider connected |
| Linux report services | systemd worker and report timer templates prepared; target host still needs provisioning and on-host validation |
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
| Hugging Face app | Gradio 6.29 built locally; browser ran the synthetic callback, verified result table/status and mobile layout with no script errors |
| Space upload bundle | Committed source allowlist; excludes private data/credentials; immutable target; rejects symlinks |
| Hosted Hugging Face | Public Space RUNNING on CPU Basic; remote API and browser completed synthetic experiment; source manifest verified; local/remote strategy metrics matched |

Private local evidence (excluded from Git):

- Synthetic experiment: `runs/qqq-scope-smoke/c2c7cf2b4beb4873b3d2c1baf8fb179a/`
- Model SHA-256: `c16094835db28bcb9568c0f68b502605b58351dc4fea54eaa7107dfbfe2ff587`
- MLflow run: `a5032cffcc4d4a399e43dd72d45cf948`
- Dataset SHA-256: `9daa5fd6217eaac76d839386fa94c9bbab35cb0fe92922a3eeaa85605546720c`
- Public snapshot: `runs/public-polymarket/cdf3ec1c89ea4cd1935c986e337dc0dc/`
- Book-file SHA-256: `7d914735a8cfbdfde6d0ca5da35e81ee17cdf011d0becbdc994e683e138d6556`

The synthetic selected threshold was 0.35; promotion remains disabled. A model freeze is an identity check, not authorization for orders. No broker paper or real-money order has been sent. Real equity experiments need authenticated historical data. Unattended paper execution also needs the lifecycle work listed in [the workflow](workflow.md).
