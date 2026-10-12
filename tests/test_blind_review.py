from pathlib import Path
from types import SimpleNamespace
import csv
import json
import pytest
from scripts.prepare_blind_review import source_record, write_packet


def test_source_packet_hides_segment_outputs_and_preserves_review_files(tmp_path):
    company = SimpleNamespace(
        id='company-a', name='Company A', resource='https://example.test',
        context={'websites': ['https://example.test'], 'legal_ids': ['private']},
        research={'base': {
            'pages': [{'url': 'https://example.test', 'title': 'Home', 'text': 'Public product',
                       'content_hash': 'hash', 'retrieved_at': '2026-10-12',
                       'links': ['https://other.test'], 'model_signal': 'private'}],
            'attempted_urls': ['https://example.test'],
            'answers': [{'value': 'do not expose'}],
        }},
        profile={'signals': [{'name': 'do not expose'}]}, group='segment_001',
    )
    record = source_record(company, 'random_core')
    assert set(record) == {'company_id', 'sample_group', 'company_name',
                           'resource_from_base', 'websites_from_base',
                           'attempted_urls', 'saved_pages'}
    assert 'do not expose' not in json.dumps(record)
    assert 'segment_001' not in json.dumps(record)
    assert 'links' not in record['saved_pages'][0]
    folder = tmp_path / 'packet'
    manifest = write_packet(folder, 'run', 'Offer text', [record])
    assert manifest['companies_with_saved_pages'] == 1
    with (folder / 'reviewer_a.csv').open(encoding='utf-8', newline='') as file:
        row = next(csv.DictReader(file))
    assert row['company_id'] == 'company-a' and not row['decision']
    with pytest.raises(FileExistsError):
        write_packet(folder, 'run', 'changed', [record])
    assert (folder / 'offer.md').read_text() == 'Offer text'
