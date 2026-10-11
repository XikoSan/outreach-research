"""Resume one selected-company campaign pilot without selecting the other source rows."""
import argparse,json,time
from pathlib import Path
from app.db import Session,Run,Company
from app.pipeline import Pipeline
from app.context import prepare_context
from app.campaigns import prepare_campaign_plan,research_base
from app.crawler import fetch

parser=argparse.ArgumentParser()
parser.add_argument('run_id')
args=parser.parse_args();rid=args.run_id;started=time.time()
with Session() as db:
    run=db.get(Run,rid)
    if not run or run.status!='PAUSED' or run.config.get('methodology_version')!='campaign-v1':raise SystemExit('Expected a paused campaign pilot')
    ids=run.reports['pilot']['selected_ids']
    if len(ids)>6:raise SystemExit('Pilot selection exceeds six companies')
    run.status='RUNNING';run.error=None;db.commit()
pipeline=Pipeline(rid);pipeline.selected_ids=set(ids)
try:
    prepare_context(pipeline)
    prepare_campaign_plan(pipeline)
    print('Campaign plan ready',flush=True)
    for cid in ids:
        with Session() as db:
            company=db.get(Company,cid)
            if company.stage=='PROFILE_READY':continue
        research_base(pipeline,cid,fetch)
        print('Base company finished '+cid,flush=True)
    pipeline.finish()
except Exception as error:
    with Session() as db:
        run=db.get(Run,rid);run.status='PAUSED';run.error=type(error).__name__+': '+str(error)[:250];db.commit()
    print('Pilot paused: '+type(error).__name__,flush=True)
finally:
    if pipeline.gw.client:pipeline.gw.client.close()
    with Session() as db:
        run=db.get(Run,rid)
        result={'run_id':rid,'status':run.status,'stage':run.stage,'spent':run.spent,'reserved':run.reserved,'seconds':round(time.time()-started,1),'segments':len(run.segments),'error':run.error,'selected_companies':[{'id':company.id,'stage':company.stage,'group':company.group,'reason':company.reason} for company in db.query(Company).filter(Company.id.in_(ids))]}
        folder=Path('data/observations')/rid;folder.mkdir(parents=True,exist_ok=True)
        (folder/'pilot-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False),flush=True)
