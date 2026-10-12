from types import SimpleNamespace

from scripts.create_stratified_pilot import select_companies


def test_stratified_pilot_has_reproducible_random_core_and_separate_diagnostics():
    companies = [SimpleNamespace(id=f'company-{index:03}', resource=f'https://site{index}.ru',
                                 group='NOT_PROCESSED') for index in range(120)]
    companies[1].resource = 'unresolved:one'
    companies[2].resource = 'https://vk.com/official'
    companies[3].group = 'NEEDS_REVIEW'
    companies[4].resource = 'https://foreign.example'
    counts = {company.id: 1 for company in companies}
    for company in companies[5:25]:
        counts[company.id] = 3
    first = select_companies(companies, counts, 50, 171)
    second = select_companies(list(reversed(companies)), counts, 50, 171)
    assert [[item.id for item in part] for part in first] == [[item.id for item in part] for part in second]
    core, hard = first
    assert len(core) == 40 and len(hard) == 10
    assert not {item.id for item in core} & {item.id for item in hard}
    assert any(item.group == 'NEEDS_REVIEW' or item.resource.startswith('unresolved:') or
               item.resource.startswith('https://vk.com/') for item in hard)


def test_stratified_pilot_rejects_oversized_sample():
    companies = [SimpleNamespace(id='one', resource='https://site.ru', group='NOT_PROCESSED')]
    try:
        select_companies(companies, {}, 2, 171)
    except ValueError as error:
        assert 'больше числа компаний' in str(error)
    else:
        raise AssertionError('Oversized pilot was accepted')


def test_frozen_plan_requires_exact_same_offer_document():
    from hashlib import sha256
    import pytest
    from scripts.create_stratified_pilot import frozen_plan_for_source
    from app.campaigns import VERSION

    source = 'Мы исследуем критерии выбора клиентов.'
    plan = {'version': VERSION, 'source_hash': sha256(source.encode()).hexdigest(),
            'base_criteria': [], 'plan': {'offers': [{
                'id': 'one', 'name': 'Исследование', 'facts': [{
                    'kind': 'service', 'text': 'Исследуем критерии выбора',
                    'source_quote': source}]}], 'criteria': []}}
    assert frozen_plan_for_source(plan, source) == plan
    with pytest.raises(ValueError):
        frozen_plan_for_source(plan, source + ' Иное предложение.')


def test_pilot_uses_source_database_filters_instead_of_offer_pilot_size():
    from scripts.create_stratified_pilot import pilot_config

    source = {'source_filters': 'Промышленность, производство'}
    offer = {'source_filters': 'Пилот новой методологии: исходная выборка 25 компаний',
             'models': {'segmentation': 'gpt-6.1-sol'}}
    config = pilot_config(source, offer, '2026-10-12')
    assert config['source_filters'] == source['source_filters']
    assert config['models'] == offer['models']
    assert config['max_companies'] is None
    assert offer['source_filters'].endswith('25 компаний')
    assert pilot_config({}, offer, '2026-10-12')['source_filters'] == ''
