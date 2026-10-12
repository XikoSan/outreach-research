"""Inspect or replay campaign segmentation on saved profiles, without web collection.

Dry-run by default. --execute changes the existing run but keeps its old
segmentation report and company-group counts in segmentation_history. Existing
exports remain on disk. No source, contact, profile or evidence text is printed.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import re
import time

from app.candidate_discovery import build_candidate_overview
from app.db import Company, Run, Session
from app.evidence import normalized_signals
from app.pipeline import Pipeline, evidence_for


def preflight(db, run_id):
    run = db.get(Run, run_id)
    if not run or run.status != 'COMPLETED_WITH_ISSUES':
        raise ValueError('Expected a completed campaign run with issues')
    if run.config.get('methodology_version') != 'campaign-v1' or run.reports.get('campaign_plan', {}).get('version') != 'campaign-v2':
        raise ValueError('Expected a frozen campaign-v2 plan')
    if not run.segmentation_version:
        raise ValueError('Run has no completed segmentation to preserve')
    previous=run.reports.get('segmentation', {}).get('candidate_discovery') or {}
    if previous.get('version', 0) >= 4:
        raise ValueError('The saved-profile replay already completed; do not repeat paid calls')
    companies = db.query(Company).filter_by(run_id=run_id).all()
    if len(companies) != 100 or run.company_count != 100:
        raise ValueError('This replay is bounded to the 100-company pilot')
    profiles = [
        {'company_id': company.id,
         'signals': normalized_signals(company.profile.get('signals', [])),
         'evidence': evidence_for(company)}
        for company in companies
        if company.stage == 'PROFILE_READY' and company.identity == 'CONFIRMED'
        and company.profile.get('signals')
    ]
    overview = build_candidate_overview(profiles)
    return run, companies, overview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id')
    parser.add_argument('--execute', action='store_true', help='Run bounded model generation and review using saved evidence')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{32}', args.run_id):
        parser.error('run_id must be 32 lowercase hex characters')
    with Session() as db:
        try:
            run, companies, overview = preflight(db, args.run_id)
        except ValueError as exc:
            parser.error(str(exc))
        summary = overview['summary']
        source_id = run.reports.get('pilot', {}).get('source_run_id')
        source = db.get(Run, source_id) if source_id else None
        source_filters = source.config.get('source_filters', '') if source else run.config.get('source_filters', '')
        print(json.dumps({'run_id': args.run_id, 'mode': 'execute' if args.execute else 'dry_run',
                          'candidate_summary': summary, 'old_segments': len(run.segments),
                          'old_spent_estimate_usd': round(run.spent or 0, 6),
                          'web_collection': False}, ensure_ascii=False), flush=True)
        if not args.execute:
            return
        history = list(run.reports.get('segmentation_history', []))
        history.append({
            'saved_at': datetime.now(timezone.utc).isoformat(),
            'segmentation_version': run.segmentation_version,
            'segments': run.segments,
            'report': run.reports.get('segmentation', {}),
            'company_group_counts': dict(Counter(company.group for company in companies)),
            'spent_estimate_usd': run.spent,
        })
        run.reports = {**run.reports, 'segmentation_history': history}
        run.config = {**run.config, 'source_filters': source_filters}
        run.segments = []
        run.segmentation_version = 0
        run.status = 'RUNNING'
        run.error = None
        db.commit()
    started = time.monotonic()
    pipeline = Pipeline(args.run_id)
    try:
        pipeline.finish(saved_evidence_only=True)
    except Exception as exc:
        with Session() as db:
            run = db.get(Run, args.run_id)
            run.status = 'PAUSED'
            run.error = f'{type(exc).__name__}: {str(exc)[:250]}'
            db.commit()
        raise
    finally:
        if pipeline.gw.client:
            pipeline.gw.client.close()
    with Session() as db:
        run = db.get(Run, args.run_id)
        groups = dict(Counter(company.group for company in db.query(Company).filter_by(run_id=args.run_id)))
        print(json.dumps({'run_id': args.run_id, 'status': run.status,
                          'new_segments': len(run.segments), 'company_group_counts': groups,
                          'incremental_spent_estimate_usd': round(run.spent - history[-1]['spent_estimate_usd'], 6),
                          'elapsed_seconds': round(time.monotonic() - started, 1),
                          'diagnostics': run.reports.get('segmentation', {}).get('diagnostics', {}),
                          'web_collection': False}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
