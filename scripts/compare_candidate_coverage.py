"""Compare saved candidate selection with current nomination, without model calls or DB writes."""
import argparse,json
from pathlib import Path
from app.db import Session,Run,Company
from app.pipeline import evidence_for
from app.evidence import normalized_signals
from app.candidate_discovery import build_candidate_overview,diverse_candidates

def compare(rid):
    with Session() as db:
        r=db.get(Run,rid)
        if not r:raise ValueError('Research not found')
        companies=db.query(Company).filter_by(run_id=rid,stage='PROFILE_READY',identity='CONFIRMED').filter(Company.group!='EXCLUDED').all()
        profiles=[dict(company_id=c.id,signals=normalized_signals(c.profile.get('signals',[])),evidence=evidence_for(c)) for c in companies if c.profile.get('signals')]
        overview=build_candidate_overview(profiles)
        old=r.reports['segmentation']['candidate_discovery']
        old_selected=old['candidates'][:12]+old.get('rule_candidates',[])[:12]
        current=overview['model_overview']
        selected=diverse_candidates(current['candidates'])+diverse_candidates(current['rule_candidates'])
        members=lambda entries:{cid for candidate in entries for cid in candidate['company_ids']}
        unknown={c.id for c in companies if c.group=='UNCLASSIFIED'}
        return {'run_id':rid,'comparison':'Saved selection versus current selection on final saved profiles; citation repairs may have added facts.',
                'old_selected_company_count':len(members(old_selected)),
                'new_selected_company_count':len(members(selected)),
                'previously_unclassified_in_new_selection':len(members(selected)&unknown),
                'selected_exact_candidates':len(diverse_candidates(current['candidates'])),
                'rules':[{'id':c['id'],'companies':c['company_count'],'observation':c['shared_observation'],'limit':c['claim_limit']} for c in current['rule_candidates']],
                'newly_segmented_companies':None,'note':'Candidates only. No offer review, assignment, model calls or research modifications.',
                'selection':selected}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('run_id');parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();report=compare(args.run_id)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ('selection','rules')},ensure_ascii=False))
