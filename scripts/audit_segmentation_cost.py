"""Read-only, contact-free audit of model calls for one or more research runs.

Run with: PYTHONPATH=backend .venv/bin/python scripts/audit_segmentation_cost.py RUN_ID [RUN_ID ...]
Costs are the application's configured estimates, not provider invoices. API and
rate-gate seconds are summed across calls, so parallel work must not be mistaken
for elapsed wall time. No model request, prompt, key, or contact row is read out.
"""
import argparse
from collections import defaultdict
import json
import re

from app.db import ModelCall, Run, Session

METRICS = ('input_tokens', 'output_tokens', 'searches', 'gate_estimated_tokens',
           'api_seconds', 'gate_wait_seconds')


def _number(metrics, key):
    value = (metrics or {}).get(key, 0)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def summarize_calls(calls):
    totals = defaultdict(lambda: {
        'calls': 0, 'complete': 0, 'uncertain_or_started': 0, 'rejected': 0,
        'estimated_cost_usd': 0.0, **{key: 0 for key in METRICS},
    })
    for call in calls:
        item = totals[call.role, call.model]
        item['calls'] += 1
        item['complete'] += call.state == 'COMPLETE'
        item['uncertain_or_started'] += call.state in ('UNCERTAIN', 'STARTED')
        item['rejected'] += call.state == 'REJECTED'
        item['estimated_cost_usd'] += call.cost or 0
        for key in METRICS:
            item[key] += _number(call.metrics, key)
    rows = []
    for (role, model), values in sorted(totals.items()):
        rows.append({'role': role, 'model': model, **{
            key: round(value, 6) if key.endswith(('_usd', '_seconds')) else value
            for key, value in values.items()}})
    combined = ({key: round(sum(row[key] for row in rows), 6)
                 if key.endswith(('_usd', '_seconds')) else sum(row[key] for row in rows)
                 for key in rows[0] if key not in ('role', 'model')} if rows else {})
    return rows, combined


def audit_run(db, run_id):
    run = db.get(Run, run_id)
    if run is None:
        raise ValueError('Исследование не найдено: ' + run_id)
    calls = db.query(ModelCall).filter_by(run_id=run_id).all()
    by_role, total = summarize_calls(calls)
    return {
        'run_id': run_id, 'status': run.status, 'stage': run.stage,
        'company_count': run.company_count,
        'run_estimated_spent_usd': round(run.spent or 0, 6),
        'run_reserved_usd': round(run.reserved or 0, 6),
        'by_role_and_model': by_role, 'total_model_calls': total,
        'latency_note': 'api_seconds and gate_wait_seconds are sums across calls, not elapsed wall time',
        'cost_note': 'Configured application estimates; not a provider invoice. ModelCall latest state only.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_ids', nargs='+', help='One or more 32-character research run IDs')
    args = parser.parse_args()
    if any(not re.fullmatch(r'[0-9a-f]{32}', run_id) for run_id in args.run_ids):
        parser.error('Каждый run_id должен содержать 32 строчных шестнадцатеричных символа')
    with Session() as db:
        try:
            reports = [audit_run(db, run_id) for run_id in args.run_ids]
        except ValueError as error:
            parser.error(str(error))
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
