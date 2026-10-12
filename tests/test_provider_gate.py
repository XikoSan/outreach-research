"""Offline timing, admission and accounting checks for the API gateway."""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import httpx
from openai import APIStatusError

from app.db import ModelCall, Run, Session
from app.llm import Gateway, UncertainCall
from app.providers import ProviderGate, estimate_request_tokens
from app.schemas import Review
from test_pipeline import gateway, new_run


def test_token_estimate_discounts_utf8_bytes_but_preserves_headroom():
    russian = 'я' * 1000
    estimate = estimate_request_tokens('', russian, '', 100)
    assert 1000 + 100 + 512 <= estimate < len(russian.encode()) + 100 + 512
    assert estimate_request_tokens('', 'x' * 1000, '', 100) >= 1000 + 100 + 512
    assert estimate_request_tokens('', russian, '', 100, search=True) == estimate + 24_000


def test_gate_settles_actual_usage_and_respects_reset_headers():
    gate = ProviderGate(rpm=2, tpm=1000)
    ticket = gate.wait(100, lambda: None)
    gate.settle(ticket, 250)
    assert list(gate.events)[0][1] == 250
    gate.rejected({'retry-after': '2', 'x-ratelimit-reset-tokens': '1m30s'})
    assert gate.cooldown - time.monotonic() >= 89
    gate.rejected({'retry-after': '120'})
    assert gate.cooldown - time.monotonic() >= 119



def test_rejected_429_releases_token_reservation_but_keeps_request(client, monkeypatch):
    rid = new_run(client)
    g = gateway(rid)
    g.profile_credentials = True
    g.current_key = 'fake'
    monkeypatch.setattr('app.llm.credentials.for_run', lambda run: 'fake')
    gate = ProviderGate(rpm=30, tpm=1000)
    monkeypatch.setattr('app.llm.API_GATE', gate)
    response = httpx.Response(429, headers={'retry-after': '1'}, request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))
    g.client.responses.create.side_effect = APIStatusError('rate limit', response=response, body=None)

    with pytest.raises(APIStatusError):
        g.ask('synthesis', 'check', {}, Review)

    assert len(gate.events) == 1
    assert gate.events[0][1] == 0
    assert gate.cooldown > time.monotonic()


def test_gateway_records_gate_and_api_durations_without_request_text(client, monkeypatch):
    rid = new_run(client)
    g = gateway(rid)
    g.profile_credentials = True
    g.current_key = 'fake'
    monkeypatch.setattr('app.llm.credentials.for_run', lambda run: 'fake')

    class GateSpy:
        def __init__(self):
            self.estimated = None
            self.actual = None

        def wait(self, tokens, check):
            check()
            self.estimated = tokens
            time.sleep(.01)
            return [time.monotonic(), tokens]

        def settle(self, ticket, actual):
            self.actual = actual

    spy = GateSpy()
    monkeypatch.setattr('app.llm.API_GATE', spy)
    response = g.client.responses.create.return_value

    def delayed_response(**kwargs):
        time.sleep(.01)
        return response

    g.client.responses.create.side_effect = delayed_response
    assert g.ask('synthesis', 'Проверь короткий отчёт', {}, Review).passed

    with Session() as db:
        metrics = db.query(ModelCall).filter_by(run_id=rid).one().metrics
        assert metrics['gate_wait_seconds'] >= .01
        assert metrics['api_seconds'] >= .01
        assert metrics['gate_estimated_tokens'] == spy.estimated
        assert spy.actual == 150
        assert 'Проверь короткий отчёт' not in str(metrics)
        assert 'fake' not in str(metrics)


def test_unknown_result_preserves_safe_timing_and_reservation(client):
    rid = new_run(client)
    g = gateway(rid)
    g.client.responses.create.return_value.usage = None
    with pytest.raises(UncertainCall):
        g.ask('synthesis', 'check', {}, Review)
    with Session() as db:
        call = db.query(ModelCall).filter_by(run_id=rid).one()
        run = db.get(Run, rid)
        assert call.state == 'UNCERTAIN'
        assert call.metrics['error_type'] == 'MissingUsage'
        assert call.metrics['api_seconds'] >= 0
        assert run.reserved > 0


def test_concurrent_gateway_calls_add_cost_atomically(client):
    rid = new_run(client)
    g = gateway(rid)
    response = g.client.responses.create.return_value

    def delayed_response(**kwargs):
        time.sleep(.01)
        return response

    g.client.responses.create.side_effect = delayed_response

    def call(index):
        return g.ask('synthesis', f'check {index}', {}, Review).passed

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert all(pool.map(call, range(8)))
    with Session() as db:
        calls = db.query(ModelCall).filter_by(run_id=rid).all()
        run = db.get(Run, rid)
        assert len(calls) == 8
        assert run.spent == pytest.approx(sum(call.cost for call in calls))
        assert run.reserved == pytest.approx(0)
