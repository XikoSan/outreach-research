"""Durable API operations with conservative reservations and response checkpoints."""
import hashlib
import json
from openai import OpenAI, APIStatusError
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import Session, Run, PromptPackage, ModelCall, ModelAttempt, uid, log
from . import credentials
from .providers import API_GATE, OpenAIAdapter

RATES = {'gpt-6-luna': (.10, .50), 'gpt-6.1-sol': (2., 10.)}

class BudgetPause(Exception): pass
class RunPaused(Exception): pass
class UncertainCall(Exception): pass
class SearchLimit(Exception): pass

def strict_schema(value):
    # Public JSON Schema transform: OpenAI strict outputs require every property.
    if isinstance(value, dict):
        value = {k: strict_schema(v) for k, v in value.items() if k != 'default'}
        if value.get('type') == 'object':
            value['additionalProperties'] = False
            value['required'] = list(value.get('properties', {}))
    elif isinstance(value, list):
        value = [strict_schema(v) for v in value]
    return value

class Gateway:
    def __init__(self, run_id, package_id=None):
        self.run_id = run_id
        self.package_id = package_id
        self.client = None
        self.profile_credentials = True
        self.current_key = None

    def ask(self, role, instructions, payload, schema, max_output=5000, search=False, company_id=None):
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        # Reuse completed operations before waiting for provider quota.
        with Session() as db:
            run=db.get(Run,self.run_id)
            data=[self.run_id,company_id,run.config['models'][role],role,instructions,text,schema.model_json_schema(),max_output,search]
            if getattr(self,'package_id',None):data.append({'package_id':self.package_id})
            cached=db.get(ModelCall,hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest())
            target=db.get(PromptPackage,self.package_id) if getattr(self,'package_id',None) else run
            if target.status!='RUNNING':raise RunPaused()
            if cached and cached.state=='COMPLETE' and cached.result is not None:return schema.model_validate(cached.result)
        if getattr(self,'profile_credentials',False):
            def active():
                with Session() as db:
                    target=PromptPackage if getattr(self,'package_id',None) else Run
                    if db.get(target,getattr(self,'package_id',None) or self.run_id).status!='RUNNING':raise RunPaused()
            API_GATE.wait(len(text.encode())+max_output,active)
        with Session() as db:
            research = db.get(Run, self.run_id)
            package_id=getattr(self,'package_id',None)
            target=PromptPackage if package_id else Run
            target_id=package_id or self.run_id
            run=db.get(target,target_id)
            if run.status != 'RUNNING': raise RunPaused()
            model = research.config['models'][role]
            if model not in RATES: raise ValueError('Для модели не настроен тариф')
            request_data = [self.run_id, company_id, model, role, instructions, text, schema.model_json_schema(), max_output, search]
            if package_id:request_data.append({'package_id':package_id})
            key = hashlib.sha256(json.dumps(request_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            saved = db.get(ModelCall, key)
            if saved and saved.state == 'COMPLETE':
                if saved.result is None: raise ValueError('Сохранённый ответ не соответствует схеме; нужна корректировка запроса')
                return schema.model_validate(saved.result)
            if saved and saved.state in ('STARTED', 'UNCERTAIN'):
                raise UncertainCall('Исход предыдущего API-вызова неизвестен. Сверьте расход в журнале перед повтором.')
            if getattr(self,'profile_credentials',False):
                credential=credentials.for_run(run)
                if not credential:
                    run.status='PAUSED';run.error='Добавьте API-ключ в настройках и продолжите обработку.'
                    db.commit();raise RunPaused()
                if credential!=self.current_key:
                    if self.client:self.client.close()
                    self.client=OpenAI(api_key=credential,max_retries=0,timeout=90)
                    self.current_key=credential
            if len(text.encode()) > 160_000: raise ValueError('Превышен лимит контекста; требуется разбиение')
            if search:
                attempts = db.query(ModelCall).filter_by(run_id=self.run_id, company_id=company_id, search=True).filter(ModelCall.state != 'REJECTED').count()
                if attempts >= research.config['max_search_queries']: raise SearchLimit('Лимит поисковых операций компании достигнут')
            inp, out = RATES[model]
            # Conservative long-context rates; text bytes upper-bound text tokens.
            input_bound = 1_050_000 if search else len((text + instructions + json.dumps(schema.model_json_schema())).encode()) + 4000
            upper = input_bound * inp * 2 / 1_000_000 + max_output * out * 1.5 / 1_000_000 + (.01 if search else 0)
            reserved = db.execute(update(target).where(target.id == target_id, target.status == 'RUNNING').values(reserved=target.reserved + upper))
            if not reserved.rowcount:
                db.refresh(run)
                if run.status != 'RUNNING': raise RunPaused()
                raise RunPaused()
            if saved:
                if not db.query(ModelAttempt).filter_by(operation_id=key).count():
                    db.add(ModelAttempt(operation_id=key,state=saved.state,response_id=saved.response_id,cost=saved.cost,reservation=saved.reservation,metrics={**saved.metrics,'legacy':True}))
                claimed=db.execute(update(ModelCall).where(ModelCall.id==key,ModelCall.state.in_(['REJECTED','RECONCILED'])).values(state='STARTED',reservation=upper,result=None,cost=0,response_id=None,metrics={}))
                if not claimed.rowcount:db.rollback();raise UncertainCall('Операция уже выполняется')
            else:
                db.add(ModelCall(id=key,run_id=self.run_id,company_id=company_id,package_id=package_id,role=role,model=model,state='STARTED',reservation=upper,search=search))
            try:db.flush()
            except IntegrityError as exc:
                db.rollback();raise UncertainCall('Операция уже зарезервирована другим исполнителем') from exc
            attempt_id=uid()
            db.add(ModelAttempt(id=attempt_id,operation_id=key,reservation=upper))
            log(db,self.run_id,role,'API-операция зарезервирована',company_id,{'call_id':key,'reserved':upper,'model':model})
            db.commit()
        kwargs = {'tools':[{'type':'web_search','search_context_size':'low'}], 'max_tool_calls':1} if search else {}
        try:
            # Parse manually after recording usage, so invalid JSON does not hide billing.
            response = OpenAIAdapter().generate(self.client,dict(model=model,instructions=instructions,input=[{'role':'user','content':text}],text={'format':{'type':'json_schema','name':schema.__name__,'strict':True,'schema':strict_schema(schema.model_json_schema())}},max_output_tokens=max_output,reasoning={'effort':'low' if role in ('discovery','extraction') else 'medium'},store=False,**kwargs),stream=credentials.uses_chatgpt(run))
        except Exception as error:
            if isinstance(error,APIStatusError) and error.status_code==429:API_GATE.rejected(error.response.headers.get('retry-after'))
            definitive = (isinstance(error, APIStatusError) and error.status_code in (400,401,403,404,422,429)) or 'subscription sharing usage limit' in str(error).lower()
            with Session() as db:
                call=db.get(ModelCall,key); call.state='REJECTED' if definitive else 'UNCERTAIN'
                attempt=db.get(ModelAttempt,attempt_id);attempt.state=call.state
                attempt.metrics={'error_type':type(error).__name__}
                if definitive:db.execute(update(target).where(target.id==target_id).values(reserved=target.reserved-upper))
                log(db,self.run_id,role,'API отклонил запрос' if definitive else 'Исход API-вызова неизвестен; резерв сохранён',company_id,{'call_id':key,'state':call.state}); db.commit()
            if not definitive:raise UncertainCall('API-вызов прерван; требуется сверка расхода в журнале.') from error
            raise
        usage=response.usage
        if usage is None:raise UncertainCall('API не вернул usage; резерв сохранён для сверки.')
        searches=sum(1 for x in response.output if x.type=='web_search_call')
        # Configured tariff estimate; reserves are separate and cached discounts remain explicit.
        actual=(usage.input_tokens*inp+usage.output_tokens*out)/1_000_000+searches*.01
        raw_usage=usage.model_dump() if hasattr(usage,'model_dump') else vars(usage)
        metrics={'input_tokens':usage.input_tokens,'output_tokens':usage.output_tokens,'searches':searches,'estimated':True,'raw_usage':raw_usage,'tariff_version':'configured-base-v1','cached_discount_applied':False}
        parsed=None
        try:parsed=schema.model_validate_json(response.output_text)
        except Exception:pass
        with Session() as db:
            call=db.get(ModelCall,key);call.state='COMPLETE';call.response_id=response.id;call.cost=actual
            call.result=parsed.model_dump() if parsed else None
            call.metrics=metrics
            attempt=db.get(ModelAttempt,attempt_id);attempt.state='COMPLETE';attempt.response_id=response.id;attempt.cost=actual;attempt.metrics=metrics
            db.execute(update(target).where(target.id==target_id).values(reserved=target.reserved-upper,spent=target.spent+actual))
            log(db,self.run_id,role,'API-вызов завершён',company_id,{'call_id':key,'model':model,'cost':actual,**call.metrics});db.commit()
        if parsed is None:raise ValueError('Ответ модели не прошёл структурную проверку')
        return parsed
