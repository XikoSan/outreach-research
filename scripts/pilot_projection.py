"""Offline, aggregate-only cost and throughput projection for a research pilot.

No database, API, secrets, company names, sites, or contact rows are read.
Historical rates are scenario inputs, not a quote for future billing.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_RESULT = ROOT / 'data/observations/951f54de3d6e4b29abf280b6bb35b777/base-only-result.json'
OLD_PROGRESS = ROOT / 'data/observations/3b3d3bef1c7d43349f571df511875b09/progress.csv'
OLD_STAGES = OLD_PROGRESS.with_name('cost_by_stage.csv')

# Usage and elapsed time reported in docs/base-collection-pilot-2026-10-11.md.
# The adjacent base-only-result.json records calls, companies and estimated cost,
# but not aggregate tokens or the total of all three batch durations.
BASE_INPUT_TOKENS = 222_821
BASE_OUTPUT_TOKENS = 63_274
BASE_ELAPSED_SECONDS = 558.8 + 12.0

# Standard API rates per million tokens (not Fast/Batch), 2026-10-12.
# https://developers.openai.com/api/docs/pricing
LUNA_INPUT_PER_MTOK = 0.10
LUNA_OUTPUT_PER_MTOK = 0.50
SOL_INPUT_PER_MTOK = 2.00
SOL_OUTPUT_PER_MTOK = 10.00
WEB_SEARCH_PER_CALL = 0.01

# Historical gate before token-aware admission; retained for comparison with
# docs/throughput-architecture-review.md, not as the current runtime forecast.
LOCAL_GATE_RPM = 30
LOCAL_GATE_UNITS_PER_MIN = 150_000
OBSERVED_GATE_UNITS_PER_COMPANY = 56_909


@dataclass(frozen=True)
class Baseline:
    base_companies: int
    base_calls: int
    base_pages: int
    base_estimate_usd: float
    old_total_companies: int
    old_source_rows: int
    old_searches: int
    old_discovery_estimate_usd: float
    old_total_estimate_usd: float


def read_baseline(base_result: Path = BASE_RESULT,
                  progress: Path = OLD_PROGRESS,
                  stages: Path = OLD_STAGES) -> Baseline:
    base = json.loads(base_result.read_text(encoding='utf-8'))
    with progress.open(encoding='utf-8-sig', newline='') as handle:
        progress_rows = list(csv.DictReader(handle))
    with stages.open(encoding='utf-8-sig', newline='') as handle:
        stage_rows = list(csv.DictReader(handle))
    if not progress_rows:
        raise ValueError('Old progress CSV has no observations')
    latest = progress_rows[-1]
    discovery = [row for row in stage_rows
                 if row['timestamp_utc'] == latest['timestamp_utc']
                 and row['role'] == 'discovery']
    if len(discovery) != 1:
        raise ValueError('Expected one discovery aggregate at latest progress timestamp')
    values = Baseline(
        base_companies=int(base['companies']),
        base_calls=int(base['model_calls']),
        base_pages=int(base['base_pages']),
        base_estimate_usd=float(base['estimated_spent_usd']),
        old_total_companies=int(latest['total_companies']),
        old_source_rows=int(latest['source_rows']),
        old_searches=int(latest['searches']),
        old_discovery_estimate_usd=float(discovery[0]['estimated_usd']),
        old_total_estimate_usd=float(latest['estimated_usd']),
    )
    if min(values.base_companies, values.base_calls, values.old_total_companies,
           values.old_source_rows, values.old_searches) <= 0:
        raise ValueError('Baselines must contain positive denominators')
    if int(discovery[0]['searches']) != values.old_searches:
        raise ValueError('Latest stage and progress search counts disagree')
    return values


def estimate(baseline: Baseline, *, companies: int, searches: int,
             review_calls: int = 1_000, review_input_tokens: int = 2_000,
             review_output_tokens: int = 500, overhead_rate: float = 0.15,
             input_factor: float = 1.0, output_factor: float = 1.0,
             calls_factor: float = 1.0) -> dict:
    """Compute two search-cost alternatives; neither is a billing prediction."""
    if companies < 1 or searches < 0 or review_calls < 0:
        raise ValueError('Companies must be positive; calls/searches cannot be negative')
    if min(review_input_tokens, review_output_tokens) < 0:
        raise ValueError('Token counts cannot be negative')
    if overhead_rate < 0 or min(input_factor, output_factor, calls_factor) <= 0:
        raise ValueError('Overhead must be nonnegative and base factors positive')
    ratio = companies / baseline.base_companies
    base_calls = baseline.base_calls * ratio * calls_factor
    base_input = BASE_INPUT_TOKENS * ratio * input_factor
    base_output = BASE_OUTPUT_TOKENS * ratio * output_factor
    base_tokens_usd = (base_input * LUNA_INPUT_PER_MTOK +
                       base_output * LUNA_OUTPUT_PER_MTOK) / 1_000_000
    base_observed_usd = baseline.base_estimate_usd * ratio
    review_usd = review_calls * (review_input_tokens * SOL_INPUT_PER_MTOK +
                                 review_output_tokens * SOL_OUTPUT_PER_MTOK) / 1_000_000
    search_floor_usd = searches * WEB_SEARCH_PER_CALL
    historical_search_usd = searches * (baseline.old_discovery_estimate_usd /
                                        baseline.old_searches)
    # Overhead is an explicit sensitivity allowance for retries/repairs. It is
    # applied to model tokens and review, not to already-counted search tool fees.
    model_with_overhead = (base_tokens_usd + review_usd) * (1 + overhead_rate)
    return {
        'companies': companies,
        'searches': searches,
        'base_calls_estimate': round(base_calls, 1),
        'base_input_tokens_estimate': round(base_input),
        'base_output_tokens_estimate': round(base_output),
        'base_tokens_usd': round(base_tokens_usd, 4),
        'base_observed_extrapolation_usd': round(base_observed_usd, 4),
        'review_calls': review_calls,
        'review_usd': round(review_usd, 4),
        'overhead_rate': overhead_rate,
        'search_tool_floor_usd': round(search_floor_usd, 4),
        'search_old_mix_usd': round(historical_search_usd, 4),
        'total_with_search_floor_usd': round(model_with_overhead + search_floor_usd, 4),
        'total_with_old_search_mix_usd': round(model_with_overhead + historical_search_usd, 4),
    }


def elapsed_hours(baseline: Baseline, *, companies: int, workers: int,
                  calls_factor: float = 1.0, gate_units_factor: float = 1.0) -> dict:
    """Optimistic lower bounds from sequential pilot and measured usage.

    The former byte-based gate is historical context. The revised gate reserves
    estimated tokens and settles with usage; its exact impact is not measured.
    """
    if companies < 1 or workers < 1 or calls_factor <= 0 or gate_units_factor <= 0:
        raise ValueError('Companies, workers, and factors must be positive')
    ideal_parallel = companies * (BASE_ELAPSED_SECONDS / baseline.base_companies)
    ideal_parallel *= calls_factor / workers / 3600
    gate_units = companies * OBSERVED_GATE_UNITS_PER_COMPANY * gate_units_factor
    old_gate_hours = gate_units / LOCAL_GATE_UNITS_PER_MIN / 60
    measured_token_hours = companies * (BASE_INPUT_TOKENS + BASE_OUTPUT_TOKENS) / baseline.base_companies / LOCAL_GATE_UNITS_PER_MIN / 60
    gate_rpm_hours = (companies * baseline.base_calls / baseline.base_companies *
                      calls_factor / LOCAL_GATE_RPM / 60)
    return {
        'workers': workers,
        'ideal_parallel_hours': round(ideal_parallel, 2),
        'historical_byte_gate_hours': round(old_gate_hours, 2),
        'measured_token_volume_floor_hours': round(measured_token_hours, 2),
        'local_gate_rpm_hours': round(gate_rpm_hours, 2),
        'optimistic_revised_floor_hours': round(max(ideal_parallel, measured_token_hours,
                                                   gate_rpm_hours), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    volume = parser.add_mutually_exclusive_group()
    volume.add_argument('--companies', type=int,
                        help='Unique companies (default: 10000)')
    volume.add_argument('--source-rows', type=int,
                        help='Contact rows; infer company count at the observed ratio')
    parser.add_argument('--base-result', type=Path, default=BASE_RESULT)
    parser.add_argument('--old-progress', type=Path, default=OLD_PROGRESS)
    parser.add_argument('--old-stages', type=Path, default=OLD_STAGES)
    parser.add_argument('--searches', type=int, nargs='+', default=[0, 500, 1_000, 2_000])
    parser.add_argument('--workers', type=int, nargs='+', default=[1, 3, 4, 8])
    parser.add_argument('--review-calls', type=int, default=1_000)
    parser.add_argument('--review-input-tokens', type=int, default=2_000)
    parser.add_argument('--review-output-tokens', type=int, default=500)
    parser.add_argument('--overhead-rate', type=float, default=0.15)
    parser.add_argument('--base-input-factor', type=float, default=1.0)
    parser.add_argument('--base-output-factor', type=float, default=1.0)
    parser.add_argument('--base-calls-factor', type=float, default=1.0)
    parser.add_argument('--gate-units-factor', type=float, default=1.0)
    args = parser.parse_args()
    baseline = read_baseline(args.base_result, args.old_progress, args.old_stages)
    if args.source_rows is not None:
        if args.source_rows < 1:
            parser.error('--source-rows must be positive')
        companies = round(args.source_rows * baseline.old_total_companies /
                          baseline.old_source_rows)
        company_basis = 'source_rows_at_observed_ratio'
    else:
        companies = args.companies if args.companies is not None else 10_000
        company_basis = 'unique_companies'
    payload = {
        'source': {
            'base_companies': baseline.base_companies,
            'base_calls': baseline.base_calls,
            'base_pages': baseline.base_pages,
            'base_estimated_usd': baseline.base_estimate_usd,
            'base_input_tokens_documented': BASE_INPUT_TOKENS,
            'base_output_tokens_documented': BASE_OUTPUT_TOKENS,
            'old_source_rows': baseline.old_source_rows,
            'old_unique_companies': baseline.old_total_companies,
            'old_searches': baseline.old_searches,
            'old_discovery_estimated_usd': baseline.old_discovery_estimate_usd,
            'old_total_estimated_usd': baseline.old_total_estimate_usd,
        },
        'assumptions': {
            'companies': companies,
            'company_basis': company_basis,
            'source_rows': args.source_rows,
            'review_calls': args.review_calls,
            'review_input_tokens': args.review_input_tokens,
            'review_output_tokens': args.review_output_tokens,
            'overhead_rate': args.overhead_rate,
            'base_input_factor': args.base_input_factor,
            'base_output_factor': args.base_output_factor,
            'base_calls_factor': args.base_calls_factor,
            'gate_units_factor': args.gate_units_factor,
            'web_search_price_usd': WEB_SEARCH_PER_CALL,
        },
        'cost_scenarios': [estimate(
            baseline, companies=companies, searches=searches,
            review_calls=args.review_calls,
            review_input_tokens=args.review_input_tokens,
            review_output_tokens=args.review_output_tokens,
            overhead_rate=args.overhead_rate,
            input_factor=args.base_input_factor,
            output_factor=args.base_output_factor,
            calls_factor=args.base_calls_factor,
        ) for searches in args.searches],
        'time_scenarios': [elapsed_hours(
            baseline, companies=companies, workers=workers,
            calls_factor=args.base_calls_factor,
            gate_units_factor=args.gate_units_factor,
        ) for workers in args.workers],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
