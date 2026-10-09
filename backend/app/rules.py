"""Conservative deterministic classification; unknown is never false evidence."""
from urllib.parse import urlsplit

def origin(url):
    host=(urlsplit(url).hostname or '').lower().removeprefix('www.')
    parts=host.split('.')
    # Conservative grouping: country suffixes may merge sources, never split a host.
    return '.'.join(parts[-2:])

def classify(profile, evidence, segments):
    signals={s['name']:s for s in profile.get('signals',[])}
    ev={e['id']:e for e in evidence}
    candidates=[]
    for seg in segments:
        required=seg['required_signals']
        if not required or not all(signals.get(n,{}).get('state')=='confirmed' and signals[n].get('confidence')=='HIGH' for n in required):continue
        if any(signals.get(n,{}).get('state')=='confirmed' and signals[n].get('confidence') in ('HIGH','MEDIUM') for n in seg['contradicting_signals']):continue
        names=required+seg['supporting_signals']
        matched=[signals[n] for n in dict.fromkeys(names) if signals.get(n,{}).get('state')=='confirmed' and signals[n]['confidence'] in ('HIGH','MEDIUM')]
        used=[ev[i] for s in matched for i in s['evidence_ids'] if i in ev and ev[i].get('scope')!='category']
        # Each required fact must have verified company/product evidence.
        if any(not any(i in ev and ev[i].get('scope')!='category' for i in signals[n]['evidence_ids']) for n in required):continue
        if len(matched)<2 or len({origin(e['url']) for e in used})<2 or len({e['quote'].strip().casefold() for e in used})<2:continue
        candidates.append(seg)
    if len(candidates)==1:return candidates[0]['id'],'Подтверждены обязательные признаки и независимые основания; исключающих противоречий нет.'
    if candidates:return 'AMBIGUOUS','Несколько подтверждённых гипотез; оснований для единственного выбора недостаточно.'
    good=[s for s in signals.values() if s.get('confidence') in ('HIGH','MEDIUM') and s.get('state')=='confirmed']
    if len(good)<2:return 'INSUFFICIENT_DATA','Недостаточно подтверждённых признаков для основной гипотезы.'
    return 'UNCLASSIFIED','Достаточные наблюдения не соответствуют проверенным сегментам.'


def classify_with_trace(profile, evidence, segments):
    from .evidence import normalized_signals, VERSION
    if profile.get('coverage_complete') is False:return 'INSUFFICIENT_DATA','Не завершены обязательные направления исследования',[{'segment_id':s['id'],'accepted':False,'reasons':['incomplete_coverage']} for s in segments]
    signals={s['name']:s for s in normalized_signals(profile.get('signals',[]))}
    ev={e['id']:e for e in evidence if e.get('validation_version')==VERSION and e.get('subject_verified') and e.get('current') and e.get('scope') in ('company','product')}
    trace=[];accepted=[]
    for seg in segments:
        required=seg['required_signals'];reasons=[]
        matches=[signals[n] for n in dict.fromkeys(required+seg['supporting_signals']) if n in signals and signals[n]['state']=='confirmed' and signals[n]['confidence'] in ('HIGH','MEDIUM') and any(i in ev for i in signals[n]['evidence_ids'])]
        if not required or any(n not in signals or signals[n]['state']!='confirmed' or signals[n]['confidence']!='HIGH' or not any(i in ev for i in signals[n]['evidence_ids']) for n in required):reasons.append('required_not_verified')
        if any(signals.get(n,{}).get('state')=='confirmed' and signals[n]['confidence'] in ('HIGH','MEDIUM') for n in seg['contradicting_signals']):reasons.append('contradiction')
        independent=False
        for a in matches:
            for b in matches:
                if a['name']==b['name']:continue
                for ai in a['evidence_ids']:
                    for bi in b['evidence_ids']:
                        x,y=ev.get(ai,{}),ev.get(bi,{})
                        if x.get('original_source_id') and y.get('original_source_id') and x['original_source_id']!=y['original_source_id'] and x.get('quote','').strip().casefold()!=y.get('quote','').strip().casefold():independent=True
        if not independent:reasons.append('independence_not_established')
        trace.append({'segment_id':seg['id'],'accepted':not reasons,'reasons':reasons,'matched_signals':[s['name'] for s in matches],'evidence_ids':sorted({i for s in matches for i in s['evidence_ids'] if i in ev})})
        if not reasons:accepted.append(seg['id'])
    if len(accepted)==1:return accepted[0],'Правила и независимые основания подтверждены',trace
    if accepted:return 'AMBIGUOUS','Несколько подтверждённых гипотез',trace
    return ('INSUFFICIENT_DATA' if len([s for s in signals.values() if s['state']=='confirmed' and s['confidence'] in ('HIGH','MEDIUM')])<2 else 'UNCLASSIFIED'),'Нет гипотезы с проверенными независимыми основаниями',trace
