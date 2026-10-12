"""Offline integration tests: fake only network/model boundaries, real DB/workbooks."""
import io,json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock
import pytest
from openpyxl import Workbook
from app.db import Session,Run,Company,ModelCall,Export,Base,engine
from app.schemas import Discovery,ResearchReport,Profile,Segments,Review,PromptResult
from app.pipeline import Pipeline
from app.llm import Gateway,BudgetPause,UncertainCall,SearchLimit
from app.config import settings


def new_run(client,budget=5,pilot=None):
    w=Workbook();w.active.append(['Название компании','Ссылка на сайт','Имя'])
    w.active.append(['Alpha','https://alpha.example','Анна']);w.active.append(['Beta','https://beta.example','Борис'])
    b=io.BytesIO();w.save(b)
    sender=client.get('/api/senders').json()[0]['id']
    r=client.post('/api/runs',data={'sender_id':sender,'budget':budget,'config_json':json.dumps({'max_companies':pilot,'max_retries':0})},files={'file':('control.xlsx',b.getvalue())}).json()
    with Session() as db:run=db.get(Run,r['id']);run.status='RUNNING';db.commit()
    return r['id']

class FakeGateway:
    def __init__(self):self.roles=[]
    def ask(self,role,instructions,payload,schema,**kwargs):
        self.roles.append(role)
        if schema is Discovery:
            d=payload['direction'];host=payload['resource']
            sources=[{'url':host,'direction':'website','rationale':'Official'}] if d=='website' else ([{'url':'https://industry.example/jobs/'+payload['company'],'direction':'jobs','rationale':'Published vacancy'}] if d=='jobs' else [])
            return Discovery(identity_confirmed=True,identity_reason='Exact company identity',products='Products',sources=sources)
        if schema is ResearchReport:
            page=payload['pages'][0];name='multiple_product_lines' if payload['direction']=='website' else 'new_market_entry'
            return ResearchReport(summary='Observed',evidence=[{'id':'e','url':page['url'],'quote':page['text'],'published_at':'2026-10-01','scope':'company','origin':page['url']}],signals=[{'name':name,'state':'confirmed','confidence':'HIGH','evidence_ids':['e'],'explanation':'Direct source'}],contradictions=[],limitations=[])
        if schema is Profile:
            signals=[s for r in payload['reports'].values() for s in r['signals']]
            return Profile(description='Company',products='Products',audiences='B2B',signals=signals,contradictions=[],limitations=[])
        if schema is Segments:
            return Segments(segments=[{'id':'candidate','name':'Новая аудитория','definition':'Two observed signals','offer_id':payload['offers'][0]['id'],'hypothesis':'Может быть актуальна адаптация позиционирования','confidence':'MEDIUM','required_signals':['multiple_product_lines'],'supporting_signals':['new_market_entry'],'contradicting_signals':[],'outreach_angle':'Adapt communication','diagnostic_question':'Актуально ли?','allowed_claims':[],'forbidden_claims':['Гарантированный рост']}])
        if schema is Review:return Review(passed=True,issues=[])
        if schema is PromptResult:return PromptResult(text='INPUT_ROW_DATA\n'+payload.get('sender',{}).get('representative','Артём Кучеренко')+'\nТема письма\nПисьмо 1\nПисьмо 2\nПисьмо 3\nПисьмо 4\nГенерационный промпт для теста, не реальные письма.')
        raise AssertionError(schema)

def fake_fetch(url):return {'url':url,'title':'Control','text':'This company offers multiple product lines.' if 'industry' not in url else 'The company is entering a new B2B market.','truncated':False}

def test_research_stops_before_prompt_and_resume_reuses_profiles(client,monkeypatch):
    rid=new_run(client);monkeypatch.setattr('app.pipeline.fetch',fake_fetch);g=FakeGateway();Pipeline(rid,g).execute()
    with Session() as db:
        r=db.get(Run,rid);assert r.status=='COMPLETED';assert r.stage=='EXPORTED';assert r.segmentation_version==1
        assert {c.group for c in db.query(Company).filter_by(run_id=rid)}=={'segment_001'}
        exp=db.query(Export).filter_by(run_id=rid).one();assert exp.manifest['rows']==2 and not exp.manifest['partial']
        assert 'databases/segment_001.xlsx' in exp.manifest['files']
        assert not any(f.endswith('.md') for f in exp.manifest['files'])
        assert 'prompt' not in g.roles
        r.status='RUNNING';db.commit()
    g.roles.clear();Pipeline(rid,g).execute();assert g.roles==[]

def test_pilot_does_not_lose_unprocessed_rows(client,monkeypatch):
    rid=new_run(client,pilot=1);monkeypatch.setattr('app.pipeline.fetch',fake_fetch);Pipeline(rid,FakeGateway()).execute()
    with Session() as db:
        e=db.query(Export).filter_by(run_id=rid).one();assert e.manifest['rows']==2;assert e.manifest['partial'];assert e.manifest['groups']['NOT_PROCESSED']==1

def test_page_checkpoint_survives_extraction_failure(client,monkeypatch):
    rid=new_run(client);fetcher=Mock(side_effect=fake_fetch);monkeypatch.setattr('app.pipeline.fetch',fetcher)
    g=FakeGateway();original=g.ask
    def fail(*args,**kwargs):
        if args[0]=='extraction':raise ValueError('stop')
        return original(*args,**kwargs)
    g.ask=fail
    with Session() as db:cid=db.query(Company).filter_by(run_id=rid).first().id
    with pytest.raises(ValueError):Pipeline(rid,g).research(cid)
    assert fetcher.call_count==1
    g.ask=original;Pipeline(rid,g).research(cid)
    # Website is reused; only the vacancy page is fetched on resume.
    assert fetcher.call_count==2

def gateway(rid,mode='ok'):
    g=object.__new__(Gateway);g.run_id=rid
    response=SimpleNamespace(id='response_test',usage=SimpleNamespace(input_tokens=100,output_tokens=50),output=[],output_text='{"passed":true,"issues":[]}')
    g.client=SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=response)))
    if mode=='timeout':g.client.responses.create.side_effect=TimeoutError('transport lost')
    return g

def test_paid_response_is_reused_without_double_billing(client):
    rid=new_run(client);g=gateway(rid)
    first=g.ask('synthesis','check',{},Review);second=g.ask('synthesis','check',{},Review)
    assert first==second;assert g.client.responses.create.call_count==1
    with Session() as db:
        r=db.get(Run,rid);assert r.spent>0;assert r.reserved==pytest.approx(0);assert db.query(ModelCall).count()==1

def test_legacy_budget_does_not_stop_call(client):
    rid=new_run(client,budget=.00001);g=gateway(rid)
    g.ask('synthesis','check',{},Review)
    assert g.client.responses.create.call_count==1
    with Session() as db:assert db.get(Run,rid).status=='RUNNING'

def test_unknown_transport_is_not_automatically_repeated(client):
    rid=new_run(client);g=gateway(rid,'timeout')
    with pytest.raises(UncertainCall):g.ask('synthesis','check',{},Review)
    with pytest.raises(UncertainCall):g.ask('synthesis','check',{},Review)
    assert g.client.responses.create.call_count==1
    with Session() as db:
        r=db.get(Run,rid);assert r.reserved>0;r.status='PAUSED';cid=db.query(ModelCall).one().id;db.commit()
    resp=client.post('/api/calls/'+cid+'/reconcile',json={'verified_cost':.001,'note':'Сверено в кабинете API'})
    assert resp.status_code==200
    with Session() as db:
        r=db.get(Run,rid);assert r.reserved==pytest.approx(0);assert r.spent==pytest.approx(.001)


def test_missing_identity_stays_review_on_retry(client):
    w=Workbook();w.active.append(['Название компании','Ссылка на сайт']);w.active.append(['Unknown',None])
    b=io.BytesIO();w.save(b)
    sender=client.get('/api/senders').json()[0]['id']
    response=client.post('/api/runs',data={'sender_id':sender,'budget':5},files={'file':('missing.xlsx',b.getvalue())})
    assert response.status_code==200
    rid=response.json()['id']
    assert client.post(f'/api/runs/{rid}/retry',json={'groups':['NEEDS_REVIEW']}).status_code==200
    c=client.get(f'/api/runs/{rid}/companies').json()['items'][0]
    assert c['group']=='NEEDS_REVIEW' and c['identity']=='NEEDS_REVIEW'


def test_authentication_failure_stops_run(client,monkeypatch):
    import httpx
    from openai import AuthenticationError
    rid=new_run(client)
    response=httpx.Response(401,request=httpx.Request('POST','https://api.openai.com/v1/responses'))
    error=AuthenticationError('Invalid key',response=response,body=None)
    research=Mock(side_effect=error)
    monkeypatch.setattr(Pipeline,'research',research)
    with pytest.raises(AuthenticationError):Pipeline(rid,gateway=FakeGateway()).execute()
    assert research.call_count==1

def test_methodology_context_and_evidence_reach_models(client,monkeypatch):
    rid=new_run(client)
    with Session() as db:
        run=db.get(Run,rid);run.config={**run.config,'source_filters':'Регион из выгрузки, не доказательство'};db.commit()
    monkeypatch.setattr('app.pipeline.fetch',fake_fetch)
    class Capture(FakeGateway):
        def __init__(self):super().__init__();self.calls=[]
        def ask(self,role,instructions,payload,schema,**kwargs):
            self.calls.append((schema,payload))
            return super().ask(role,instructions,payload,schema,**kwargs)
    gateway=Capture();pipeline=Pipeline(rid);pipeline.gw=gateway;pipeline.execute()
    for schema,payload in gateway.calls:
        if schema in (Discovery,ResearchReport,Profile):
            assert payload['offers']
            assert payload['source_filters']=='Регион из выгрузки, не доказательство'
            assert payload['date']
        if schema is Profile:
            evidence=[e for report in payload['reports'].values() for e in report['evidence']]
            assert evidence and all('quote' in e and 'published_at' in e for e in evidence)
        if schema is Review:
            assert payload['review_kind']=='segment'
            assert all('evidence' in p for p in payload['profiles'])


def test_invalid_response_repairs_once_and_reuses_both_calls(client):
    rid=new_run(client);g=gateway(rid)
    valid=g.client.responses.create.return_value
    invalid=SimpleNamespace(id='invalid',usage=valid.usage,output=[],output_text='{"passed":',incomplete_details=SimpleNamespace(reason='max_output_tokens'))
    g.client.responses.create.side_effect=[invalid,valid]
    assert g.ask('extraction','check',{},Review,max_output=2200).passed
    assert g.ask('extraction','check',{},Review,max_output=2200).passed
    assert g.client.responses.create.call_count==2
    assert g.client.responses.create.call_args.kwargs['max_output_tokens']==4400
    with Session() as db:
        calls=db.query(ModelCall).all();r=db.get(Run,rid)
        assert len(calls)==2
        assert r.spent==pytest.approx(sum(c.cost for c in calls))
        assert r.reserved==pytest.approx(0)
        assert any(c.metrics.get('validation_failure',{}).get('raw_response')=='{"passed":' for c in calls)


def test_repair_failure_is_bounded_and_cached(client):
    from app.llm import InvalidStructuredResponse
    rid=new_run(client);g=gateway(rid)
    g.client.responses.create.return_value.output_text='invalid'
    for _ in range(2):
        with pytest.raises(InvalidStructuredResponse):g.ask('extraction','check',{},Review)
    assert g.client.responses.create.call_count==2


def test_segmentation_uses_research_document_without_sender_offers(client,monkeypatch):
    rid=new_run(client)
    with Session() as db:
        r=db.get(Run,rid)
        r.sender_snapshot={}
        r.context_snapshot={'name':'Marketing','summary':'Исследование рынков и позиционирования'}
        db.commit()
    monkeypatch.setattr('app.pipeline.fetch',fake_fetch)
    g=FakeGateway();Pipeline(rid,g).execute()
    assert 'segmentation' in g.roles
    with Session() as db:
        r=db.get(Run,rid)
        assert r.segments
        assert r.reports['segmentation']['diagnostics']['activity_directions']==1
        assert r.reports['segmentation']['diagnostics']['reason']=='completed'


def test_failed_companies_require_explicit_retry(client,monkeypatch):
    rid=new_run(client,pilot=1)
    with Session() as db:
        failed=db.query(Company).filter_by(run_id=rid).order_by(Company.id).first()
        failed.stage='FAILED';failed.group='FAILED';failed_id=failed.id;db.commit()
    monkeypatch.setattr('app.pipeline.fetch',fake_fetch)
    Pipeline(rid,FakeGateway()).execute()
    with Session() as db:
        assert db.get(Company,failed_id).stage=='FAILED'
        assert db.query(Company).filter_by(run_id=rid,stage='PROFILE_READY').count()==1


def test_legacy_truncated_result_gets_larger_repair_limit(client):
    rid=new_run(client);g=gateway(rid)
    g.ask('extraction','check',{},Review,max_output=2200)
    with Session() as db:
        call=db.query(ModelCall).one();call.result=None;call.metrics={'output_tokens':2200};db.commit()
    assert g.ask('extraction','check',{},Review,max_output=2200).passed
    assert g.client.responses.create.call_count==2
    assert g.client.responses.create.call_args.kwargs['max_output_tokens']==4400


def test_empty_segments_preserve_actionable_reason(client,monkeypatch):
    rid=new_run(client);monkeypatch.setattr('app.pipeline.fetch',fake_fetch)
    class NoCandidates(FakeGateway):
        def ask(self,role,instructions,payload,schema,**kwargs):
            if schema is Segments:
                return Segments(segments=[],abstention_reason='Нет независимого подтверждения гипотезы',missing_data=['Проверить первичный внешний источник'])
            return super().ask(role,instructions,payload,schema,**kwargs)
    Pipeline(rid,NoCandidates()).execute()
    with Session() as db:
        report=db.get(Run,rid).reports['segmentation']
        assert report['diagnostics']['reason']=='no_candidates'
        assert report['generation_notes'][0]['abstention_reason']=='Нет независимого подтверждения гипотезы'
        assert report['generation_notes'][0]['missing_data']==['Проверить первичный внешний источник']
