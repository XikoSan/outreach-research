"""Compact, evidence-grounded read models for the workspace."""
from collections import Counter
from sqlalchemy import func
from .db import Company
from .rules import classify

ATTENTION=('FAILED','NEEDS_REVIEW','INSUFFICIENT_DATA','AMBIGUOUS','UNCLASSIFIED')

def counts(db,rid):
    rows=db.query(Company.group,Company.stage,Company.identity,func.count(Company.id)).filter_by(run_id=rid).group_by(Company.group,Company.stage,Company.identity).all()
    groups=Counter();researched=0;finished=0
    for group,stage,identity,n in rows:
        groups[group]+=n
        if stage=='PROFILE_READY' and identity=='CONFIRMED':researched+=n
        if stage=='PROFILE_READY' or group in ATTENTION:finished+=n
    return {'groups':dict(groups),'researched':researched,'finished':finished,'assigned':sum(n for g,n in groups.items() if g.startswith('segment_')),'attention':sum(groups[g] for g in ATTENTION)}

def evidence(company):
    return list({e['id']:{**e,'direction':direction} for direction,report in company.research.items() for e in report.get('evidence',[])}.values())

def company_view(company,run,detail=False):
    segments={s['id']:s for s in run.segments}
    segment=segments.get(company.group)
    signals=company.profile.get('signals',[])
    names=set((segment or {}).get('required_signals',[])+(segment or {}).get('supporting_signals',[]))
    important=[s for s in signals if s.get('state')=='confirmed' and s.get('confidence') in ('HIGH','MEDIUM') and (not segment or s['name'] in names)]
    ev=evidence(company)
    group,reason=classify(company.profile,ev,run.segments) if company.stage=='PROFILE_READY' and company.identity=='CONFIRMED' else (company.group,company.reason)
    result={k:getattr(company,k) for k in ('id','name','resource','identity','stage','group','reason')}
    result.update(segment=segment,signals=important[:3],evidence_count=len(ev),evidence_state='Достаточно для сегмента' if segment and group==company.group else ('Есть ограничения' if company.group in ATTENTION else 'Исследование не завершено'),evidence_reason=reason,latest_publication=max((e.get('published_at','') or '' for e in ev),default=''))
    if detail:
        result.update(profile=company.profile,signals=signals,evidence=ev,reports={k:{a:v for a,v in report.items() if a in ('summary','state','limitations','contradictions')} for k,report in company.research.items()})
    if not detail and segment:result['segment']={k:segment[k] for k in ('id','name','hypothesis')}
    return result
