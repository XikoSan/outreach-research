"""Revalidate saved base model results after an evidence validator correction.

Dry run by default; no model calls or web requests. Only for a paused base-only pilot.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from app.campaigns import BasePageResult, criterion_signals, validate_answers
from app.db import Company, ModelCall, Run, Session, engine
from app.evidence import normalized_signals, page_key, verify_report


def backup_sqlite():
    if engine.url.get_backend_name() != 'sqlite':
        raise ValueError('Automatic backup supports SQLite only')
    path = Path(engine.url.database).resolve()
    folder = path.parent / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    backup = folder / f'{path.stem}-before-base-evidence-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.db'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id')
    parser.add_argument('resource')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with Session() as db:
        run = db.get(Run, args.run_id)
        if not run or run.status != 'PAUSED' or run.reports.get('pilot', {}).get('kind') != 'base_only' or run.segments:
            raise SystemExit('Expected a paused base-only pilot without segments')
        company = db.query(Company).filter_by(run_id=run.id, resource=args.resource).one()
        base = company.research.get('base', {})
        if base.get('state') != 'COMPLETE' or not base.get('pages'):
            raise SystemExit('Base pages are not complete')
        criteria = run.reports['campaign_plan']['base_criteria'] + run.reports['campaign_plan']['plan']['criteria']
        pages = base['pages']
        calls = db.query(ModelCall).filter_by(run_id=run.id, company_id=company.id, role='extraction', state='COMPLETE').all()
        by_page = {}
        for call in calls:
            if not call.result or 'report' not in call.result:
                continue
            result = BasePageResult.model_validate(call.result)
            urls = {page_key(item.url) for item in result.report.evidence}
            matches = [index for index, page in enumerate(pages) if page_key(page['url']) in urls]
            if len(matches) != 1 or matches[0] in by_page:
                raise SystemExit('Saved model result is ambiguous; manual review needed')
            by_page[matches[0]] = result
        if len(by_page) != len(pages):
            raise SystemExit('Not all saved pages have an unambiguous model result')
        evidence = []
        signals = []
        answers = {}
        losses = set()
        last_limitations = []
        for index in sorted(by_page):
            result = by_page[index]
            checked = verify_report(result.report, [pages[index]], 'website', run.config['social_forum_days'],
                                    as_of=run.config['research_as_of'], company_id=company.id)
            mapping = {item.id: hashlib.sha256((item.url + '\n' + ' '.join(item.quote.split())).encode()).hexdigest()[:24]
                       for item in result.report.evidence}
            rewritten = [item.model_copy(update={'evidence_ids': [mapping.get(eid, eid) for eid in item.evidence_ids]})
                         for item in result.answers]
            fresh = validate_answers(rewritten, criteria, checked['evidence'])
            by_id = {item['criterion_id']: item for item in fresh}
            losses.update(item.criterion_id for item in rewritten
                          if item.status == 'confirmed' and by_id[item.criterion_id]['status'] == 'unknown')
            for item in fresh:
                if item['status'] == 'confirmed' or item['criterion_id'] not in answers:
                    answers[item['criterion_id']] = item
            evidence.extend(checked['evidence'])
            signals.extend(checked['signals'])
            last_limitations = checked['limitations']
        losses.difference_update(cid for cid, answer in answers.items() if answer['status'] == 'confirmed')
        evidence = list({item['id']: item for item in evidence}.values())
        summary = {'resource': args.resource, 'pages': len(pages), 'accepted_evidence': len(evidence),
                   'confirmed_answers': sum(item['status'] == 'confirmed' for item in answers.values()),
                   'citation_losses': sorted(losses), 'apply': args.apply}
        if args.apply:
            backup = backup_sqlite()
            base = {**base, 'evidence': evidence, 'signals': normalized_signals(signals),
                    'answers': list(answers.values()), 'citation_losses': sorted(losses),
                    'limitations': last_limitations}
            company.research = {**company.research, 'base': base}
            company.profile = {**company.profile,
                               'base_research': {**company.profile.get('base_research', {}), 'answers': list(answers.values())},
                               'signals': normalized_signals(signals + criterion_signals(list(answers.values()), evidence))}
            db.commit()
            summary['backup'] = str(backup)
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
