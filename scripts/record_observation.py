"""Read-only research telemetry; writes aggregate CSVs without source/contact data."""
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from app.db import Session, Run, Company, ModelCall, ModelAttempt

rid = sys.argv[1]
folder = Path('data/observations') / rid
folder.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).isoformat()

def append(name, rows):
    if not rows:
        return
    path = folder / name
    exists = path.exists() and path.stat().st_size > 0
    with path.open('a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        if not exists:
            writer.writeheader()
        writer.writerows(rows)

with Session() as db:
    run = db.get(Run, rid)
    if run is None:
        raise SystemExit('Research not found')
    companies = db.query(Company.stage, Company.group).filter(Company.run_id == rid).all()
    stages = Counter(stage for stage, group in companies)
    groups = Counter(group for stage, group in companies)
    operations = db.query(ModelCall).filter(ModelCall.run_id == rid, ModelCall.package_id.is_(None)).all()
    history = defaultdict(list)
    for attempt in db.query(ModelAttempt).join(ModelCall,ModelCall.id==ModelAttempt.operation_id).filter(ModelCall.run_id==rid,ModelCall.package_id.is_(None)):
        history[attempt.operation_id].append(attempt)
    calls=[(c.role,c.model,a.state,a.cost,a.metrics) for c in operations for a in (history[c.id] or [c])]
    totals = defaultdict(lambda: dict(calls=0, complete=0, uncertain=0, input_tokens=0, output_tokens=0, searches=0, estimated_usd=0.0))
    for role, model, state, cost, metrics in calls:
        t = totals[role, model]
        t['calls'] += 1
        t['complete'] += state == 'COMPLETE'
        t['uncertain'] += state in ('UNCERTAIN', 'STARTED')
        t['estimated_usd'] += cost or 0
        for key in ('input_tokens', 'output_tokens', 'searches'):
            t[key] += (metrics or {}).get(key, 0)
    row = dict(timestamp_utc=stamp, run_id=rid, status=run.status, stage=run.stage, total_companies=run.company_count, source_rows=run.row_count, profile_ready=stages['PROFILE_READY'], pending=stages['PENDING'], needs_review=groups['NEEDS_REVIEW'], failed=groups['FAILED'], estimated_usd=run.spent, reserved_usd=run.reserved, api_calls=len(calls), input_tokens=sum(t['input_tokens'] for t in totals.values()), output_tokens=sum(t['output_tokens'] for t in totals.values()), searches=sum(t['searches'] for t in totals.values()))
    append('progress.csv', [row])
    append('cost_by_stage.csv', [dict(timestamp_utc=stamp, role=role, model=model, **t) for (role, model), t in sorted(totals.items())])
    print(row)
