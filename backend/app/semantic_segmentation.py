"""Review all evidence-bearing profiles; never assign membership from similarity alone."""
import json
from pydantic import Field
from .schemas import Strict
from .campaigns import CampaignSegment, APPLICABILITY_RUBRIC
from .prompts import GUARD
from .evidence import VERSION

class Member(Strict):
    company_id: str
    context_signal: str
    product_signal: str

class Proposal(CampaignSegment):
    members: list[Member]

class Decision(Strict):
    company_id: str
    reason: str

class BatchResult(Strict):
    segments: list[Proposal]
    unassigned: list[Decision]

PROMPT = GUARD + APPLICABILITY_RUBRIC + '''
Сегментируй компании для массового предложения сотрудничества по реальным услугам отправителя.
Рассмотри КАЖДУЮ компанию из profiles. Участники могут иметь разные товары, но должны разделять
основание одного предложения, аргументации и следующего шага. Не ищи только одинаковые слова.
Не назначай готовые офферы заранее: начни с наблюдаемых ситуаций компаний и сопоставь с услугами.
Для каждого участника укажи два РАЗНЫХ существующих имени признаков: context_signal и product_signal.
Оба должны обосновывать предложение; не выдумывай связь между независимо упомянутыми фактами.
Только applicability; required_signals/supporting_signals/contradicting_signals/event_signals оставь пустыми:
проверка идёт по двум точным фактам каждого участника. Не ограничивай число групп тремя.
Если подходит existing_groups, используй его id и сохрани предложение без изменения смысла.
Иначе предложи новое id. Одиночную группу можно предложить как кандидата для объединения с будущими
порциями; окончательный сегмент должен содержать минимум две компании. Одну компанию назначай
не более чем в одну группу. Для каждой неназначенной компании запиши unassigned с конкретной
причиной. Отсутствие публичного спроса не причина отказа. Нельзя назначать компании из existing_groups
повторно, только из текущих profiles. Не указывай численность группы в тексте предложения.
'''

def discover(pipeline, profiles, offers):
    prepared=[];index={};notes=[];rejected=[]
    for p in profiles:
        ev={e['id']:e for e in p.get('evidence',[]) if e.get('validation_version')==VERSION and e.get('subject_verified') and e.get('current') and e.get('scope') in ('company','product')}
        signals=[s for s in p.get('signals',[]) if s.get('state')=='confirmed' and s.get('confidence')=='HIGH' and set(s.get('evidence_ids',[]))&ev.keys()]
        index[p['company_id']]={s['name']:{**s,'evidence_ids':sorted(set(s['evidence_ids'])&ev.keys())} for s in signals}
        if len(signals)<2:
            rejected.append({'company_id':p['company_id'],'reason':'Меньше двух подтверждённых фактов'});continue
        prepared.append({'company_id':p['company_id'],'description':p.get('description','')[:300],
                         'signals':[{'name':s['name'],'label':s.get('label',''),'quotes':[ev[e]['quote'][:650] for e in s['evidence_ids'] if e in ev][:1]} for s in signals]})
    groups={};batch=[];size=0;batches=[]
    for p in prepared:
        length=len(json.dumps(p,ensure_ascii=False).encode())
        if length>65000:raise ValueError('Профиль слишком велик для полной смысловой проверки; требуется разбиение без потери фактов')
        if batch and size+length>65000:batches.append(batch);batch=[];size=0
        batch.append(p);size+=length
    if batch:batches.append(batch)
    for batch in batches:
        existing=[{k:g[k] for k in ('id','name','offer_id','hypothesis','cooperation_proposal','shared_message','next_step')} for g in groups.values()]
        payload={'profiles':batch,'existing_groups':existing,'offers':offers}
        if len(json.dumps(payload,ensure_ascii=False).encode())>150000:raise ValueError('Обзор групп слишком велик; требуется отдельное объединение')
        result=pipeline.ask('segmentation',PROMPT,payload,BatchResult,max_output=10000)
        current={p['company_id'] for p in batch};assigned=set();decisions=[]
        for proposed in result.segments:
            value=proposed.model_dump();members=value.pop('members')
            if value['basis_type']!='applicability' or value['offer_id'] not in {o['id'] for o in offers}:continue
            if any(value[k] for k in ('required_signals','supporting_signals','contradicting_signals','event_signals')):continue
            if value['id'] in groups:
                previous=groups[value['id']]
                if any(value[k]!=previous[k] for k in ('offer_id','cooperation_proposal','shared_message','next_step')):
                    continue
            valid={}
            for member in members:
                cid=member['company_id'];names=[member['context_signal'],member['product_signal']]
                if cid not in current or cid in assigned or len(set(names))!=2 or not all(n in index[cid] for n in names):continue
                valid[cid]={facet:{'raw_signal_names':[name],'evidence_ids':index[cid][name]['evidence_ids']} for facet,name in zip(('channel','product'),names)}
                assigned.add(cid)
            if not valid:continue
            if value['id'] not in groups:groups[value['id']]={**value,'member_bindings':{},'candidate_rule':{'kind':'semantic_profiles','claim_limit':'Общее основание предложения не доказывает потребность'}}
            groups[value['id']]['member_bindings'].update(valid)
        reasons={d.company_id:d.reason for d in result.unassigned if d.company_id in current}
        for cid in sorted(current-assigned):decisions.append({'company_id':cid,'reason':reasons.get(cid,'Модель не вернула проверяемое назначение')})
        notes.append({'input_company_ids':sorted(current),'assigned_company_ids':sorted(assigned),'unassigned':decisions})
    candidates=[]
    for group in groups.values():
        if len(group['member_bindings'])>=2:candidates.append(group)
        else:rejected.extend({'company_id':cid,'reason':'Для общего предложения не найден второй участник'} for cid in group['member_bindings'])
    return candidates,{'method':'semantic-profiles-v1','input_companies':len(profiles),'reviewed_companies':len(prepared),'batches':notes,'not_reviewed_or_singleton':rejected}
