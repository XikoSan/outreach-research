"""Versioned offer-derived research contracts, independent of individual recipients."""
import hashlib
from typing import Literal
from pydantic import Field
from .schemas import Strict, ResearchReport, EvidenceItem
from .db import Session, Run
from .prompts import GUARD

VERSION = 'campaign-v1'

class OfferFact(Strict):
    kind: Literal['service','applicability','outcome','restriction']
    text: str = Field(min_length=1)
    source_quote: str = Field(min_length=10)

class CampaignOffer(Strict):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    facts: list[OfferFact] = Field(min_length=1)

class ResearchCriterion(Strict):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    answer_options: list[str] = Field(min_length=1)
    source_types: list[str] = Field(min_length=1)
    evidence_requirement: str = Field(min_length=1)
    decision_impact: str = Field(min_length=1)
    stop_condition: str = Field(min_length=1)
    offer_ids: list[str] = Field(min_length=1)

class CampaignResearchPlan(Strict):
    offers: list[CampaignOffer] = Field(min_length=1,max_length=15)
    criteria: list[ResearchCriterion] = Field(max_length=12)

# The common pass records observations, never an inferred commercial need.
BASE_CRITERIA = [
    {'id':'products','question':'Какие основные продукты или услуги предлагает компания?','decision_impact':'Соответствие направлению сотрудничества'},
    {'id':'audiences','question':'Какие типы покупателей и задачи применения названы прямо?','decision_impact':'Общая ситуация компаний сегмента'},
    {'id':'sales_model','question':'Какая модель продажи подтверждена: прямая, партнёрская, проектная, подписка?','decision_impact':'Выбор общего предложения и аргументации'},
    {'id':'presentation','question':'Как разделены продукты и аудитории в публичном предложении?','decision_impact':'Основания и опровержения гипотезы о позиционировании'},
]
for item in BASE_CRITERIA:
    item.update(answer_options=['подтверждено','не установлено'],source_types=['официальный сайт','официальная корпоративная страница'],evidence_requirement='Точная цитата и URL; не делать вывод об отсутствии по отсутствию упоминания',stop_condition='Получено прямое подтверждение либо исчерпаны доступные релевантные страницы базового прохода')

PLAN_PROMPT = GUARD + '''
Подготовь общий для всей базы план исследования на основе документа с предложением отправителя.
Не создавай гипотезы для конкретных компаний и не выдавай услуги за сегменты.
Выдели услуги и отдельно условия применимости, результаты и ограничения. Каждый факт должен иметь дословный source_quote из source_text; не усиливай обещания исходного документа.
Дополнительные criteria нужны только если ответ меняет выбор предложения или принадлежность к сегменту. Не повторяй четыре base_criteria. Для каждого вопроса укажи варианты ответа с «не установлено», доступные публичные источники, достаточное подтверждение, влияние на решение, условие остановки и offer_ids.
Не требуй интервью, контакта с компанией, внутренней выручки/конверсии и доказательства потребности в услуге. Не собирай личные данные сотрудников. Не вводи денежные лимиты.
'''

def source_span(quote, source):
    """Recover literal source text after presentation-only Markdown removal."""
    import re
    def plain(text):
        excluded=set()
        for pattern in (r'(?m)^\s*(?:[-*+] |\d+\. |#{1,6} )',r'\*\*|__|`'):
            for match in re.finditer(pattern,text):excluded.update(range(match.start(),match.end()))
        chars=[];positions=[]
        for index,character in enumerate(text):
            if index in excluded:continue
            if character.isspace():
                if chars and chars[-1]!=' ':chars.append(' ');positions.append(index)
            else:chars.append(character);positions.append(index)
        return ''.join(chars).strip(),positions
    if quote in source:return quote
    needle,_=plain(quote);haystack,positions=plain(source)
    start=haystack.find(needle) if needle else -1
    if start<0:return None
    return source[positions[start]:positions[start+len(needle)-1]+1]

def validate_plan(plan, source):
    offer_ids=[o.id for o in plan.offers]
    if len(offer_ids)!=len(set(offer_ids)):
        raise ValueError('Повторяющиеся идентификаторы предложений')
    for offer in plan.offers:
        if not any(f.kind=='service' for f in offer.facts):
            raise ValueError('Предложение не содержит подтверждённой услуги')
        for fact in offer.facts:
            literal=source_span(fact.source_quote,source)
            if literal is None:
                raise ValueError('Факт предложения не подтверждён точной цитатой')
            fact.source_quote=literal
    ids=[c.id for c in plan.criteria]
    if len(ids)!=len(set(ids)) or set(ids)&{c['id'] for c in BASE_CRITERIA}:
        raise ValueError('Критерии дублируют идентификаторы')
    for criterion in plan.criteria:
        if not set(criterion.offer_ids)<=set(offer_ids):
            raise ValueError('Критерий ссылается на неизвестное предложение')
        if 'не установлено' not in [v.strip().casefold() for v in criterion.answer_options]:
            raise ValueError('Критерий должен допускать неизвестный ответ')
    return plan

def prepare_campaign_plan(pipeline):
    """Explicit opt-in until the downstream campaign workflow has passed its pilot."""
    with Session() as db:
        run=db.get(Run,pipeline.id)
        source=run.context_snapshot.get('source_text') or run.context_snapshot.get('summary','')
        if not source.strip():raise ValueError('Для плана требуется документ с предложением')
        digest=hashlib.sha256(source.encode()).hexdigest()
        cached=run.reports.get('campaign_plan',{})
        if cached.get('source_hash')==digest and cached.get('version')==VERSION:
            return validate_plan(CampaignResearchPlan.model_validate(cached['plan']),source)
    # Existing context preparation bounds documents; exact quotes are checked against the original.
    with Session() as db:
        run=db.get(Run,pipeline.id)
        prepared=run.context_snapshot.get('prepared_text') or source
    plan=pipeline.ask('synthesis',PLAN_PROMPT,{'source_text':prepared,'base_criteria':BASE_CRITERIA},CampaignResearchPlan,max_output=6000)
    validate_plan(plan,source)
    with Session() as db:
        run=db.get(Run,pipeline.id);old=run.reports.get('campaign_plan')
        history=list(run.reports.get('campaign_plan_history',[]))
        if old:history.append(old)
        run.reports={**run.reports,'campaign_plan_history':history,'campaign_plan':{'version':VERSION,'source_hash':digest,'plan':plan.model_dump(),'base_criteria':BASE_CRITERIA}}
        db.commit()
    return plan

class CriterionAnswer(Strict):
    criterion_id: str
    status: Literal['confirmed','unknown']
    answer: str
    evidence_ids: list[str]


def validate_answers(answers, criteria, evidence):
    """Only bound, current, quoted company facts can fill a base criterion."""
    allowed={c['id'] for c in criteria}
    known={e['id']:e for e in evidence if e.get('subject_verified') and e.get('current') and e.get('scope') in ('company','product') and e.get('quote')}
    result=[];seen=set()
    for answer in answers:
        if answer.criterion_id not in allowed or answer.criterion_id in seen:
            raise ValueError('Неизвестный или повторяющийся критерий')
        seen.add(answer.criterion_id)
        data=answer.model_dump()
        if answer.status=='unknown' or not answer.evidence_ids or not set(answer.evidence_ids)<=set(known):
            data.update(status='unknown',answer='Не установлено',evidence_ids=[])
        result.append(data)
    for cid in sorted(allowed-seen):
        result.append({'criterion_id':cid,'status':'unknown','answer':'Не установлено','evidence_ids':[]})
    return result

BASIS_POLICIES = {
    'applicability': {'independent_sources_required':False,'min_confirmed_signals':2,'allowed_claim':'Предложение применимо к наблюдаемой ситуации; потребность не установлена'},
    'hypothesis': {'independent_sources_required':True,'min_confirmed_signals':2,'allowed_claim':'Осторожная гипотеза о потенциальной проблеме или возможности'},
    'event': {'independent_sources_required':True,'min_confirmed_signals':2,'dated_event_required':True,'allowed_claim':'Актуальное событие усиливает общую гипотезу, но не заменяет её'},
}

class BaseAnswers(Strict):
    answers: list[CriterionAnswer]

class CitationRepairResult(Strict):
    evidence: list[EvidenceItem]
    answers: list[CriterionAnswer]
    limitations: list[str]


def repair_citation_losses(pipeline, company_id, wanted):
    """Recheck only lost criteria against saved pages; never browse or relax quotes."""
    from .db import Company
    from .evidence import verify_report, normalized_signals
    with Session() as db:
        run=db.get(Run,pipeline.id);company=db.get(Company,company_id)
        if run.config.get('methodology_version')!='campaign-v1':return False
        base=company.research.get('base',{})
        criteria=run.reports['campaign_plan']['base_criteria']+run.reports['campaign_plan']['plan']['criteria']
        by_id={item['id']:item for item in criteria}
        answers={item['criterion_id']:item for item in company.profile.get('base_research',{}).get('answers',[])}
        previous=set(base.get('citation_repair_attempts',[]))
        requested=sorted(set(wanted)&set(base.get('citation_losses',[]))&set(by_id)-previous)
        requested=[cid for cid in requested if answers.get(cid,{}).get('status')!='confirmed']
        pages=base.get('pages',[])
        if not requested or not pages:return False
        # One bounded call per company. The original pages remain the evidence source.
        selected_pages=[{**page,'text':page['text'][:32000]} for page in pages[:3] if page.get('subject_id')==company_id and page.get('publisher_id')=='company:'+company_id]
        if not selected_pages:return False
        name=company.name;as_of=run.config['research_as_of'];days=run.config['social_forum_days']
    prompt=GUARD+'''Восстанови ТОЛЬКО указанные потерянные ответы по сохранённым страницам. Каждый evidence.quote должен быть одним непрерывным буквальным фрагментом одной страницы, без «...», «[…]», добавленных кавычек и склейки частей. Если для утверждения нужны несколько фрагментов, создай отдельный evidence для каждого. Ответ формулируй только в пределах этих цитат; иначе status=unknown. Не ищи источники и не делай выводов о потребности в услуге. Не добавляй другие критерии.'''
    result=pipeline.ask('extraction',prompt,{'company':name,'criteria':[by_id[cid] for cid in requested],'pages':selected_pages,'date':as_of},CitationRepairResult,max_output=2600,company_id=company_id)
    checked=verify_report(ResearchReport(summary='Перепроверка цитат',evidence=result.evidence,signals=[],contradictions=[],limitations=result.limitations),selected_pages,'website',days,as_of=as_of,company_id=company_id)
    mapping={item.id:hashlib.sha256((item.url+'\n'+' '.join(item.quote.split())).encode()).hexdigest()[:24] for item in result.evidence}
    rewritten=[answer.model_copy(update={'evidence_ids':[mapping.get(eid,eid) for eid in answer.evidence_ids]}) for answer in result.answers]
    fresh=validate_answers(rewritten,[by_id[cid] for cid in requested],checked['evidence'])
    recovered={item['criterion_id']:item for item in fresh if item['status']=='confirmed'}
    with Session() as db:
        company=db.get(Company,company_id)
        base=dict(company.research.get('base',{}));base['citation_repair_attempts']=sorted(set(base.get('citation_repair_attempts',[]))|set(requested))
        base['citation_losses']=sorted(set(base.get('citation_losses',[]))-set(recovered))
        repair_key='base_repair_'+hashlib.sha256(','.join(requested).encode()).hexdigest()[:12]
        company.research={**company.research,'base':base,repair_key:{**checked,'criteria':requested,'state':'COMPLETE','page_urls':[p['url'] for p in selected_pages]}}
        old={item['criterion_id']:item for item in company.profile.get('base_research',{}).get('answers',[])}
        old.update(recovered)
        all_evidence=[e for report in company.research.values() for e in report.get('evidence',[])]
        raw_signals=[signal for report in company.research.values() for signal in report.get('signals',[])]
        company.profile={**company.profile,'base_research':{**company.profile.get('base_research',{}),'answers':list(old.values())},'signals':normalized_signals(raw_signals+criterion_signals(list(old.values()),all_evidence))}
        db.commit()
    return bool(recovered)


def complete_base_answers(pipeline, company_id):
    """Answer the shared research contract from validated facts, without web searches."""
    from .db import Company
    with Session() as db:
        run=db.get(Run,pipeline.id)
        contract=run.reports.get('campaign_plan')
        if not contract:return
        company=db.get(Company,company_id)
        criteria=contract['base_criteria']+contract['plan']['criteria']
        evidence=[e for report in company.research.values() for e in report.get('evidence',[])]
        verified=[e for e in evidence if e.get('subject_verified') and e.get('current') and e.get('scope') in ('company','product')]
    if verified:
        response=pipeline.ask('extraction',GUARD+' Ответь на общие критерии исследования только по переданным evidence. Неподтверждённый ответ — unknown. Не выводи потребность или отсутствие свойства из молчания источника. evidence_ids только из входа.',{'criteria':criteria,'evidence':verified},BaseAnswers,max_output=3000,company_id=company_id)
        answers=validate_answers(response.answers,criteria,verified)
    else:answers=validate_answers([],criteria,[])
    with Session() as db:
        company=db.get(Company,company_id)
        company.profile={**company.profile,'base_research':{'version':VERSION,'source_hash':contract['source_hash'],'answers':answers}}
        db.commit()

def criterion_signals(answers, evidence):
    """Comparable, evidence-backed names for answers shared across companies."""
    from .evidence import normalized_signals
    known={e['id']:e for e in evidence if e.get('subject_verified') and e.get('current') and e.get('scope') in ('company','product')}
    signals=[]
    for answer in answers:
        if answer.get('status')!='confirmed':continue
        ids=list(dict.fromkeys(i for i in answer.get('evidence_ids',[]) if i in known))
        if not ids:continue
        signals.append({'name':'criterion_'+answer['criterion_id'],'label':answer['criterion_id'],'state':'confirmed','confidence':'HIGH','evidence_ids':ids,'explanation':answer.get('answer','')})
    return normalized_signals(signals)

class BasePageResult(Strict):
    report: ResearchReport
    answers: list[CriterionAnswer]


def research_base(pipeline,cid,fetch):
    from .db import Company
    from .evidence import verify_report, normalized_signals
    from .pipeline import checkpoint
    from .crawler import SourceUnavailable
    from urllib.parse import urlsplit
    from .acquisition import social
    with Session() as db:
        run=db.get(Run,pipeline.id);c=db.get(Company,cid)
        contract=run.reports['campaign_plan'];config=dict(run.config)
        criteria=contract['base_criteria']+contract['plan']['criteria']
        resource=c.resource;name=c.name
        state=dict(c.research.get('base',{}));pages=list(state.get('pages',[]));attempted=list(state.get('attempted_urls',[]));errors=list(state.get('errors',[]))
        answers=state.get('answers',[]);evidence=state.get('evidence',[]);signals=state.get('signals',[]);citation_losses=set(state.get('citation_losses',[]))
        if c.context.get('identity_conflict') or resource.startswith('unresolved:') or social(resource) or resource in c.context.get('socials',[]):
            reason=('Конфликт ИНН у строк с одним опорным адресом' if c.context.get('identity_conflict')
                    else 'Нет пригодного сайта или корпоративной соцстраницы' if resource.startswith('unresolved:')
                    else 'Базовое исследование по корпоративной соцстранице пока недоступно')
            c.identity='NEEDS_REVIEW';c.stage='PROFILE_READY';c.group='NEEDS_REVIEW';c.reason=reason;db.commit();return
    queue=[resource]
    def links(page):
        host=(urlsplit(resource).hostname or '').removeprefix('www.')
        return [v['url'] for v in page.get('links',[]) if (urlsplit(v['url']).hostname or '').removeprefix('www.')==host and any(k in (v['url']+' '+v['label']).lower() for k in ('product','service','solution','partner','about','продукт','услуг','решен','партнер','компан'))]
    for page in pages:queue.extend(links(page))
    limit=config['max_website_pages']
    while queue and len(attempted)<limit:
        if answers and all(a['status']=='confirmed' for a in answers):break
        url=queue.pop(0)
        if url in attempted:continue
        checkpoint(pipeline.id,'BASE_RESEARCH')
        page=next((p for p in pages if p['url']==url),None)
        if page is None:
            try:page=fetch(url)
            except SourceUnavailable as exc:
                attempted.append(url);errors.append({'url':url,'reason':str(exc)[:200]})
                with Session() as db:
                    c=db.get(Company,cid);c.research={**c.research,'base':{**state,'pages':pages,'attempted_urls':attempted,'errors':errors,'answers':answers,'evidence':evidence,'signals':signals}};db.commit()
                continue
            if (urlsplit(page['url']).hostname or '').removeprefix('www.')!=(urlsplit(resource).hostname or '').removeprefix('www.'):
                attempted.append(url);errors.append({'url':url,'reason':'Неподтверждённый междоменный редирект'});continue
            page={**page,'subject_id':cid,'publisher_id':'company:'+cid}
            pages.append(page)
        # Persist the fetched page before any paid work, to survive extraction failure.
        with Session() as db:
            c=db.get(Company,cid);c.research={**c.research,'base':{**state,'pages':pages,'attempted_urls':attempted,'errors':errors,'answers':answers,'evidence':evidence,'signals':signals}};db.commit()
        result=pipeline.ask('extraction',GUARD+' Извлеки атомарные признаки и заполни критерии. report.evidence: точная цитата и URL страницы. answers используют id из report.evidence. Неизвестное — unknown; не делай вывод о потребности. Указывай опровержения.',{'company':name,'criteria':criteria,'pages':[page],'date':config['research_as_of']},BasePageResult,max_output=config['extraction_tokens'],company_id=cid)
        checked=verify_report(result.report,[page],'website',config['social_forum_days'],as_of=config['research_as_of'],company_id=cid)
        import hashlib
        mapping={e.id:hashlib.sha256((e.url+'\n'+' '.join(e.quote.split())).encode()).hexdigest()[:24] for e in result.report.evidence}
        rewritten=[a.model_copy(update={'evidence_ids':[mapping.get(i,i) for i in a.evidence_ids]}) for a in result.answers]
        fresh=validate_answers(rewritten,criteria,checked['evidence'])
        accepted_by_id={a['criterion_id']:a for a in fresh}
        citation_losses.update(a.criterion_id for a in rewritten if a.status=='confirmed' and accepted_by_id[a.criterion_id]['status']=='unknown')
        prior={a['criterion_id']:a for a in answers}
        for a in fresh:
            if a['status']=='confirmed' or a['criterion_id'] not in prior:prior[a['criterion_id']]=a
        answers=list(prior.values());citation_losses.difference_update(a['criterion_id'] for a in answers if a['status']=='confirmed');evidence=list({e['id']:e for e in evidence+checked['evidence']}.values());signals=normalized_signals(signals+checked['signals'])
        attempted.append(url);queue.extend(links(page))
        state={'pages':pages,'attempted_urls':attempted,'errors':errors,'answers':answers,'evidence':evidence,'signals':signals,'citation_losses':sorted(citation_losses),'summary':'Базовый сбор по критериям','limitations':checked['limitations'],'contradictions':checked['contradictions']}
        with Session() as db:
            c=db.get(Company,cid);c.research={**c.research,'base':state};db.commit()
    answers=answers or validate_answers([],criteria,[])
    with Session() as db:
        c=db.get(Company,cid);c.identity='CONFIRMED';c.stage='PROFILE_READY';c.group='NOT_PROCESSED'
        c.profile={'description':name,'signals':normalized_signals(signals+criterion_signals(answers,evidence)),'base_research':{'version':VERSION,'source_hash':contract['source_hash'],'answers':answers},'coverage_complete':False,'research_as_of':config['research_as_of'],'limitations':['Базовый проход; внешние источники не проверялись'],'contradictions':state.get('contradictions',[])}
        c.research={**c.research,'base':{**state,'pages':pages,'attempted_urls':attempted,'errors':errors,'answers':answers,'evidence':evidence,'signals':signals,'state':'COMPLETE','termination_reason':'criteria_answered' if all(a['status']=='confirmed' for a in answers) else 'page_limit' if len(attempted)>=limit else 'sources_exhausted'}}
        db.commit()

from .schemas import Segment, Segments

class CampaignSegment(Segment):
    basis_type: Literal['applicability','hypothesis','event']
    cooperation_proposal: str = Field(min_length=10)
    next_step: str = Field(min_length=3)
    shared_message: str = Field(min_length=10)
    event_signals: list[str]

class TargetedQuestion(Strict):
    candidate_id: str
    company_ids: list[str] = Field(min_length=2,max_length=50)
    criterion_id: str
    question: str = Field(min_length=15,max_length=1000)
    decision_impact: str = Field(min_length=10,max_length=1000)

class CampaignSegments(Segments):
    segments: list[CampaignSegment]
    research_requests: list[TargetedQuestion] = Field(default_factory=list,max_length=12)

CAMPAIGN_PROMPT=GUARD+'''
В required_signals, supporting_signals и contradicting_signals записывай ТОЛЬКО точные значения поля name из profiles.examples.signals. Не пиши объяснения, метки, названия компаний, confidence, evidence_ids или предложения внутри этих списков. Общие подтверждённые критерии имеют имена criterion_products, criterion_audiences и т. д. Если у компаний разные имена одного признака, используй общий criterion_*. Найди повторяющиеся ситуации по всей переданной выборке и предложи общие кампании, не отдельное предложение каждой компании. Минимум две компании на сегмент. Один сегмент — одна услуга, общая аргументация и следующий шаг; одинаковые стратегии объединяй. Используй base_research и подтверждённые сигналы.
applicability: минимум два содержательных признака уместности услуги; допускается один официальный источник. Нельзя заявлять проблему или потребность. hypothesis: минимум два независимых сигнала и осторожная гипотеза. event: те же требования плюс датированный актуальный event_signals. Required только HIGH, supporting HIGH/MEDIUM. LOW не используй. Не придумывай услуги, признаки или доказательства. Отрасль сама по себе не сегмент. cooperation_proposal, shared_message и next_step общие для группы. Гипотеза применимости объясняет уместность сотрудничества, а не диагноз. Если оснований нет, объясни abstention_reason и missing_data, доступные в публичных источниках.
'''
CAMPAIGN_PROMPT+=' Если конкретный пробел мешает проверить ОБЩЕГО кандидата, добавь research_requests: существующий candidate_id, минимум две company_ids из примеров выборки, criterion_id из base_research.answers, конкретный вопрос проверки или опровержения и влияние ответа на решение. Не создавай запрос без кандидата, личный оффер или общее задание исследовать глубже. Запрос необязателен; если данных достаточно, оставь пусто.'
CAMPAIGN_REVIEW=GUARD+'''
Проверь общую кампанию по basis_type: applicability требует двух содержательных проверенных признаков применимости, но не независимых источников; hypothesis и event требуют независимых оснований. Для event нужен подтверждённый актуальный датированный признак. Required HIGH; LOW запрещён для назначения. Проверь соответствие реальной услуге, общность предложения, аргументации и следующего шага, отсутствие диагноза в applicability, опровержения и различие с other_segments. Не требуй доказать внутреннюю потребность. passed только если существенных ошибок нет.
'''

def classify_campaign(profile,evidence,segments):
    from .rules import classify_with_trace
    from .evidence import normalized_signals,VERSION as EV_VERSION
    signals={s['name']:s for s in normalized_signals(profile.get('signals',[]))}
    ev={e['id']:e for e in evidence if e.get('validation_version')==EV_VERSION and e.get('subject_verified') and e.get('current') and e.get('scope') in ('company','product')}
    accepted=[];traces=[]
    for seg in segments:
        if seg['basis_type']=='applicability':
            required=seg['required_signals'];reasons=[]
            matches=[s for n,s in signals.items() if n in required+seg['supporting_signals'] and s['state']=='confirmed' and s['confidence'] in ('HIGH','MEDIUM') and any(i in ev for i in s['evidence_ids'])]
            if not required or any(n not in signals or signals[n]['state']!='confirmed' or signals[n]['confidence']!='HIGH' or not any(i in ev for i in signals[n]['evidence_ids']) for n in required):reasons.append('required_not_verified')
            if len(matches)<2 or len({i for s in matches for i in s['evidence_ids'] if i in ev})<2:reasons.append('insufficient_distinct_facts')
            if any(signals.get(n,{}).get('state')=='confirmed' and signals[n]['confidence'] in ('HIGH','MEDIUM') for n in seg['contradicting_signals']):reasons.append('contradiction')
            trace={'segment_id':seg['id'],'accepted':not reasons,'reasons':reasons,'matched_signals':[s['name'] for s in matches]}
        else:
            _,_,ts=classify_with_trace({**profile,'coverage_complete':True},evidence,[seg]);trace=ts[0]
            if seg['basis_type']=='event':
                from datetime import date
                reference=date.fromisoformat(profile.get('research_as_of',date.today().isoformat())[:10])
                def recent(e):
                    try:return 0<=(reference-date.fromisoformat(e['published_at'][:10])).days<=180
                    except (ValueError,TypeError,KeyError):return False
                dated=any(n in signals and signals[n]['state']=='confirmed' and signals[n]['confidence']=='HIGH' and any(i in ev and recent(ev[i]) for i in signals[n]['evidence_ids']) for n in seg['event_signals'])
                if not dated:trace['reasons'].append('event_not_verified');trace['accepted']=False
        traces.append(trace)
        if trace['accepted']:accepted.append(seg['id'])
    if len(accepted)==1:return accepted[0],'Подтверждены правила общей кампании',traces
    if accepted:return 'AMBIGUOUS','Подходит несколько общих предложений',traces
    return 'UNCLASSIFIED' if ev else 'INSUFFICIENT_DATA','Нет подходящей проверенной кампании',traces


def campaign_prompt_contract(segment,next_step):
    """Freeze the common message; user-selected CTA overrides the proposal default."""
    required=('basis_type','cooperation_proposal','shared_message')
    if any(not segment.get(k) for k in required):
        raise ValueError('Кампания не содержит общего предложения и идеи письма')
    if segment['basis_type'] not in BASIS_POLICIES:
        raise ValueError('Неизвестный тип основания кампании')
    return {'basis_type':segment['basis_type'],'cooperation_proposal':segment['cooperation_proposal'],
            'shared_message':segment['shared_message'],'next_step':next_step,
            'allowed_claim':BASIS_POLICIES[segment['basis_type']]['allowed_claim'],
            'forbidden_claims':segment.get('forbidden_claims',[]),
            'personalization_only':['recipient_name','company_name','verified_product_example']}
