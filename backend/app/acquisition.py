"""Source-first acquisition plan, persisted before direction extraction."""
from urllib.parse import urlsplit
from .db import Session, Run, Company
from .schemas import Discovery
from .prompts import DISCOVERY
from .llm import RunPaused

SOCIAL_HOSTS=('vk.com','t.me','telegram.me','linkedin.com','ok.ru','youtube.com','instagram.com','facebook.com')

def social(url):
    host=(urlsplit(url).hostname or '').lower()
    return any(host==h or host.endswith('.'+h) for h in SOCIAL_HOSTS)

def prepare(pipeline,cid,config,sender,fetch):
    with Session() as db:
        c=db.get(Company,cid)
        if c.context.get('acquisition_plan'):return c.context['acquisition_plan']
        name,resource,context=c.name,c.resource,dict(c.context)
    saved=context.get('initial_snapshot');initial=saved;error=None
    if initial is None:
        try:initial=fetch(resource)
        except Exception as exc:error=type(exc).__name__+': '+str(exc)[:200]
        with Session() as db:
            c=db.get(Company,cid);c.context={**c.context,'initial_snapshot':initial,'initial_error':error};db.commit()
    # One external baseline covers all five directions. Missing results remain explicit.
    found=pipeline.ask('discovery',DISCOVERY+' Исследуй все пять направлений одним общим внешним проходом. identity_quote — точный фрагмент переданной исходной страницы, подтверждающий идентичность; если страницы нет, оставь пустым и identity_confirmed=false. checked_directions содержит только направления, реально проверенные внешним поиском, а не просто перечисленные в задании.',
        {'company':name,'resource':resource,'names':context.get('names',[]),'description':context.get('description',''),
         'direction':'all','directions':['website','social','news_jobs','reviews','forums'],'limit':config['max_external_sources'],
         'initial_page':initial,'offers':sender['offers'],'source_filters':config.get('source_filters',''),
         'date':config['research_as_of'],'freshness_days':config['social_forum_days']},Discovery,search=True,company_id=cid)
    quote=found.identity_quote.strip()
    names=[n.casefold().strip() for n in context.get('names',[name]) if len(n.strip())>=3]
    # A website supplied by the source workbook is an authoritative identity anchor.
    # Pages discovered on that same origin inherit the company identity; social and
    # externally discovered resources still require the exact evidence gate below.
    trusted_website=bool(resource and not resource.startswith('unresolved:') and not social(resource))
    exact_evidence=bool(initial and found.identity_confirmed and len(quote)>=15 and quote in initial['text'] and any(n in quote.casefold() for n in names))
    confirmed=bool(not context.get('identity_conflict') and (trusted_website or exact_evidence))
    sources={d:[] for d in ('website','social','news_jobs','reviews','forums')}
    official_socials=[]
    if initial:
        if social(resource):sources['social'].append(initial['url']);official_socials.append(initial['url'])
        else:sources['website'].append(initial['url'])
        host=urlsplit(initial['url']).hostname
        for link in initial.get('links',[]):
            url=link['url']
            if social(url):sources['social'].append(url);official_socials.append(url)
            elif urlsplit(url).hostname==host and any(x in (url+' '+link['label']).lower() for x in ('about','contact','product','service','company','услуг','продукт','компан','контакт')):sources['website'].append(url)
    for source in found.sources:sources[source.direction].append(source.url)
    for d in sources:sources[d]=list(dict.fromkeys(sources[d]))
    plan={'discovery':found.model_dump(),'sources':sources,'initial_page':initial,'official_socials':official_socials,
          'identity_confirmed':confirmed,'external_checked':True,'checked_directions':found.checked_directions,'identity_reason':found.identity_reason if confirmed else 'Не подтверждена идентичность исходного ресурса точным фрагментом','initial_error':error}
    with Session() as db:
        c=db.get(Company,cid);c.context={**c.context,'acquisition_plan':plan};c.identity='CONFIRMED' if confirmed else 'NEEDS_REVIEW';c.reason=plan['identity_reason']
        if not confirmed:
            c.group='NEEDS_REVIEW';c.stage='PROFILE_READY'
            c.research={**c.research,'website':{'state':'IDENTITY_REJECTED','summary':c.reason,'discovery':found.model_dump(),'sources':[resource],'evidence':[],'signals':[],'limitations':[error] if error else [],'coverage':'unavailable' if not initial else 'partial'}}
        db.commit()
    return plan
