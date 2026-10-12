from types import SimpleNamespace

from scripts.run_base_pilot import eligible_company_ids


def test_uncertain_api_company_is_never_automatically_retried():
    companies = [
        SimpleNamespace(id='done', research={'base': {'state': 'COMPLETE'}}),
        SimpleNamespace(id='uncertain', research={'base': {'pages': []}}),
        SimpleNamespace(id='pending', research={}),
    ]
    assert eligible_company_ids(
        ['done', 'uncertain', 'pending'], companies, {'uncertain'}
    ) == ['pending']
