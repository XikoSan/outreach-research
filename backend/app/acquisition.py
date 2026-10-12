"""Source-first acquisition plan, persisted before direction extraction."""
from urllib.parse import urlsplit
from .db import Session, Run, Company
from .schemas import Discovery, RESEARCH_DIRECTIONS, selected_directions
from .prompts import DISCOVERY
from .llm import RunPaused
from .workbooks import SOCIAL

def social(url):
    host=(urlsplit(url).hostname or '').lower()
    return any(host==h or host.endswith('.'+h) for h in SOCIAL)

def prepare(pipeline,cid,config,sender,fetch):
    with Session() as db:
        c=db.get(Company,cid)
        if c.context.get('acquisition_plan'):
            plan=c.context['acquisition_plan']
            discovery=plan.get('discovery',{})
            return {**plan,
                    'sources':{d:plan.get('sources',{}).get(d,[]) for d in RESEARCH_DIRECTIONS},
                    'checked_directions':[d for d in plan.get('checked_directions',[]) if d in RESEARCH_DIRECTIONS],
                    'discovery':{**discovery,
                                 'checked_directions':[d for d in discovery.get('checked_directions',[]) if d in RESEARCH_DIRECTIONS],
                                 'sources':[s for s in discovery.get('sources',[]) if s.get('direction') in RESEARCH_DIRECTIONS]}}
        name,resource,context=c.name,c.resource,dict(c.context)
    anchor_social=social(resource) or resource in context.get('socials',[])
    allowed=selected_directions(config)
    if anchor_social:allowed.add('social')  # Required identity fallback when no website exists.
    directions=[d for d in RESEARCH_DIRECTIONS if d in allowed]
    saved=context.get('initial_snapshot');initial=saved;error=None
    if initial is None:
        try:initial=fetch(resource)
        except Exception as exc:error=type(exc).__name__+': '+str(exc)[:200]
        with Session() as db:
            c=db.get(Company,cid);c.context={**c.context,'initial_snapshot':initial,'initial_error':error};db.commit()
    # A supplied, readable website needs no paid discovery when no external area is selected.
    searched=not (allowed=={'website'} and initial is not None)
    if searched:
        found=pipeline.ask('discovery',DISCOVERY+' Исследуй только перечисленные directions одним общим внешним проходом. Не ищи отключённые направления. identity_quote — точный фрагмент переданной исходной страницы, подтверждающий идентичность; если страницы нет, оставь пустым и identity_confirmed=false. checked_directions содержит только реально проверенные направления.',
            {'company':name,'resource':resource,'names':context.get('names',[]),'legal_names':context.get('legal_names',[]),'description':context.get('description',''),
             'direction':'all','directions':directions,'limit':config['max_external_sources'],
             'initial_page':initial,'offers':sender['offers'],'source_filters':config.get('source_filters',''),
             'date':config['research_as_of'],'freshness_days':config['social_forum_days']},Discovery,search=True,company_id=cid)
    else:
        found=Discovery(identity_confirmed=True,identity_reason='Сайт указан в исходной базе',products='',sources=[],checked_directions=[])
    quote=found.identity_quote.strip()
    names=[n.casefold().strip() for n in [*context.get('names',[name]),*context.get('legal_names',[])] if len(n.strip())>=3]
    # A website supplied by the source workbook is an authoritative identity anchor.
    # Pages discovered on that same origin inherit the company identity; social and
    # externally discovered resources still require the exact evidence gate below.
    declared_social=resource in context.get('socials',[])
    trusted_website=bool(resource and not resource.startswith('unresolved:') and not (social(resource) or declared_social))
    exact_evidence=bool(initial and found.identity_confirmed and len(quote)>=15 and quote in initial['text'] and any(n in quote.casefold() for n in names))
    confirmed=bool(not context.get('identity_conflict') and (trusted_website or exact_evidence))
    sources={d:[] for d in RESEARCH_DIRECTIONS}
    official_socials=[]
    if initial:
        if social(resource) or declared_social:sources['social'].append(initial['url']);official_socials.append(initial['url'])
        else:sources['website'].append(initial['url'])
        host=urlsplit(initial['url']).hostname
        for link in initial.get('links',[]):
            url=link['url']
            if social(url) and 'social' in allowed:sources['social'].append(url);official_socials.append(url)
            elif urlsplit(url).hostname==host and any(x in (url+' '+link['label']).lower() for x in ('about','contact','product','service','company','услуг','продукт','компан','контакт')):sources['website'].append(url)
    if 'social' in allowed:
        for source in context.get('socials',[]):sources['social'].append(source)
    for source in found.sources:
        if source.direction in allowed:sources[source.direction].append(source.url)
    for d in sources:sources[d]=list(dict.fromkeys(sources[d]))
    plan={'discovery':found.model_dump(),'sources':sources,'initial_page':initial,'official_socials':official_socials,
          'identity_confirmed':confirmed,'external_checked':searched,'checked_directions':[d for d in found.checked_directions if d in allowed],'identity_reason':found.identity_reason if confirmed else 'Не подтверждена идентичность исходного ресурса точным фрагментом','initial_error':error}
    with Session() as db:
        c=db.get(Company,cid);c.context={**c.context,'acquisition_plan':plan};c.identity='CONFIRMED' if confirmed else 'NEEDS_REVIEW';c.reason=plan['identity_reason']
        if not confirmed:
            c.group='NEEDS_REVIEW';c.stage='PROFILE_READY'
            c.research={**c.research,'website':{'state':'IDENTITY_REJECTED','summary':c.reason,'discovery':found.model_dump(),'sources':[resource],'evidence':[],'signals':[],'limitations':[error] if error else [],'coverage':'unavailable' if not initial else 'partial'}}
        db.commit()
    return plan
