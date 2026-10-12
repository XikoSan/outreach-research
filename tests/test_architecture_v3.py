import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import pytest
from app.db import Session, Run, Company, ModelCall, ModelAttempt, CompanyJob
from app.schemas import ResearchReport, Review
from app.evidence import verify_report,preserve_synthesis,build_feature_matrix
from app.rules import classify_with_trace
from app.pipeline import Pipeline
from app.acquisition import prepare as prepare_sources, social as is_social
from app.schemas import Discovery
from app.jobs import ensure_jobs,claim,execute_jobs
from app.llm import UncertainCall,RunPaused
from app.crawler import parse_page
from test_pipeline import new_run,gateway
from test_workflow import workbook
from v3_fakes import V3Gateway,v3_fetch


def v3_run(client):
    context=client.post('/api/contexts',json={'name':'Offer','summary':'Research products and audiences without promising growth'}).json()
    result=client.post('/api/runs',data={'context_id':context['id']},files={'file':('source.xlsx',workbook([['Alpha','https://alpha.example','a','1'],['Beta','https://beta.example','b','2']]))})
    assert result.status_code==200
    rid=result.json()['id']
    with Session() as db:
        r=db.get(Run,rid);r.status='RUNNING';r.config={**r.config,'research_as_of':'2026-10-09'};db.commit()
    return rid


def report(date='2026-10-09',basis=''):
    return ResearchReport(summary='Observed',evidence=[{'id':'e','url':'https://alpha.example','quote':'Alpha has several product lines.','published_at':date,'date_evidence':basis,'scope':'company','origin':'alpha'}],signals=[{'name':'multiple_products','state':'confirmed','confidence':'HIGH','evidence_ids':['e'],'explanation':'Products'}],contradictions=[],limitations=[])


def test_dates_require_source_basis_and_social_is_not_fresh_without_date():
    page={'url':'https://alpha.example','text':'Alpha has several product lines.','subject_id':'a','publisher_id':'company:a'}
    checked=verify_report(report(),[page],'social',30,as_of='2026-10-09',company_id='a')
    assert checked['evidence'][0]['published_at'] is None
    assert checked['signals'][0]['confidence']=='LOW'
    page['text']+=' Published 2026-10-01'
    checked=verify_report(report('2026-10-01','2026-10-01'),[page],'social',30,as_of='2026-10-09',company_id='a')
    assert checked['signals'][0]['confidence']=='HIGH'


def test_synthesis_cannot_flip_polarity_or_create_fact():
    old=[{'name':'multiple_products','state':'refuted','confidence':'MEDIUM','evidence_ids':['e'],'explanation':'Explicit denial'}]
    malicious=[{**old[0],'name':'weak_positioning','state':'confirmed','confidence':'HIGH'}]
    result=preserve_synthesis(malicious,old,[{'id':'e'}])
    assert result[0]['name']=='multiple_product_lines' and result[0]['state']=='refuted'


def test_same_owner_crosspost_cannot_confirm_segment():
    signals=[{'name':n,'state':'confirmed','confidence':'HIGH','evidence_ids':[str(i)],'explanation':''} for i,n in enumerate(('a','b'))]
    evidence=[{'id':str(i),'url':u,'quote':q,'scope':'company','current':True,'subject_verified':True,'validation_version':'evidence-v3','original_source_id':'company:a'} for i,(u,q) in enumerate([('https://alpha.example','Product offering'),('https://vk.com/alpha','Own product release')])]
    seg={'id':'s','required_signals':['a'],'supporting_signals':['b'],'contradicting_signals':[]}
    group,_,trace=classify_with_trace({'signals':signals},evidence,[seg])
    assert group!='s' and 'independence_not_established' in trace[0]['reasons']


def test_feature_matrix_normalizes_known_aliases_and_keeps_conflict():
    def sig(name,state):return {'name':name,'state':state,'confidence':'HIGH','evidence_ids':['e'],'explanation':''}
    matrix=build_feature_matrix([{'company_id':'a','signals':[sig('multiple_products','confirmed'),sig('multiple_product_lines','refuted')]}])
    assert matrix['rows'][0]['features']['multiple_product_lines']['state']=='unknown'


def test_source_first_single_discovery_and_durable_chunks(client,monkeypatch):
    rid=v3_run(client);events=[];g=V3Gateway();original=g.ask
    def ask(*args,**kwargs):events.append(args[0]);return original(*args,**kwargs)
    g.ask=ask
    def fetch(url):events.append('fetch');return v3_fetch(url)
    monkeypatch.setattr('app.pipeline.fetch',fetch)
    Pipeline(rid,g).execute()
    assert events[0]=='fetch' and events.count('discovery')==2
    with Session() as db:
        run=db.get(Run,rid);assert run.reports['segmentation']['feature_matrix']['rows']
        assert run.status=='COMPLETED'
        c=db.query(Company).filter_by(run_id=rid).first();assert c.research['website']['chunks'];assert c.profile['classification']['trace']
        assert db.query(CompanyJob).filter_by(run_id=rid,state='COMPLETE').count()==2


def test_corporate_social_from_workbook_is_separate_unverified_source(client):
    rid=v3_run(client)
    with Session() as db:
        company=db.query(Company).filter_by(run_id=rid,name='Alpha').one()
        company.context={**company.context,'socials':['https://t.me/alpha','https://x.com/alpha']}
        cid=company.id;config=dict(db.get(Run,rid).config);db.commit()
    class FakePipeline:
        id=rid
        def ask(self,*args,**kwargs):
            return Discovery(identity_confirmed=True,identity_quote='',identity_reason='Site supplied in workbook',products='',sources=[])
    plan=prepare_sources(FakePipeline(),cid,config,{'offers':[]},lambda url:{'url':url,'text':'Alpha makes industrial equipment.','links':[]})
    assert is_social('https://x.com/alpha')
    assert plan['identity_confirmed']
    assert plan['sources']['social']==['https://t.me/alpha','https://x.com/alpha']
    assert plan['official_socials']==[]


def test_declared_social_on_unknown_host_is_not_trusted_as_website(client):
    context=client.post('/api/contexts',json={'name':'Offer','summary':'Research products and audiences without promising growth'}).json()
    content=workbook([['Alpha',None,'https://social.example/@alpha']],['Название компании','Ссылка на сайт','Официальная соцсеть компании'])
    response=client.post('/api/runs',data={'context_id':context['id']},files={'file':('source.xlsx',content)})
    assert response.status_code==200,response.text
    rid=response.json()['id']
    with Session() as db:
        company=db.query(Company).filter_by(run_id=rid).one()
        assert company.resource=='https://social.example/@alpha'
        assert company.context['socials']==[company.resource]
        cid=company.id;config=dict(db.get(Run,rid).config)
    class FakePipeline:
        id=rid
        def ask(self,*args,**kwargs):
            return Discovery(identity_confirmed=False,identity_quote='',identity_reason='Unverified',products='',sources=[])
    plan=prepare_sources(FakePipeline(),cid,config,{'offers':[]},lambda url:{'url':url,'text':'Unrelated text.','links':[]})
    assert not plan['identity_confirmed']
    assert plan['sources']['social']==['https://social.example/@alpha']
    assert plan['sources']['website']==[]


def test_retry_preserves_successful_reports_and_history(client,monkeypatch):
    rid=v3_run(client);monkeypatch.setattr('app.pipeline.fetch',v3_fetch);Pipeline(rid,V3Gateway()).execute()
    with Session() as db:
        c=db.query(Company).filter_by(run_id=rid).first();cid=c.id;before=c.research;c.group='FAILED';db.commit()
    assert client.post(f'/api/runs/{rid}/retry',json={'groups':['FAILED']}).status_code==200
    with Session() as db:
        c=db.get(Company,cid);assert c.research==before;assert c.context['research_history'][0]['research']==before
    assert client.get(f'/api/runs/{rid}/companies/{cid}/history').json()['current_revision']==2


def test_attempt_history_survives_reconcile_and_retry(client):
    rid=new_run(client);g=gateway(rid,'timeout')
    with pytest.raises(UncertainCall):g.ask('synthesis','x',{},Review)
    with Session() as db:
        cid=db.query(ModelCall).one().id;r=db.get(Run,rid);r.status='PAUSED';db.commit()
    assert client.post('/api/calls/'+cid+'/reconcile',json={'verified_cost':.01,'note':'verified'}).status_code==200
    with Session() as db:r=db.get(Run,rid);r.status='RUNNING';db.commit()
    g.client.responses.create.side_effect=None;g.ask('synthesis','x',{},Review)
    with Session() as db:
        attempts=db.query(ModelAttempt).filter_by(operation_id=cid).all()
        assert len(attempts)==2 and {a.state for a in attempts}=={'RECONCILED','COMPLETE'}
        assert sum(a.cost for a in attempts)==pytest.approx(db.get(Run,rid).spent)


def test_duplicate_job_claim_has_single_owner(client):
    rid=v3_run(client)
    with Session() as db:cid=db.query(Company).filter_by(run_id=rid).first().id
    ensure_jobs(rid,[cid])
    with ThreadPoolExecutor(max_workers=2) as pool:claims=list(pool.map(lambda _:claim(rid,cid),range(2)))
    assert sum(c is not None for c in claims)==1


@pytest.mark.parametrize('concurrency',[1,2,3,4,8])
def test_queue_identical_results_for_bounded_concurrency(client,concurrency):
    rid=v3_run(client)
    with Session() as db:ids=[c.id for c in db.query(Company).filter_by(run_id=rid)]
    finished=[];lock=threading.Lock()
    def perform(cid):
        time.sleep(.005)
        with lock:finished.append(cid)
    execute_jobs(rid,ids,perform,concurrency)
    assert sorted(finished)==sorted(ids)


def test_pause_does_not_admit_next_job(client):
    rid=v3_run(client)
    with Session() as db:ids=[c.id for c in db.query(Company).filter_by(run_id=rid)]
    done=[]
    def perform(cid):
        done.append(cid)
        with Session() as db:r=db.get(Run,rid);r.status='PAUSING';db.commit()
    with pytest.raises(RunPaused):execute_jobs(rid,ids,perform,1)
    assert len(done)==1


def test_crawler_retains_footer_links_before_cleanup():
    page=parse_page('https://alpha.example','<title>Alpha</title><main>'+'Public products and audiences. '*10+'</main><footer><a href="https://vk.com/alpha">Social</a></footer>')
    assert page['links'][0]['url']=='https://vk.com/alpha'
    assert 'Social' not in page['text'] and page['content_hash']


def test_full_offer_document_is_frozen(client):
    text='Intro material. '*250+'\nNever guarantee growth or provide accounting services.'
    ctx=client.post('/api/contexts/upload',files={'file':('offer.md',text.encode())}).json()
    result=client.post('/api/runs',data={'context_id':ctx['id']},files={'file':('source.xlsx',workbook([['Alpha','https://alpha.example','a','1']]))}).json()
    assert result['context_snapshot']['source_text']==text
    assert result['config']['research_as_of']


def test_incomplete_external_coverage_cannot_assign_segment(client,monkeypatch):
    rid=v3_run(client);monkeypatch.setattr('app.pipeline.fetch',v3_fetch)
    class Unchecked(V3Gateway):
        def ask(self,*args,**kwargs):
            result=super().ask(*args,**kwargs)
            if args[0]=='discovery':result.checked_directions=[]
            return result
    Pipeline(rid,Unchecked()).execute()
    with Session() as db:
        assert not db.get(Run,rid).segments
        assert all(c.group=='INSUFFICIENT_DATA' for c in db.query(Company).filter_by(run_id=rid))


def test_completed_chunks_reused_after_failure_without_fetch_or_extraction(client,monkeypatch):
    rid=v3_run(client);calls=Mock(side_effect=v3_fetch);monkeypatch.setattr('app.pipeline.fetch',calls)
    with Session() as db:cid=db.query(Company).filter_by(run_id=rid).first().id
    g=V3Gateway();original=g.ask
    def ask(*args,**kwargs):
        if args[0]=='synthesis':raise ValueError('Simulated failure before profile commit')
        return original(*args,**kwargs)
    g.ask=ask
    with pytest.raises(ValueError):Pipeline(rid,g).research(cid)
    count=calls.call_count;g.roles.clear();g.ask=original
    Pipeline(rid,g).research(cid)
    assert calls.call_count==count and g.roles==['synthesis']


def test_context_tail_reaches_model(client,monkeypatch):
    rid=v3_run(client);tail='No accounting services; no guaranteed revenue.'
    with Session() as db:
        run=db.get(Run,rid);run.context_snapshot={**run.context_snapshot,'source_text':'Description. '*300+tail};db.commit()
    g=V3Gateway();original=g.ask;seen=[]
    def ask(*args,**kwargs):
        if args[0]=='discovery':seen.append(args[2]['offers'][0]['description'])
        return original(*args,**kwargs)
    g.ask=ask;monkeypatch.setattr('app.pipeline.fetch',v3_fetch);Pipeline(rid,g).execute()
    assert seen and all(tail in description for description in seen)


def test_contact_page_uses_index_without_reopening_workbook(client,monkeypatch):
    rid=v3_run(client)
    monkeypatch.setattr('app.workflow.load_workbook',Mock(side_effect=AssertionError('Must use index')))
    assert client.get(f'/api/runs/{rid}/contacts').status_code==200


def test_publication_date_parsing_does_not_confuse_update_date():
    from app.evidence import source_date
    assert source_date('Опубликовано 1 октября 2026').isoformat()=='2026-10-01'
    page=parse_page('https://alpha.example','<meta property="article:modified_time" content="2026-10-09"><main>'+'Public text. '*15+'</main>')
    assert page['date_metadata']==[]


def test_new_campaign_run_uses_measured_worker_limit_without_changing_legacy(client):
    context = client.post('/api/contexts', json={
        'name': 'Offer', 'summary': 'Research products and audiences without promising growth'
    }).json()
    source = workbook([['Alpha', 'https://alpha.example', 'a', '1']])
    campaign = client.post('/api/runs', data={
        'context_id': context['id'],
        'config_json': json.dumps({'methodology_version': 'campaign-v1'}),
    }, files={'file': ('campaign.xlsx', source)})
    legacy = client.post('/api/runs', data={'context_id': context['id']},
                         files={'file': ('legacy.xlsx', source)})
    assert campaign.status_code == legacy.status_code == 200
    with Session() as db:
        assert db.get(Run, campaign.json()['id']).config['concurrency'] == 8
        assert db.get(Run, legacy.json()['id']).config['concurrency'] == 3
