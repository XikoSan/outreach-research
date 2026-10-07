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
