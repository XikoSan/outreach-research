"""Freeze source-only materials for independent, offline segmentation review.

No model calls, web requests, contact rows, extracted answers or assigned segments.
The saved public page text can itself contain incidental personal information; keep
this packet local. Existing reviewer files are never overwritten.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from app.db import Company, Run, Session


COLUMNS = (
    'company_id', 'sample_group', 'identity_ok', 'source_status',
    'products', 'buyer_types', 'application_scenarios', 'confirmed_links',
    'source_quotes_and_urls', 'counter_evidence', 'eligible_offer_ids',
    'basis_type', 'shared_situation', 'forbidden_claims',
    'same_message_for_group', 'decision', 'notes',
)


def source_record(company, sample_group):
    base = company.research.get('base', {})
    pages = [
        {key: page.get(key) for key in ('url', 'title', 'text', 'retrieved_at',
                                        'content_hash', 'truncated')}
        for page in base.get('pages', [])
    ]
    return {
        'company_id': company.id,
        'sample_group': sample_group,
        'company_name': company.name,
        'resource_from_base': company.resource,
        'websites_from_base': company.context.get('websites', []),
        'attempted_urls': base.get('attempted_urls', []),
        'saved_pages': pages,
    }


def prepare(run, companies):
    if run.company_count != 100 or len(companies) != 100:
        raise ValueError('Expected the frozen 100-company pilot')
    groups = run.reports.get('pilot', {}).get('imported_groups', {})
    if {key: len(groups.get(key, [])) for key in ('random_core', 'diagnostic')} != {
        'random_core': 80, 'diagnostic': 20,
    }:
        raise ValueError('Expected the 80+20 pilot split')
    labels = {company_id: label for label in ('random_core', 'diagnostic')
              for company_id in groups[label]}
    if set(labels) != {company.id for company in companies}:
        raise ValueError('Pilot split does not match company IDs')
    offer = run.context_snapshot.get('source_text') or run.context_snapshot.get('summary')
    if not offer:
        raise ValueError('No frozen sender offer text')
    records = [source_record(company, labels[company.id])
               for company in sorted(companies, key=lambda item: item.id)]
    return offer, records


def write_packet(folder, run_id, offer, records):
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError('Review folder is not empty; annotations must not be overwritten')
    folder.mkdir(parents=True, exist_ok=True)
    materials = ''.join(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n'
                        for record in records)
    (folder / 'offer.md').write_text(offer, encoding='utf-8')
    (folder / 'materials.jsonl').write_text(materials, encoding='utf-8')
    for reviewer in ('a', 'b'):
        with (folder / f'reviewer_{reviewer}.csv').open('w', encoding='utf-8', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=COLUMNS)
            writer.writeheader()
            for record in records:
                writer.writerow({'company_id': record['company_id'],
                                 'sample_group': record['sample_group']})
    manifest = {
        'run_id': run_id,
        'company_count': len(records),
        'random_core': sum(record['sample_group'] == 'random_core' for record in records),
        'diagnostic': sum(record['sample_group'] == 'diagnostic' for record in records),
        'companies_with_saved_pages': sum(bool(record['saved_pages']) for record in records),
        'offer_sha256': hashlib.sha256(offer.encode()).hexdigest(),
        'materials_sha256': hashlib.sha256(materials.encode()).hexdigest(),
        'excluded': ['contacts', 'research_answers', 'model_signals',
                     'candidate_aliases', 'segment_assignments', 'prompt_text'],
    }
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                                           encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if len(args.run_id) != 32 or any(c not in '0123456789abcdef' for c in args.run_id):
        parser.error('run_id must be 32 lowercase hex characters')
    with Session() as db:
        run = db.get(Run, args.run_id)
        if not run:
            parser.error('Run not found')
        companies = db.query(Company).filter_by(run_id=args.run_id).all()
        try:
            offer, records = prepare(run, companies)
        except ValueError as exc:
            parser.error(str(exc))
    folder = args.output or Path('data/observations') / args.run_id / 'blind-review'
    try:
        manifest = write_packet(folder, args.run_id, offer, records)
    except FileExistsError as exc:
        parser.error(str(exc))
    print(json.dumps({'folder': str(folder.resolve()), **{
        key: manifest[key] for key in ('company_count', 'random_core', 'diagnostic',
                                       'companies_with_saved_pages', 'materials_sha256')
    }}, ensure_ascii=False))


if __name__ == '__main__':
    main()
