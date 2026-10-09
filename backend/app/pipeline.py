import json, re, time, hashlib
from datetime import datetime, timezone, timedelta
from collections import Counter
from openai import AuthenticationError, PermissionDeniedError, NotFoundError
from urllib.parse import urlsplit
from .db import Session, Run, Company, log
from .llm import Gateway, BudgetPause, RunPaused, UncertainCall, SearchLimit
from .schemas import Discovery, ResearchReport, Profile, Segments, Review, PromptResult
from .crawler import fetch
from .rules import classify, origin, classify_with_trace
from . import evidence as evidence_v3
from . import acquisition
from .jobs import execute_jobs, touch
from .prompts import GUARD, DISCOVERY, EXTRACT, SYNTHESIS, SEGMENT, REVIEW, PROMPT
from .workbooks import export_run
from .config import settings

DIRECTIONS=['website','social','news_jobs','reviews','forums']
LIMIT_KEYS={'website':'max_website_pages','social':'max_social_sources','news_jobs':'max_external_sources','reviews':'max_review_sources','forums':'max_review_sources'}

def sender_directions(sender):
    # Legacy run snapshots retain their original scope. New profiles need no offer catalogue.
    if sender.get('offers'):return sender['offers']
    description=sender.get('description','').strip()
    if not description:return []
    return [{'id':'sender_activity','name':sender['brand'],'description':description,
             'outcomes':sender.get('approved_claims',''),
             'limits':'Формируй гипотезы из исследований. Не приписывай отправителю услуги и результаты, которых нет в описании.'}]

def research_sender(run):
    if run.context_snapshot:
        return {'brand':run.context_snapshot['name'],'description':run.context_snapshot.get('prepared_text') or run.context_snapshot.get('source_text') or run.context_snapshot['summary'],'offers':[]}
    return run.sender_snapshot.get('profile',{})

def checkpoint(run_id,stage):
    with Session() as db:
        r=db.get(Run,run_id)
        if r.status not in ('RUNNING',):raise RunPaused()
        if r.config.get('pipeline_version',1)<3 or stage in ('SEGMENTATION','CLASSIFICATION','EXPORT'):r.stage=stage
        else:r.stage='RESEARCH'
        db.commit()

def evidence_for(company):
    return [e for report in company.research.values() for e in report.get('evidence',[])]

def verify_report(report,pages,direction,days):
    by_url={p['url']:p for p in pages}
    accepted=[];mapping={};issues=[]
    for e in report.evidence:
        page=by_url.get(e.url)
        quote=' '.join(e.quote.split())
        if not page or len(quote)<15 or quote not in ' '.join(page['text'].split()):
            issues.append('Отброшено доказательство без точной цитаты');continue
        # Undated and older social/forum evidence is context only, never a primary signal.
        current=True
        if direction in ('social','forums'):
            try: current=datetime.fromisoformat(e.published_at.replace('Z','+00:00')).date() >= (datetime.now(timezone.utc)-timedelta(days=days)).date() and e.published_at[:10] <= datetime.now(timezone.utc).date().isoformat()
            except (TypeError,ValueError,AttributeError):current=False
        eid=hashlib.sha256((e.url+'\n'+quote).encode()).hexdigest()[:24]
        mapping[e.id]=eid
        accepted.append({**e.model_dump(),'id':eid,'origin':origin(e.url),'retrieved_at':datetime.now(timezone.utc).isoformat(),'current':current})
    known={e['id']:e for e in accepted}
    signals=[]
    for s in report.signals:
        ids=list(dict.fromkeys(mapping[i] for i in s.evidence_ids if i in mapping))
        confidence=s.confidence
        if not ids:continue
        if not all(known[i]['current'] for i in ids):confidence='LOW'
        signals.append({**s.model_dump(),'confidence':confidence,'evidence_ids':ids})
    return {'summary':report.summary,'evidence':accepted,'signals':signals,'contradictions':report.contradictions,'limitations':report.limitations+issues}

class Pipeline:
    def __init__(self,run_id,gateway=None):self.id=run_id;self.gw=gateway or Gateway(run_id);self.injected_gateway=gateway is not None
    def ask(self,*args,**kwargs):
        with Session() as db:retries=db.get(Run,self.id).config['max_retries']
        for attempt in range(retries+1):
            checkpoint(self.id,args[0].upper())
            if kwargs.get('company_id'):touch(self.id,kwargs['company_id'],args[0].upper())
            try:return self.gw.ask(*args,**kwargs)
            except (BudgetPause,RunPaused,UncertainCall,SearchLimit,AuthenticationError,PermissionDeniedError,NotFoundError):raise
            except Exception as error:
                from openai import APIStatusError
                if not isinstance(error,APIStatusError) or error.status_code!=429 or attempt==retries:raise
                with Session() as db:log(db,self.id,args[0],'Повтор этапа',kwargs.get('company_id'),{'attempt':attempt+1});db.commit()
                import random
                time.sleep(min(2**attempt,4)+random.random())
    def research(self, company_id):
        with Session() as db:
            c=db.get(Company,company_id);r=db.get(Run,self.id)
            if c.stage=='PROFILE_READY' or c.identity=='NEEDS_REVIEW':return
            config=dict(r.config);config.setdefault('research_as_of',r.created_at[:10]);sender={**research_sender(r),'offers':sender_directions(research_sender(r))};resource=c.resource;name=c.name;context=c.context
        v3=config.get('pipeline_version',1)>=3
        plan=acquisition.prepare(self,company_id,config,sender,fetch) if v3 else None
        if plan and not plan['identity_confirmed']:return
        for direction in DIRECTIONS:
            checkpoint(self.id,'RESEARCH')
            with Session() as db:
                c=db.get(Company,company_id);reports=dict(c.research)
                state=dict(reports.get(direction,{}))
                if state.get('state') in ('COMPLETE','NO_RESULTS','UNAVAILABLE','LIMIT_REACHED'):continue
                review_attempts=sum(len(v.get('attempted_urls',[])) for k,v in reports.items() if k in ('reviews','forums'))
            limit=config[LIMIT_KEYS[direction]]
            if direction in ('reviews','forums'):
                limit=max(0,limit-review_attempts)
                if direction=='reviews' and limit>1:limit-=1  # Preserve a forum check.
            if 'sources' not in state:
                sources=[];found=None;limited=False
                if plan:
                    sources=plan['sources'][direction]
                    limited=bool(sources and limit==0)
                    found=Discovery.model_validate(plan['discovery'])
                elif limit:
                    try:
                        found=self.ask('discovery',DISCOVERY,{'company':name,'resource':resource,'names':context.get('names',[]),'description':context.get('description',''),'direction':direction,'limit':limit,'offers':sender['offers'],'source_filters':config.get('source_filters',''),'date':config['research_as_of'],'freshness_days':config['social_forum_days']},Discovery,search=True,company_id=company_id)
                        sources=[x.url for x in found.sources if x.direction==direction]
                    except SearchLimit:limited=True
                if direction=='website' and not plan:
                    if found is None or not found.identity_confirmed:
                        with Session() as db:
                            c=db.get(Company,company_id);c.identity='NEEDS_REVIEW';c.group='NEEDS_REVIEW';c.stage='PROFILE_READY';c.reason=found.identity_reason if found else 'Не удалось подтвердить идентичность в пределах лимита';c.research={**c.research,direction:{'state':'IDENTITY_REJECTED','discovery':found.model_dump() if found else {},'sources':sources,'summary':c.reason}};db.commit()
                        return
                    with Session() as db:
                        c=db.get(Company,company_id);c.identity='CONFIRMED';c.reason=found.identity_reason;db.commit()
                    if not any(h in (urlsplit(resource).hostname or '') for h in ('vk.com','linkedin.com')):sources=[resource]+sources
                if direction=='social' and not plan:sources=context.get('socials',[])+sources
                state={'discovery':found.model_dump() if found else {},'state':'DISCOVERED','sources':list(dict.fromkeys(sources))[:limit],'pages':[],'errors':[],'attempted_urls':[],'limited':limited}
                with Session() as db:
                    c=db.get(Company,company_id);c.research={**c.research,direction:state};db.commit()
            for url in state['sources']:
                if url in state['attempted_urls']:continue
                checkpoint(self.id,'COLLECTING')
                if direction=='website' and origin(url)!=origin(resource):continue
                try:
                    page=dict(plan['initial_page']) if plan and plan.get('initial_page') and url==plan['initial_page']['url'] else fetch(url)
                    if plan and (direction=='website' or (url in plan['official_socials'] and any(n.casefold() in page['text'].casefold() for n in context.get('names',[name]) if len(n)>=3))):page={**page,'subject_id':company_id,'publisher_id':'company:'+company_id}
                    if plan:page={**page,'subject_names':context.get('names',[name])}
                    state['pages'].append(page)
                except Exception as e:state['errors'].append({'url':url,'reason':str(e)[:200]})
                state['attempted_urls'].append(url)
                with Session() as db:
                    c=db.get(Company,company_id);c.research={**c.research,direction:state};db.commit()
            pages=state['pages'];result={'state':'LIMIT_REACHED' if state['limited'] else ('NO_RESULTS' if not state['sources'] else 'UNAVAILABLE'),'summary':'','evidence':[],'signals':[],'contradictions':[],'limitations':[]}
            if pages:
                # Bounded extraction batches avoid context growth on long pages.
                chunks=[];chunk=[];size=0
                for page in pages:
                    size_next=len(json.dumps(page,ensure_ascii=False).encode())
                    if size+size_next>70_000 and chunk:chunks.append(chunk);chunk=[];size=0
                    chunk.append(page);size+=size_next
                if chunk:chunks.append(chunk)
                for part in chunks:
                    chunk_key=hashlib.sha256(json.dumps(part,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                    saved=state.get('chunks',{}).get(chunk_key) if v3 else None
                    if saved:
                        result['summary']+='\n'+saved['summary']
                        for field in ('evidence','signals','contradictions','limitations'):result[field]+=saved[field]
                        continue
                    report=self.ask('extraction',EXTRACT,{'company':name,'direction':direction,'date':config['research_as_of'],'offers':sender['offers'],'source_filters':config.get('source_filters',''),'freshness_days':config['social_forum_days'],'pages':part},ResearchReport,max_output=config['extraction_tokens'],company_id=company_id)
                    checked=evidence_v3.verify_report(report,part,direction,config['social_forum_days'],as_of=config['research_as_of'],company_id=company_id) if v3 else verify_report(report,part,direction,config['social_forum_days'])
                    if v3:
                        state['chunks']={**state.get('chunks',{}),chunk_key:checked}
                        with Session() as db:
                            c=db.get(Company,company_id);c.research={**c.research,direction:state};db.commit()
                    result['summary']+='\n'+checked['summary']
                    for field in ('evidence','signals','contradictions','limitations'):result[field]+=checked[field]
                result['state']='COMPLETE'
            if v3:
                result['coverage']='partial' if state['errors'] or state['limited'] else ('checked' if pages or not state['sources'] else 'unavailable')
                result['termination_reason']='resource_limit' if state['limited'] else ('access_error' if state['errors'] else ('sources_processed' if pages else 'no_results'))
                result['external_checked']=direction in plan['checked_directions']
                if direction!='website' and not result['external_checked']:result['coverage']='not_checked'
                result['validation_version']=evidence_v3.VERSION
            with Session() as db:
                c=db.get(Company,company_id);c.research={**c.research,direction:{**state,**result}};c.stage='RESEARCHING'
                log(db,self.id,direction,'Направление завершено',company_id,{'pages':len(pages),'evidence':len(result['evidence']),'state':result['state']});db.commit()
        with Session() as db:
            c=db.get(Company,company_id)
            reports={k:{a:b for a,b in v.items() if a in ('summary','evidence','signals','contradictions','limitations')} for k,v in c.research.items()}
            all_evidence=evidence_for(c)
        if all_evidence:
            # Quotes are already validated; synthesis needs compact evidence identities.
            compact={k:{**v,'evidence':[{key:e.get(key) for key in ('id','url','scope','quote','published_at','origin')} for e in v['evidence']]} for k,v in reports.items()}
            profile=self.ask('synthesis',SYNTHESIS,{'company':name,'reports':compact,'offers':sender['offers'],'source_filters':config.get('source_filters',''),'date':config['research_as_of'],'freshness_days':config['social_forum_days']},Profile,company_id=company_id)
            known={e['id'] for e in all_evidence};original=[s for v in reports.values() for s in v['signals']]
            checked=[];ranks={'LOW':0,'MEDIUM':1,'HIGH':2}
            for s in profile.signals:
                if not s.evidence_ids or not set(s.evidence_ids)<=known:continue
                supports=[v for v in original if set(v['evidence_ids']) & set(s.evidence_ids)]
                if not supports:continue
                rank=min([ranks[s.confidence]]+[ranks[v['confidence']] for v in supports])
                states={v['state'] for v in supports}
                conflict='confirmed' in states and 'refuted' in states
                checked.append({**s.model_dump(),'state':'unknown' if conflict else s.state,'confidence':'LOW' if conflict else ['LOW','MEDIUM','HIGH'][rank]})
            if v3:checked=evidence_v3.preserve_synthesis(profile.signals,original,all_evidence)
            payload={**profile.model_dump(),'signals':checked}
        else:payload={'description':'Публичных данных недостаточно','signals':[],'limitations':['Не получены проверяемые доказательства']}
        with Session() as db:
            c=db.get(Company,company_id)
            if v3:payload={**payload,'validation_version':evidence_v3.VERSION,'coverage_complete':all(v.get('coverage')=='checked' for v in c.research.values()) and all(d in c.research for d in DIRECTIONS)}
            c.profile=payload;c.stage='PROFILE_READY';c.group='INSUFFICIENT_DATA' if not payload['signals'] else 'NOT_PROCESSED';db.commit()
    def segment(self):
        checkpoint(self.id,'SEGMENTATION')
        with Session() as db:
            r=db.get(Run,self.id)
            if r.segmentation_version:return
            source_filters=r.config.get('source_filters','');sender={**research_sender(r),'offers':sender_directions(research_sender(r))};max_tokens=r.config['strategy_tokens']
            companies=db.query(Company).filter_by(run_id=self.id,stage='PROFILE_READY',identity='CONFIRMED').all()
            profiles=[{'company_id':c.id,'description':c.profile.get('description',''),'signals':c.profile.get('signals',[]),'contradictions':c.profile.get('contradictions',[]),'evidence':evidence_for(c),'limitations':c.profile.get('limitations',[]),'coverage_complete':c.profile.get('coverage_complete',True)} for c in companies if c.profile.get('signals')]
        v3=r.config.get('pipeline_version',1)>=3
        matrix=evidence_v3.build_feature_matrix(profiles) if v3 else None
        if v3:
            for p in profiles:p['signals']=evidence_v3.normalized_signals(p['signals'])
        candidates=[]
        # Bounded discovery batches. A final merge considers candidate definitions globally.
        batches=[];batch=[];size=0
        strategy_profiles=evidence_v3.feature_patterns(profiles) if v3 else profiles
        for p in strategy_profiles:
            length=len(json.dumps(p,ensure_ascii=False).encode())
            if size+length>70_000 and batch:batches.append(batch);batch=[];size=0
            batch.append(p);size+=length
        if batch:batches.append(batch)
        for batch in batches if sender['offers'] else []:
            result=self.ask('segmentation',SEGMENT,{'profiles':batch,'input_kind':'feature_patterns' if v3 else 'profiles','offers':sender['offers'],'source_filters':source_filters},Segments,max_output=max_tokens)
            candidates.extend(x.model_dump() for x in result.segments)
        # Incremental global consolidation remains bounded for large datasets.
        merged=[]
        for start in range(0,len(candidates),12):
            result=self.ask('segmentation',SEGMENT+' Объедини кандидатов со сходной гипотезой; не добавляй новые имена признаков.',{'candidates':merged+candidates[start:start+12],'offers':sender['offers']},Segments,max_output=max_tokens)
            merged=[s.model_dump() for s in result.segments]
        reviews=[];valid=[];known_signals={s['name'] for p in profiles for s in p['signals']};offer_ids={o['id'] for o in sender['offers']}
        for seg in merged:
            if seg['offer_id'] not in offer_ids or not set(seg['required_signals']+seg['supporting_signals']+seg['contradicting_signals'])<=known_signals:continue
            examples=[p for p in profiles if set(seg['required_signals']) <= {s['name'] for s in p['signals']}][:5]
            evaluations=[]
            if v3:
                positive=[];negative=[]
                for p in profiles:
                    group,reason,trace=classify_with_trace(p,p['evidence'],[seg]);evaluations.append({'company_id':p['company_id'],'trace':trace})
                    (positive if group==seg['id'] else negative).append(p)
                if not positive:continue
                examples=positive[:3]+negative[:2]
            review=self.ask('segmentation',REVIEW,{'review_kind':'segment','segment':seg,'profiles':examples,'offers':sender['offers'],'source_filters':source_filters,'other_segments':[{'name':x['name'],'hypothesis':x['hypothesis'],'outreach_angle':x['outreach_angle']} for x in merged if x is not seg]},Review,max_output=3000)
            reviews.append({'segment':seg,'review':review.model_dump(),'evaluations':evaluations})
            if review.passed:
                signal_labels={s['name']:s['label'] for p in profiles for s in p['signals'] if s.get('label')}
                valid.append({**seg,'id':f'segment_{len(valid)+1:03d}','signal_labels':signal_labels})
        with Session() as db:
            r=db.get(Run,self.id);r.segments=valid;r.segmentation_version=1;r.reports={**r.reports,'segmentation':{'candidates':candidates,'reviews':reviews,'feature_matrix':matrix,'created_at':datetime.now(timezone.utc).isoformat()}};log(db,self.id,'SEGMENTATION','Правила сегментов зафиксированы',metrics={'segments':len(valid)});db.commit()
    def finish(self):
        self.segment();checkpoint(self.id,'CLASSIFICATION')
        with Session() as db:
            r=db.get(Run,self.id)
            for c in db.query(Company).filter_by(run_id=self.id,stage='PROFILE_READY',identity='CONFIRMED'):
                if r.config.get('pipeline_version',1)>=3:
                    c.group,c.reason,trace=classify_with_trace(c.profile,evidence_for(c),r.segments)
                    c.profile={**c.profile,'classification':{'segment_version':r.segmentation_version,'trace':trace}}
                else:c.group,c.reason=classify(c.profile,evidence_for(c),r.segments)
            db.commit()
        checkpoint(self.id,'EXPORT')
        with Session() as db:
            r=db.get(Run,self.id)
            issues=db.query(Company).filter(Company.run_id==self.id,Company.group.in_(['FAILED','NEEDS_REVIEW','NOT_PROCESSED','INSUFFICIENT_DATA','AMBIGUOUS','UNCLASSIFIED'])).count()
            export_run(db,r,settings.data_dir/'exports',partial=bool(db.query(Company).filter_by(run_id=self.id,group='NOT_PROCESSED').count()))
            r.status='COMPLETED_WITH_ISSUES' if issues else 'COMPLETED';r.stage='EXPORTED';log(db,self.id,'EXPORTED','Экспорт прошёл проверку целостности');db.commit()
    def execute(self):
        from .context import prepare_context
        prepare_context(self)
        with Session() as db:
            r=db.get(Run,self.id);ids=[c.id for c in db.query(Company).filter_by(run_id=self.id).order_by(Company.id) if c.stage!='PROFILE_READY' and c.identity!='NEEDS_REVIEW']
            maximum=r.config.get('max_companies')
            if maximum:ids=ids[:maximum]
        def perform(cid):
            child=Pipeline(self.id) if r.config.get('pipeline_version',1)>=3 and isinstance(self.gw,Gateway) and not self.injected_gateway else self
            try:child.research(cid)
            except (BudgetPause,RunPaused,UncertainCall,SearchLimit,AuthenticationError,PermissionDeniedError,NotFoundError):raise
            except Exception as e:
                with Session() as db:
                    c=db.get(Company,cid);c.group='FAILED';c.stage='FAILED';c.reason=type(e).__name__+': '+str(e)[:300]
                    log(db,self.id,'FAILED',c.reason,cid);db.commit()
            finally:
                if child is not self and getattr(child.gw,'client',None):child.gw.client.close()
        if r.config.get('pipeline_version',1)>=3:
            execute_jobs(self.id,ids,perform,1 if self.injected_gateway else min(4,max(1,r.config.get('concurrency',3))))
        else:
            for cid in ids:perform(cid)
        self.finish()
