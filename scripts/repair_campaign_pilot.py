"""Repair citation-loss answers from saved pages and reclassify the six-company pilot.

No fetching, search, campaign generation, or new segment review is performed.
"""
import argparse,json,time
from collections import Counter
from pathlib import Path
from sqlalchemy import select
from app.db import Session,Run,Company,ModelCall,log
from app.pipeline import Pipeline,evidence_for
from app.campaigns import repair_citation_losses,classify_campaign
from app.workbooks import export_run
from app.config import settings

parser=argparse.ArgumentParser();parser.add_argument('run_id');args=parser.parse_args()
rid=args.run_id;started=time.time()
with Session() as db:
    run=db.get(Run,rid)
    if not run or run.status not in ('COMPLETED','COMPLETED_WITH_ISSUES') or run.config.get('methodology_version')!='campaign-v1':
        raise SystemExit('Expected completed campaign pilot')
    ids=run.reports.get('pilot',{}).get('selected_ids',[])
    if not ids or len(ids)>6 or len(run.segments)!=1:raise SystemExit('Expected one approved segment and at most six selected companies')
    segment=run.segments[0]
    needed={name.removeprefix('criterion_') for name in segment['required_signals']+segment['supporting_signals'] if name.startswith('criterion_')}
    old_groups={cid:db.get(Company,cid).group for cid in ids}
    start_spent=run.spent
    for cid in ids:
        company=db.get(Company,cid);base=dict(company.research.get('base',{}))
        current={a['criterion_id'] for a in company.profile.get('base_research',{}).get('answers',[]) if a['status']=='confirmed'}
        raw=set()
        calls=db.execute(select(ModelCall).where(ModelCall.run_id==rid,ModelCall.company_id==cid,ModelCall.role=='extraction')).scalars()
        for call in calls:
            result=call.result or {}
            if 'report' not in result:continue
            raw.update(a['criterion_id'] for a in result.get('answers',[]) if a.get('status')=='confirmed')
        base['citation_losses']=sorted((set(base.get('citation_losses',[]))|raw)-current)
        company.research={**company.research,'base':base}
    history=list(run.reports.get('citation_repair_history',[]));history.append({'groups_before':old_groups,'spent_before':start_spent,'segment_id':segment['id']})
    run.reports={**run.reports,'citation_repair_history':history}
    run.status='RUNNING';run.error=None;db.commit()
pipe=Pipeline(rid);pipe.selected_ids=set(ids)
try:
    repaired=[]
    for cid in ids:
        with Session() as db:
            company=db.get(Company,cid)
            wanted=set(company.research.get('base',{}).get('citation_losses',[])) & needed
            already=company.group==segment['id']
        if wanted and not already:
            changed=repair_citation_losses(pipe,cid,wanted)
            repaired.append({'company_id':cid,'criteria':sorted(wanted),'recovered':changed})
    with Session() as db:
        run=db.get(Run,rid)
        segment=run.segments[0]
        for cid in ids:
            company=db.get(Company,cid)
            group,reason,trace=classify_campaign(company.profile,evidence_for(company),[segment])
            company.group=group;company.reason=reason
            company.profile={**company.profile,'classification':{'segment_version':run.segmentation_version,'trace':trace}}
        # Keep the reusable rule, not a frozen enumeration of the first three companies.
        if ' В переданной выборке' in segment['definition']:
            segment={**segment,'definition':segment['definition'].split(' В переданной выборке',1)[0]}
            run.segments=[segment]
        export_run(db,run,settings.data_dir/'exports',partial=True)
        groups=Counter(db.get(Company,cid).group for cid in ids)
        run.status='COMPLETED_WITH_ISSUES' if any(group!=segment['id'] for group in groups) or db.query(Company).filter_by(run_id=rid,group='NOT_PROCESSED').count() else 'COMPLETED'
        run.stage='EXPORTED'
        log(db,rid,'CITATION_REPAIR','Перепроверены сохранённые страницы и назначения пилота',metrics={'repaired_companies':sum(x['recovered'] for x in repaired),'selected_companies':len(ids)})
        db.commit()
except Exception as error:
    with Session() as db:
        run=db.get(Run,rid);run.status='PAUSED';run.error=type(error).__name__+': '+str(error)[:250];db.commit()
    raise
finally:
    if pipe.gw.client:pipe.gw.client.close()
    with Session() as db:
        run=db.get(Run,rid)
        groups={cid:db.get(Company,cid).group for cid in ids}
        report={'run_id':rid,'status':run.status,'spent':run.spent,'additional_cost':round(run.spent-start_spent,8),'seconds':round(time.time()-started,1),'groups_before':old_groups,'groups_after':groups,'repairs':locals().get('repaired',[])}
        folder=Path('data/observations')/rid;folder.mkdir(parents=True,exist_ok=True)
        (folder/'citation-repair-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({'status':report['status'],'additional_cost':report['additional_cost'],'seconds':report['seconds'],'groups_before':dict(Counter(old_groups.values())),'groups_after':dict(Counter(groups.values())),'repairs':report['repairs']},ensure_ascii=False),flush=True)
