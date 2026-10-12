from app.db import Session,Run,Company
from app.campaigns import TargetedQuestion,BasePageResult
from app.targeted import perform_targeted,TargetSources
from app.pipeline import Pipeline
from app.evidence import VERSION
from test_pipeline import new_run


def setup(client):
    rid=new_run(client)
    with Session() as db:
        r=db.get(Run,rid);r.config={**r.config,'research_as_of':'2026-10-10','max_targeted_research_pages':1,'max_targeted_research_queries':1}
        r.reports={'campaign_plan':{'base_criteria':[{'id':'products','question':'Products?'}],'plan':{'criteria':[]}}}
        ids=[]
        for c in db.query(Company).filter_by(run_id=rid):
            ids.append(c.id);c.identity='CONFIRMED';c.stage='PROFILE_READY'
            c.profile={'signals':[{'name':'technical','state':'confirmed','confidence':'HIGH','evidence_ids':['old'],'explanation':'Observed'}]}
            c.research={'base':{'signals':c.profile['signals'],'evidence':[{'id':'old','scope':'company','subject_verified':True,'current':True,'validation_version':VERSION,'original_source_id':'company:'+c.id,'quote':'Technical products exist','url':c.resource}]}}
        db.commit()
    candidate={'id':'s','basis_type':'applicability','required_signals':['technical'],'supporting_signals':['applications'],'contradicting_signals':[]}
    task=TargetedQuestion(candidate_id='s',company_ids=ids,criterion_id='products',question='Какие применения продукции подтверждены?',decision_impact='Подтвердить применимость общей услуги')
    return rid,ids,candidate,task


def test_targeted_bounded_and_idempotent(client):
    rid,ids,candidate,task=setup(client);calls=[];fetches=[]
    class Fake:
        def ask(self,role,instructions,payload,schema,**kw):
            calls.append(schema)
            if schema is TargetSources:return TargetSources(sources=[{'url':payload['resource']+'/products','direction':'website','rationale':'Products'}],limitations=[])
            page=payload['pages'][0]
            return BasePageResult(report={'summary':'Applications','evidence':[{'id':'e','url':page['url'],'quote':page['text'],'published_at':None,'scope':'company','origin':'test'}],'signals':[{'name':'applications','state':'confirmed','confidence':'HIGH','evidence_ids':['e'],'explanation':'Direct'}],'contradictions':[],'limitations':[]},answers=[{'criterion_id':'products','status':'confirmed','answer':'Applications','evidence_ids':['e']}])
    def fetch(url):fetches.append(url);return {'url':url,'text':'Products have several industrial applications.'}
    pipe=Pipeline(rid,Fake());pipe.selected_ids=set(ids)
    assert perform_targeted(pipe,[task],[candidate],fetch)
    assert len(calls)==4 and len(fetches)==2
    assert not perform_targeted(pipe,[task],[candidate],fetch)
    assert len(calls)==4
    with Session() as db:
        for cid in ids:
            c=db.get(Company,cid)
            assert list(c.context['targeted_tasks'].values())[0]['termination_reason']=='decision_reached'
            assert c.profile['base_research']['answers'][0]['status']=='confirmed'


def test_targeted_rejects_foreign_company_and_missing_candidate(client):
    rid,ids,candidate,task=setup(client)
    class Fake:
        def ask(self,*a,**kw):raise AssertionError('Must not spend')
    pipe=Pipeline(rid,Fake());pipe.selected_ids=set(ids)
    assert not perform_targeted(pipe,[task],[],None)
    task.company_ids=[ids[0],'outside-run']
    assert not perform_targeted(pipe,[task],[candidate],None)


def test_empty_search_consumes_limit_and_is_not_repeated(client):
    rid,ids,candidate,task=setup(client);calls=[]
    class Fake:
        def ask(self,*a,**kw):calls.append(kw);return TargetSources(sources=[],limitations=['Nothing found'])
    pipe=Pipeline(rid,Fake());pipe.selected_ids=set(ids)
    assert not perform_targeted(pipe,[task],[candidate],None)
    assert len(calls)==2
    assert not perform_targeted(pipe,[task],[candidate],None)
    task.question='Другой вопрос о применении этих же продуктов?'
    assert not perform_targeted(pipe,[task],[candidate],None)
    assert len(calls)==2


def test_targeted_search_discards_disabled_forum_sources(client):
    rid,ids,candidate,task=setup(client)
    with Session() as db:
        run=db.get(Run,rid);run.config={**run.config,'research_directions':['website']};db.commit()
    seen=[]
    class Fake:
        def ask(self,role,instructions,payload,schema,**kw):
            assert schema is TargetSources
            seen.append(payload['allowed_directions'])
            return TargetSources(sources=[{'url':'https://forum.example/thread','direction':'forums','rationale':'Outside scope'}],limitations=[])
    def fetch(url):raise AssertionError('A disabled forum must not be fetched')
    pipe=Pipeline(rid,Fake());pipe.selected_ids=set(ids)
    assert not perform_targeted(pipe,[task],[candidate],fetch)
    assert seen==[['website'],['website']]
    with Session() as db:
        for cid in ids:
            states=db.get(Company,cid).context['targeted_tasks']
            assert next(iter(states.values()))['sources']==[]
