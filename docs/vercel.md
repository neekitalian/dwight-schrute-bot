# Vercel web portal

Import this repository into the existing Vercel project. Use the repository root
and the **Python** framework preset. Leave build and output directory overrides
empty. `pyproject.toml` explicitly points to the WSGI application `app:app`.
No account credentials are required for this public deployment.

The home page is the weekly QQQ learning ticket. Visitors make a prediction,
replay the sample week and compare three approaches on the same path. The values
are illustrative. `/learn` remains an alias for the home ticket.

`/research` is an optional one-page form for downloading a comparison definition
to validate in a private workspace. The strategy cohort stays fixed; capital and
cost assumptions are collapsed by default. `/setup` contains optional platform
guides. Neither secondary page is required to use the learning ticket.

## Local preview

```sh
python3 app.py
```

Open `http://127.0.0.1:8080`. The entrypoint needs only the Python standard library
and Dwight's public connection catalog. A health request at `/api/health` reports
the web portal's availability. It deliberately reports `live_worker: false`;
it is not evidence that a trading worker is running.

## Public routes

| Route | Result |
| --- | --- |
| `/` | Weekly prediction, replay and comparison learning ticket |
| `/learn` | Alias for the home learning ticket |
| `/research` | One-page comparison definition form |
| `/setup` | Platform setup guides |
| `/api/health` | Portal version and capability boundaries |
| `/api/platforms` | Public platform catalog |
| `/api/profile/alpaca?feed=sip` | Downloadable, read-only configuration |

Profiles name private environment variables but never contain their values.
GET and HEAD are the only accepted methods. There is no account, order or
webhook endpoint. TradingView's native paper account still requires manual
order entry; Alpaca paper execution belongs to a separate private account.

## Deployment boundaries

`.vercelignore` allowlists the few public portal files for CLI uploads.
`vercel.json` additionally excludes private state and research datasets from the
Python function bundle. Deploy from this repository or a reviewed clean staging
directory, never the parent workspace. Keep existing Vercel deployment protection.
Do not add broker credentials to this public project.

The always-on Linux service remains a separate deployment. This request-based
portal has no durable trading state, process ownership or execution controls.
Future account controls require a private authenticated backend and verified
execution lifecycle before they can be advertised here.
