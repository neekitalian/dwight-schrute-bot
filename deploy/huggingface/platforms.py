"""Public connection setup. Only fixed, unauthenticated data probes are exposed."""
from functools import lru_cache
from html import escape
import json
from pathlib import Path
import tempfile

from dwight.connection_catalog import PLATFORMS, build_profile
from dwight.connection_checks import check_connection

PUBLIC_PLATFORMS = frozenset({'coinbase', 'binance', 'kraken', 'polymarket'})
INTRO = '''### Choose a platform

Use public data checks here, then download a setup profile for your private Dwight workspace.
Account credentials stay on your machine or server. This Space has no account login or order routing.

**Stocks:** QQQ research and manual TradingView paper records are available in the toolkit.
**Crypto:** the checks below test public endpoints. Crypto strategies and authenticated exchange adapters
are not implemented. A successful data check does not connect your account.
'''
FLOW = '''<div class="dw-flow">
<div class="dw-node"><span>01 / SELECT</span><b>Your platform</b><small>Stocks, crypto or prediction markets.</small></div>
<div class="dw-node"><span>02 / CHECK</span><b>Public data access</b><small>A timestamped read-only result from this server.</small></div>
<div class="dw-node"><span>03 / PREPARE</span><b>Private workspace</b><small>Download a profile. Supply supported credentials locally.</small></div>
<div class="dw-node"><span>04 / REVIEW</span><b>Research first</b><small>Validate your feed and strategy before any manual paper order.</small></div></div>'''
BOUNDARY = '''**Account boundary** Paper Trading by TradingView and Alpaca paper are separate accounts.
Pine strategies cannot place orders in TradingView's native paper simulator. Dwight's TradingView route
receives observations in a private inbox; you review and enter native paper orders yourself.

**Strategy boundary** The charts in Performance use invented QQQ prices. Public crypto probes do not
feed those charts or establish crypto trading results. Regional access, subscriptions and account
permissions must be checked in your own deployment. No funds or orders move through this page.
'''


def _platform(platform):
    if not isinstance(platform, str) or platform not in PLATFORMS:
        raise ValueError('Choose a listed platform')
    return PLATFORMS[platform]


def overview():
    cards = []
    for item in PLATFORMS.values():
        cards.append('<div class="dw-node"><span>' + escape(item['category'].replace('_', ' ').title()) + '</span><b>'
                     + escape(item['name']) + '</b><small>' + escape(item['status_label'])
                     + '</small></div>')
    return '<div class="dw-platform-grid">' + ''.join(cards) + '</div>'


def details(platform):
    item = _platform(platform)
    steps = '\n'.join(f'{i}. {step}' for i, step in enumerate(item['setup_steps'], 1))
    limits = '\n'.join(f'* {line}' for line in item['limitations'])
    docs = ' · '.join(f"[{link['label']}]({link['url']})" for link in item['docs'])
    return f"### {item['name']}\n\n**{item['status_label']}**\n\n{item['summary']}\n\n{steps}\n\n**Current limits**\n\n{limits}\n\n{docs}"


@lru_cache(maxsize=20)
def profile_download(platform, feed):
    # Validate before creating a path. At most ten platforms times two feeds;
    # the directory contains only generated, credential-free catalog profiles.
    profile = build_profile(platform, feed=feed)
    folder = Path(tempfile.mkdtemp(prefix='dwight-public-profile-'))
    path = folder / f'dwight-{platform}.json'
    path.write_text(json.dumps(profile, indent=2, allow_nan=False) + '\n')
    return str(path)


def setup(platform, feed='sip'):
    _platform(platform)
    profile = build_profile(platform, feed=feed)
    command = f'dwight connection-check --profile ./dwight-{platform}.json'
    instructions = details(platform) + '\n\n**Use the downloaded profile locally**\n\n```sh\n' + command + '\n```\n\nInstall the current toolkit first. This command makes read-only checks; it cannot submit orders.'
    if platform in {'databento', 'massive'}:
        instructions += ('\n\n**Add your key privately**\n\nRun `dwight prepare-data-keys` in your local workspace. '
                         'Open `.env` and fill the field named in this profile. Keep it off this public page. '
                         '[History download guide](https://github.com/neekitalian/dwight-schrute-bot/blob/main/docs/data-providers.md)')
    return instructions, json.dumps(profile, indent=2), profile_download(platform, feed)


def public_check(platform):
    _platform(platform)
    if platform not in PUBLIC_PLATFORMS:
        # Never dispatch private account checks, even if a caller bypasses the UI.
        return {'platform': platform, 'status': 'private_setup_required',
                'message': 'Use the private setup instructions. This Space does not inspect account credentials.',
                'capabilities': {'public_market_data': False, 'account_read': False,
                                 'qqq_data': False, 'order_execution': False}}
    return check_connection(platform, environ={})
