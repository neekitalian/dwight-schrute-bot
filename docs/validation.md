# Local validation — 2026-10-03

These checks establish implementation behavior, not investment performance or broker integration readiness. The latest suite and synthetic experiment enforce QQQ-only equity scope; SPY requests, widened release policies and order submissions are rejected.

| Check | Result |
|---|---|
| Package build/install | Editable Dwight 0.2.0 installed successfully on Python 3.11 |
| Full local unit suite | 261 tests ran: 255 passed and six optional-dependency checks skipped (four Gym, two Gradio). Includes real sample ingestion/provenance/TLS, private artifact permissions, end-to-end private replay, feature comparison, manual FIFO/import audit, CLI evidence modes, walk-forward leakage/holdout checks and the public view. All 12 public walk-forward tests separately passed in the Hugging Face environment. |
| Robustness batch | Three fixed synthetic seeds plus a doubled cost case completed with MLflow records; all four audits passed; the model beat the baseline in only one of the three ordinary cases |
| Visual report | Four private HTML reports and PNG chart pairs generated; browser loaded the comparison page and both example charts without horizontal overflow |
| Milestone delivery | Private ledger initialized for 12, 24, 48 and 168 hours; observation clock not started; no emails sent; no mail provider connected |
| Linux report services | Prior pinned release installation verified on the existing Ubuntu 24.04 host: 173 tests ran, 169 passed, four optional checks skipped. The shadow service and report timer are loaded, inactive and disabled. The current source update still needs remote access, which became unavailable when the Mac locked. |
| Synthetic experiment CLI | Completed for QQQ: baseline, volume filter, logistic model, chronological evaluation |
| Training/validation/test labels | 66 / 29 / 33, fabricated prices only |
| MLflow | Local SQLite run completed with artifacts and metrics |
| Release integrity | Model/report/source checksums verified; synthetic live-shadow startup refused |
| Paper recovery | Mock lifecycle: intent, timeout, lookup, restart, deduplication, protected fill, exit, flat reconciliation |
| Shadow lifecycle | Mocked current data: settlement delay, restart deduplication, persistent revision halt, final post-close candle |
| Public Polymarket API | Real GETs: 3 markets, 6 books, 0 errors; TLS verification enabled |
| Credential preflight | Both Alpaca credential entries absent; read-only paper check stops before network |
| Docker/server | Docker remains unverified. The prior Ubuntu systemd installation is verified; the latest source is not yet installed there. No live shadow start or real-data release exists. |
| GitHub CI | Workflow created through GitHub’s editor. Initial run failed validation before any jobs ran due to runner context in job environment. Corrected template prepared; publishing that repair is blocked by workflow permission and unavailable browser access. No passing CI claim. |
| Hugging Face app | Gradio 6.29 built locally; browser completed the new walk-forward callback and verified three windows, 40 reserved sessions, the timeline and result table. No browser script errors were observed. Earlier dashboard checks also covered mobile layout. |
| Space upload bundle | Committed source allowlist; excludes private data/credentials; immutable target; rejects symlinks |
| Previously hosted Hugging Face | The preceding walk-forward release was verified RUNNING on CPU Basic with source manifest and API results. Each subsequent upload requires a fresh hosted revision and callback check; local checks alone do not establish deployment success. |

The new fixed walk-forward smoke completed all three planned windows. Its 40-session final holdout was not scored. The logistic filter underperformed the baseline in aggregate in this fabricated fixture; no settings were changed to improve that result. This tests the evaluation pipeline, not market profitability.

The manual CSV fixture is invented and labelled `synthetic`. It is isolated from user-supplied paper exports; repeated imports are idempotent. Realized P&L is calculated from imported executions, while account equity and unrealized P&L remain unknown.

Private local evidence (excluded from Git):

- Synthetic experiment: `runs/qqq-scope-smoke/c2c7cf2b4beb4873b3d2c1baf8fb179a/`
- Model SHA-256: `c16094835db28bcb9568c0f68b502605b58351dc4fea54eaa7107dfbfe2ff587`
- MLflow run: `a5032cffcc4d4a399e43dd72d45cf948`
- Dataset SHA-256: `9daa5fd6217eaac76d839386fa94c9bbab35cb0fe92922a3eeaa85605546720c`
- Public snapshot: `runs/public-polymarket/cdf3ec1c89ea4cd1935c986e337dc0dc/`
- Book-file SHA-256: `7d914735a8cfbdfde6d0ca5da35e81ee17cdf011d0becbdc994e683e138d6556`

The synthetic selected threshold was 0.35; promotion remains disabled. A model freeze is an identity check, not authorization for orders. No broker paper or real-money order has been sent. A free official FirstRate QQQ sample has now supported a private real historical replay. Its 11 sessions are insufficient for training, and it is not the Alpaca feed intended for deployment. More matching historical data still requires preparation. Unattended paper execution also needs the lifecycle work listed in [the workflow](workflow.md).

## Real sample and enrichment review

The authenticated HTTPS download of the official FirstRate QQQ sample yields 858 five-minute bars across 11 complete regular sessions, September 17 to October 1, 2026. The separately recorded long-only full-sample replay returned a $5.20 simulated loss after fixed costs. This whole sample has been inspected and cannot later be called a pristine holdout. The existing standard long-and-short chronological path returned insufficient data, with 1/1/0 labelled trades, and no fitted model. Source hashes and the raw archive are private.

The new research comparison keeps technical v1 intact and adds four causal session features in a separate v2 schema. On a fixed synthetic smoke, v2 was worse than v1 and the baseline in independent-window net P&L. These are invented prices and do not estimate a QQQ edge. The genuine short sample cannot train either arm under the unchanged gates. FinRL and transformer performance remain unmeasured.
