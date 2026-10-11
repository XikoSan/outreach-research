"""Recheck the selected pilot after a feature schema change; retain old results."""
import argparse,json,time
from pathlib import Path
from app.db import Session,Run,Company
from app.campaigns import criterion_signals
from app.evidence import normalized_signals
from app.pipeline import Pipeline

parser=argparse.ArgumentParser();parser.add_argument('run_id');args=parser.parse_args()
rid=args.run_id;started=time.time()
with Session() as db:
    run=db.get(Run,rid)
    if not run or run.status!='COMPLETED_WITH_ISSUES' or run.config.get('methodology_version')!='campaign-v1':raise SystemExit('Expected completed campaign pilot')
    ids=run.reports['pilot']['selected_ids']
    if len(ids)>6:raise SystemExit('Selection exceeds six companies')
    history=list(run.reports.get('segmentation_history',[]))
    if run.reports.get('segmentation'):history.append(run.reports['segmentation'])
    run.reports={**run.reports,'segmentation_history':history}
    for cid in ids:
        company=db.get(Company,cid)
        answers=company.profile.get('base_research',{}).get('answers',[])
        evidence=[e for report in company.research.values() for e in report.get('evidence',[])]
        raw=[s for report in company.research.values() for s in report.get('signals',[])]
        company.profile={**company.profile,'signals':normalized_signals(raw+criterion_signals(answers,evidence))}
    run.segments=[];run.segmentation_version=0;run.status='RUNNING';run.error=None;db.commit()
pipe=Pipeline(rid);pipe.selected_ids=set(ids)
try:pipe.finish()
except Exception as error:
    with Session() as db:
        run=db.get(Run,rid);run.status='PAUSED';run.error=type(error).__name__+': '+str(error)[:250];db.commit()
    print('Paused: '+type(error).__name__,flush=True)
finally:
    if pipe.gw.client:pipe.gw.client.close()
    with Session() as db:
        run=db.get(Run,rid)
        report={'run_id':rid,'status':run.status,'spent':run.spent,'reserved':run.reserved,'seconds':round(time.time()-started,1),'segments':len(run.segments),'diagnostics':run.reports.get('segmentation',{}).get('diagnostics'),'groups':{c.group:sum(x.group==c.group for x in db.query(Company).filter(Company.id.in_(ids))) for c in db.query(Company).filter(Company.id.in_(ids))}}
        folder=Path('data/observations')/rid;folder.mkdir(parents=True,exist_ok=True)
        (folder/'resegmentation-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False),flush=True)
