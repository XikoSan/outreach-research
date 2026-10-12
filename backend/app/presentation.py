"""Compact, evidence-grounded read models for the workspace."""
from collections import Counter
from sqlalchemy import func
from .db import Company
from .schemas import selected_directions
from .rules import classify, classify_with_trace

ATTENTION=('FAILED','NEEDS_REVIEW','INSUFFICIENT_DATA','AMBIGUOUS','UNCLASSIFIED')

def counts(db,rid):
    rows=db.query(Company.group,Company.stage,Company.identity,func.count(Company.id)).filter_by(run_id=rid).group_by(Company.group,Company.stage,Company.identity).all()
    groups=Counter();researched=0;finished=0
    for group,stage,identity,n in rows:
        groups[group]+=n
        if stage=='PROFILE_READY' and identity=='CONFIRMED':researched+=n
        if stage=='PROFILE_READY' or group in ATTENTION or group=='EXCLUDED':finished+=n
    return {'groups':dict(groups),'researched':researched,'finished':finished,'assigned':sum(n for g,n in groups.items() if g.startswith('segment_')),'attention':sum(groups[g] for g in ATTENTION),'excluded':groups['EXCLUDED']}

def visible_reports(company,run):
    allowed=selected_directions(run.config)
    return {direction:report for direction,report in company.research.items()
            if direction in allowed or direction.startswith(('base','targeted_'))}

def evidence(reports):
    return list({e['id']:{**e,'direction':direction} for direction,report in reports.items() for e in report.get('evidence',[])}.values())

def public_segment(segment, company_id=None):
    """Expose common segment copy and, at most, one member's audit binding."""
    if segment is None:
        return None
    visible={key:value for key,value in segment.items() if key not in ('member_bindings','signal_labels')}
    binding=segment.get('member_bindings',{}).get(company_id) if company_id else None
    if binding:
        visible['member_bindings']={company_id:binding}
    names=set(segment.get('required_signals',[])+segment.get('supporting_signals',[])
              +segment.get('contradicting_signals',[])+segment.get('event_signals',[]))
    if binding:
        for facet in ('channel','product'):
            names.update(binding.get(facet,{}).get('raw_signal_names',[]))
    visible['signal_labels']={name:label for name,label in segment.get('signal_labels',{}).items()
                              if name in names}
    return visible


def company_view(company,run,detail=False):
    segments={s['id']:s for s in run.segments}
    segment=segments.get(company.group)
    reports=visible_reports(company,run)
    ev=evidence(reports)
    known={e['id'] for e in ev}
    signals=[s for s in company.profile.get('signals',[]) if set(s.get('evidence_ids',[]))<=known]
    names=set((segment or {}).get('required_signals',[])+(segment or {}).get('supporting_signals',[]))
    mapped=(segment or {}).get('member_bindings',{}).get(company.id,{})
    for facet in ('channel','product'):
        names.update(mapped.get(facet,{}).get('raw_signal_names',[])[:1])
    important=[s for s in signals if s.get('state')=='confirmed' and s.get('confidence') in ('HIGH','MEDIUM') and (not segment or s['name'] in names)]
    if company.stage=='PROFILE_READY' and company.identity=='CONFIRMED':
        if run.config.get('methodology_version')=='campaign-v1':
            from .campaigns import classify_campaign
            group,reason,_=classify_campaign({**company.profile,'company_id':company.id},ev,run.segments)
        elif run.config.get('pipeline_version',1)>=3:
            group,reason,_=classify_with_trace(company.profile,ev,run.segments)
        else:
            group,reason=classify(company.profile,ev,run.segments)
    else:
        group,reason=company.group,company.reason
    result={k:getattr(company,k) for k in ('id','name','resource','identity','stage','group','reason')}
    result.update(segment=public_segment(segment,company.id if detail else None),signals=important[:3],evidence_count=len(ev),evidence_state='Исключена из выборки' if company.group=='EXCLUDED' else 'Достаточно для сегмента' if segment and group==company.group else ('Есть ограничения' if company.group in ATTENTION else 'Исследование не завершено'),evidence_reason=reason,latest_publication=max((e.get('published_at','') or '' for e in ev),default=''))
    if detail:
        result.update(legal_ids=company.context.get('legal_ids',[]),websites=company.context.get('websites',[]),socials=company.context.get('socials',[]),profile={**company.profile,'signals':signals},signals=signals,evidence=ev,source_urls=sorted({page['url'] for report in reports.values() for page in report.get('pages',[]) if page.get('url')}),reports={k:{a:v for a,v in report.items() if a in ('summary','state','limitations','contradictions','coverage','termination_reason','validation_version')} for k,report in reports.items()})
    if not detail and segment:result['segment']={k:segment[k] for k in ('id','name','hypothesis')}
    return result
