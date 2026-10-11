"""One bounded, durable pass answering gaps in shared campaign candidates."""
import hashlib
import json
from urllib.parse import urlsplit
from .db import Session,Run,Company
from .schemas import Strict,CandidateSource
from .campaigns import BasePageResult,validate_answers,criterion_signals,classify_campaign
from .prompts import GUARD,EXTRACT
from .evidence import verify_report,normalized_signals
from .crawler import SourceUnavailable
from .llm import SearchLimit
from pydantic import Field

class TargetSources(Strict):
    sources:list[CandidateSource]=Field(max_length=5)
    limitations:list[str]


def perform_targeted(pipeline,requests,candidates,fetch):
    by_id={s['id']:s for s in candidates}
    with Session() as db:
        run=db.get(Run,pipeline.id);config=dict(run.config)
        criteria=run.reports['campaign_plan']['base_criteria']+run.reports['campaign_plan']['plan']['criteria']
        known_criteria={c['id']:c for c in criteria}
        companies={c.id for c in db.query(Company).filter_by(run_id=pipeline.id,identity='CONFIRMED',stage='PROFILE_READY')}
        if pipeline.selected_ids is not None:companies &= pipeline.selected_ids
    changed=False
    for task in requests:
        candidate=by_id.get(task.candidate_id)
        ids=list(dict.fromkeys(task.company_ids))
        if not candidate or task.criterion_id not in known_criteria or len(ids)<2 or not set(ids)<=companies:continue
        for cid in ids:
            with Session() as db:
                c=db.get(Company,cid);states=dict(c.context.get('targeted_tasks',{}))
                ev=[e for r in c.research.values() for e in r.get('evidence',[])]
                group,_,_=classify_campaign(c.profile,ev,[candidate])
                if group==candidate['id']:continue
                # Exclude completely unrelated companies: at least one candidate signal must exist.
                if not {s['name'] for s in c.profile.get('signals',[])} & set(candidate['required_signals']+candidate['supporting_signals']):continue
                key=hashlib.sha256(json.dumps({'candidate':candidate,'task':task.model_dump(),'company':cid},sort_keys=True).encode()).hexdigest()
                state=dict(states.get(key,{}))
                if state.get('state')=='COMPLETE':continue
                query_count=sum(bool(v.get('search_started')) for v in states.values())
                page_count=sum(len(v.get('attempted',[])) for v in states.values())
                if not state and query_count>=config.get('max_targeted_research_queries',3):continue
                if page_count>=config.get('max_targeted_research_pages',5):continue
                name,resource=c.name,c.resource
                state.setdefault('task',task.model_dump());state.setdefault('attempted',[]);state.setdefault('pages',{});state.setdefault('errors',[])
            def save():
                with Session() as db:
                    c=db.get(Company,cid);c.context={**c.context,'targeted_tasks':{**c.context.get('targeted_tasks',{}),key:state}};db.commit()
            if 'sources' not in state:
                state['search_started']=True;save()
                try:
                    found=pipeline.ask('discovery',GUARD+' Найди публичные первичные источники для конкретного вопроса общей кампании. Ищи подтверждения И опровержения. Не проводи интервью и не ищи личные страницы. Сторонний домен не означает независимого автора.',{'company':name,'resource':resource,'question':task.question,'decision_impact':task.decision_impact,'candidate':candidate,'date':config['research_as_of']},TargetSources,search=True,company_id=cid)
                except SearchLimit:
                    state.update(state='COMPLETE',termination_reason='search_limit');save();continue
                state['sources']=[s.model_dump() for s in found.sources];state['limitations']=found.limitations;save()
            for source in state['sources']:
                if page_count>=config.get('max_targeted_research_pages',5):break
                url=source['url']
                if url in state['attempted']:continue
                from .pipeline import checkpoint
                checkpoint(pipeline.id,'TARGETED_RESEARCH')
                page=state['pages'].get(url)
                if page is None:
                    try:page=fetch(url)
                    except SourceUnavailable as exc:
                        state['attempted'].append(url);page_count+=1;state['errors'].append({'url':url,'reason':str(exc)[:200]});save();continue
                    host=lambda u:(urlsplit(u).hostname or '').removeprefix('www.')
                    page={**page,'subject_names':[name]}
                    if host(page['url'])==host(resource):page.update(subject_id=cid,publisher_id='company:'+cid)
                    state['pages'][url]=page;save()
                result=pipeline.ask('extraction',EXTRACT+' Ответь также на критерий: answers ссылается на id report.evidence. Неизвестное — unknown. Не меняй общую кампанию под компанию.',{'company':name,'question':task.question,'criteria':[known_criteria[task.criterion_id]],'pages':[page],'date':config['research_as_of'],'direction':source['direction'],'freshness_days':config['social_forum_days']},BasePageResult,max_output=config['extraction_tokens'],company_id=cid)
                checked=verify_report(result.report,[page],source['direction'],config['social_forum_days'],as_of=config['research_as_of'],company_id=cid)
                mapping={e.id:hashlib.sha256((e.url+'\n'+' '.join(e.quote.split())).encode()).hexdigest()[:24] for e in result.report.evidence}
                answers=validate_answers([a.model_copy(update={'evidence_ids':[mapping.get(i,i) for i in a.evidence_ids]}) for a in result.answers],[known_criteria[task.criterion_id]],checked['evidence'])
                with Session() as db:
                    c=db.get(Company,cid);report_key='targeted_'+hashlib.sha256((key+url).encode()).hexdigest()[:20]
                    c.research={**c.research,report_key:{**checked,'state':'COMPLETE','question':task.question,'pages':[page]}}
                    base=dict(c.profile.get('base_research',{}));old={a['criterion_id']:a for a in base.get('answers',[])}
                    for a in answers:
                        if a['status']=='confirmed':old[a['criterion_id']]=a
                    base['answers']=list(old.values())
                    all_signals=[s for r in c.research.values() for s in r.get('signals',[])]
                    ev=[e for r in c.research.values() for e in r.get('evidence',[])]
                    c.profile={**c.profile,'signals':normalized_signals(all_signals+criterion_signals(base['answers'],ev)),'base_research':base}
                    group,_,trace=classify_campaign(c.profile,ev,[candidate]);db.commit()
                changed=True;state['attempted'].append(url);page_count+=1;save()
                if group==candidate['id'] or any('contradiction' in t['reasons'] for t in trace):
                    state['termination_reason']='decision_reached';break
            state.update(state='COMPLETE',termination_reason=state.get('termination_reason','page_limit' if page_count>=config.get('max_targeted_research_pages',5) else 'sources_exhausted'));save()
    return changed
