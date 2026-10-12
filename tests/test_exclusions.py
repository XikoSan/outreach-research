"""Exclusions require positive evidence or completed source coverage."""
from types import SimpleNamespace

from app.db import Session, Run, Company, PromptPackage
from app.exclusions import automatic_reason, no_current_sources, ceased_operations, database_mismatch
from app.evidence import VERSION
from test_pipeline import new_run


def company(**overrides):
    values = dict(name='Alpha', resource='https://alpha.example',
                  context={'legal_ids': ['1234567890'], 'socials': []},
                  research={}, profile={}, identity='CONFIRMED', stage='PROFILE_READY')
    values.update(overrides)
    return SimpleNamespace(**values)


def run(**config):
    return SimpleNamespace(config={'research_directions': ['website'], **config})


def test_missing_sources_requires_completed_coverage_not_fetch_failure():
    c = company(research={'website': {'state': 'UNAVAILABLE', 'coverage': 'unavailable'}})
    assert not no_current_sources(c, run())
    c.research['website'] = {'state': 'NO_RESULTS', 'coverage': 'checked'}
    assert no_current_sources(c, run())
    c.context['socials'] = ['https://social.example/alpha']
    assert not no_current_sources(c, run())
    c.context['socials'] = []
    assert not no_current_sources(c, run(research_directions=['website', 'jobs']))
    c.research['jobs'] = {'state': 'NO_RESULTS', 'coverage': 'checked'}
    assert no_current_sources(c, run(research_directions=['website', 'jobs']))


def test_campaign_fetch_errors_are_not_no_current_sources():
    c = company(research={'base': {'state': 'COMPLETE', 'termination_reason': 'sources_exhausted',
                                   'attempted_urls': ['https://alpha.example'],
                                   'errors': [{'reason': 'timeout'}], 'pages': []}})
    assert not no_current_sources(c, run(methodology_version='campaign-v1'))


def test_cessation_must_be_current_official_evidence_for_this_company():
    item = {'id': 'e1', 'url': 'https://alpha.example/about',
            'quote': 'Компания Alpha прекратила деятельность в 2025 году.',
            'validation_version': VERSION, 'subject_verified': True,
            'current': True, 'scope': 'company'}
    c = company(research={'website': {'evidence': [item]}})
    assert ceased_operations(c) == [item]
    item['url'] = 'https://other.example/story'
    assert not ceased_operations(c)
    item['url'] = 'https://alpha.example/about'
    item['quote'] = 'Компания Alpha не прекратила деятельность.'
    assert not ceased_operations(c)


def test_mismatch_needs_one_official_tax_id_disagreeing_with_base():
    page = {'url': 'https://alpha.example/contacts', 'text': 'Реквизиты: ИНН 9876543210'}
    c = company(research={'website': {'pages': [page]}})
    assert database_mismatch(c)
    assert automatic_reason(c, run()) is None  # Different INN alone cannot prove an unrelated company.
    page['text'] = 'Реквизиты: ИНН 1234567890, филиал: ИНН 9876543210'
    assert not database_mismatch(c)
    page['url'] = 'https://other.example/contacts'
    page['text'] = 'ИНН 9876543210'
    assert not database_mismatch(c)


def test_manual_exclusion_has_reason_can_be_restored_and_invalidates_old_files(client):
    rid = new_run(client)
    with Session() as db:
        r = db.get(Run, rid)
        r.status = 'COMPLETED'
        c = db.query(Company).filter_by(run_id=rid).first()
        c.identity = 'CONFIRMED'; c.stage = 'PROFILE_READY'; c.group = 'UNCLASSIFIED'
        c.profile = {'signals': []}
        c.context = {**c.context, 'legal_ids': ['1234567890']}
        c.research = {'website': {'state': 'COMPLETE', 'pages': [
            {'url': 'https://alpha.example/contacts', 'text': 'ИНН 9876543210'}]}}
        cid = c.id
        package = PromptPackage(id='package_for_exclusion', run_id=rid, name='Старый комплект',
                                segment_snapshot={'name': 'Сегмент'}, sender_snapshot={},
                                context_snapshot={}, selection={'row_ids': [], 'company_ids': [cid]},
                                recipient_type='company', next_step='Ответить', budget=0,
                                path='/tmp', status='COMPLETED')
        db.add(package); db.commit()
    old_export = client.post(f'/api/runs/{rid}/exports').json()['id']
    conflict = client.get(f'/api/companies/{cid}').json()['legal_id_conflict']
    assert conflict['source_url'] == 'https://alpha.example/contacts'
    assert conflict['base_inn'] == '1234567890' and conflict['site_inn'] == '9876543210'
    response = client.post(f'/api/companies/{cid}/exclusion', json={
        'code': 'DATABASE_MISMATCH', 'note': 'ИНН на странице отличается от базы',
        'source_url': 'https://alpha.example/contacts'})
    assert response.status_code == 200, response.text
    assert response.json()['group'] == 'EXCLUDED'
    assert response.json()['profile']['exclusion']['code'] == 'DATABASE_MISMATCH'
    assert client.get(f'/api/runs/{rid}').json()['excluded'] == 1
    assert client.get(f'/api/runs/{rid}/companies?group=EXCLUDED').json()['total'] == 1
    assert client.get('/api/packages/package_for_exclusion').json()['stale']
    assert client.get('/api/packages/package_for_exclusion/download').status_code == 409
    assert client.get(f'/api/exports/{old_export}/download').status_code == 409
    fresh = client.post(f'/api/runs/{rid}/exports').json()
    assert fresh['manifest']['groups']['EXCLUDED'] == 1
    assert 'statuses/EXCLUDED.xlsx' in fresh['manifest']['files']
    from openpyxl import load_workbook
    from app.db import Export
    with Session() as db:
        export=db.get(Export,fresh['id'])
        wb=load_workbook(export.path+'/statuses/EXCLUDED.xlsx',read_only=True)
        assert wb['Причины исключения']['B2'].value
        assert wb['Причины исключения']['D2'].value=='https://alpha.example/contacts'
        wb.close()
    assert client.delete(f'/api/companies/{cid}/exclusion').status_code == 200
    assert client.get(f'/api/runs/{rid}').json()['excluded'] == 0
    assert client.get(f'/api/exports/{fresh["id"]}/download').status_code == 409


def test_manual_exclusion_rejects_external_supporting_page(client):
    rid = new_run(client)
    with Session() as db:
        run = db.get(Run, rid); run.status = 'COMPLETED'
        c = db.query(Company).filter_by(run_id=rid).first()
        c.identity = 'CONFIRMED'; c.stage = 'PROFILE_READY'; c.group = 'UNCLASSIFIED'
        c.research = {'forums': {'pages': [{'url': 'https://forum.example/thread', 'text': 'Company moved'}]}}
        cid = c.id; db.commit()
    response = client.post(f'/api/companies/{cid}/exclusion', json={
        'code': 'DATABASE_MISMATCH', 'note': 'Данные не относятся к компании',
        'source_url': 'https://forum.example/thread'})
    assert response.status_code == 422


def test_unclassified_and_unavailable_are_not_exclusion_reasons():
    c = company(research={'website': {'state': 'UNAVAILABLE', 'coverage': 'unavailable'}},
                profile={'signals': []})
    assert automatic_reason(c, run()) is None


def test_pipeline_excludes_before_segment_generation_without_model_calls(client):
    from app.pipeline import Pipeline
    from app.db import ModelCall
    rid = new_run(client)
    with Session() as db:
        r = db.get(Run, rid)
        r.config = {**r.config, 'research_directions': ['website']}
        c = db.query(Company).filter_by(run_id=rid).first()
        c.identity = 'CONFIRMED'; c.stage = 'PROFILE_READY'; c.group = 'INSUFFICIENT_DATA'
        c.profile = {'signals': []}
        c.research = {'website': {'state': 'NO_RESULTS', 'coverage': 'checked', 'pages': []}}
        cid = c.id
        db.commit()
    Pipeline(rid).finish(saved_evidence_only=True)
    with Session() as db:
        assert db.get(Company, cid).group == 'EXCLUDED'
        assert db.query(ModelCall).filter_by(run_id=rid).count() == 0
