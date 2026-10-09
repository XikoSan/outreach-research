import json,time,statistics,sys
from collections import Counter,defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from app.db import Session,Run,Company,ModelCall,Event
from app.crawler import fetch
rid=sys.argv[1]
with Session() as db:
 r=db.get(Run,rid);cs=db.query(Company).filter_by(run_id=rid).all();calls=db.query(ModelCall).filter_by(run_id=rid).all();events=db.query(Event).filter_by(run_id=rid).order_by(Event.created_at).all()
 totals=defaultdict(lambda:dict(calls=0,cost=0,input_tokens=0,output_tokens=0,searches=0))
 for c in calls:
  t=totals[c.role];t['calls']+=1;t['cost']+=c.cost
  for k in ['input_tokens','output_tokens','searches']:t[k]+=c.metrics.get(k,0)
 pages=[p for c in cs for v in c.research.values() for p in v.get('pages',[])]
 timings=defaultdict(list);starts={}
 for e in events:
  m=e.metrics or {};cid=m.get('call_id')
  if not cid:continue
  if e.message=='API-операция зарезервирована':starts[cid]=datetime.fromisoformat(e.created_at)
  elif e.message=='API-вызов завершён' and cid in starts:timings[e.stage].append((datetime.fromisoformat(e.created_at)-starts[cid]).total_seconds())
 rejected=[c.resource for c in cs if c.research.get('website',{}).get('state')=='IDENTITY_REJECTED']
 report={'status':r.status,'cost':r.spent,'companies_total':len(cs),'stages':dict(Counter(c.stage for c in cs)),'cost_by_role':dict(totals),'api_seconds_by_role':{k:{'count':len(v),'median':statistics.median(v),'total':sum(v)} for k,v in timings.items()},'pages':len(pages),'unique_urls':len(set(p['url'] for p in pages)),'states':dict(Counter(v.get('state') for c in cs for v in c.research.values()))}
 samples=[]
 for c in cs:
  for d,v in c.research.items():
   if v.get('pages') and d=='website':samples.append({'direction':d,'pages':v['pages'][:1]})
 Path('data/experiments/local_samples.json').write_text(json.dumps(samples[:3],ensure_ascii=False))
def check(url):
 t=time.monotonic()
 try:
  p=fetch(url);return {'success':True,'chars':len(p['text']),'seconds':time.monotonic()-t}
 except Exception as e:return {'success':False,'reason':str(e),'seconds':time.monotonic()-t}
t=time.monotonic()
with ThreadPoolExecutor(max_workers=3) as pool:report['direct_site_checks']=list(pool.map(check,rejected))
report['direct_check_wall_seconds']=time.monotonic()-t
Path('data/experiments/audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))
