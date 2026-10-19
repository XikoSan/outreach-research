"""One durable worker per database; restart pauses uncertain in-flight work."""
import time, logging
from sqlalchemy import text, update
from .db import Session, Run, PromptPackage, CompanyJob, engine
from .config import settings
from openai import APIStatusError
from .pipeline import Pipeline
from .workflow import generate_package
from . import credentials
from .llm import BudgetPause, RunPaused, UncertainCall

logger=logging.getLogger(__name__)

def main():
    credentials.migrate_global()
    if engine.dialect.name=='sqlite':
        import fcntl
        lock=open(settings.data_dir/'worker.lock','w')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    else:
        lock=engine.connect()
        if not lock.execute(text('SELECT pg_try_advisory_lock(18482026)')).scalar():raise RuntimeError('Worker already running')
    with Session() as db:
        db.execute(update(CompanyJob).where(CompanyJob.state=='RUNNING').values(state='PAUSED',owner=None,lease_until=0))
        for model in (Run,PromptPackage):
            db.execute(update(model).where(model.status.in_(['RUNNING','PAUSING'])).values(status='PAUSED',error='Обработка прервана перезапуском. Проверьте расходы и продолжите.'))
        db.commit()
    while True:
        with Session() as db:
            run=db.query(Run).filter_by(status='QUEUED').order_by(Run.created_at).first()
            if run:run.status='RUNNING';run.error=None;run_id=run.id;db.commit()
            else:run_id=None
            package=None if run_id else db.query(PromptPackage).filter_by(status='QUEUED').order_by(PromptPackage.created_at).first()
            package_id=package.id if package else None
            if package:package.status='RUNNING';package.error=None;db.commit()
        if not run_id and not package_id:time.sleep(1);continue
        try:
            if package_id:generate_package(package_id)
            elif run and run.config.get('workflow')=='offer-coarse-v2':
                from .offer_coarse import execute as execute_offer_coarse
                execute_offer_coarse(run_id)
            elif run and run.config.get('workflow')=='lead-research-v1':
                from .lead_research import execute as execute_lead
                from .db import Lead
                with Session() as db:
                    lead=db.query(Lead).filter_by(research_run_id=run_id).one()
                    lead_id=lead.id
                execute_lead(lead_id)
            elif run and run.config.get('workflow')=='coarse-v1':
                from .coarse import execute as execute_coarse
                execute_coarse(run_id)
            else:Pipeline(run_id).execute()
        except (BudgetPause,RunPaused,UncertainCall) as e:
            with Session() as db:
                r=db.get(PromptPackage,package_id) if package_id else db.get(Run,run_id)
                if r.status not in ('PAUSED',):r.status='PAUSED'
                if isinstance(e,UncertainCall):r.error=str(e)
                db.commit()
        except Exception as e:
            logger.error('Run failed: %s (%s)',run_id,type(e).__name__)
            with Session() as db:
                r=db.get(PromptPackage,package_id) if package_id else db.get(Run,run_id);r.status='FAILED'
                r.error=('API отклонил запрос (HTTP '+str(e.status_code)+'). Проверьте подключение и доступ к моделям в аккаунте API.') if isinstance(e,APIStatusError) else type(e).__name__+': '+str(e)[:500]
                db.commit()

if __name__=='__main__':main()
