"""Run only the base research stage for a separate, paused campaign pilot.

Does not segment, generate prompts, export, or touch the large source run.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from time import monotonic

from app.campaigns import prepare_campaign_plan, repair_citation_losses, research_base
from app.crawler import fetch
from app.db import Company, ModelCall, Run, Session
from app.pipeline import Pipeline


def snapshot(run_id):
    with Session() as db:
        run = db.get(Run, run_id)
        companies = db.query(Company).filter_by(run_id=run_id).all()
        calls = db.query(ModelCall).filter_by(run_id=run_id).all()
        return {
            'run_id': run_id, 'status': run.status, 'stage': run.stage,
            'estimated_spent_usd': run.spent, 'reserved_usd': run.reserved,
            'model_calls': len(calls), 'model_calls_complete': sum(call.state == 'COMPLETE' for call in calls),
            'base_ready': sum(company.research.get('base', {}).get('state') == 'COMPLETE' for company in companies),
            'companies': len(companies), 'base_pages': sum(len(company.research.get('base', {}).get('pages', [])) for company in companies),
            'confirmed_answers': sum(sum(a['status'] == 'confirmed' for a in company.profile.get('base_research', {}).get('answers', [])) for company in companies),
            'unknown_answers': sum(sum(a['status'] == 'unknown' for a in company.profile.get('base_research', {}).get('answers', [])) for company in companies),
            'citation_losses': sum(len(company.research.get('base', {}).get('citation_losses', [])) for company in companies),
            'errors': sum(len(company.research.get('base', {}).get('errors', [])) for company in companies),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id')
    parser.add_argument('--company-limit', type=int, default=3)
    parser.add_argument('--max-estimated-usd', type=float, default=0.12)
    parser.add_argument('--max-seconds', type=float, default=1200)
    args = parser.parse_args()
    if not 1 <= args.company_limit <= 30 or not 0 < args.max_estimated_usd <= 1 or args.max_seconds < 60:
        raise SystemExit('Unsafe pilot limit')
    with Session() as db:
        run = db.get(Run, args.run_id)
        if not run or run.status != 'PAUSED' or run.reports.get('pilot', {}).get('kind') != 'base_only':
            raise SystemExit('Expected a paused base-only pilot')
        sites = run.reports['pilot']['sites']
        companies = {company.resource: company for company in db.query(Company).filter_by(run_id=run.id)}
        if set(sites) != set(companies):
            raise SystemExit('Pilot company list changed')
        pending = [companies[site].id for site in sites if companies[site].research.get('base', {}).get('state') != 'COMPLETE']
        if not pending:
            print(json.dumps(snapshot(args.run_id), ensure_ascii=False))
            return
        if run.spent >= args.max_estimated_usd:
            raise SystemExit('Estimated pilot spend guard reached')
        selected = pending[:args.company_limit]
        run.status = 'RUNNING'
        run.error = None
        db.commit()
    started = monotonic()
    pipeline = Pipeline(args.run_id)
    problem = None
    processed = 0
    try:
        prepare_campaign_plan(pipeline)
        for company_id in selected:
            if monotonic() - started >= args.max_seconds:
                problem = 'Pilot wall-time guard reached'
                break
            with Session() as db:
                if db.get(Run, args.run_id).spent >= args.max_estimated_usd:
                    problem = 'Estimated pilot spend guard reached'
                    break
            research_base(pipeline, company_id, fetch)
            with Session() as db:
                losses = set(db.get(Company, company_id).research.get('base', {}).get('citation_losses', []))
            if losses:
                repair_citation_losses(pipeline, company_id, losses)
            processed += 1
            current = snapshot(args.run_id)
            print(json.dumps({'processed_this_batch': processed, 'base_ready': current['base_ready'],
                              'estimated_spent_usd': current['estimated_spent_usd'],
                              'base_pages': current['base_pages'], 'confirmed_answers': current['confirmed_answers']},
                             ensure_ascii=False), flush=True)
    except Exception as error:
        problem = type(error).__name__ + ': ' + str(error)[:200]
    finally:
        if pipeline.gw.client:
            pipeline.gw.client.close()
        with Session() as db:
            run = db.get(Run, args.run_id)
            run.status = 'PAUSED'
            run.error = problem
            pilot = dict(run.reports['pilot'])
            pilot['last_base_batch'] = {'processed': processed, 'seconds': round(monotonic() - started, 1),
                                        'finished_at': datetime.now(timezone.utc).isoformat(), 'error': problem}
            run.reports = {**run.reports, 'pilot': pilot}
            db.commit()
        result = {**snapshot(args.run_id), 'batch_processed': processed,
                  'batch_seconds': round(monotonic() - started, 1), 'error': problem}
        folder = Path('data/observations') / args.run_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'base-only-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    if problem:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
