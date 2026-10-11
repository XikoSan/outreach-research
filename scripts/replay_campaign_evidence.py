"""Read-only evidence replay; no model, crawler, credentials or company contacts."""
import argparse,json
from pathlib import Path
from collections import Counter
from app.db import Session,Run,Company,ModelCall
from app.evidence import verify_report,page_key
from app.schemas import ResearchReport

parser=argparse.ArgumentParser()
parser.add_argument('run_id');parser.add_argument('--output',required=True)
a=parser.parse_args();result={'run_id':a.run_id,'mode':'offline_evidence_replay','api_calls':0,'companies':[]}
with Session() as db:
    r=db.get(Run,a.run_id)
    if not r:raise SystemExit('Run not found')
    for c in db.query(Company).filter_by(run_id=a.run_id):
        pages={}
        for direction,report in c.research.items():
            for p in report.get('pages',[]):pages[page_key(p['url'])]=(direction,p)
        accepted={};signals=[];limitations=Counter();responses=0
        for call in db.query(ModelCall).filter_by(run_id=a.run_id,company_id=c.id,role='extraction',state='COMPLETE'):
            if not call.result:continue
            try:report=ResearchReport.model_validate(call.result)
            except ValueError:continue
            relevant={pages[page_key(e.url)][0] for e in report.evidence if page_key(e.url) in pages}
            for direction in relevant:
                batch=[p for d,p in pages.values() if d==direction]
                checked=verify_report(report,batch,direction,r.config['social_forum_days'],as_of=r.config['research_as_of'],company_id=c.id)
                accepted.update({e['id']:e for e in checked['evidence']});signals+=checked['signals']
                limitations.update(x for x in checked['limitations'] if x.startswith('Доказательство отклонено:'))
            responses+=1
        result['companies'].append({'id':c.id,'saved_pages':len(pages),'responses_checked':responses,'exact_evidence':len(accepted),'bound_evidence':sum(bool(e['subject_verified']) for e in accepted.values()),'current_bound_evidence':sum(bool(e['subject_verified'] and e['current']) for e in accepted.values()),'rejections':dict(limitations)})
result['totals']={key:sum(c[key] for c in result['companies']) for key in ('saved_pages','responses_checked','exact_evidence','bound_evidence','current_bound_evidence')}
result['limitations']=['Historical responses do not contain campaign criterion answers. This checks evidence integrity, not new model quality or segment usefulness.','Repeated historical responses and direction checks may repeat rejection counters.','No segment assignments or database records changed.']
p=Path(a.output);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps({'companies':len(result['companies']),**result['totals'],'report':str(p)}))
