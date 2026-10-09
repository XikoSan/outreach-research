"""Read every part of long offer documents once; keep source-anchored extracts."""
import hashlib
from pydantic import Field
from .schemas import Strict
from .db import Session,Run
from .prompts import GUARD

class MaterialFact(Strict):
    quote:str=Field(min_length=10,max_length=1500)
    kind:str
class MaterialExtract(Strict):
    facts:list[MaterialFact]=Field(max_length=20)

def prepare_context(pipeline):
    with Session() as db:
        run=db.get(Run,pipeline.id)
        if run.config.get('pipeline_version',1)<3:return
        context=dict(run.context_snapshot)
        if context.get('prepared_text'):return
        text=context.get('source_text') or context.get('summary','')
        saved=dict(run.reports.get('context_parts',{}))
    if len(text.encode())<=24000:prepared=text
    else:
        quotes=[]
        for start in range(0,len(text),10000):
            part=text[start:start+10000];key=hashlib.sha256(part.encode()).hexdigest()
            if key not in saved:
                result=pipeline.ask('synthesis',GUARD+' Выдели из этой части документа все услуги, результаты, ограничения, запрещённые обещания и условия применимости. Только дословные quote; kind объясняет тип. Не считай рекомендацию утверждённой услугой. Не создавай новых услуг.',{'part':part,'part_start':start},MaterialExtract,max_output=5000)
                if any(f.quote not in part for f in result.facts):raise ValueError('Извлечение офферов содержит неподтверждённую цитату')
                saved[key]={'start':start,'facts':result.model_dump()['facts']}
                with Session() as db:
                    run=db.get(Run,pipeline.id);run.reports={**run.reports,'context_parts':saved};db.commit()
            quotes.extend(f['quote'] for f in saved[key]['facts'])
        prepared='\n'.join(dict.fromkeys(quotes))
        if not prepared:raise ValueError('Не удалось выделить предложение из документа')
        if len(prepared.encode())>60000:raise ValueError('Описание предложения слишком велико для безопасного контекста; требуется компактный документ')
    # User-edited summary remains explicit alongside source, never silently discarded.
    if context.get('summary') and context['summary'] not in prepared:prepared=context['summary']+'\nМатериал:\n'+prepared
    with Session() as db:
        run=db.get(Run,pipeline.id);run.context_snapshot={**context,'prepared_text':prepared,'preparation_version':1};db.commit()
