"""Additive history and immutable context snapshots for pipeline v3."""
import hashlib
from copy import deepcopy
from .db import now

def context_snapshot(context):
    text=context.source_text or context.summary
    return {k:getattr(context,k) for k in ('id','name','summary','source_name')}|{'source_text':text,'source_hash':hashlib.sha256(text.encode()).hexdigest(),'version':1}

def prepare_retry(company):
    previous={'revision':company.context.get('research_revision',1),'saved_at':now(),
              'research':deepcopy(company.research),'profile':deepcopy(company.profile),
              'stage':company.stage,'group':company.group,'identity':company.identity,'reason':company.reason}
    context=dict(company.context)
    context['research_history']=[*context.get('research_history',[]),previous]
    context['research_revision']=previous['revision']+1
    if not context.get('acquisition_plan',{}).get('identity_confirmed',True):
        context.pop('acquisition_plan',None)
        if context.get('initial_snapshot') is None:context.pop('initial_error',None)
    company.context=context
    reports={}
    for name,value in company.research.items():
        state=deepcopy(value)
        if state.get('state') in ('UNAVAILABLE','LIMIT_REACHED','IDENTITY_REJECTED','FAILED') or (state.get('state')=='DISCOVERED' and 'pages' not in state):
            # Identity rejection may have no page checkpoint; rebuild that direction cleanly.
            if state.get('state') in ('IDENTITY_REJECTED','DISCOVERED') and 'pages' not in state:
                state={}
            else:
                failed={e['url'] for e in state.get('errors',[]) if 'url' in e}
                state['attempted_urls']=[u for u in state.get('attempted_urls',[]) if u not in failed]
                state['errors']=[];state['state']='DISCOVERED'
        reports[name]=state
    company.research=reports
    company.stage='PENDING';company.group='NOT_PROCESSED';company.profile={};company.reason=''
    if company.identity=='NEEDS_REVIEW':company.identity='UNVERIFIED'
