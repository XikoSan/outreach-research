"""Durable API operations with conservative reservations and response checkpoints."""
import hashlib
import json
from openai import OpenAI, APIStatusError
from sqlalchemy import update
from .config import settings
from .db import Session, Run, PromptPackage, ModelCall, log
from . import credentials

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
                saved.state='STARTED'; saved.reservation=upper; saved.result=None
            else:
                db.add(ModelCall(id=key,run_id=self.run_id,company_id=company_id,package_id=package_id,role=role,model=model,state='STARTED',reservation=upper,search=search))
            log(db,self.run_id,role,'API-операция зарезервирована',company_id,{'call_id':key,'reserved':upper,'model':model})
            db.commit()
        kwargs = {'tools':[{'type':'web_search','search_context_size':'low'}], 'max_tool_calls':1} if search else {}
        try:
            # Parse manually after recording usage, so invalid JSON does not hide billing.
            response = self.client.responses.create(model=model,instructions=instructions,input=text,text={'format':{'type':'json_schema','name':schema.__name__,'strict':True,'schema':strict_schema(schema.model_json_schema())}},max_output_tokens=max_output,reasoning={'effort':'low' if role in ('discovery','extraction') else 'medium'},store=False,**kwargs)
        except Exception as error:
            definitive = isinstance(error, APIStatusError) and error.status_code in (400,401,403,404,422,429)
            with Session() as db:
                call=db.get(ModelCall,key); call.state='REJECTED' if definitive else 'UNCERTAIN'
                if definitive:db.execute(update(target).where(target.id==target_id).values(reserved=target.reserved-upper))
                log(db,self.run_id,role,'API отклонил запрос' if definitive else 'Исход API-вызова неизвестен; резерв сохранён',company_id,{'call_id':key,'state':call.state}); db.commit()
            if not definitive:raise UncertainCall('API-вызов прерван; требуется сверка расхода в журнале.') from error
            raise
        usage=response.usage
        if usage is None:raise UncertainCall('API не вернул usage; резерв сохранён для сверки.')
        searches=sum(1 for x in response.output if x.type=='web_search_call')
        # Track a conservative estimate at long-context rates until tariff verification.
        actual=(usage.input_tokens*inp*2+usage.output_tokens*out*1.5)/1_000_000+searches*.01
        parsed=None
        try:parsed=schema.model_validate_json(response.output_text)
        except Exception:pass
        with Session() as db:
            call=db.get(ModelCall,key);call.state='COMPLETE';call.response_id=response.id;call.cost=actual
            call.result=parsed.model_dump() if parsed else None
            call.metrics={'input_tokens':usage.input_tokens,'output_tokens':usage.output_tokens,'searches':searches,'estimated':True}
            db.execute(update(target).where(target.id==target_id).values(reserved=target.reserved-upper,spent=target.spent+actual))
            log(db,self.run_id,role,'API-вызов завершён',company_id,{'call_id':key,'model':model,'cost':actual,**call.metrics});db.commit()
        if parsed is None:raise ValueError('Ответ модели не прошёл структурную проверку')
        return parsed
