from types import SimpleNamespace

from scripts.audit_segmentation_cost import summarize_calls


def test_cost_audit_uses_only_aggregate_model_call_metrics():
    calls = [
        SimpleNamespace(role='segmentation', model='gpt-6.1-sol', state='COMPLETE', cost=.08,
                        metrics={'input_tokens': 25_000, 'output_tokens': 3_000,
                                 'api_seconds': 18.5, 'gate_wait_seconds': .2,
                                 'gate_estimated_tokens': 45_000,
                                 'raw_usage': {'secret': 'must-not-appear'}}),
        SimpleNamespace(role='segmentation', model='gpt-6.1-sol', state='UNCERTAIN', cost=0,
                        metrics={'api_seconds': 5}),
        SimpleNamespace(role='extraction', model='gpt-6-luna', state='COMPLETE', cost=.01,
                        metrics={'input_tokens': 1000, 'output_tokens': 100}),
    ]
    rows, total = summarize_calls(calls)
    segmentation = next(row for row in rows if row['role'] == 'segmentation')
    assert segmentation['calls'] == 2
    assert segmentation['uncertain_or_started'] == 1
    assert segmentation['estimated_cost_usd'] == .08
    assert segmentation['api_seconds'] == 23.5
    assert total['input_tokens'] == 26_000
    assert total['estimated_cost_usd'] == .09
    assert 'secret' not in str(rows)
