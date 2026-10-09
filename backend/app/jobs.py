"""Bounded company scheduler; external calls remain guarded by operation records."""
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from .db import Session, Run, Company, CompanyJob, uid
from .llm import RunPaused

def ensure_jobs(run_id,company_ids):
    with Session() as db:
        for cid in company_ids:
            revision=db.get(Company,cid).context.get('research_revision',1)
            existing=db.query(CompanyJob).filter_by(run_id=run_id,company_id=cid,revision=revision).first()
            if not existing:db.add(CompanyJob(run_id=run_id,company_id=cid,revision=revision))
        db.commit()

def claim(run_id,cid):
    owner=uid();now=time.time()
    with Session() as db:
        if db.get(Run,run_id).status!='RUNNING':raise RunPaused()
        revision=db.get(Company,cid).context.get('research_revision',1)
        job=db.query(CompanyJob).filter_by(run_id=run_id,company_id=cid,revision=revision).one()
        changed=db.execute(update(CompanyJob).where(CompanyJob.id==job.id,CompanyJob.state.in_(['PENDING','PAUSED','FAILED'])).values(state='RUNNING',owner=owner,lease_until=now+180,stage='RESEARCH'))
        db.commit()
        return (job.id,owner) if changed.rowcount else None

def touch(run_id,cid,stage):
    with Session() as db:
        db.execute(update(CompanyJob).where(CompanyJob.run_id==run_id,CompanyJob.company_id==cid,CompanyJob.state=='RUNNING').values(stage=stage,lease_until=time.time()+180));db.commit()

def complete(job,state):
    with Session() as db:
        db.execute(update(CompanyJob).where(CompanyJob.id==job[0],CompanyJob.owner==job[1]).values(state=state,owner=None,lease_until=0));db.commit()

def execute_jobs(run_id,ids,perform,concurrency):
    ensure_jobs(run_id,ids)
    def task(cid):
        job=claim(run_id,cid)
        if not job:return
        try:perform(cid)
        except BaseException:
            complete(job,'PAUSED');raise
        else:complete(job,'COMPLETE')
    iterator=iter(ids);pending=set();failure=None
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        while True:
            while failure is None and len(pending)<concurrency:
                with Session() as db:running=db.get(Run,run_id).status=='RUNNING'
                if not running:failure=RunPaused();break
                cid=next(iterator,None)
                if cid is None:break
                pending.add(pool.submit(task,cid))
            if not pending:break
            done,pending=wait(pending,return_when=FIRST_COMPLETED)
            for result in done:
                try:result.result()
                except BaseException as exc:
                    failure=failure or exc
                    with Session() as db:
                        run=db.get(Run,run_id)
                        if run.status=='RUNNING':run.status='PAUSING';db.commit()
    if failure:raise failure
    with Session() as db:
        if db.query(CompanyJob).filter_by(run_id=run_id,state='RUNNING').count():raise RunPaused('Остались активные задания')
