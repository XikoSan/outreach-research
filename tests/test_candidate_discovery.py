from hashlib import sha256

import pytest

from app.campaigns import normalized_value
from app.candidate_discovery import build_candidate_overview


def fact(criterion, value, evidence_id, *, confidence='HIGH', state='confirmed'):
    digest = sha256(normalized_value(value).encode()).hexdigest()[:16]
    return {
        'name': f'criterion_{criterion}__{digest}',
        'label': value,
        'state': state,
        'confidence': confidence,
        'evidence_ids': [evidence_id],
    }


def evidence(evidence_id, **changes):
    return {
        'id': evidence_id,
        'validation_version': 'evidence-v3',
        'subject_verified': True,
        'current': True,
        'scope': 'company',
        **changes,
    }


def profile(company_id, signals, evidence_items):
    return {'company_id': company_id, 'signals': signals, 'evidence': evidence_items}


def test_shared_pair_found_across_distinct_complete_profiles_and_binds_evidence():
    product_a = fact('products', 'Станки для металлообработки', 'a-product')
    buyer_a = fact('buyer_types', 'Производители косметики', 'a-buyer')
    product_b = fact('products', 'Станки для металлообработки', 'b-product')
    buyer_b = fact('buyer_types', 'Производители косметики', 'b-buyer')
    result = build_candidate_overview([
        profile('a', [product_a, buyer_a, fact('application_scenarios', 'Автоматизация', 'a-extra')],
                [evidence('a-product'), evidence('a-buyer'), evidence('a-extra')]),
        profile('b', [product_b, buyer_b, fact('application_scenarios', 'Контроль качества', 'b-extra')],
                [evidence('b-product'), evidence('b-buyer'), evidence('b-extra')]),
        profile('c', [fact('products', 'Другая продукция', 'c-product')], [evidence('c-product')]),
    ], max_examples=1)
    assert result['summary']['company_count'] == 3
    assert result['summary']['candidate_count'] == 1
    candidate = result['candidates'][0]
    assert candidate['company_ids'] == ['a', 'b']
    assert candidate['example_company_ids'] == ['a']
    assert candidate['two_verified_facts_company_count'] == 2
    assert candidate['distinct_citation_company_count'] == 2
    assert candidate['independent_source_company_count'] == 0
    assert set(candidate['criterion_ids']) == {'products', 'buyer_types'}
    bindings = result['bindings']['candidates'][candidate['id']]
    assert bindings['a'][product_a['name']]['evidence_ids'] == ['a-product']
    assert bindings['b'][buyer_b['name']]['evidence_ids'] == ['b-buyer']
    overview = result['model_overview']['candidates'][0]
    assert overview['raw_signal_names'] == candidate['concept_ids']
    assert overview['company_ids'] == ['a', 'b']
    assert overview['examples'][0]['company_id'] == 'a'
    assert result['model_overview']['semantic_suggestions'] == []


def test_does_not_infer_semantic_equivalence_or_product_buyer_use_relationship():
    left = [fact('products', 'Металлические изделия', 'a1'),
            fact('buyer_types', 'Заводы', 'a2')]
    right = [fact('products', 'Изделия из металла', 'b1'),
             fact('buyer_types', 'Промышленные предприятия', 'b2')]
    result = build_candidate_overview([
        profile('a', left, [evidence('a1'), evidence('a2')]),
        profile('b', right, [evidence('b1'), evidence('b2')]),
    ])
    assert result['candidates'] == []
    assert all(s['requires_review'] and s['criterion_id'] == 'products'
               for s in result['model_overview']['semantic_suggestions'])
    assert not any(c['criterion_id'] == 'product_buyer_use_link' for c in result['concepts'])


def test_ignores_unverified_low_generic_and_mismatched_facts():
    bad = fact('products', 'Продукт А', 'bad')
    low = fact('buyer_types', 'Покупатель', 'low', confidence='LOW')
    generic = {'name': 'criterion_products', 'label': 'Продукты', 'state': 'confirmed',
               'confidence': 'HIGH', 'evidence_ids': ['generic']}
    mismatch = {**fact('products', 'Продукт Б', 'mismatch'), 'label': 'Другой продукт'}
    result = build_candidate_overview([
        profile('a', [bad, low, generic, mismatch],
                [evidence('bad', subject_verified=False), evidence('low'),
                 evidence('generic'), evidence('mismatch')]),
    ])
    assert result['summary']['companies_with_specific_facts'] == 0
    assert result['concepts'] == []
    assert result['candidates'] == []


def test_candidate_is_not_evidence_ready_with_one_distinct_citation_per_company():
    a = [fact('products', 'Косметические наборы', 'a'), fact('buyer_types', 'Салоны красоты', 'a')]
    b = [fact('products', 'Косметические наборы', 'b'), fact('buyer_types', 'Салоны красоты', 'b')]
    result = build_candidate_overview([
        profile('a', a, [evidence('a')]),
        profile('b', b, [evidence('b')]),
    ])
    assert result['summary']['candidate_count'] == 1
    assert result['summary']['two_verified_facts_candidate_count'] == 1
    assert result['summary']['distinct_citation_candidate_count'] == 0
    assert result['summary']['independent_source_candidate_count'] == 0
    assert result['candidates'][0]['two_verified_facts_company_count'] == 2
    assert result['candidates'][0]['distinct_citation_company_count'] == 0
    assert result['candidates'][0]['independent_source_company_count'] == 0


def test_rejects_duplicate_companies_and_invalid_example_limit():
    with pytest.raises(ValueError, match='positive'):
        build_candidate_overview([], max_examples=0)
    with pytest.raises(ValueError, match='Duplicate company_id'):
        build_candidate_overview([profile('a', [], []), profile('a', [], [])])


def test_generic_words_cannot_create_a_shared_audience():
    result = build_candidate_overview([
        profile('a', [fact('buyer_types', 'партнёров', 'a1'),
                      fact('products', 'производство', 'a2')],
                [evidence('a1'), evidence('a2')]),
        profile('b', [fact('buyer_types', 'партнёров', 'b1'),
                      fact('products', 'производство', 'b2')],
                [evidence('b1'), evidence('b2')]),
    ])
    assert result['candidates'] == []
    assert result['summary']['excluded_nondiagnostic_pair_count'] == 1
    assert result['summary']['diagnostic_concept_count'] == 0


def test_lexical_suggestion_is_same_criterion_and_never_adds_membership():
    left = fact('products', 'упаковка', 'a1')
    right = fact('products', 'упаковку', 'b1')
    cross = fact('buyer_types', 'упаковка', 'c1')
    result = build_candidate_overview([
        profile('a', [left], [evidence('a1')]),
        profile('b', [right], [evidence('b1')]),
        profile('c', [cross], [evidence('c1')]),
    ])
    assert result['candidates'] == []
    assert result['summary']['semantic_suggestion_count'] == 1
    suggestion = result['semantic_suggestions'][0]
    assert suggestion['criterion_id'] == 'products'
    assert {suggestion['left_concept_id'], suggestion['right_concept_id']} == {left['name'], right['name']}
    assert suggestion['status'] == 'unverified_retrieval_suggestion'
    assert suggestion['requires_review'] is True
    assert result['bindings']['candidates'] == {}


def test_distinct_citations_are_not_independent_sources():
    left = fact('products', 'Косметические наборы', 'a1')
    right = fact('buyer_types', 'Салоны красоты', 'a2')
    result = build_candidate_overview([
        profile('a', [left, right], [evidence('a1', original_source_id='company:a', quote='Один факт'),
                                     evidence('a2', original_source_id='company:a', quote='Другой факт')]),
        profile('b', [fact('products', 'Косметические наборы', 'b1'),
                      fact('buyer_types', 'Салоны красоты', 'b2')],
                [evidence('b1', original_source_id='company:b', quote='Один факт'),
                 evidence('b2', original_source_id='company:b', quote='Другой факт')]),
    ])
    candidate = result['candidates'][0]
    assert candidate['two_verified_facts_company_count'] == 2
    assert candidate['distinct_citation_company_count'] == 2
    assert candidate['independent_source_company_count'] == 0


def test_shared_generic_equipment_word_does_not_suggest_equivalence():
    result = build_candidate_overview([
        profile('a', [fact('products', 'Парковочное оборудование', 'a1')], [evidence('a1')]),
        profile('b', [fact('products', 'Барное оборудование', 'b1')], [evidence('b1')]),
    ])
    assert result['candidates'] == []
    assert result['semantic_suggestions'] == []


def test_dealer_channel_rule_connects_different_products_without_equating_them():
    a_channel = fact('buyer_types', 'дилерам', 'a-channel')
    b_channel = fact('buyer_types', 'дилеров', 'b-channel')
    a_product = fact('products', 'Кессоны', 'a-product')
    b_product = fact('products', 'Компрессоры', 'b-product')
    result = build_candidate_overview([
        profile('a', [a_channel, a_product], [evidence('a-channel'), evidence('a-product')]),
        profile('b', [b_channel, b_product], [evidence('b-channel'), evidence('b-product')]),
        profile('c', [fact('buyer_types', 'без дилеров', 'c-channel'),
                      fact('products', 'Насосы', 'c-product')],
                [evidence('c-channel'), evidence('c-product')]),
        profile('d', [fact('buyer_types', 'дилерам', 'd-channel'),
                      fact('products', 'производство', 'd-product')],
                [evidence('d-channel'), evidence('d-product')]),
    ])
    assert result['candidates'] == []
    assert result['summary']['rule_candidate_count'] == 1
    rule = result['rule_candidates'][0]
    assert rule['company_ids'] == ['a', 'b']
    assert rule['status'] == 'unreviewed_rule_candidate'
    assert rule['requires_offer_review'] is True
    assert 'не установлены' in rule['claim_limit']
    assert rule['two_verified_facts_company_count'] == 2
    assert rule['distinct_citation_company_count'] == 2
    assert rule['independent_source_company_count'] == 0
    bindings = result['bindings']['rule_candidates'][rule['id']]
    assert bindings['a']['channel']['raw_signal_names'] == [a_channel['name']]
    assert bindings['a']['product']['raw_signal_names'] == [a_product['name']]
    assert bindings['b']['channel']['evidence_ids'] == ['b-channel']
    assert bindings['b']['product']['evidence_ids'] == ['b-product']
    assert result['model_overview']['rule_candidates'][0]['company_ids'] == ['a', 'b']
