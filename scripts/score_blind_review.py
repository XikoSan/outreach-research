"""Reveal machine assignments only after two reviews and adjudication are frozen.

This is a descriptive pilot score, not a production accuracy claim. No model or
web calls are made; the output contains only aggregate counts and file hashes.
"""
import argparse
from collections import Counter
import csv
from hashlib import sha256
import json
from pathlib import Path

from app.db import Company, Run, Session
from scripts.compare_blind_reviews import compare, load_review, DECISIONS


def sha(path):
    return sha256(path.read_bytes()).hexdigest()


def normalized(value):
    return value.strip().casefold()


def consensus(folder):
    _, disagreements = compare(folder)
    source = [json.loads(line) for line in (folder / 'materials.jsonl').read_text(encoding='utf-8').splitlines()]
    expected = {record['company_id'] for record in source}
    a = load_review(folder / 'reviewer_a.csv', expected)
    if disagreements:
        path = folder / 'adjudication.csv'
        with path.open(encoding='utf-8', newline='') as file:
            adjudicated = list(csv.DictReader(file))
        ids = [row['company_id'] for row in adjudicated]
        if len(ids) != len(set(ids)) or set(ids) != {row['company_id'] for row in disagreements}:
            raise ValueError('Adjudication rows differ from blind disagreements')
        decisions = {}
        for row in adjudicated:
            decision = row['adjudicated_decision'].strip().upper()
            if decision not in DECISIONS or not row['adjudication_reason'].strip():
                raise ValueError('Adjudication is incomplete')
            if decision == 'ELIGIBLE' and (not row['adjudicated_offer_ids'].strip()
                                           or normalized(row['adjudicated_same_message']) not in ('yes', 'no')):
                raise ValueError('Eligible adjudication needs offer and shared-message decision')
            decisions[row['company_id']] = {
                'decision': decision,
                'offer': row['adjudicated_offer_ids'].strip(),
                'same_message': normalized(row['adjudicated_same_message']),
            }
    else:
        decisions = {}
    for record in source:
        company_id = record['company_id']
        if company_id not in decisions:
            row = a[company_id]
            decisions[company_id] = {'decision': row['decision'].strip().upper(),
                                     'offer': row['eligible_offer_ids'].strip(),
                                     'same_message': normalized(row['same_message_for_group'])}
    return source, decisions, len(disagreements)


def score(source, decisions, machine_groups, segment_offers, offer_names):
    by_group = {}
    for group in ('random_core', 'diagnostic'):
        counts = Counter()
        for record in source:
            if record['sample_group'] != group:
                continue
            company_id = record['company_id']
            label = decisions[company_id]
            decision = label['decision']
            counts['total'] += 1
            counts['reference_' + decision] += 1
            if decision == 'ELIGIBLE' and label['same_message'] == 'yes':
                counts['reference_with_shared_message'] += 1
            assigned = machine_groups[company_id] in segment_offers
            if assigned:
                counts['machine_assigned'] += 1
                expected_offer = segment_offers[machine_groups[company_id]]
                if (decision == 'ELIGIBLE' and label['same_message'] == 'yes'
                        and normalized(label['offer']) == normalized(offer_names[expected_offer])):
                    counts['assigned_matching_selected_offer'] += 1
                else:
                    counts['assigned_different_selected_offer'] += 1
            elif decision == 'ELIGIBLE' and label['same_message'] == 'yes':
                counts['unassigned_reference_with_shared_message'] += 1
        by_group[group] = dict(counts)
    return by_group


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    folder = args.folder
    try:
        source, decisions, disputed = consensus(folder)
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        with Session() as db:
            run = db.get(Run, manifest['run_id'])
            if not run:
                raise ValueError('Pilot run not found')
            companies = db.query(Company).filter_by(run_id=run.id).all()
            machine_groups = {company.id: company.group for company in companies}
            if set(machine_groups) != set(decisions):
                raise ValueError('Pilot company IDs changed')
            segment_offers = {segment['id']: segment['offer_id'] for segment in run.segments}
            offer_names = {offer['id']: offer['name'] for offer in run.reports['campaign_plan']['plan']['offers']}
            result = {'run_id': run.id, 'segmentation_version': run.segmentation_version,
                      'adjudicated_disagreements': disputed,
                      'groups': score(source, decisions, machine_groups, segment_offers, offer_names),
                      'sha256': {name: sha(folder / name) for name in (
                          'materials.jsonl', 'offer.md', 'reviewer_a.csv', 'reviewer_b.csv')},
                      'machine_snapshot_sha256': sha256(json.dumps({
                          'segments': run.segments, 'company_groups': machine_groups},
                          ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                      'interpretation': 'The blind review selected one preferred offer per company, not every valid alternative. A different selected offer is not a false positive; unassigned eligible companies are not validated false negatives until a shared campaign and membership are checked.'}
            if disputed:
                result['sha256']['adjudication.csv'] = sha(folder / 'adjudication.csv')
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
