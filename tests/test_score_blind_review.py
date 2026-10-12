from scripts.score_blind_review import score


def test_score_separates_sample_groups_and_other_offers():
    source = [{'company_id': 'a', 'sample_group': 'random_core'},
              {'company_id': 'b', 'sample_group': 'random_core'},
              {'company_id': 'c', 'sample_group': 'diagnostic'}]
    decisions = {'a': {'decision': 'ELIGIBLE', 'offer': 'Развитие бизнеса', 'same_message': 'yes'},
                 'b': {'decision': 'ELIGIBLE', 'offer': 'Маркетинг', 'same_message': 'yes'},
                 'c': {'decision': 'UNKNOWN', 'offer': '', 'same_message': ''}}
    groups = {'a': 'segment_001', 'b': 'UNCLASSIFIED', 'c': 'segment_001'}
    segments = {'segment_001': 'business_development'}
    offers = {'business_development': 'Развитие бизнеса'}
    result = score(source, decisions, groups, segments, offers)
    assert result['random_core']['assigned_matching_selected_offer'] == 1
    assert result['random_core']['unassigned_reference_with_shared_message'] == 1
    assert result['diagnostic']['assigned_different_selected_offer'] == 1
