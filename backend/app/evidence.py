"""Versioned evidence checks. Literal checks constrain, but do not prove, semantics."""
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from .rules import origin
from urllib.parse import urlsplit, urlunsplit

VERSION = 'evidence-v3'
ALIASES = {'multiple_products':'multiple_product_lines', 'many_product_lines':'multiple_product_lines',
           'new_market_expansion':'new_market_entry', 'multiple_audiences':'multiple_customer_segments'}

def canonical(name):
    name=re.sub(r'[^a-z0-9_]+','_',name.strip().casefold()).strip('_')
    return ALIASES.get(name,name)

def normalized_signals(signals):
    groups={}
    for signal in signals:
        value=dict(signal);name=canonical(value['name'])
        if not name:continue
        value['name']=name
        if name not in groups:groups[name]=value;continue
        old=groups[name]
        if old['state']!=value['state']:
            old.update(state='unknown',confidence='LOW',explanation='Противоречащие наблюдения; требуется сопоставление')
        elif value['confidence']=='LOW' or old['confidence']=='LOW':old['confidence']='LOW'
        elif value['confidence']=='MEDIUM':old['confidence']='MEDIUM'
        old['evidence_ids']=sorted(set(old['evidence_ids']+value['evidence_ids']))
    return [groups[key] for key in sorted(groups)]

def page_key(url):
    """Normalize equivalent root paths and default ports; preserve host and query."""
    parts=urlsplit(url)
    scheme=parts.scheme.lower()
    netloc=parts.netloc.lower()
    if scheme=='https' and netloc.endswith(':443'):netloc=netloc[:-4]
    elif scheme=='http' and netloc.endswith(':80'):netloc=netloc[:-3]
    return urlunsplit((scheme,netloc,parts.path or '/',parts.query,''))

def verify_report(report,pages,direction,days,*,as_of,company_id):
    reference=date.fromisoformat(as_of[:10]);accepted=[];mapping={};issues=[]
    by_url={page_key(p['url']):p for p in pages}
    for e in report.evidence:
        page=by_url.get(page_key(e.url));quote=' '.join(e.quote.split())
        text=' '.join(page['text'].split()) if page else ''
        if not page or len(quote)<15 or quote not in text:
            issues.append('Доказательство отклонено: ' + ('страница не найдена' if not page else 'точная цитата отсутствует'));continue
        data=e.model_dump();published=None
        basis=data.get('date_evidence','').strip()
        if e.published_at and basis and (basis in text or basis in page.get('date_metadata',[])):
            try:
                candidate=date.fromisoformat(e.published_at[:10])
                # ISO source date or a literal textual date independently parsed by code.
                parsed=source_date(basis)
                if parsed==candidate and candidate<=reference:published=candidate.isoformat()
            except ValueError:pass
        if e.published_at and published is None:issues.append('Дата не подтверждена источником; сохранено null')
        age=(reference-date.fromisoformat(published)).days if published else None
        current=True
        if direction in ('social','forums'):current=age is not None and age<=days
        elif direction=='news_jobs':current=age is not None and age<=365
        elif direction=='reviews':current=age is not None and age<=365
        # All external company/product assertions need an explicit subject span.
        subject=data.get('subject_quote','').strip()
        bound=page.get('subject_id')==company_id or bool(subject and subject in text and any(n.casefold() in subject.casefold() for n in page.get('subject_names',[]) if len(n.strip())>=3))
        provenance=data.get('provenance_quote','').strip()
        primary=bool(provenance and provenance in text and data.get('source_relation')=='primary')
        publisher=page.get('publisher_id') or (origin(e.url) if primary else None)
        eid=hashlib.sha256((e.url+'\n'+quote).encode()).hexdigest()[:24];mapping[e.id]=eid
        accepted.append({**data,'id':eid,'published_at':published,'current':current,
            'retrieved_at':page.get('retrieved_at'),'snapshot_hash':page.get('content_hash'),
            'publisher_id':publisher,'original_source_id':publisher if primary or page.get('publisher_id') else None,
            'subject_verified':bound,'validation_version':VERSION,'origin':origin(e.url)})
    known={e['id']:e for e in accepted};signals=[]
    for s in report.signals:
        ids=list(dict.fromkeys(mapping[i] for i in s.evidence_ids if i in mapping))
        if not ids:continue
        level=s.confidence
        if not all(known[i]['current'] and (known[i]['scope']=='category' or known[i]['subject_verified']) for i in ids):level='LOW'
        # Events require a verified publication date even on an official website.
        if any(token in canonical(s.name) for token in ('recent','launch','hiring','growth','new_market','new_business')):
            bound=90 if 'hiring' in canonical(s.name) else 365
            if not all(known[i]['published_at'] and (reference-date.fromisoformat(known[i]['published_at'])).days<=bound for i in ids):level='LOW'
        signals.append({**s.model_dump(),'confidence':level,'evidence_ids':ids})
    return {'summary':report.summary,'evidence':accepted,'signals':normalized_signals(signals),
            'contradictions':report.contradictions,'limitations':report.limitations+issues,'validation_version':VERSION}

def source_date(text):
    match=re.search(r'\b(20\d{2})-(\d{2})-(\d{2})\b',text)
    if match:return date(*map(int,match.groups()))
    match=re.search(r'\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b',text)
    if match:
        d,m,y=map(int,match.groups());return date(y,m,d)
    months=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']
    match=re.search(r'\b(\d{1,2})\s+('+'|'.join(months)+r')\s+(20\d{2})\b',text.casefold())
    if match:return date(int(match[3]),months.index(match[2])+1,int(match[1]))
    return None

def preserve_synthesis(proposed,original,evidence):
    """Synthesis prose cannot replace the validated atomic observations."""
    known={e['id'] for e in evidence}
    return normalized_signals([dict(s) for s in original if s.get('evidence_ids') and set(s['evidence_ids'])<=known])

def build_feature_matrix(profiles):
    rows=[];registry={}
    for p in profiles:
        signals=normalized_signals(p.get('signals',[]))
        rows.append({'company_id':p['company_id'],'features':{s['name']:{k:s[k] for k in ('state','confidence','evidence_ids')} for s in signals}})
        for s in signals:registry.setdefault(s['name'],{'id':s['name'],'label':s.get('label') or s['name'],'version':1})
    return {'version':1,'registry':registry,'rows':rows,'aliases':ALIASES}


def feature_patterns(profiles):
    groups={}
    for profile in profiles:
        key=tuple((s['name'],s['state'],s['confidence']) for s in normalized_signals(profile['signals']))
        groups.setdefault(key,[]).append(profile)
    return [{'features':[{'name':n,'state':state,'confidence':level} for n,state,level in key],
             'company_count':len(members),'examples':sorted(members,key=lambda p:p['company_id'])[:3]}
            for key,members in sorted(groups.items())]
