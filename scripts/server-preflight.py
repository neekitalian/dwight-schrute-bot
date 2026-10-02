"""Read-only installation/readiness facts, with no credential values or network."""
import json
import os
from pathlib import Path
import platform
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def status():
    from dwight.ops import doctor, load_env, verify_release
    private_env = Path('/etc/dwight/paper.env')
    env_readable = private_env.is_file() and os.access(private_env, os.R_OK)
    if env_readable:
        load_env(private_env)
    release = Path('/var/lib/dwight/releases/candidate')
    valid_release = False
    release_error = None
    if (release / 'release.json').is_file():
        try:
            manifest = verify_release(release)
            valid_release = manifest.get('synthetic') is False
            if not valid_release:
                release_error = 'synthetic_release'
        except Exception as exc:
            release_error = type(exc).__name__
    try:
        revision = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = 'unavailable'
    facts = doctor()
    ready = env_readable and facts['alpaca_key_present'] and facts['alpaca_secret_present'] and valid_release
    observer_prepared = (env_readable and facts['alpaca_key_present']
                         and facts['alpaca_secret_present']
                         and facts['dependencies']['exchange_calendars'])
    return {
        'hostname': platform.node(), 'platform': platform.system(), 'python': platform.python_version(),
        'source_revision': revision, 'private_env_readable': env_readable,
        'alpaca_key_present': facts['alpaca_key_present'], 'alpaca_secret_present': facts['alpaca_secret_present'],
        'research_dependencies': facts['dependencies'],
        'validated_real_release_present': valid_release, 'release_error_type': release_error,
        'shadow_prepared': bool(ready), 'connectivity_verified': False,
        'manual_observer_locally_prepared': bool(observer_prepared),
        'manual_observer_requires_qualified_model': False,
        'manual_observer_account_risk_verified': False,
        'active_execution_mode': 'none_checked', 'broker_orders_enabled': False,
        'next_step': 'verify feed access then start shadow' if ready else 'prepare private data access and evaluated real release',
    }


if __name__ == '__main__':
    print(json.dumps(status(), indent=2))
