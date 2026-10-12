"""Run only base collection for a separate pilot, with bounded optional concurrency.

Does not segment, generate prompts, export, or touch the source research run.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
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


def pilot_company_ids(run, companies):
    pilot = run.reports['pilot']
    by_site = {company.resource: company.id for company in companies}
    if 'sites' in pilot:
        if len(by_site) != len(companies) or set(pilot['sites']) != set(by_site):
            raise ValueError('Список сайтов пилота изменился')
        return [by_site[site] for site in pilot['sites']]
    groups = pilot.get('imported_groups', {})
    ordered = [cid for label in ('random_core', 'diagnostic', 'mixed') for cid in groups.get(label, [])]
    if len(ordered) != len(set(ordered)) or set(ordered) != {company.id for company in companies}:
        raise ValueError('Список компаний стратифицированного пилота изменился')
    return ordered


def eligible_company_ids(ordered, companies, uncertain_company_ids):
    by_id = {company.id: company for company in companies}
    return [cid for cid in ordered if cid not in uncertain_company_ids
            and by_id[cid].research.get('base', {}).get('state') != 'COMPLETE']


def research_one(run_id, company_id, batch_pages=False):
    pipeline = Pipeline(run_id)
    try:
        research_base(pipeline, company_id, fetch, batch_pages=batch_pages)
        with Session() as db:
            losses = set(db.get(Company, company_id).research.get('base', {}).get('citation_losses', []))
        if losses:
            repair_citation_losses(pipeline, company_id, losses)
    finally:
        if pipeline.gw.client:
            pipeline.gw.client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id')
    parser.add_argument('--company-limit', type=int, default=3)
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--max-estimated-usd', type=float, default=0.12)
    parser.add_argument('--max-seconds', type=float, default=1200)
    parser.add_argument('--batch-pages', action='store_true', help='Эксперимент: один вызов модели на две страницы')
    parser.add_argument('--start-index', type=int, default=0, help='Смещение в списке ещё не обработанных компаний')
    parser.add_argument('--experimental-concurrency', action='store_true', help='Разрешить до 12 работников только в отдельном пилоте')
    args = parser.parse_args()
    max_workers = 12 if args.experimental_concurrency else 4
    if not 1 <= args.company_limit <= 30 or not 1 <= args.concurrency <= max_workers or not 0 <= args.start_index <= 300 or not 0 < args.max_estimated_usd <= 5 or args.max_seconds < 60:
        raise SystemExit('Unsafe pilot limit')
    with Session() as db:
        run = db.get(Run, args.run_id)
        if not run or run.status != 'PAUSED' or run.reports.get('pilot', {}).get('kind') != 'base_only':
            raise SystemExit('Expected a paused base-only pilot')
        companies = db.query(Company).filter_by(run_id=run.id).all()
        mode = 'batch2' if args.batch_pages else 'single'
        previous_mode = run.reports['pilot'].get('base_mode')
        if previous_mode is None and (db.query(ModelCall).filter_by(run_id=run.id, role='extraction').count()
                                      or any(company.research.get('base') for company in companies)):
            previous_mode = 'single'
        if previous_mode and previous_mode != mode:
            raise SystemExit('Нельзя смешивать режимы базового прохода в одном пилоте')
        try:
            ordered = pilot_company_ids(run, companies)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        uncertain = {cid for (cid,) in db.query(ModelCall.company_id).filter(
            ModelCall.run_id == run.id, ModelCall.state.in_(['STARTED', 'UNCERTAIN'])).all() if cid}
        pending = eligible_company_ids(ordered, companies, uncertain)
        if not pending:
            print(json.dumps(snapshot(args.run_id), ensure_ascii=False))
            return
        if run.spent >= args.max_estimated_usd:
            raise SystemExit('Estimated pilot spend guard reached')
        selected = pending[args.start_index:args.start_index + args.company_limit]
        if not selected:
            raise SystemExit('В выбранном диапазоне нет компаний для пилота')
        run.reports = {**run.reports, 'pilot': {**run.reports['pilot'], 'base_mode': mode}}
        run.status = 'RUNNING'
        run.error = None
        db.commit()
    started = monotonic()
    pipeline = Pipeline(args.run_id)
    problem = None
    initially_ready = snapshot(args.run_id)['base_ready']
    processed = 0
    try:
        prepare_campaign_plan(pipeline)
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for offset in range(0, len(selected), args.concurrency):
                if monotonic() - started >= args.max_seconds:
                    problem = 'Pilot wall-time guard reached'
                    break
                with Session() as db:
                    if db.get(Run, args.run_id).spent >= args.max_estimated_usd:
                        problem = 'Estimated pilot spend guard reached'
                        break
                wave = selected[offset:offset + args.concurrency]
                futures = [pool.submit(research_one, args.run_id, cid, args.batch_pages) for cid in wave]
                failures = []
                for future in futures:
                    try: future.result()
                    except Exception as error: failures.append(error)
                current = snapshot(args.run_id)
                processed = current['base_ready'] - initially_ready
                if failures: raise failures[0]
                print(json.dumps({'processed_this_batch': processed, 'base_ready': current['base_ready'],
                                  'estimated_spent_usd': current['estimated_spent_usd'],
                                  'base_pages': current['base_pages'],
                                  'confirmed_answers': current['confirmed_answers']},
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
                                        'concurrency': args.concurrency, 'start_index': args.start_index,
                                        'finished_at': datetime.now(timezone.utc).isoformat(), 'error': problem}
            run.reports = {**run.reports, 'pilot': pilot}
            db.commit()
        result = {**snapshot(args.run_id), 'batch_processed': snapshot(args.run_id)['base_ready'] - initially_ready,
                  'batch_seconds': round(monotonic() - started, 1), 'error': problem}
        folder = Path('data/observations') / args.run_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'base-only-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    if problem:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
