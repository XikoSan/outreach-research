import csv
import pytest
from scripts.compare_blind_reviews import compare
from scripts.prepare_blind_review import write_packet


def test_compare_requires_complete_reviews_and_keeps_machine_outputs_hidden(tmp_path):
    folder = tmp_path / 'packet'
    records = [
        {'company_id': 'a', 'sample_group': 'random_core', 'saved_pages': []},
        {'company_id': 'b', 'sample_group': 'diagnostic', 'saved_pages': []},
    ]
    write_packet(folder, 'run', 'offer', records)
    with pytest.raises(ValueError, match='invalid decisions'):
        compare(folder)
    for reviewer, decisions in [('a', ['UNKNOWN', 'ELIGIBLE']),
                                ('b', ['UNKNOWN', 'NO_FIT'])]:
        path = folder / f'reviewer_{reviewer}.csv'
        with path.open(encoding='utf-8', newline='') as file:
            rows = list(csv.DictReader(file))
        for row, decision in zip(rows, decisions):
            row['decision'] = decision
        with path.open('w', encoding='utf-8', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    summary, disagreements = compare(folder)
    assert summary['random_core']['decision_agreements'] == 1
    assert summary['diagnostic']['decision_agreements'] == 0
    assert disagreements[0]['company_id'] == 'b'
    (folder / 'materials.jsonl').write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='materials changed'):
        compare(folder)
