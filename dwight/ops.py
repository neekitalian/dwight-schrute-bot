"""Local preparation, release identity, and process health; no account mutations."""
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone


def load_env(path=Path('.env')):
    """Read simple KEY=value secrets without executing shell or overriding env."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key.strip()):
            raise ValueError('Invalid .env entry; use KEY=value, no shell expressions')
        value = value.strip()
        if value[:1] in ('"', "'") and value[-1:] == value[:1]:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def doctor():
    def present(*names): return any(bool(os.environ.get(n)) for n in names)
    return {
        'alpaca_key_present': present('APCA_API_KEY_ID', 'ALPACA_API_KEY'),
        'alpaca_secret_present': present('APCA_API_SECRET_KEY', 'ALPACA_SECRET_KEY'),
        'databento_key_present': present('DATABENTO_API_KEY'),
        'massive_key_present': present('MASSIVE_API_KEY'),
        'docker_available': bool(shutil.which('docker')),
        'dependencies': {name: importlib.util.find_spec(name) is not None
                         for name in ('sklearn','exchange_calendars','mlflow','pyarrow')},
        'execution': 'paper_only; broker connection is checked separately',
        'secrets': 'values never included',
    }


def sha256(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_sha256():
    source = Path(__file__).parent.parent
    digest = hashlib.sha256()
    for folder in ('dwight','vwap_bot'):
        for path in sorted((source/folder).glob('**/*.py')):
            digest.update(str(path.relative_to(source)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _validate_candidate(model, report, policy, feed, symbol):
    from .experiments import JSONModel, direction_policy
    if symbol != 'QQQ' or policy.get('allowed_symbols') != ['QQQ']:
        raise ValueError('Dwight equity releases and policies must permit QQQ only')
    JSONModel(model)
    direction = direction_policy(model)
    if direction_policy(report) != direction:
        raise ValueError('model/report direction policy mismatch')
    if 'direction_policy' in policy and direction_policy(policy) != direction:
        raise ValueError('shadow policy direction differs from the experiment')
    settings = report.get('experiment_settings', {})
    if not isinstance(settings, dict):
        raise ValueError('invalid experiment settings')
    if 'long_only' in settings:
        if type(settings['long_only']) is not bool:
            raise ValueError('invalid experiment long_only setting')
        if ('long_only' if settings['long_only'] else 'long_and_short') != direction:
            raise ValueError('experiment setting differs from direction policy')
    if model.get('symbol') != symbol or report.get('symbol') != symbol:
        raise ValueError('release symbol differs from the experiment')
    if model.get('feed') != feed or report.get('feed') != feed:
        raise ValueError('release feed differs from training data')
    if model.get('synthetic') != report.get('synthetic'):
        raise ValueError('inconsistent synthetic provenance')
    if not model['synthetic'] and (model.get('source') != 'alpaca' or not model.get('dataset_sha256')):
        raise ValueError('shadow deployment requires a verified Alpaca dataset manifest')
    if model.get('strategy') != report.get('strategy') or not model.get('strategy'):
        raise ValueError('strategy configuration differs from the experiment')
    if model.get('input_sha256') != report.get('input_sha256') or not model.get('input_sha256'):
        raise ValueError('model/report input mismatch')
    for name in ('source','adjustment','dataset_sha256','feature_version'):
        if model.get(name) != report.get(name):
            raise ValueError('model/report provenance mismatch: '+name)
    source = Path(__file__).parent.parent
    training_code = hashlib.sha256((source/'dwight/experiments.py').read_bytes()+
                                   (source/'vwap_bot/engine.py').read_bytes()).hexdigest()
    if model.get('code_sha256') != training_code or report.get('code_sha256') != training_code:
        raise ValueError('training/feature code differs from evaluated model')
    if not model['synthetic'] and model.get('adjustment') != 'raw':
        raise ValueError('live shadow requires the same raw-price adjustment policy')
    if model.get('threshold') != report.get('selected_threshold'):
        raise ValueError('model threshold differs from evaluated threshold')
    if report.get('status') not in ('completed_research','completed_synthetic_smoke'):
        raise ValueError('experiment did not finish successfully')
    if policy.get('mode') != 'shadow' or policy.get('feed') != feed or symbol not in policy.get('allowed_symbols',[]):
        raise ValueError('policy must permit this symbol/feed in shadow mode')
    for key, low, high in (('max_bar_delay_seconds',1,299),('poll_seconds',1,60),('bar_settle_seconds',0,120)):
        value = policy.get(key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('invalid shadow policy '+key)
    if policy['bar_settle_seconds']+policy['poll_seconds'] > policy['max_bar_delay_seconds']:
        raise ValueError('bar settle and polling delays exceed the allowed signal delay')


def release(experiment_dir, output, *, feed, symbol, policy_path):
    """Freeze a candidate for shadow evaluation. Never grants paper approval."""
    experiment_dir, output = Path(experiment_dir), Path(output)
    if feed not in ('sip', 'iex', 'synthetic') or symbol != 'QQQ':
        raise ValueError('unsupported feed or symbol')
    model = experiment_dir/'model.json'
    report = experiment_dir/'report.json'
    if not model.is_file() or not report.is_file():
        raise ValueError('experiment must contain model.json and report.json')
    policy = json.loads(Path(policy_path).read_text())
    artifact, evaluation = json.loads(model.read_text()), json.loads(report.read_text())
    _validate_candidate(artifact,evaluation,policy,feed,symbol)
    if evaluation.get('model_sha256') != sha256(model):
        raise ValueError('model checksum differs from evaluated artifact')
    output.mkdir(parents=True, exist_ok=False)
    for src, name in ((model,'model.json'),(report,'report.json'),(Path(policy_path),'policy.json')):
        (output/name).write_bytes(src.read_bytes())
    try:
        commit = subprocess.check_output(['git','rev-parse','HEAD'], stderr=subprocess.DEVNULL,
                                        text=True).strip()
        dirty = bool(subprocess.check_output(['git','status','--porcelain'], text=True).strip())
    except (OSError,subprocess.CalledProcessError):
        commit,dirty='unavailable',True
    manifest={'schema':1,'created_at':datetime.now(timezone.utc).isoformat(),
              'application_commit':commit,'dirty_checkout':dirty,
              'application_source_sha256':source_sha256(),
              'feed':feed,'symbol':symbol,'mode':'shadow','paper_approved':False,
              'synthetic':artifact['synthetic'],
              'direction_policy':artifact.get('direction_policy','long_and_short'),
              'files':{name:sha256(output/name) for name in ('model.json','report.json','policy.json')}}
    (output/'release.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


def verify_release(directory, feed=None):
    directory=Path(directory)
    manifest=json.loads((directory/'release.json').read_text())
    if manifest.get('mode') != 'shadow' or set(manifest.get('files',{})) != {'model.json','report.json','policy.json'}:
        raise ValueError('invalid shadow release manifest')
    for name,expected in manifest['files'].items():
        if sha256(directory/name) != expected:
            raise ValueError('release checksum mismatch: '+name)
    if feed and manifest['feed'] != feed:
        raise ValueError('model release/feed mismatch')
    if manifest.get('application_source_sha256') != source_sha256():
        raise ValueError('running source differs from the frozen release')
    artifact = json.loads((directory/'model.json').read_text())
    report = json.loads((directory/'report.json').read_text())
    policy = json.loads((directory/'policy.json').read_text())
    _validate_candidate(artifact,report,policy,manifest['feed'],manifest['symbol'])
    from .experiments import direction_policy
    if direction_policy(manifest) != direction_policy(artifact):
        raise ValueError('release direction policy differs from the model')
    if report.get('model_sha256') != sha256(directory/'model.json'):
        raise ValueError('model checksum differs from evaluated artifact')
    if manifest.get('synthetic') != artifact['synthetic'] or manifest.get('paper_approved') is not False:
        raise ValueError('release provenance or mode changed')
    return manifest
