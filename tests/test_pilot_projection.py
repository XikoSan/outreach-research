"""Offline consistency checks for the aggregate-only pilot projection."""

import csv
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.pilot_projection import (
    BASE_INPUT_TOKENS,
    BASE_OUTPUT_TOKENS,
    LUNA_INPUT_PER_MTOK,
    LUNA_OUTPUT_PER_MTOK,
    estimate,
    elapsed_hours,
    read_baseline,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def aggregates(tmp_path):
    """Minimal aggregate artifacts; portable when private observations are absent."""
    base = tmp_path / 'base.json'
    base.write_text(json.dumps({
        'companies': 14, 'model_calls': 29, 'base_pages': 24,
        'estimated_spent_usd': 0.0539191,
    }), encoding='utf-8')
    progress = tmp_path / 'progress.csv'
    with progress.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            'timestamp_utc', 'total_companies', 'source_rows', 'searches',
            'estimated_usd',
        ])
        writer.writeheader()
        writer.writerow({'timestamp_utc': 'first', 'total_companies': 7675,
                         'source_rows': 9135, 'searches': 1,
                         'estimated_usd': 0.02})
        writer.writerow({'timestamp_utc': 'latest', 'total_companies': 7675,
                         'source_rows': 9135, 'searches': 126,
                         'estimated_usd': 1.78608165})
    stages = tmp_path / 'cost_by_stage.csv'
    with stages.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            'timestamp_utc', 'role', 'searches', 'estimated_usd',
        ])
        writer.writeheader()
        writer.writerow({'timestamp_utc': 'first', 'role': 'discovery',
                         'searches': 1, 'estimated_usd': 0.015})
        writer.writerow({'timestamp_utc': 'latest', 'role': 'discovery',
                         'searches': 126, 'estimated_usd': 1.602667})
    return base, progress, stages


def test_pilot_usage_reconciles_to_recorded_estimate(aggregates):
    baseline = read_baseline(*aggregates)
    from_tokens = (BASE_INPUT_TOKENS * LUNA_INPUT_PER_MTOK +
                   BASE_OUTPUT_TOKENS * LUNA_OUTPUT_PER_MTOK) / 1_000_000
    assert math.isclose(from_tokens, baseline.base_estimate_usd, abs_tol=1e-8)
    assert (baseline.base_companies, baseline.base_calls, baseline.base_pages) == (14, 29, 24)
    assert (baseline.old_source_rows, baseline.old_total_companies,
            baseline.old_searches) == (9135, 7675, 126)


def test_search_tool_floor_is_distinct_from_old_token_mix(aggregates):
    baseline = read_baseline(*aggregates)
    scenario = estimate(baseline, companies=10_000, searches=1000)
    assert scenario['search_tool_floor_usd'] == 10.0
    assert scenario['search_old_mix_usd'] > scenario['search_tool_floor_usd']
    assert scenario['total_with_search_floor_usd'] < scenario['total_with_old_search_mix_usd']
    assert scenario['base_observed_extrapolation_usd'] == scenario['base_tokens_usd']


def test_revised_gate_projection_separates_old_byte_limit_from_measured_tokens(aggregates):
    baseline = read_baseline(*aggregates)
    three = elapsed_hours(baseline, companies=10_000, workers=3)
    eight = elapsed_hours(baseline, companies=10_000, workers=8)
    assert eight['ideal_parallel_hours'] < three['ideal_parallel_hours']
    assert three['historical_byte_gate_hours'] == 63.23
    assert three['measured_token_volume_floor_hours'] == 22.71
    assert three['optimistic_revised_floor_hours'] == three['ideal_parallel_hours']
    assert eight['optimistic_revised_floor_hours'] == eight['measured_token_volume_floor_hours']


def test_source_row_mode_uses_observed_group_ratio(aggregates):
    base, progress, stages = aggregates
    output = subprocess.check_output(
        [sys.executable, str(ROOT / 'scripts/pilot_projection.py'),
         '--base-result', str(base), '--old-progress', str(progress),
         '--old-stages', str(stages), '--source-rows', '9135',
         '--searches', '0', '--workers', '1'],
        cwd=ROOT, text=True,
    )
    data = json.loads(output)
    assert data['assumptions']['companies'] == 7675
    assert data['assumptions']['company_basis'] == 'source_rows_at_observed_ratio'
    assert data['source']['old_source_rows'] == 9135
