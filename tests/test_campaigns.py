import pytest
from app.campaigns import CampaignResearchPlan,validate_plan,validate_answers,CriterionAnswer,BASE_CRITERIA,prepare_campaign_plan
from app.db import Session,Run
from test_pipeline import new_run

SOURCE='Мы проводим исследования критериев выбора покупателей.'
def plan():
    return CampaignResearchPlan(offers=[{'id':'research','name':'Исследование','facts':[{'kind':'service','text':'Исследование критериев выбора','source_quote':SOURCE}]}],criteria=[{'id':'use_cases','question':'Какие сценарии применения названы?','answer_options':['названы','не установлено'],'source_types':['сайт'],'evidence_requirement':'Цитата','decision_impact':'Выбор исследовательского предложения','stop_condition':'Сценарии найдены или страницы исчерпаны','offer_ids':['research']}])

def test_offer_plan_requires_real_source_and_known_offer():
    value=plan();assert validate_plan(value,SOURCE)==value
    with pytest.raises(ValueError):validate_plan(value,'Другой текст документа')
    value.criteria[0].offer_ids=['invented']
    with pytest.raises(ValueError):validate_plan(value,SOURCE)

def test_unknown_is_mandatory_and_base_ids_cannot_be_replaced():
    value=plan();value.criteria[0].answer_options=['yes','no']
    with pytest.raises(ValueError):validate_plan(value,SOURCE)
    value=plan();value.criteria[0].id='products'
    with pytest.raises(ValueError):validate_plan(value,SOURCE)

def test_unbound_evidence_cannot_fill_base_answers():
    a=CriterionAnswer(criterion_id='products',status='confirmed',answer='Products',evidence_ids=['e'])
    ev={'id':'e','scope':'company','current':True,'quote':'Exact source statement','subject_verified':False}
    assert all(x['status']=='unknown' for x in validate_answers([a],BASE_CRITERIA,[ev]))
    ev['subject_verified']=True
    checked=validate_answers([a],BASE_CRITERIA,[ev])
    assert checked[0]['status']=='confirmed'
    assert len(checked)==4

def test_campaign_plan_reuses_matching_source_and_versions_changes(client):
    rid=new_run(client)
    with Session() as db:
        r=db.get(Run,rid);r.context_snapshot={'summary':SOURCE};db.commit()
    class Fake:
        id=rid
        calls=0
        def ask(self,*args,**kwargs):self.calls+=1;return plan()
    fake=Fake();prepare_campaign_plan(fake);prepare_campaign_plan(fake)
    assert fake.calls==1
    with Session() as db:
        r=db.get(Run,rid);r.context_snapshot={'summary':SOURCE+' Уточнение.'};db.commit()
    prepare_campaign_plan(fake)
    assert fake.calls==2
    with Session() as db:assert len(db.get(Run,rid).reports['campaign_plan_history'])==1


def test_research_plan_endpoint_is_read_only(client):
    rid=new_run(client)
    response=client.get(f'/api/runs/{rid}/research-plan')
    assert response.status_code==200
    assert response.json()['status']=='not_prepared'
    assert len(response.json()['base_criteria'])==4
    with Session() as db:assert not db.get(Run,rid).reports.get('campaign_plan')


def test_base_answers_do_not_call_model_without_verified_evidence(client):
    from app.campaigns import complete_base_answers
    from app.db import Company
    rid=new_run(client)
    with Session() as db:
        run=db.get(Run,rid);run.context_snapshot={'summary':SOURCE};db.commit()
    class Fake:
        id=rid
        def ask(self,*args,**kwargs):return plan()
    prepare_campaign_plan(Fake())
    with Session() as db:cid=db.query(Company).filter_by(run_id=rid).first().id
    class NoCalls:
        id=rid
        def ask(self,*args,**kwargs):raise AssertionError('Unnecessary paid call')
    complete_base_answers(NoCalls(),cid)
    with Session() as db:
        answers=db.get(Company,cid).profile['base_research']['answers']
        assert len(answers)==5
        assert all(a['status']=='unknown' for a in answers)


def test_campaign_pipeline_shared_segment_and_early_stop(client,monkeypatch):
    from app.campaigns import BasePageResult,CampaignSegments
    from app.schemas import Review
    from app.pipeline import Pipeline
    from app.db import Company
    rid=new_run(client)
    with Session() as db:
        r=db.get(Run,rid);r.context_snapshot={'summary':SOURCE,'name':'Research'};r.config={**r.config,'pipeline_version':3,'methodology_version':'campaign-v1','research_as_of':'2026-10-10'};db.commit()
    fetches=[]
    text='Company sells technical products. Buyers use products in different applications.'
    def fetch(url):
        fetches.append(url)
        return {'url':url,'text':text,'links':[{'url':url+'/products','label':'products'}]}
    monkeypatch.setattr('app.pipeline.fetch',fetch)
    class Fake:
        segment_calls=0
        def ask(self,role,instructions,payload,schema,**kwargs):
            assert not kwargs.get('search')
            if schema is CampaignResearchPlan:return plan()
            if schema is BasePageResult:
                url=payload['pages'][0]['url']
                evidence=[{'id':str(i),'url':url,'quote':q,'published_at':None,'scope':'company','origin':url} for i,q in enumerate(['Company sells technical products.','Buyers use products in different applications.'])]
                signals=[{'name':n,'state':'confirmed','confidence':'HIGH','evidence_ids':[str(i)],'explanation':'Direct observation'} for i,n in enumerate(['technical_products','multiple_applications'])]
                return BasePageResult(report={'summary':'Products','evidence':evidence,'signals':signals,'contradictions':[],'limitations':[]},answers=[{'criterion_id':c['id'],'status':'confirmed','answer':'Observed','evidence_ids':['0','1']} for c in payload['criteria']])
            if schema is CampaignSegments:
                self.segment_calls+=1
                return CampaignSegments(segments=[{'id':'candidate','name':'Technical products','definition':'Shared situation','offer_id':'research','hypothesis':'Research may be relevant','confidence':'MEDIUM','required_signals':['technical_products'],'supporting_signals':['multiple_applications'],'contradicting_signals':[],'outreach_angle':'Shared research proposal','diagnostic_question':'Relevant?','allowed_claims':[],'forbidden_claims':['Your sales are falling'],'basis_type':'applicability','cooperation_proposal':'Research buyer selection criteria','next_step':'Discuss research scope','shared_message':'We offer buyer criteria research','event_signals':[]}])
            if schema is Review:return Review(passed=True,issues=[])
            raise AssertionError(schema)
    gateway=Fake()
    Pipeline(rid,gateway).execute()
    assert gateway.segment_calls==1
    assert len(fetches)==2 # One page per company; linked page is unnecessary.
    with Session() as db:
        r=db.get(Run,rid);assert r.status=='COMPLETED'
        assert len(r.segments)==1
        assert {c.group for c in db.query(Company).filter_by(run_id=rid)}=={'segment_001'}
        assert all(c.profile['base_research']['answers'] for c in db.query(Company).filter_by(run_id=rid))
        from app.campaigns import classify_campaign
        c=db.query(Company).filter_by(run_id=rid).first();ev=c.research['base']['evidence']
        seg={**r.segments[0],'basis_type':'hypothesis'}
        assert classify_campaign(c.profile,ev,[seg])[0]!='segment_001'


def test_markdown_citation_recovers_original_literal_span():
    from app.campaigns import source_span
    source='1. **Маркетинг** — система привлечения.\n\n- карта направлений;\n- критерии оценки;'
    for quote in ('Маркетинг — система привлечения.','карта направлений;\nкритерии оценки;'):
        literal=source_span(quote,source)
        assert literal and literal in source
    assert source_span('Маркетинг — гарантия привлечения.',source) is None
    assert source_span('карта направлений; критерии роста;',source) is None


def test_confirmed_criterion_becomes_common_evidence_backed_signal():
    from app.campaigns import criterion_signals
    answers=[{'criterion_id':'products','status':'confirmed','answer':'Several products','evidence_ids':['a']},{'criterion_id':'audiences','status':'unknown','answer':'Не установлено','evidence_ids':[]}]
    evidence=[{'id':'a','subject_verified':True,'current':True,'scope':'company'}]
    signals=criterion_signals(answers,evidence)
    assert len(signals)==1
    assert signals[0]['name']=='criterion_products'
    assert signals[0]['evidence_ids']==['a']
    evidence[0]['subject_verified']=False
    assert criterion_signals(answers,evidence)==[]
