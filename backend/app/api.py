from contextlib import asynccontextmanager
from pathlib import Path
import hashlib, json, shutil
from collections import Counter
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import update
from pydantic import BaseModel, Field
from .config import settings
from .db import Base, engine, Session, Sender, Run, Company, SourceRow, Event, Export, ModelCall, uid, log
from .seed import seed
from .schemas import SenderProfile, RunConfig
from .workbooks import import_rows, export_run
from .llm import RATES

@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    with Session() as db:seed(db)
    yield

app=FastAPI(title='Outreach Research',version='0.1.0',lifespan=lifespan)

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

def run_view(r):
    return {k:getattr(r,k) for k in ('id','filename','status','stage','row_count','company_count','spent','reserved','budget','error','segments','segmentation_version','created_at','config','sender_snapshot')}

def models():
    return {'discovery':settings.model_discovery,'extraction':settings.model_extraction,'synthesis':settings.model_synthesis,'segmentation':settings.model_segmentation,'prompt':settings.model_prompt}

@app.get('/api/health')
def health():
    return {'status':'ok','api_key_configured':bool(settings.openai_api_key),'models':models(),'rates':RATES,'version':'0.1.0'}

@app.get('/api/senders')
def senders():
    with Session() as db:return [{'id':s.id,'version':s.version,'profile':s.profile} for s in db.query(Sender).order_by(Sender.created_at.desc())]

@app.post('/api/senders')
def save_sender(profile:SenderProfile):
    with Session() as db:
        previous=db.query(Sender).filter(Sender.profile['brand'].as_string()==profile.brand).order_by(Sender.version.desc()).first()
        s=Sender(profile=profile.model_dump(),version=(previous.version+1) if previous else 1);db.add(s);db.commit();return {'id':s.id,'version':s.version,'profile':s.profile}

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

@app.post('/api/runs')
def create_run(file:UploadFile=File(...),sender_id:str=Form(...),budget:float=Form(...),config_json:str=Form('{}')):
    if not 0<budget<=10000:raise HTTPException(422,'Бюджет должен быть больше нуля и не более $10 000')
    if Path(file.filename or '').suffix.lower()!='.xlsx':raise HTTPException(422,'Нужен файл XLSX')
    try:config=RunConfig.model_validate_json(config_json)
    except Exception:raise HTTPException(422,'Некорректные настройки')
    merged=models()|config.models
    if set(merged)!=set(models()) or any(m not in RATES for m in merged.values()):raise HTTPException(422,'Неподдерживаемая роль или модель')
    content=file.file.read(25_000_001)
    if len(content)>25_000_000:raise HTTPException(413,'Лимит XLSX 25 МБ')
    rid=uid();folder=settings.data_dir/'uploads'/rid;folder.mkdir(parents=True)
    path=folder/'source.xlsx';path.write_bytes(content)
    with Session() as db:
        sender=get(db,Sender,sender_id)
        r=Run(id=rid,filename=Path(file.filename).name,source_path=str(path.resolve()),source_hash=hashlib.sha256(content).hexdigest(),sender_snapshot={'id':sender.id,'version':sender.version,'profile':sender.profile},config={**config.model_dump(),'models':merged},budget=budget)
        try:
            db.add(r);db.flush();import_rows(db,r);log(db,r.id,'IMPORTED','База импортирована',metrics={'rows':r.row_count,'companies':r.company_count});db.commit()
        except ValueError as e:db.rollback();shutil.rmtree(folder);raise HTTPException(422,str(e))
        return run_view(r)

@app.get('/api/runs/{rid}')
def run_detail(rid:str):
    with Session() as db:
        r=get(db,Run,rid);cs=db.query(Company).filter_by(run_id=rid).all()
        return {**run_view(r),'groups':dict(Counter(c.group for c in cs)),'completed_companies':sum(c.stage=='PROFILE_READY' or c.identity=='NEEDS_REVIEW' for c in cs),'exports':[{'id':e.id,'manifest':e.manifest,'created_at':e.created_at} for e in db.query(Export).filter_by(run_id=rid).order_by(Export.created_at.desc())]}

@app.post('/api/runs/{rid}/start')
def start(rid:str):
    if not settings.openai_api_key:raise HTTPException(409,'Настройте OPENAI_API_KEY в .env и перезапустите API и worker')
    with Session() as db:
        r=get(db,Run,rid)
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

class BudgetUpdate(BaseModel):budget:float=Field(gt=0,le=10000)
@app.patch('/api/runs/{rid}/budget')
def budget(rid:str,payload:BudgetUpdate):
    with Session() as db:
        r=get(db,Run,rid)
        if payload.budget<r.spent+r.reserved:raise HTTPException(409,'Лимит ниже уже потраченного и зарезервированного')
        r.budget=payload.budget;db.commit();return run_view(r)

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
            c.stage='PENDING';c.group='NOT_PROCESSED';c.research={};c.profile={};c.reason=''
            if not c.resource.startswith('unresolved:'):c.identity='UNVERIFIED'
            else:
                c.identity='NEEDS_REVIEW';c.group='NEEDS_REVIEW';c.stage='IDENTITY'
                c.reason='Нет сайта или официальной социальной сети: повтор требует уточнения исходных данных'
        r.status='PAUSED';r.error=None;log(db,rid,'RETRY','Выбранные компании подготовлены к повтору',metrics={'companies':len(cs),'segmentation_version':r.segmentation_version});db.commit()
        return {'companies':len(cs)}

@app.get('/api/runs/{rid}/companies')
def companies(rid:str,offset:int=Query(0,ge=0),limit:int=Query(40,ge=1,le=100),group:str|None=None,q:str=''):
    with Session() as db:
        get(db,Run,rid);query=db.query(Company).filter_by(run_id=rid)
        if group:query=query.filter_by(group=group)
        if q:query=query.filter(Company.name.ilike('%'+q+'%'))
        total=query.count()
        return {'total':total,'items':[{'id':c.id,'name':c.name,'resource':c.resource,'identity':c.identity,'stage':c.stage,'group':c.group,'reason':c.reason} for c in query.order_by(Company.name).offset(offset).limit(limit)]}

@app.get('/api/companies/{cid}')
def company(cid:str):
    with Session() as db:
        c=get(db,Company,cid)
        return {'id':c.id,'name':c.name,'resource':c.resource,'identity':c.identity,'group':c.group,'reason':c.reason,'profile':c.profile,'reports':{k:{a:b for a,b in v.items() if a!='pages'} for k,v in c.research.items()}}

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
        try:e=export_run(db,r,settings.data_dir/'exports')
        except ValueError as e:raise HTTPException(409,str(e))
        return {'id':e.id,'manifest':e.manifest}

@app.get('/api/exports/{eid}/download')
def download(eid:str,file:str='results.zip'):
    with Session() as db:
        e=get(db,Export,eid)
        if file!='results.zip' and file not in e.manifest['files']:raise HTTPException(404,'Файл не найден')
        return FileResponse(Path(e.path)/file,filename=Path(file).name)

@app.get('/api/runs/{rid}/calls')
def calls(rid:str):
    with Session() as db:
        get(db,Run,rid)
        return [{'id':c.id,'role':c.role,'model':c.model,'state':c.state,'reservation':c.reservation,'cost':c.cost,'response_id':c.response_id} for c in db.query(ModelCall).filter(ModelCall.run_id==rid,ModelCall.state.in_(['STARTED','UNCERTAIN']))]

class Reconciliation(BaseModel):
    verified_cost:float=Field(ge=0,le=10000)
    note:str=Field(min_length=5,max_length=1000)

class APIKeyPayload(BaseModel):
    api_key: str = Field(min_length=20, max_length=500)

@app.post('/api/settings/openai-key')
def save_openai_key(payload:APIKeyPayload):
    """Keep the provider key in the local .env, never in a sender profile or API response."""
    from .config import settings
    env_path=Path('.env')
    lines=env_path.read_text().splitlines() if env_path.exists() else []
    replaced=False;out=[]
    for line in lines:
        if line.startswith('OPENAI_API_KEY='):
            out.append('OPENAI_API_KEY='+payload.api_key);replaced=True
        else:out.append(line)
    if not replaced:out.append('OPENAI_API_KEY='+payload.api_key)
    env_path.write_text('\n'.join(out)+'\n')
    settings.openai_api_key=payload.api_key
    return {'api_key_configured':True}

@app.post('/api/calls/{cid}/reconcile')
def reconcile(cid:str,payload:Reconciliation):
    with Session() as db:
        c=get(db,ModelCall,cid);r=get(db,Run,c.run_id)
        if r.status in ('RUNNING','QUEUED','PAUSING'):raise HTTPException(409,'Сначала дождитесь остановки')
        if c.state not in ('STARTED','UNCERTAIN'):raise HTTPException(409,'Операция уже сверена')
        r.reserved=max(0,r.reserved-c.reservation);r.spent+=payload.verified_cost
        c.state='RECONCILED';c.cost=payload.verified_cost;c.reservation=0
        log(db,r.id,'RECONCILIATION','Расход сверён пользователем',c.company_id,{'call_id':cid,'cost':payload.verified_cost,'note':payload.note});db.commit()
        return {'state':c.state}

frontend=Path(__file__).resolve().parents[2]/'frontend'/'dist'
if frontend.exists():app.mount('/',StaticFiles(directory=frontend,html=True),name='ui')
