"""One durable worker per database; restart pauses uncertain in-flight work."""
import time, logging
from sqlalchemy import text, update
from .db import Session, Run, engine
from .config import settings, Settings
from .pipeline import Pipeline
from .llm import BudgetPause, RunPaused, UncertainCall

logger=logging.getLogger(__name__)

def main():
    if engine.dialect.name=='sqlite':
        import fcntl
        lock=open(settings.data_dir/'worker.lock','w')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    else:
        lock=engine.connect()
        if not lock.execute(text('SELECT pg_try_advisory_lock(18482026)')).scalar():raise RuntimeError('Worker already running')
    with Session() as db:
        db.execute(update(Run).where(Run.status.in_(['RUNNING','PAUSING'])).values(status='PAUSED',error='Worker перезапущен. Проверьте расходы и продолжите с checkpoint.'));db.commit()
    while True:
        with Session() as db:
            run=db.query(Run).filter_by(status='QUEUED').order_by(Run.created_at).first()
            if run:run.status='RUNNING';run.error=None;run_id=run.id;db.commit()
            else:run_id=None
        if not run_id:time.sleep(1);continue
        try:
            # Reload a key saved through the sender profile without restarting the worker.
            settings.openai_api_key=Settings().openai_api_key
            Pipeline(run_id).execute()
        except (BudgetPause,RunPaused,UncertainCall) as e:
            with Session() as db:
                r=db.get(Run,run_id)
                if r.status not in ('PAUSED',):r.status='PAUSED'
                if isinstance(e,UncertainCall):r.error=str(e)
                db.commit()
        except Exception as e:
            logger.exception('Run failed: %s',run_id)
            with Session() as db:
                r=db.get(Run,run_id);r.status='FAILED';r.error=type(e).__name__+': '+str(e)[:500];db.commit()

if __name__=='__main__':main()
