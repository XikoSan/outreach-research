"""Compare two completed blind reviews before revealing model assignments."""
import argparse
from collections import Counter
import csv
from hashlib import sha256
import json
from pathlib import Path

DECISIONS = {'ELIGIBLE', 'NO_FIT', 'UNKNOWN', 'IDENTITY_REVIEW'}


def load_review(path, expected_ids):
    with path.open(encoding='utf-8-sig', newline='') as file:
        rows = list(csv.DictReader(file))
    ids = [row['company_id'] for row in rows]
    if len(ids) != len(set(ids)) or set(ids) != expected_ids:
        raise ValueError(f'{path.name}: company IDs are missing, extra or duplicated')
    invalid = [row['company_id'] for row in rows if row['decision'].strip().upper() not in DECISIONS]
    if invalid:
        raise ValueError(f'{path.name}: {len(invalid)} blank or invalid decisions')
    return {row['company_id']: row for row in rows}


def compare(folder):
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    materials = (folder / 'materials.jsonl').read_bytes()
    if sha256(materials).hexdigest() != manifest['materials_sha256']:
        raise ValueError('Frozen source materials changed')
    if sha256((folder / 'offer.md').read_bytes()).hexdigest() != manifest['offer_sha256']:
        raise ValueError('Frozen sender offer changed')
    source = [json.loads(line) for line in materials.splitlines()]
    ids = {record['company_id'] for record in source}
    if len(ids) != manifest['company_count']:
        raise ValueError('Frozen source company count changed')
    reviews = [load_review(folder / f'reviewer_{label}.csv', ids) for label in ('a', 'b')]
    for record in source:
        company_id = record['company_id']
        if any(review[company_id]['sample_group'] != record['sample_group'] for review in reviews):
            raise ValueError('Reviewer sample group differs from frozen source')
    summary = {}
    disagreements = []
    for group in ('random_core', 'diagnostic'):
        members = [record for record in source if record['sample_group'] == group]
        counts = Counter()
        for record in members:
            company_id = record['company_id']
            a, b = (review[company_id] for review in reviews)
            a_decision, b_decision = (row['decision'].strip().upper() for row in (a, b))
            counts['total'] += 1
            counts['reviewer_a_' + a_decision] += 1
            counts['reviewer_b_' + b_decision] += 1
            if a_decision == b_decision:
                counts['decision_agreements'] += 1
            else:
                disagreements.append({'company_id': company_id,
                                      'sample_group': group,
                                      'reviewer_a_decision': a_decision,
                                      'reviewer_b_decision': b_decision,
                                      'reviewer_a_offer_ids': a['eligible_offer_ids'],
                                      'reviewer_b_offer_ids': b['eligible_offer_ids'],
                                      'reviewer_a_same_message': a['same_message_for_group'],
                                      'reviewer_b_same_message': b['same_message_for_group'],
                                      'adjudicated_decision': '',
                                      'adjudicated_offer_ids': '',
                                      'adjudicated_same_message': '',
                                      'adjudication_reason': ''})
            if a_decision == b_decision == 'ELIGIBLE':
                if (a['eligible_offer_ids'].strip().casefold() == b['eligible_offer_ids'].strip().casefold()
                        and a['same_message_for_group'].strip().casefold() == b['same_message_for_group'].strip().casefold()):
                    counts['eligible_offer_agreements'] += 1
                else:
                    disagreements.append({'company_id': company_id,
                                          'sample_group': group,
                                          'reviewer_a_decision': a_decision,
                                          'reviewer_b_decision': b_decision,
                                          'reviewer_a_offer_ids': a['eligible_offer_ids'],
                                          'reviewer_b_offer_ids': b['eligible_offer_ids'],
                                          'reviewer_a_same_message': a['same_message_for_group'],
                                          'reviewer_b_same_message': b['same_message_for_group'],
                                          'adjudicated_decision': '',
                                          'adjudicated_offer_ids': '',
                                          'adjudicated_same_message': '',
                                          'adjudication_reason': ''})
        summary[group] = {**dict(counts), 'decision_agreements': counts['decision_agreements'],
                          'eligible_offer_agreements': counts['eligible_offer_agreements']}
    return summary, disagreements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    try:
        summary, disagreements = compare(args.folder)
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    if disagreements:
        path = args.folder / 'adjudication.csv'
        if path.exists():
            parser.error('adjudication.csv exists; cannot overwrite it')
        with path.open('w', encoding='utf-8', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=list(disagreements[0]))
            writer.writeheader()
            writer.writerows(disagreements)
    print(json.dumps({'groups': summary, 'disagreements': len(disagreements)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
