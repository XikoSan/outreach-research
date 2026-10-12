import pytest
from app.campaigns import CampaignResearchPlan,validate_plan,validate_answers,CriterionAnswer,BASE_CRITERIA,LEGACY_BASE_CRITERIA,prepare_campaign_plan
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
    a=CriterionAnswer(criterion_id='products',status='confirmed',answer='Products',evidence_ids=['e'],facts=[{'value':'Industrial products','evidence_ids':['e']}])
    ev={'id':'e','scope':'company','current':True,'quote':'Company sells Industrial products.','subject_verified':False}
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
        saved=db.get(Run,rid).reports['campaign_plan']
        assert saved['version']=='campaign-v2'
        assert all(c['specific_values'] for c in saved['base_criteria']+saved['plan']['criteria'])
    with Session() as db:
        r=db.get(Run,rid);r.context_snapshot={'summary':SOURCE+' Уточнение.'};db.commit()
    prepare_campaign_plan(fake)
    assert fake.calls==2
    with Session() as db:assert len(db.get(Run,rid).reports['campaign_plan_history'])==1


def test_offer_plan_repairs_only_invalid_literal_quote_once(client):
    from app.campaigns import OfferQuoteCorrections
    rid=new_run(client)
    with Session() as db:
        run=db.get(Run,rid);run.context_snapshot={'summary':SOURCE};db.commit()
    class Fake:
        id=rid
        calls=[]
        def ask(self,role,instructions,payload,schema,**kwargs):
            self.calls.append(schema)
            if schema is CampaignResearchPlan:
                result=plan()
                result.offers[0].facts[0].source_quote='Недословная цитата об исследовании покупателей'
                return result
            assert schema is OfferQuoteCorrections
            assert len(payload['invalid_facts'])==1
            return OfferQuoteCorrections(corrections=[{'offer_index':0,'fact_index':0,
                                                      'source_quote':SOURCE}])
    fake=Fake()
    prepared=prepare_campaign_plan(fake)
    assert prepared.offers[0].facts[0].source_quote==SOURCE
    assert fake.calls==[CampaignResearchPlan,OfferQuoteCorrections]
    prepare_campaign_plan(fake)
    assert len(fake.calls)==2


def test_offer_plan_rejects_unsupportable_quote_after_repair(client):
    from app.campaigns import OfferQuoteCorrections
    rid=new_run(client)
    with Session() as db:
        run=db.get(Run,rid);run.context_snapshot={'summary':SOURCE};db.commit()
    class Fake:
        id=rid
        def ask(self,role,instructions,payload,schema,**kwargs):
            if schema is CampaignResearchPlan:
                result=plan()
                result.offers[0].facts[0].source_quote='Выдуманный текст в роли цитаты предложения'
                return result
            return OfferQuoteCorrections(corrections=[{'offer_index':0,'fact_index':0,
                                                      'source_quote':''}])
    with pytest.raises(ValueError,match='не подтверждён'):
        prepare_campaign_plan(Fake())
    with Session() as db:
        assert not db.get(Run,rid).reports.get('campaign_plan')


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
    text='Company sells technical products. Manufacturers use technical products for equipment maintenance.'
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
                evidence=[{'id':str(i),'url':url,'quote':q,'published_at':None,'scope':'company','origin':url} for i,q in enumerate(['Company sells technical products.','Manufacturers use technical products for equipment maintenance.'])]
                signals=[{'name':n,'state':'confirmed','confidence':'HIGH','evidence_ids':[str(i)],'explanation':'Direct observation'} for i,n in enumerate(['technical_products','multiple_applications'])]
                facts={'products':('technical products',['0']),'buyer_types':('Manufacturers',['1']),
                       'application_scenarios':('equipment maintenance',['1']),
                       'product_buyer_use_link':('technical products | Manufacturers | equipment maintenance',['1']),
                       'use_cases':('equipment maintenance',['1'])}
                return BasePageResult(report={'summary':'Products','evidence':evidence,'signals':signals,'contradictions':[],'limitations':[]},answers=[{'criterion_id':c['id'],'status':'confirmed','answer':facts[c['id']][0],'evidence_ids':facts[c['id']][1], 'facts':[{'value':facts[c['id']][0],'evidence_ids':facts[c['id']][1]}]} for c in payload['criteria']])
            if schema is CampaignSegments:
                self.segment_calls+=1
                assert payload['input_kind']=='candidate_overview'
                shared=payload['candidate_overview']['candidates'][0]['raw_signal_names']
                assert len(shared)==2
                proposal={'id':'candidate','name':'Technical products','definition':'Shared situation','offer_id':'research','hypothesis':'Research may be relevant','confidence':'MEDIUM','required_signals':shared,'supporting_signals':[],'contradicting_signals':[],'outreach_angle':'Shared research proposal','diagnostic_question':'Relevant?','allowed_claims':[],'forbidden_claims':['Your sales are falling'],'basis_type':'applicability','cooperation_proposal':'Research buyer selection criteria','next_step':'Discuss research scope','shared_message':'We offer buyer criteria research','event_signals':[]}
                return CampaignSegments(segments=[proposal,{**proposal,'id':'duplicate'}])
            if schema is Review:return Review(passed=True,issues=[])
            raise AssertionError(schema)
    gateway=Fake()
    Pipeline(rid,gateway).execute()
    assert gateway.segment_calls==1
    assert len(fetches)==2 # One page per company; linked page is unnecessary.
    with Session() as db:
        r=db.get(Run,rid);assert r.status=='COMPLETED'
        assert len(r.segments)==1
        assert r.reports['segmentation']['generation_notes'][0]['rejected_unanchored']==1
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


def test_citation_loss_repairs_from_saved_page_without_new_fetch(client):
    from app.campaigns import BasePageResult,CitationRepairResult,research_base,repair_citation_losses
    from app.db import Company
    from app.pipeline import Pipeline
    rid=new_run(client)
    with Session() as db:
        run=db.get(Run,rid)
        run.config={**run.config,'pipeline_version':3,'methodology_version':'campaign-v1','research_as_of':'2026-10-10'}
        run.reports={**run.reports,'campaign_plan':{'version':'campaign-v1','source_hash':'test','plan':plan().model_dump(),'base_criteria':LEGACY_BASE_CRITERIA}}
        company=db.query(Company).filter_by(run_id=rid).first();cid=company.id
        db.commit()
    page_text='Products include lifts. Schools use lifts. Hospitals use lifts.'
    fetches=[]
    def fetch(url):
        fetches.append(url)
        return {'url':url,'text':page_text,'links':[]}
    def item(eid,quote,url):
        return {'id':eid,'url':url,'quote':quote,'published_at':None,'scope':'company','origin':url}
    class Fake:
        calls=[]
        def ask(self,role,instructions,payload,schema,**kwargs):
            self.calls.append(schema)
            url=payload['pages'][0]['url']
            if schema is BasePageResult:
                return BasePageResult(report={'summary':'Products','evidence':[item('good','Products include lifts.',url),item('bad','Schools use lifts. [...] Hospitals use lifts.',url)],'signals':[],'contradictions':[],'limitations':[]},answers=[{'criterion_id':'products','status':'confirmed','answer':'Products include lifts.','evidence_ids':['good']},{'criterion_id':'audiences','status':'confirmed','answer':'Schools and hospitals use lifts.','evidence_ids':['bad']}])
            if schema is CitationRepairResult:
                assert [c['id'] for c in payload['criteria']]==['audiences']
                assert payload['pages'][0]['text']==page_text
                return CitationRepairResult(evidence=[item('school','Schools use lifts.',url),item('hospital','Hospitals use lifts.',url)],answers=[{'criterion_id':'audiences','status':'confirmed','answer':'Schools and hospitals use lifts.','evidence_ids':['school','hospital']}],limitations=[])
            raise AssertionError(schema)
    fake=Fake();pipe=Pipeline(rid,fake)
    research_base(pipe,cid,fetch)
    with Session() as db:
        company=db.get(Company,cid)
        assert company.research['base']['citation_losses']==['audiences']
        assert next(a for a in company.profile['base_research']['answers'] if a['criterion_id']=='audiences')['status']=='unknown'
    assert repair_citation_losses(pipe,cid,{'audiences'})
    assert not repair_citation_losses(pipe,cid,{'audiences'})
    assert fetches==['https://alpha.example']
    assert fake.calls==[BasePageResult,CitationRepairResult]
    with Session() as db:
        company=db.get(Company,cid)
        assert company.research['base']['citation_losses']==[]
        assert {s['name'] for s in company.profile['signals']}=={'criterion_products','criterion_audiences'}
        assert next(a for a in company.profile['base_research']['answers'] if a['criterion_id']=='audiences')['status']=='confirmed'
        assert all(e['quote'] in page_text for report in company.research.values() if report.get('criteria')==['audiences'] for e in report['evidence'])


def test_segmentation_rechecks_company_with_no_initial_signals(client):
    from app.campaigns import CampaignSegments,CitationRepairResult
    from app.schemas import Review
    from app.evidence import VERSION as EVIDENCE_VERSION
    from app.db import Company
    from app.pipeline import Pipeline
    rid=new_run(client)
    with Session() as db:
        run=db.get(Run,rid)
        run.config={**run.config,'pipeline_version':3,'methodology_version':'campaign-v1','research_as_of':'2026-10-10'}
        run.reports={**run.reports,'campaign_plan':{'version':'campaign-v1','source_hash':'test','plan':plan().model_dump(),'base_criteria':LEGACY_BASE_CRITERIA}}
        companies=db.query(Company).filter_by(run_id=rid).order_by(Company.id).all()
        first,second=companies
        for company in companies:company.identity='CONFIRMED';company.stage='PROFILE_READY'
        def ev(eid,quote):
            return {'id':eid,'url':first.resource,'quote':quote,'scope':'company','origin':first.resource,'subject_verified':True,'current':True,'validation_version':EVIDENCE_VERSION}
        first.research={'base':{'evidence':[ev('p','Company sells technical products.'),ev('a','Schools use these products.')], 'signals':[]}}
        first.profile={'signals':[{'name':'criterion_products','state':'confirmed','confidence':'HIGH','evidence_ids':['p'],'explanation':'Products'},{'name':'criterion_audiences','state':'confirmed','confidence':'HIGH','evidence_ids':['a'],'explanation':'Schools'}],'base_research':{'answers':[]}}
        second.research={'base':{'pages':[{'url':second.resource,'text':'Company sells technical products. Schools use these products.','subject_id':second.id,'publisher_id':'company:'+second.id}],'evidence':[],'signals':[],'citation_losses':['products','audiences']}}
        second.profile={'signals':[],'base_research':{'answers':[{'criterion_id':cid,'status':'unknown','answer':'Не установлено','evidence_ids':[]} for cid in ('products','audiences')]}}
        second_id=second.id
        db.commit()
    class Fake:
        calls=[]
        def ask(self,role,instructions,payload,schema,**kwargs):
            self.calls.append(schema)
            if schema is CampaignSegments:
                assert len(payload['profiles'])==1
                return CampaignSegments(segments=[{'id':'candidate','name':'Shared products','definition':'Several products for schools','offer_id':'research','hypothesis':'The service may be applicable','confidence':'MEDIUM','required_signals':['criterion_products','criterion_audiences'],'supporting_signals':[],'contradicting_signals':[],'outreach_angle':'Research selection criteria','diagnostic_question':'Relevant?','allowed_claims':[],'forbidden_claims':[],'basis_type':'applicability','cooperation_proposal':'Research buyer selection criteria','next_step':'Discuss research scope','shared_message':'We can research selection criteria','event_signals':[]}])
            if schema is CitationRepairResult:
                url=payload['pages'][0]['url']
                assert url=='https://alpha.example' or url=='https://beta.example'
                return CitationRepairResult(evidence=[{'id':'p','url':url,'quote':'Company sells technical products.','published_at':None,'scope':'company','origin':url},{'id':'a','url':url,'quote':'Schools use these products.','published_at':None,'scope':'company','origin':url}],answers=[{'criterion_id':'products','status':'confirmed','answer':'Technical products','evidence_ids':['p']},{'criterion_id':'audiences','status':'confirmed','answer':'Schools use the products','evidence_ids':['a']}],limitations=[])
            if schema is Review:
                assert len(payload['profiles'])==2
                return Review(passed=True,issues=[])
            raise AssertionError(schema)
    fake=Fake();Pipeline(rid,fake).segment()
    assert fake.calls==[CampaignSegments,CitationRepairResult,Review]
    with Session() as db:
        run=db.get(Run,rid)
        assert len(run.segments)==1
        second=db.get(Company,second_id)
        assert {s['name'] for s in second.profile['signals']}=={'criterion_products','criterion_audiences'}
        assert run.reports['segmentation']['diagnostics']['profiles_with_signals']==2


def test_citation_repair_does_not_trust_unbound_saved_page(client):
    from app.campaigns import repair_citation_losses
    from app.db import Company
    rid=new_run(client)
    with Session() as db:
        run=db.get(Run,rid)
        run.config={**run.config,'methodology_version':'campaign-v1','research_as_of':'2026-10-10'}
        run.reports={**run.reports,'campaign_plan':{'version':'campaign-v1','source_hash':'test','plan':plan().model_dump(),'base_criteria':LEGACY_BASE_CRITERIA}}
        company=db.query(Company).filter_by(run_id=rid).first();cid=company.id
        company.profile={'base_research':{'answers':[{'criterion_id':'products','status':'unknown','answer':'Не установлено','evidence_ids':[]}]},'signals':[]}
        company.research={'base':{'pages':[{'url':'https://unbound.example','text':'Products include lifts.'}],'citation_losses':['products']}}
        db.commit()
    class NoCalls:
        id=rid
        def ask(self,*args,**kwargs):raise AssertionError('Unbound page must not reach a paid model call')
    assert not repair_citation_losses(NoCalls(),cid,{'products'})


def test_base_fetch_retries_equivalent_www_host_after_network_failure():
    from app.campaigns import fetch_base_page
    from app.crawler import SourceUnavailable
    calls = []

    def fetch(url):
        calls.append(url)
        if url == 'https://example.org':
            raise SourceUnavailable('HTTP 404')
        return {'url': url, 'text': 'Company information'}

    page = fetch_base_page(fetch, 'https://example.org', 'https://example.org')
    assert page['url'] == 'https://www.example.org'
    assert calls == ['https://example.org', 'https://www.example.org']


def test_base_fetch_does_not_retry_robots_denial_or_unrelated_page():
    from app.campaigns import fetch_base_page
    from app.crawler import SourceUnavailable
    calls = []

    def fetch(url):
        calls.append(url)
        raise SourceUnavailable('Запрещено robots.txt')

    with pytest.raises(SourceUnavailable, match='robots.txt'):
        fetch_base_page(fetch, 'https://example.org', 'https://example.org')
    assert calls == ['https://example.org']

    def timeout(url):
        calls.append(url)
        raise TimeoutError('network timeout')

    with pytest.raises(TimeoutError):
        fetch_base_page(timeout, 'https://example.org/products', 'https://example.org')
    assert calls[-1] == 'https://example.org/products'


def test_base_unreadable_site_has_insufficient_data_without_model_call(client):
    from app.campaigns import research_base
    from app.crawler import SourceUnavailable
    from app.db import Company
    from app.pipeline import Pipeline
    rid = new_run(client)
    with Session() as db:
        run = db.get(Run, rid)
        run.config = {**run.config, 'pipeline_version': 3, 'methodology_version': 'campaign-v1',
                      'research_as_of': '2026-10-10', 'max_website_pages': 2}
        run.reports = {**run.reports, 'campaign_plan': {'version': 'campaign-v1', 'source_hash': 'test',
                         'plan': plan().model_dump(), 'base_criteria': LEGACY_BASE_CRITERIA}}
        cid = db.query(Company).filter_by(run_id=rid).first().id
        db.commit()

    class NoModel:
        def ask(self, *args, **kwargs):
            raise AssertionError('An unreadable site must not trigger a model call')

    def fail(url):
        raise SourceUnavailable('HTTP 425')

    research_base(Pipeline(rid, NoModel()), cid, fail)
    with Session() as db:
        company = db.get(Company, cid)
        assert company.group == 'INSUFFICIENT_DATA'
        assert company.stage == 'PROFILE_READY'
        assert not company.research['base']['pages']
        assert company.research['base']['errors']
        assert all(answer['status'] == 'unknown' for answer in company.profile['base_research']['answers'])


def test_base_only_pilot_cannot_start_full_pipeline(client):
    rid = new_run(client)
    with Session() as db:
        run = db.get(Run, rid)
        run.reports = {'pilot': {'kind': 'base_only'}}
        original_status = run.status
        db.commit()
    response = client.post(f'/api/runs/{rid}/start')
    assert response.status_code == 409
    assert 'только базового сбора' in response.json()['detail']
    with Session() as db:
        assert db.get(Run, rid).status == original_status


def test_new_plan_separates_buyers_uses_and_explicit_product_relationship():
    from app.campaigns import criterion_signals
    def profile(quote, buyer, use):
        evidence=[{'id':'e','scope':'company','current':True,'subject_verified':True,'quote':quote}]
        values={'products':'Machines','buyer_types':buyer,'application_scenarios':use,
                'product_buyer_use_link':f'Machines | {buyer} | {use}'}
        answers=[CriterionAnswer(criterion_id=cid,status='confirmed',answer=value,evidence_ids=['e'],
                                 facts=[{'value':value,'evidence_ids':['e']}]) for cid,value in values.items()]
        checked=validate_answers(answers,BASE_CRITERIA,evidence)
        assert all(a['status']=='confirmed' for a in checked)
        return {s['name'] for s in criterion_signals(checked,evidence)}
    factories=profile('Machines serve factories for sheet cutting.','factories','sheet cutting')
    hospitals=profile('Machines serve hospitals for sterilization.','hospitals','sterilization')
    assert len(factories)==len(hospitals)==4
    assert len(factories & hospitals)==1  # Product matches; buyers, tasks, and relation do not.
    assert not {'criterion_buyer_types','criterion_application_scenarios','criterion_product_buyer_use_link'} & factories


def test_new_plan_rejects_generic_or_unlinked_answers():
    evidence=[{'id':'a','scope':'company','current':True,'subject_verified':True,'quote':'Machines serve factories.'},
              {'id':'b','scope':'company','current':True,'subject_verified':True,'quote':'Used for sheet cutting.'}]
    answers=[CriterionAnswer(criterion_id='buyer_types',status='confirmed',answer='confirmed',evidence_ids=['a'],
                             facts=[{'value':'confirmed','evidence_ids':['a']}]),
             CriterionAnswer(criterion_id='product_buyer_use_link',status='confirmed',
                             answer='Machines | factories | sheet cutting',evidence_ids=['a','b'],
                             facts=[{'value':'Machines | factories | sheet cutting','evidence_ids':['a','b']}])]
    checked=validate_answers(answers,BASE_CRITERIA,evidence)
    assert {a['criterion_id'] for a in checked if a['status']=='confirmed'}==set()


def test_historical_plan_is_reused_with_its_frozen_criteria(client):
    import hashlib
    from app.campaigns import VERSION
    assert VERSION=='campaign-v2'
    rid=new_run(client)
    with Session() as db:
        run=db.get(Run,rid)
        run.context_snapshot={'summary':SOURCE}
        run.reports={'campaign_plan':{'version':'campaign-v1','source_hash':hashlib.sha256(SOURCE.encode()).hexdigest(),
                                      'plan':plan().model_dump(),'base_criteria':LEGACY_BASE_CRITERIA}}
        db.commit()
    class NoCalls:
        id=rid
        def ask(self,*args,**kwargs):raise AssertionError('Historical plan must remain frozen')
    prepared=prepare_campaign_plan(NoCalls())
    assert prepared.criteria[0].id=='use_cases'
    with Session() as db:
        assert db.get(Run,rid).reports['campaign_plan']['version']=='campaign-v1'


def test_specific_answers_preserve_distinct_cited_values_across_pages():
    from app.campaigns import merge_base_answers, criterion_signals
    first={'criterion_id':'buyer_types','status':'confirmed','answer':'Factories',
           'evidence_ids':['a'],'value_specific':True,
           'facts':[{'value':'Factories','evidence_ids':['a']}]}
    second={'criterion_id':'buyer_types','status':'confirmed','answer':'Hospitals',
            'evidence_ids':['b'],'value_specific':True,
            'facts':[{'value':'Hospitals','evidence_ids':['b']}]}
    merged=merge_base_answers([first],[second])[0]
    assert merged['answer']=='Factories; Hospitals'
    assert merged['evidence_ids']==['a','b']
    assert [fact['value'] for fact in merged['facts']]==['Factories','Hospitals']
    evidence=[{'id':key,'scope':'company','current':True,'subject_verified':True}
              for key in ('a','b')]
    assert len(criterion_signals([merged],evidence))==2
    assert merge_base_answers([merged],[{**second, 'facts':[{'value':'Factories','evidence_ids':['b']}]}])[0]['facts'][0]['evidence_ids']==['a','b']


def test_product_buyer_use_link_rejects_unrelated_sentences():
    evidence=[{'id':'e','scope':'company','current':True,'subject_verified':True,
               'quote':'Machines serve factories. Hospitals use sterilization products.'}]
    answer=CriterionAnswer(criterion_id='product_buyer_use_link',status='confirmed',
                           answer='Machines | Hospitals | sterilization',evidence_ids=['e'],
                           facts=[{'value':'Machines | Hospitals | sterilization','evidence_ids':['e']}])
    checked=validate_answers([answer],BASE_CRITERIA,evidence)
    assert checked[0]['status']=='unknown'


def test_two_page_batch_keeps_page_provenance_and_rejects_misattributed_quote(client):
    from app.campaigns import BasePageResult, research_base
    from app.db import Company
    from app.pipeline import Pipeline

    rid = new_run(client)
    with Session() as db:
        run = db.get(Run, rid)
        run.config = {**run.config, 'pipeline_version': 3,
                      'methodology_version': 'campaign-v1',
                      'research_as_of': '2026-10-10', 'max_website_pages': 2}
        run.reports = {**run.reports, 'campaign_plan': {
            'version': 'campaign-v2', 'source_hash': 'test',
            'plan': {'offers': [], 'criteria': []}, 'base_criteria': BASE_CRITERIA}}
        cid = db.query(Company).filter_by(run_id=rid).first().id
        db.commit()

    root = 'https://alpha.example'
    second = root + '/products'
    first_text = 'Alpha sells industrial machines.'
    second_text = 'Manufacturers buy industrial machines for warehouses.'
    visited = []

    def fetch(url):
        visited.append(url)
        return {'url': url, 'text': first_text if url == root else second_text,
                'links': [{'url': second, 'label': 'Products'}] if url == root else []}

    class Fake:
        calls = 0
        def ask(self, role, instructions, payload, schema, **kwargs):
            assert schema is BasePageResult
            self.calls += 1
            assert [page['url'] for page in payload['pages']] == [root, second]
            assert kwargs['max_output'] == 2 * 5000
            return BasePageResult(report={
                'summary': 'Direct site facts',
                'evidence': [
                    {'id': 'e1', 'url': root, 'quote': first_text,
                     'published_at': None, 'scope': 'company', 'origin': root},
                    {'id': 'e2', 'url': second, 'quote': second_text,
                     'published_at': None, 'scope': 'company', 'origin': second},
                    {'id': 'wrong', 'url': root, 'quote': second_text,
                     'published_at': None, 'scope': 'company', 'origin': root},
                ], 'signals': [], 'contradictions': [], 'limitations': []},
                answers=[
                    {'criterion_id': 'products', 'status': 'confirmed',
                     'answer': 'industrial machines', 'evidence_ids': ['e1'],
                     'facts': [{'value': 'industrial machines', 'evidence_ids': ['e1']}]},
                    {'criterion_id': 'buyer_types', 'status': 'confirmed',
                     'answer': 'Manufacturers', 'evidence_ids': ['wrong'],
                     'facts': [{'value': 'Manufacturers', 'evidence_ids': ['wrong']}]},
                    {'criterion_id': 'application_scenarios', 'status': 'confirmed',
                     'answer': 'warehouses', 'evidence_ids': ['e2'],
                     'facts': [{'value': 'warehouses', 'evidence_ids': ['e2']}]},
                    {'criterion_id': 'product_buyer_use_link', 'status': 'confirmed',
                     'answer': 'industrial machines | Manufacturers | warehouses',
                     'evidence_ids': ['e2'], 'facts': [{
                         'value': 'industrial machines | Manufacturers | warehouses',
                         'evidence_ids': ['e2']}]},
                ])

    fake = Fake()
    research_base(Pipeline(rid, fake), cid, fetch, batch_pages=True)
    assert visited == [root, second]
    assert fake.calls == 1
    with Session() as db:
        company = db.get(Company, cid)
        base = company.research['base']
        answers = {answer['criterion_id']: answer for answer in base['answers']}
        assert base['batch_pages'] is True
        assert len(base['pages']) == 2
        assert len(base['evidence']) == 2
        assert answers['products']['status'] == 'confirmed'
        assert answers['buyer_types']['status'] == 'unknown'
        assert answers['application_scenarios']['status'] == 'confirmed'
        assert answers['product_buyer_use_link']['status'] == 'confirmed'
        assert 'buyer_types' in base['citation_losses']


def test_applicability_accepts_two_distinct_facts_in_one_verified_quote():
    from app.campaigns import classify_campaign
    from app.evidence import VERSION as EVIDENCE_VERSION
    evidence=[{'id':'quote','validation_version':EVIDENCE_VERSION,'subject_verified':True,
               'current':True,'scope':'company','quote':'We make parts for hospitals.',
               'original_source_id':'company:one'}]
    profile={'signals':[{'name':'makes_parts','state':'confirmed','confidence':'HIGH',
                         'evidence_ids':['quote'],'explanation':'Product'},
                        {'name':'hospitals','state':'confirmed','confidence':'HIGH',
                         'evidence_ids':['quote'],'explanation':'Buyer'}]}
    segment={'id':'s','basis_type':'applicability','required_signals':['makes_parts','hospitals'],
             'supporting_signals':[],'contradicting_signals':[]}
    assert classify_campaign(profile,evidence,[segment])[0]=='s'
    profile['signals'][1]['evidence_ids']=[]
    assert classify_campaign(profile,evidence,[segment])[0]!='s'


def test_campaign_v2_abstains_without_shared_facts_and_spends_nothing(client):
    from app.db import Company
    from app.pipeline import Pipeline
    rid=new_run(client)
    with Session() as db:
        run=db.get(Run,rid)
        run.config={**run.config,'pipeline_version':3,'methodology_version':'campaign-v1',
                    'research_as_of':'2026-10-10'}
        run.context_snapshot={'summary':SOURCE,'name':'Research'}
        run.reports={**run.reports,'campaign_plan':{
            'version':'campaign-v2','plan':plan().model_dump(),
            'base_criteria':BASE_CRITERIA}}
        for company in db.query(Company).filter_by(run_id=rid):
            company.identity='CONFIRMED';company.stage='PROFILE_READY'
            company.profile={'signals':[]}
        db.commit()
    class NoCalls:
        def ask(self,*args,**kwargs):
            raise AssertionError('No shared facts must incur no model cost')
    Pipeline(rid,NoCalls()).segment(saved_evidence_only=True)
    with Session() as db:
        report=db.get(Run,rid).reports['segmentation']
        assert report['diagnostics']['candidate_count']==0
        assert report['diagnostics']['model_batches']==0
        assert report['candidate_discovery']['summary']['candidate_count']==0


def test_compact_review_keeps_rule_citations_and_drops_unrelated_research():
    from app.pipeline import compact_segment_review_profile
    profile={'company_id':'company','description':'Example','research_as_of':'2026-10-10',
             'signals':[{'name':'relevant','state':'confirmed','confidence':'HIGH',
                         'evidence_ids':['source'],'explanation':'Relevant'},
                        {'name':'unrelated','state':'confirmed','confidence':'HIGH',
                         'evidence_ids':['other'],'explanation':'Other'}],
             'evidence':[{'id':'source','url':'https://example.test','quote':'A'*5000,
                          'scope':'company','subject_verified':True,'current':True},
                         {'id':'other','url':'https://example.test/other','quote':'B'*5000}],
             'base_research':{'large':'C'*50000}}
    segment={'required_signals':['relevant'],'supporting_signals':[],
             'contradicting_signals':[],'event_signals':[]}
    compact=compact_segment_review_profile(profile,segment)
    assert len(compact['signals'])==1
    assert [item['id'] for item in compact['evidence']]==['source']
    assert compact['evidence'][0]['quote_truncated'] is True
    assert 'base_research' not in compact


def test_campaign_rule_candidate_checks_each_company_raw_evidence(client,monkeypatch):
    from app.campaigns import CampaignSegments,criterion_signals
    from app.db import Company
    from app.evidence import VERSION as EVIDENCE_VERSION
    from app.pipeline import Pipeline
    from app.schemas import Review
    rid=new_run(client)
    with Session() as db:
        run=db.get(Run,rid)
        run.config={**run.config,'pipeline_version':3,'methodology_version':'campaign-v1',
                    'research_as_of':'2026-10-10'}
        run.context_snapshot={'summary':SOURCE,'name':'Research'}
        run.reports={**run.reports,'campaign_plan':{
            'version':'campaign-v2','plan':plan().model_dump(),
            'base_criteria':BASE_CRITERIA}}
        companies=db.query(Company).filter_by(run_id=rid).order_by(Company.id).all()
        for company,product,channel in zip(companies,('Септики','Компрессоры'),('дилерам','дилеров')):
            evidence=[{'id':'product-'+company.id,'validation_version':EVIDENCE_VERSION,
                       'subject_verified':True,'current':True,'scope':'company',
                       'quote':'Компания производит '+product,'url':company.resource},
                      {'id':'channel-'+company.id,'validation_version':EVIDENCE_VERSION,
                       'subject_verified':True,'current':True,'scope':'company',
                       'quote':'В публичных материалах упомянуты '+channel,
                       'url':company.resource}]
            answers=[{'criterion_id':criterion,'status':'confirmed','value_specific':True,
                      'facts':[{'value':value,'evidence_ids':[eid]}]}
                     for criterion,value,eid in (
                         ('products',product,evidence[0]['id']),
                         ('buyer_types',channel,evidence[1]['id']))]
            company.identity='CONFIRMED';company.stage='PROFILE_READY'
            company.profile={'signals':criterion_signals(answers,evidence)}
            company.research={'base':{'evidence':evidence}}
        db.commit()
    def no_fetch(*args,**kwargs):
        raise AssertionError('Saved-evidence replay must not fetch pages')
    monkeypatch.setattr('app.pipeline.fetch',no_fetch)
    class Fake:
        def ask(self,role,instructions,payload,schema,**kwargs):
            assert role=='segmentation'
            if schema is CampaignSegments:
                rule=payload['candidate_overview']['rule_candidates'][0]
                assert rule['company_count']==2
                return CampaignSegments(segments=[{
                    'id':rule['id'],'name':'Дилерский канал','definition':'Упомянуты дилеры и товары',
                    'offer_id':'research','hypothesis':'Проверить применимость услуги',
                    'confidence':'MEDIUM','required_signals':[],'supporting_signals':[],
                    'contradicting_signals':[],'outreach_angle':'Общее предложение',
                    'diagnostic_question':'Актуально ли обсудить?',
                    'allowed_claims':[],'forbidden_claims':['Дилеров не хватает'],
                    'basis_type':'applicability','cooperation_proposal':'Изучение дилерского канала',
                    'next_step':'Обсудить задачу','shared_message':'Предложение изучить дилерский канал',
                    'event_signals':[]}])
            assert schema is Review
            assert len(payload['profiles'])==2
            assert all(len(profile['signals'])>=2 for profile in payload['profiles'])
            return Review(passed=True,issues=[])
    Pipeline(rid,Fake()).finish(saved_evidence_only=True)
    with Session() as db:
        run=db.get(Run,rid)
        assert len(run.segments)==1
        assert len(run.segments[0]['member_bindings'])==2
        assert {company.group for company in db.query(Company).filter_by(run_id=rid)}=={'segment_001'}
        from app.presentation import company_view
        for company in db.query(Company).filter_by(run_id=rid):
            summary=company_view(company,run)
            detail=company_view(company,run,detail=True)
            assert summary['evidence_state']=='Достаточно для сегмента'
            assert detail['evidence_state']=='Достаточно для сегмента'
            assert {signal['name'].split('__')[0] for signal in summary['signals']} >= {
                'criterion_buyer_types','criterion_products'}
            assert 'member_bindings' not in summary['segment']
            assert set(detail['segment']['member_bindings'])=={company.id}
            assert set(detail['segment']['signal_labels']) >= {
                name for facet in ('channel','product')
                for name in detail['segment']['member_bindings'][company.id][facet]['raw_signal_names']}
        assert 'member_bindings' not in client.get(f'/api/runs/{rid}').json()['segments'][0]
        assert 'member_bindings' not in client.get(f'/api/runs/{rid}/segments').json()[0]
        from app.campaigns import classify_campaign
        from app.pipeline import evidence_for
        company=db.query(Company).filter_by(run_id=rid).first()
        assert classify_campaign({**company.profile,'company_id':'foreign'},evidence_for(company),run.segments)[0]!='segment_001'
        weakened={**company.profile,'company_id':company.id,
                  'signals':[signal for signal in company.profile['signals']
                             if not signal['name'].startswith('criterion_buyer_types__')]}
        assert classify_campaign(weakened,evidence_for(company),run.segments)[0]!='segment_001'
