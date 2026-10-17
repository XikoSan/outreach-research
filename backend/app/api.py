from .audiences import GENERAL, GENERAL_RULES, audience_filter, general_segment, general_template
from contextlib import asynccontextmanager
from pathlib import Path
import hashlib, json, shutil
from collections import Counter
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import update
from pydantic import BaseModel, Field
from typing import Literal
from .config import settings
from .db import Base, engine, Session, Sender, Run, Company, SourceRow, Event, Export, ModelCall, ResearchContext, PromptPackage, uid, log, now
from . import credentials
from .presentation import counts, company_view, ATTENTION
from .seed import seed
from .schemas import SenderProfile, RunConfig
from .workbooks import import_rows, export_run
from .llm import RATES
from . import oauth

@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    from .base_stats import backfill_indexes
    with Session() as db:backfill_indexes(db)
    credentials.migrate_global()
    with Session() as db:seed(db)
    yield

app=FastAPI(title='XTECH Research',version='0.2.0',lifespan=lifespan)

@app.middleware('http')
async def local_write_guard(request:Request,call_next):
    # No credentialed cross-origin mutations on the local application.
    origin=request.headers.get('origin')
    if request.method not in ('GET','HEAD','OPTIONS') and origin:
        from urllib.parse import urlsplit
        if urlsplit(origin).netloc != request.headers.get('host'):
            return JSONResponse({'detail':'Cross-origin mutation denied'},status_code=403)
    return await call_next(request)

def get(db,model,id):
    item=db.get(model,id)
    if not item:raise HTTPException(404,'Не найдено')
    return item

def export_stale(db, export):
    return db.query(Event.id).filter(Event.run_id==export.run_id, Event.stage.in_(['EXCLUSION','COARSE_REVIEW']), Event.created_at>export.created_at).first() is not None

def run_view(r):
    result={k:getattr(r,k) for k in ('id','name','filename','status','stage','row_count','company_count','spent','reserved','budget','error','segmentation_version','created_at','config','sender_snapshot','context_snapshot')}
    result['segments']=r.segments if r.config.get('workflow')=='offer-coarse-v2' else [{'id':segment['id'],'name':segment['name']} for segment in r.segments]
    result['confirmed_vacancies']=(r.reports or {}).get('coarse',{}).get('confirmed_vacancies',0)
    return result

def sender_view(s):
    return {'id':s.id,'version':s.version,'profile':s.profile}

def active_sender(db,sid):
    sender=get(db,Sender,sid)
    if sender.archived:raise HTTPException(404,'Профиль удалён')
    return sender

def models():
    return {'discovery':settings.model_discovery,'extraction':settings.model_extraction,'synthesis':settings.model_synthesis,'segmentation':settings.model_segmentation,'prompt':settings.model_prompt}

@app.get('/api/health')
def health():
    return {'status':'ok','version':'0.2.0'}

@app.get('/api/senders')
def senders():
    with Session() as db:return [sender_view(s) for s in db.query(Sender).filter_by(archived=False).order_by(Sender.created_at.desc())]

@app.post('/api/senders')
def save_sender(profile:SenderProfile):
    with Session() as db:
        s=Sender(profile=profile.model_dump());db.add(s);db.commit();return sender_view(s)

@app.put('/api/senders/{sid}')
def edit_sender(sid:str,profile:SenderProfile):
    with Session() as db:
        s=active_sender(db,sid)
        s.history=s.history+[{'version':s.version,'profile':s.profile,'saved_at':now()}]
        s.profile=profile.model_dump();s.version+=1;db.commit();return sender_view(s)

@app.get('/api/senders/{sid}/history')
def sender_history(sid:str):
    with Session() as db:return active_sender(db,sid).history

@app.delete('/api/senders/{sid}')
def delete_sender(sid:str):
    with Session() as db:
        sender=active_sender(db,sid)
        if any(r.status in ('RUNNING','QUEUED','PAUSING') and r.sender_snapshot.get('id')==sid for r in db.query(Run)):
            raise HTTPException(409,'Сначала остановите исследования этого профиля')
        sender.archived=True;credentials.write(sid,{});db.commit();return {'deleted':sid}

@app.post('/api/materials/extract')
def extract_material(file:UploadFile=File(...)):
    import zipfile,io
    from xml.etree import ElementTree
    content=file.file.read(5_000_001)
    if len(content)>5_000_000:raise HTTPException(413,'Лимит документа 5 МБ')
    try:
        suffix=Path(file.filename or '').suffix.lower()
        if suffix in ('.md','.txt'):text=content.decode('utf-8-sig')
        elif suffix=='.docx':
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                if z.getinfo('word/document.xml').file_size>10_000_000:raise ValueError()
                root=ElementTree.fromstring(z.read('word/document.xml'));ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                text='\n'.join(''.join(p.itertext()) for p in root.findall('.//w:p',ns))
        else:raise ValueError()
        if len(text)>100_000:raise ValueError()
        return {'text':text,'filename':file.filename}
    except Exception:raise HTTPException(422,'Поддерживаются UTF-8 TXT, MD и DOCX с текстом до 100 000 символов')

@app.get('/api/runs')
def runs():
    with Session() as db:return [run_view(r) for r in db.query(Run).order_by(Run.created_at.desc()).limit(100)]

@app.get('/api/research')
def research_list(q:str='',status:str='',offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=50)):
    with Session() as db:
        query=db.query(Run)
        if q:query=query.filter(Run.name.ilike('%'+q+'%'))
        if status:query=query.filter(Run.status==status)
        total=query.count();items=[]
        for r in query.order_by(Run.created_at.desc(),Run.id).offset(offset).limit(limit):
            items.append({k:getattr(r,k) for k in ('id','name','filename','company_count','status','stage','created_at')}|{'sender_name':r.context_snapshot.get('name') or r.sender_snapshot.get('profile',{}).get('brand',''), 'row_count':r.row_count,'workflow':r.config.get('workflow','legacy'),**counts(db,r.id)})
        return {'items':items,'total':total}

@app.get('/api/offer-runs')
def offer_runs(offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=50)):
    with Session() as db:
        query=db.query(Run).filter(Run.config['workflow'].as_string()=='offer-coarse-v2')
        total=query.count()
        items=[]
        for r in query.order_by(Run.created_at.desc(),Run.id).offset(offset).limit(limit):
            items.append({k:getattr(r,k) for k in ('id','name','company_count','row_count','status','stage','created_at','spent')}|counts(db,r.id))
        return {'items':items,'total':total}

@app.get('/api/offer-runs/overview')
def offer_runs_overview():
    from .base_stats import workspace_overview
    with Session() as db:return workspace_overview(db)

@app.post('/api/runs')
def create_run(file:UploadFile=File(...),sender_id:str=Form(''),budget:float=Form(0),config_json:str=Form('{}'),name:str=Form(''),context_id:str=Form(''),source_filters:str=Form('')):
    if len(name)>200:raise HTTPException(422,'Название: до 200 символов')
    if len(source_filters)>8000:raise HTTPException(422,'Фильтры выгрузки: до 8000 символов')
    if Path(file.filename or '').suffix.lower()!='.xlsx':raise HTTPException(422,'Нужен файл XLSX')
    try:config=RunConfig.model_validate_json(config_json)
    except Exception:raise HTTPException(422,'Некорректные настройки')
    merged=models()
    if config.models and config.models!=merged:raise HTTPException(422,'Модели заранее заданы на сервере')
    if set(merged)!=set(models()) or any(m not in RATES for m in merged.values()):raise HTTPException(422,'Неподдерживаемая роль или модель')
    content=file.file.read(25_000_001)
    if len(content)>25_000_000:raise HTTPException(413,'Лимит XLSX 25 МБ')
    with Session() as db:
        sender_snapshot={}
        if context_id:
            context=get(db,ResearchContext,context_id)
            from .revisions import context_snapshot as freeze_context
            context_snapshot=freeze_context(context)
        elif sender_id:
            sender=active_sender(db,sender_id)
            sender_snapshot={'id':sender.id,'version':sender.version,'profile':sender.profile}
            context_snapshot={'name':sender.profile['brand'],'summary':sender.profile.get('description','')}
        else:raise HTTPException(422,'Выберите или заполните «Что предлагаем клиентам»')
        if len(context_snapshot['summary'].strip())<10:raise HTTPException(422,'Добавьте описание деятельности и предложения')
    rid=uid();folder=settings.data_dir/'uploads'/rid;folder.mkdir(parents=True)
    path=folder/'source.xlsx';path.write_bytes(content)
    with Session() as db:
        r=Run(id=rid,name=name.strip() or Path(file.filename).stem,filename=Path(file.filename).name,source_path=str(path.resolve()),source_hash=hashlib.sha256(content).hexdigest(),sender_snapshot=sender_snapshot,context_snapshot=context_snapshot,config={**config.model_dump(),'models':merged,'source_filters':source_filters.strip(),'pipeline_version':3 if context_id else 1,'research_as_of':now()[:10],'research_revision':1,'concurrency':8 if context_id and config.methodology_version=='campaign-v1' else 3 if context_id else 1},budget=0)
        try:
            db.add(r);db.flush();import_rows(db,r);log(db,r.id,'IMPORTED','База импортирована',metrics={'rows':r.row_count,'companies':r.company_count});db.commit()
        except ValueError as e:db.rollback();shutil.rmtree(folder);raise HTTPException(422,str(e))
        return run_view(r)

@app.post('/api/coarse-runs')
def create_coarse_run(file:UploadFile=File(...),name:str=Form(''),source_filters:str=Form('')):
    """Import a base without requiring a seller offer or an API credential."""
    if len(name)>200 or len(source_filters)>8000:raise HTTPException(422,'Слишком длинное название или описание фильтров')
    if Path(file.filename or '').suffix.lower()!='.xlsx':raise HTTPException(422,'Нужен файл XLSX')
    content=file.file.read(25_000_001)
    if len(content)>25_000_000:raise HTTPException(413,'Лимит XLSX 25 МБ')
    rid=uid();folder=settings.data_dir/'uploads'/rid;folder.mkdir(parents=True)
    path=folder/'source.xlsx';path.write_bytes(content)
    with Session() as db:
        run=Run(id=rid,name=name.strip() or Path(file.filename).stem,filename=Path(file.filename).name,
            source_path=str(path.resolve()),source_hash=hashlib.sha256(content).hexdigest(),
            sender_snapshot={},context_snapshot={},config={'workflow':'coarse-v1','source_filters':source_filters.strip()},budget=0)
        try:
            db.add(run);db.flush();import_rows(db,run)
            log(db,rid,'IMPORTED','База импортирована для быстрого анализа',metrics={'rows':run.row_count,'companies':run.company_count})
            db.commit()
        except ValueError as exc:
            db.rollback();shutil.rmtree(folder,ignore_errors=True);raise HTTPException(422,str(exc))
        return run_view(run)

@app.post('/api/offer-runs')
def create_offer_run(file:UploadFile=File(...),name:str=Form(''),source_filters:str=Form(''),offer_text:str=Form(...),offer_filename:str=Form('')):
    """Freeze the offered services with the imported base; no sender profile needed."""
    offer=offer_text.strip()
    if not 10<=len(offer)<=100_000:raise HTTPException(422,'Опишите предложение: от 10 до 100 000 символов')
    if len(name)>200 or len(source_filters)>8000:raise HTTPException(422,'Слишком длинное название или описание фильтров')
    if Path(file.filename or '').suffix.lower()!='.xlsx':raise HTTPException(422,'Нужен файл XLSX')
    content=file.file.read(25_000_001)
    if len(content)>25_000_000:raise HTTPException(413,'Лимит XLSX 25 МБ')
    rid=uid();folder=settings.data_dir/'uploads'/rid;folder.mkdir(parents=True)
    path=folder/'source.xlsx';path.write_bytes(content)
    with Session() as db:
        run=Run(id=rid,name=name.strip() or Path(file.filename).stem,filename=Path(file.filename).name,
            source_path=str(path.resolve()),source_hash=hashlib.sha256(content).hexdigest(),
            sender_snapshot={},context_snapshot={'name':'Предложение','summary':offer[:2500],
                'source_text':offer,'source_hash':hashlib.sha256(offer.encode()).hexdigest(),
                'source_name':Path(offer_filename).name[:200]},
            config={'workflow':'offer-coarse-v2','source_filters':source_filters.strip(),
                    'models':{'synthesis':settings.model_synthesis},'pipeline_version':3},budget=0)
        try:
            db.add(run);db.flush();import_rows(db,run)
            log(db,rid,'IMPORTED','База и предложение сохранены',metrics={'rows':run.row_count,'companies':run.company_count})
            db.commit()
        except ValueError as exc:
            db.rollback();shutil.rmtree(folder,ignore_errors=True);raise HTTPException(422,str(exc))
        return run_view(run)

@app.get('/api/runs/{rid}')
def run_detail(rid:str):
    with Session() as db:
        r=get(db,Run,rid);summary=counts(db,rid)
        return {**run_view(r),**summary,'completed_companies':summary['researched'],'connection':credentials.status(),'exports':[{'id':e.id,'manifest':e.manifest,'created_at':e.created_at,'stale':export_stale(db,e)} for e in db.query(Export).filter_by(run_id=rid).order_by(Export.created_at.desc())]}

class RunName(BaseModel):name:str=Field(min_length=1,max_length=200)
@app.patch('/api/runs/{rid}')
def rename_run(rid:str,payload:RunName):
    if not payload.name.strip():raise HTTPException(422,'Введите название')
    with Session() as db:
        r=get(db,Run,rid);r.name=payload.name.strip();db.commit();return run_view(r)

@app.post('/api/runs/{rid}/start')
def start(rid:str):
    with Session() as db:
        r=get(db,Run,rid)
        if r.reports.get('pilot',{}).get('kind')=='base_only':
            raise HTTPException(409,'Это пилот только базового сбора; обычный запуск сегментации отключён')
        if r.config.get('workflow') not in ('coarse-v1',) and not credentials.for_run(r):raise HTTPException(409,'Добавьте API-ключ в настройках')
        if r.config.get('pipeline_version',1)<3 and db.query(ModelCall).filter_by(run_id=rid).count():raise HTTPException(409,'Это исследование создано старой версией конвейера. Для продолжения требуется перенос сохранённых результатов; автоматический повтор отключён.')
        if r.status in ('RUNNING','QUEUED','PAUSING'):raise HTTPException(409,'Запуск уже выполняется')
        if r.status in ('COMPLETED','COMPLETED_WITH_ISSUES'):raise HTTPException(409,'Используйте повторную обработку статусов')
        r.status='QUEUED';r.error=None;db.commit();return run_view(r)

@app.post('/api/runs/{rid}/pause')
def pause(rid:str):
    with Session() as db:
        r=get(db,Run,rid)
        if r.status=='QUEUED':r.status='PAUSED'
        elif r.status=='RUNNING':r.status='PAUSING'
        else:raise HTTPException(409,'Запуск не выполняется')
        db.commit();return run_view(r)

class RetryRequest(BaseModel):
    groups:list[str]=Field(default_factory=lambda:['FAILED','INSUFFICIENT_DATA','NOT_PROCESSED'])
@app.post('/api/runs/{rid}/retry')
def retry(rid:str,payload:RetryRequest):
    with Session() as db:
        r=get(db,Run,rid)
        if r.status in ('RUNNING','QUEUED','PAUSING'):raise HTTPException(409,'Сначала остановите запуск')
        allowed={'FAILED','INSUFFICIENT_DATA','NOT_PROCESSED','AMBIGUOUS','UNCLASSIFIED','NEEDS_REVIEW'}
        if not set(payload.groups)<=allowed:raise HTTPException(422,'Повтор доступен только для статусных групп')
        cs=db.query(Company).filter(Company.run_id==rid,Company.group.in_(payload.groups)).all()
        for c in cs:
            from .revisions import prepare_retry
            prepare_retry(c)
            if c.resource.startswith('unresolved:') or c.context.get('identity_conflict'):
                c.identity='NEEDS_REVIEW';c.group='NEEDS_REVIEW';c.stage='IDENTITY'
                c.reason=('Неоднозначная принадлежность контактов или общего ресурса: уточните исходную базу'
                          if c.context.get('identity_conflict') else
                          'Нет сайта или официальной социальной сети: повтор требует уточнения исходных данных')
        if cs:
            history=list((r.reports or {}).get('segmentation_history',[]))
            if r.segmentation_version or r.segments:
                history.append({'version':r.segmentation_version,'segments':r.segments,'reason':'invalidated_by_retry'})
            r.reports={**(r.reports or {}),'segmentation_history':history}
            r.segments=[];r.segmentation_version=0
        r.status='PAUSED';r.error=None;log(db,rid,'RETRY','Выбранные компании подготовлены к повтору',metrics={'companies':len(cs),'segmentation_version':r.segmentation_version});db.commit()
        return {'companies':len(cs)}

@app.get('/api/runs/{rid}/companies')
def companies(rid:str,offset:int=Query(0,ge=0),limit:int=Query(40,ge=1,le=100),group:str|None=None,q:str='',attention_only:bool=False):
    with Session() as db:
        run=get(db,Run,rid);query=db.query(Company).filter_by(run_id=rid)
        if group:query=query.filter(audience_filter(run,group))
        if attention_only:query=query.filter(Company.group.in_(ATTENTION))
        if q:query=query.filter(Company.name.ilike('%'+q+'%'))
        total=query.count()
        return {'total':total,'items':[company_view(c,run) for c in query.order_by(Company.name,Company.id).offset(offset).limit(limit)]}

@app.get('/api/companies/{cid}')
def company(cid:str):
    with Session() as db:
        c=get(db,Company,cid)
        return {**company_view(c,db.get(Run,c.run_id),detail=True),
                'contact_count':db.query(SourceRow).filter_by(company_id=cid).count()}

class ExclusionInput(BaseModel):
    code:Literal['NO_CURRENT_SOURCES','CEASED_OPERATIONS','DATABASE_MISMATCH']
    note:str=Field(default='',max_length=500)
    source_url:str=''

@app.post('/api/companies/{cid}/exclusion')
def exclude_company(cid:str,payload:ExclusionInput):
    from .exclusions import no_current_sources, set_excluded, same_origin, ceased_operations
    with Session() as db:
        c=get(db,Company,cid);r=get(db,Run,c.run_id)
        if r.status in ('RUNNING','QUEUED','PAUSING'):raise HTTPException(409,'Сначала остановите исследование')
        if c.group=='EXCLUDED':raise HTTPException(409,'Компания уже исключена')
        if c.stage!='PROFILE_READY' or c.identity!='CONFIRMED':raise HTTPException(409,'Сначала завершите исследование и подтвердите компанию')
        if payload.code=='NO_CURRENT_SOURCES':
            if not no_current_sources(c,r):raise HTTPException(422,'Другие источники ещё доступны или не проверены')
            sources=[]
        else:
            if len(payload.note.strip())<12:raise HTTPException(422,'Укажите конкретную причину')
            sources=[{'id':'page:'+(page.get('content_hash') or page['url']),'url':page['url']}
                     for report in c.research.values() for page in report.get('pages',[])
                     if page.get('url')==payload.source_url and same_origin(page.get('url',''),c.resource)]
            if not sources:raise HTTPException(422,'Нужно выбрать сохранённую страницу официального сайта компании')
            if payload.code=='CEASED_OPERATIONS' and not any(item.get('url')==payload.source_url for item in ceased_operations(c)):
                raise HTTPException(422,'Прекращение деятельности не подтверждено проверенной цитатой с сайта компании')
        set_excluded(c,payload.code,sources,source='reviewed',note=payload.note)
        log(db,r.id,'EXCLUSION','Компания исключена из выборки',c.id,{'code':payload.code})
        db.commit()
        return company_view(c,r,detail=True)

@app.delete('/api/companies/{cid}/exclusion')
def restore_company(cid:str):
    with Session() as db:
        c=get(db,Company,cid);r=get(db,Run,c.run_id)
        if r.status in ('RUNNING','QUEUED','PAUSING'):raise HTTPException(409,'Сначала остановите исследование')
        if c.group!='EXCLUDED':raise HTTPException(409,'Компания не исключена')
        exclusion=c.profile.get('exclusion',{})
        previous=exclusion.get('previous',{})
        c.profile={key:value for key,value in c.profile.items() if key!='exclusion'}|{'exclusion_override':'include'}
        prior=previous.get('group','UNCLASSIFIED')
        c.group=prior if prior!='EXCLUDED' and (not prior.startswith('segment_') or any(seg['id']==prior for seg in r.segments)) else 'UNCLASSIFIED'
        c.reason=previous.get('reason') or 'Исключение снято; компания доступна для повторной оценки'
        log(db,r.id,'EXCLUSION','Исключение снято',c.id,{'previous_code':exclusion.get('code')})
        db.commit()
        return company_view(c,r,detail=True)

@app.get('/api/coarse-runs/{rid}/companies')
def coarse_companies(rid:str,group:str='',q:str='',offset:int=Query(0,ge=0),limit:int=Query(40,ge=1,le=100)):
    with Session() as db:
        run=get(db,Run,rid)
        if run.config.get('workflow') not in ('coarse-v1','offer-coarse-v2'):raise HTTPException(404,'База не найдена')
        query=db.query(Company).filter_by(run_id=rid)
        if group:query=query.filter(Company.group==group)
        if q:query=query.filter(Company.name.ilike('%'+q+'%'))
        total=query.count()
        items=query.order_by(Company.name,Company.id).offset(offset).limit(limit).all()
        return {'total':total,'items':[{'id':c.id,'name':c.name,'resource':c.resource,'audience_id':c.group,
            'reason':c.reason,'confidence':(c.profile or {}).get('coarse',{}).get('confidence',''),
            'source':(c.profile or {}).get('coarse',{}).get('source',''),
            'vacancy':(c.profile or {}).get('coarse_vacancy',{'state':'NOT_CONFIRMED'})} for c in items]}

class CoarseReviewInput(BaseModel):
    audience_id:str
    reason:str=Field(min_length=8,max_length=500)

@app.post('/api/coarse-runs/{rid}/companies/{cid}/review')
def review_coarse_company(rid:str,cid:str,payload:CoarseReviewInput):
    with Session() as db:
        run=get(db,Run,rid);company=get(db,Company,cid)
        if run.config.get('workflow') not in ('coarse-v1','offer-coarse-v2') or company.run_id!=rid:raise HTTPException(404,'Компания не найдена в этой базе')
        if run.status!='COMPLETED':raise HTTPException(409,'Дождитесь окончания обработки')
        allowed={'GENERAL','DO_NOT_SEND'}|{segment['id'] for segment in run.segments}
        if payload.audience_id not in allowed:raise HTTPException(422,'Неизвестная аудитория')
        if len(payload.reason.strip())<8:raise HTTPException(422,'Укажите конкретную причину')
        previous=company.group
        company.group=payload.audience_id;company.reason=payload.reason.strip()
        history=list((company.profile or {}).get('coarse_review_history',[]))
        history.append({'from':previous,'to':company.group,'reason':company.reason,'at':now()})
        company.profile={**(company.profile or {}),'coarse_review_history':history}
        run.segmentation_version+=1
        coarse_report=(run.reports or {}).get('coarse',{})
        groups=Counter(c.group for c in db.query(Company).filter_by(run_id=rid).all())
        run.reports={**(run.reports or {}),'coarse':{**coarse_report,'groups':dict(groups),'reviewed_at':now()}}
        log(db,rid,'COARSE_REVIEW','Аудитория компании изменена вручную',cid,{'from':previous,'to':company.group,'version':run.segmentation_version})
        db.commit()
        return {'id':company.id,'audience_id':company.group,'reason':company.reason,'segment_set_version':run.segmentation_version}

@app.get('/api/runs/{rid}/events')
def events(rid:str):
    with Session() as db:
        get(db,Run,rid)
        return [{'id':e.id,'stage':e.stage,'message':e.message,'metrics':e.metrics,'created_at':e.created_at} for e in db.query(Event).filter_by(run_id=rid).order_by(Event.id.desc()).limit(100)]

@app.post('/api/runs/{rid}/exports')
def make_export(rid:str):
    with Session() as db:
        r=get(db,Run,rid)
        if r.status in ('RUNNING','QUEUED','PAUSING'):raise HTTPException(409,'Для согласованного экспорта дождитесь остановки')
        try:
            if r.config.get('workflow') in ('coarse-v1','offer-coarse-v2'):
                from .coarse import export as export_coarse
                e=export_coarse(db,r,settings.data_dir/'exports')
            else:e=export_run(db,r,settings.data_dir/'exports')
        except ValueError as e:raise HTTPException(409,str(e))
        return {'id':e.id,'manifest':e.manifest}

@app.get('/api/exports/{eid}/download')
def download(eid:str,file:str='results.zip'):
    with Session() as db:
        e=get(db,Export,eid)
        if export_stale(db,e):raise HTTPException(409,'Состав выборки изменился. Создайте новый экспорт')
        if file!='results.zip' and file not in e.manifest['files']:raise HTTPException(404,'Файл не найден')
        return FileResponse(Path(e.path)/file,filename=Path(file).name)

@app.get('/api/runs/{rid}/calls')
def calls(rid:str):
    with Session() as db:
        get(db,Run,rid)
        return [{'id':c.id,'role':c.role,'model':c.model,'state':c.state,'reservation':c.reservation,'cost':c.cost,'response_id':c.response_id,'package_id':c.package_id,'package_name':db.get(PromptPackage,c.package_id).name if c.package_id else None} for c in db.query(ModelCall).filter(ModelCall.run_id==rid,ModelCall.state.in_(['STARTED','UNCERTAIN']))]

class Reconciliation(BaseModel):
    verified_cost:float=Field(ge=0,le=10000)
    note:str=Field(min_length=5,max_length=1000)


@app.post('/api/auth/chatgpt/start')
def chatgpt_start():
    from fastapi.responses import RedirectResponse
    return {'authorization_url':oauth.start()}

@app.get('/api/auth/chatgpt/callback')
def chatgpt_callback(request:Request):
    try:
        result=oauth.callback(dict(request.query_params))
        return {'status':'connected','email':result.get('email',''),'scopes':result['scopes']}
    except Exception as exc:
        return JSONResponse({'status':'error','detail':str(exc)},status_code=400)

@app.get('/api/settings/chatgpt')
def chatgpt_status():
    value=credentials.read(credentials.GLOBAL_ID)
    return {'configured':value.get('provider')=='chatgpt','state':value.get('state','missing'),'email':value.get('email',''),'scopes':value.get('scopes',[])}

@app.get('/api/settings/connection')
def connection_status():return credentials.status()

@app.put('/api/settings/connection')
async def save_connection(request:Request):
    sid=credentials.GLOBAL_ID
    # Validate manually so validation responses never echo credential input.
    try:
        payload=await request.json();key=payload.get('api_key','').strip()
        if not 20<=len(key)<=500 or any(c.isspace() for c in key):raise ValueError()
    except Exception:raise HTTPException(422,'Некорректный API-ключ')
    credentials.write(sid,{'key':key,'state':'saved'})
    return credentials.status(sid)

@app.delete('/api/settings/connection')
def delete_connection():
    sid=credentials.GLOBAL_ID
    credentials.write(sid,{})
    return credentials.status(sid)

@app.post('/api/settings/connection/check')
def check_connection():
    sid=credentials.GLOBAL_ID
    from openai import OpenAI,AuthenticationError,PermissionDeniedError
    value=credentials.read(sid)
    if not value.get('key'):raise HTTPException(409,'Сначала сохраните API-ключ')
    try:
        with OpenAI(api_key=value['key'],max_retries=0,timeout=15) as provider:provider.models.list()
        value.update(state='verified',checked_at=now())
    except (AuthenticationError,PermissionDeniedError):value.update(state='rejected',checked_at=now())
    except Exception:raise HTTPException(502,'Не удалось проверить подключение. Повторите проверку позже.')
    if credentials.read(sid).get('key')==value['key']:credentials.write(sid,value)
    return credentials.status(sid)

@app.post('/api/calls/{cid}/reconcile')
def reconcile(cid:str,payload:Reconciliation):
    with Session() as db:
        c=get(db,ModelCall,cid);r=get(db,PromptPackage,c.package_id) if c.package_id else get(db,Run,c.run_id)
        if r.status in ('RUNNING','QUEUED','PAUSING'):raise HTTPException(409,'Сначала дождитесь остановки')
        if c.state not in ('STARTED','UNCERTAIN'):raise HTTPException(409,'Операция уже сверена')
        r.reserved=max(0,r.reserved-c.reservation);r.spent+=payload.verified_cost
        c.state='RECONCILED';c.cost=payload.verified_cost;c.reservation=0
        from .db import ModelAttempt
        attempt=db.query(ModelAttempt).filter_by(operation_id=cid).filter(ModelAttempt.state.in_(['STARTED','UNCERTAIN'])).order_by(ModelAttempt.created_at.desc()).first()
        if attempt:
            attempt.state='RECONCILED';attempt.cost=payload.verified_cost;attempt.metrics={**attempt.metrics,'reconciled':True,'note':payload.note}
        log(db,c.run_id,'RECONCILIATION','Расход сверён пользователем',c.company_id,{'call_id':cid,'cost':payload.verified_cost,'note':payload.note});db.commit()
        return {'state':c.state}

from .workflow import router as workflow_router
app.include_router(workflow_router)




@app.get('/api/runs/{rid}/attempts')
def attempts(rid:str,offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100)):
    from .db import ModelAttempt
    with Session() as db:
        get(db,Run,rid)
        query=db.query(ModelAttempt,ModelCall).join(ModelCall,ModelCall.id==ModelAttempt.operation_id).filter(ModelCall.run_id==rid)
        total=query.count()
        return {'total':total,'items':[{'id':a.id,'operation_id':a.operation_id,'role':c.role,'model':c.model,'state':a.state,'cost':a.cost,'reservation':a.reservation,'metrics':a.metrics,'created_at':a.created_at} for a,c in query.order_by(ModelAttempt.created_at.desc()).offset(offset).limit(limit)]}

@app.get('/api/runs/{rid}/research-plan')
def research_plan(rid:str):
    from .campaigns import BASE_CRITERIA, BASIS_POLICIES
    with Session() as db:
        run=get(db,Run,rid)
        saved=run.reports.get('campaign_plan')
        return {'status':'prepared' if saved else 'not_prepared','active_methodology':run.config.get('methodology_version','legacy'),'plan':saved,'base_criteria':BASE_CRITERIA,'basis_policies':BASIS_POLICIES}

frontend=Path(__file__).resolve().parents[2]/'frontend'/'dist'
if frontend.exists():app.mount('/',StaticFiles(directory=frontend,html=True),name='ui')
