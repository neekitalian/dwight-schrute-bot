"""Run a fixed synthetic robustness batch. No market or broker requests.

All cases are declared before evaluation. No case is selected for deployment.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from examples.make_experiment_demo import generate
from dwight.experiments import experiment
from dwight.audit import audit_experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases = [(42, False), (43, False), (44, False), (42, True)]
    summary = {'provenance':'synthetic software exercise', 'market_performance':False,
               'deployment_selection':None,'cases':[]}
    for seed, stress in cases:
        case = output/f'seed{seed}-{"doubled-costs" if stress else "base-costs"}'
        case.mkdir()
        data = case/'generated.csv'
        generate(data, days=500, seed=seed)
        config = json.loads((ROOT/'configs/synthetic-experiment.json').read_text())
        config['tracking_uri'] = f'sqlite:///{output}/mlflow.db'
        if stress:
            config['strategy'] = {'slippage':.02,'commission':.01}
        result = experiment(data,'QQQ',case/'experiments',synthetic=True,config=config)
        audit = audit_experiment(Path(result['directory']))
        (case/'audit.json').write_text(json.dumps(audit,indent=2)+'\n')
        item = {'seed':seed, 'doubled_costs':stress, 'experiment':result['directory'],
                'status':result['status'], 'audit':audit['status'],
                'audit_check_counts':audit.get('check_counts'),
                'evaluation':result.get('evaluation',{}).get('test',{}),
                'blocking_reasons':result.get('blocking_reasons',[])}
        summary['cases'].append(item)
        (output/'batch.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
        print(json.dumps({key:item[key] for key in ('seed','doubled_costs','status','audit','experiment')}),flush=True)
    print(json.dumps({'summary':str(output/'batch.json'),'synthetic':True}),flush=True)


if __name__ == '__main__':
    main()
