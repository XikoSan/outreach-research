"""Source switches must prevent disabled collection without paid model calls."""
import json
from app.db import Session, Run, Company
from app.pipeline import Pipeline
from app.schemas import RESEARCH_DIRECTIONS, RunConfig, selected_directions
from app.acquisition import prepare
from app.schemas import CandidateSource, Discovery
from test_architecture_v3 import v3_run
from test_workflow import workbook
from v3_fakes import V3Gateway, v3_fetch


def test_run_freezes_selected_directions_and_requires_website(client):
    context=client.post('/api/contexts',json={'name':'Услуги','summary':'Исследование продукта и аудитории компании'}).json()
    file=('source.xlsx',workbook([['Alpha','https://alpha.example','a','1']]))
    response=client.post('/api/runs',data={'context_id':context['id'],'config_json':json.dumps({'research_directions':['website','social','reviews']})},files={'file':file})
    assert response.status_code==200
    assert response.json()['config']['research_directions']==['website','social','reviews']
    bad=client.post('/api/runs',data={'context_id':context['id'],'config_json':json.dumps({'research_directions':['forums']})},files={'file':file})
    assert bad.status_code==422


def test_website_only_avoids_discovery_and_has_complete_selected_coverage(client,monkeypatch):
    rid=v3_run(client)
    with Session() as db:
        run=db.get(Run,rid);run.config={**run.config,'research_directions':['website']};db.commit()
        cid=db.query(Company).filter_by(run_id=rid).first().id
    fetched=[]
    def fetch(url):fetched.append(url);return v3_fetch(url)
    monkeypatch.setattr('app.pipeline.fetch',fetch)
    gateway=V3Gateway();Pipeline(rid,gateway).research(cid)
    with Session() as db:
        company=db.get(Company,cid)
        assert company.identity=='CONFIRMED'
        assert set(company.research)=={'website'}
        assert company.profile['coverage_complete']
    assert fetched==['https://alpha.example'] or fetched==['https://beta.example']
    assert 'discovery' not in gateway.roles


def test_forum_disabled_is_not_fetched_even_if_discovery_returns_it(client,monkeypatch):
    rid=v3_run(client)
    with Session() as db:
        run=db.get(Run,rid);run.config={**run.config,'research_directions':['website','social','jobs','reviews']};db.commit()
        cid=db.query(Company).filter_by(run_id=rid).first().id
    fetched=[];requested=[]
    def fetch(url):fetched.append(url);return v3_fetch(url)
    monkeypatch.setattr('app.pipeline.fetch',fetch)
    class Gateway(V3Gateway):
        def ask(self,role,instructions,payload,schema,**kwargs):
            if schema is Discovery:
                requested.extend(payload['directions'])
                result=super().ask(role,instructions,payload,schema,**kwargs)
                result.sources.append(CandidateSource(url='https://forum.example/thread',direction='forums',rationale='Unexpected forum'))
                return result
            return super().ask(role,instructions,payload,schema,**kwargs)
    Pipeline(rid,Gateway()).research(cid)
    with Session() as db:
        company=db.get(Company,cid)
        assert 'forums' not in company.research
        assert not company.context['acquisition_plan']['sources']['forums']
        assert company.profile['coverage_complete']
    assert 'forums' not in requested
    assert not any('forum.example' in url for url in fetched)


def test_official_social_identity_anchor_survives_social_switch_off(client,monkeypatch):
    context=client.post('/api/contexts',json={'name':'Услуги','summary':'Исследование продукта и аудитории компании'}).json()
    content=workbook([['Alpha',None,'https://social.example/@alpha']],['Название компании','Ссылка на сайт','Официальная соцсеть компании'])
    response=client.post('/api/runs',data={'context_id':context['id'],'config_json':json.dumps({'research_directions':['website']})},files={'file':('source.xlsx',content)})
    assert response.status_code==200
    rid=response.json()['id']
    with Session() as db:
        run=db.get(Run,rid);run.status='RUNNING';db.commit()
        cid=db.query(Company).filter_by(run_id=rid).one().id
    def fetch(url):
        return {'url':url,'title':'Alpha','text':'Alpha company offers several industrial product lines.','links':[],'truncated':False}
    monkeypatch.setattr('app.pipeline.fetch',fetch)
    Pipeline(rid,V3Gateway()).research(cid)
    with Session() as db:
        company=db.get(Company,cid)
        assert company.identity=='CONFIRMED'
        assert 'social' in company.research
        assert 'forums' not in company.research


def test_vacancies_are_distinct_and_old_combined_direction_is_ignored(client):
    assert RESEARCH_DIRECTIONS == ('website','social','jobs','reviews','forums')
    assert 'jobs' in RunConfig().research_directions
    assert selected_directions({'research_directions':['website','news_jobs']}) == {'website'}
    context=client.post('/api/contexts',json={'name':'Услуги','summary':'Исследование продукта и аудитории компании'}).json()
    file=('source.xlsx',workbook([['Alpha','https://alpha.example','a','1']]))
    result=client.post('/api/runs',data={'context_id':context['id'],'config_json':json.dumps({'research_directions':['website','news_jobs']})},files={'file':file})
    assert result.status_code==422

def test_saved_combined_source_plan_cannot_reintroduce_news(client):
    rid=v3_run(client)
    with Session() as db:
        run=db.get(Run,rid)
        run.config={**run.config,'research_directions':['website','jobs']}
        company=db.query(Company).filter_by(run_id=rid).first()
        company.context={**company.context,'acquisition_plan':{
            'identity_confirmed':True,
            'sources':{'website':['https://alpha.example'],'news_jobs':['https://example.org/article']},
            'checked_directions':['website','news_jobs'],
            'discovery':{'identity_confirmed':True,'identity_reason':'saved','products':'',
                         'sources':[{'url':'https://example.org/article','direction':'news_jobs','rationale':'old'}],
                         'checked_directions':['website','news_jobs']}}}
        company.research={'website':{'state':'COMPLETE','discovery':{
            'identity_reason':'saved',
            'sources':[{'url':'https://alpha.example','direction':'website','rationale':'source'},
                       {'url':'https://example.org/article','direction':'news_jobs','rationale':'old'}],
            'checked_directions':['website','news_jobs']}}}
        cid=company.id
        db.commit()
    plan=prepare(None,cid,{},None,None)
    assert plan['sources']['jobs']==[]
    assert 'news_jobs' not in plan['sources']
    assert plan['discovery']['sources']==[]
    assert plan['checked_directions']==['website']
    shown=client.get(f'/api/runs/{rid}/reports?company_id={cid}').json()['directions']
    assert list(shown)==['website']
    assert [source['url'] for source in shown['website']['discovery']['sources']]==['https://alpha.example']
