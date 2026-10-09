"""Isolated local extraction benchmark. Never calls the production gateway."""
import json,time,urllib.request
from pathlib import Path
from app.schemas import ResearchReport
from app.prompts import EXTRACT
from app.pipeline import verify_report
samples=json.loads(Path('data/experiments/local_samples.json').read_text())
results=[]
for i,sample in enumerate(samples):
    payload={**sample,'date':'2026-10-09','freshness_days':30,'offers':[],'source_filters':''}
    for p in payload['pages']:p['text']=p['text'][:7000]
    req={'model':'qwen3:4b','stream':False,'format':ResearchReport.model_json_schema(),'messages':[{'role':'system','content':EXTRACT+' Ответ краткий, максимум 3 evidence и 3 signals. /no_think'},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}],'options':{'temperature':0,'num_ctx':8192,'num_predict':1500},'keep_alive':'2m'}
    start=time.monotonic()
    try:
        with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:11435/api/chat',data=json.dumps(req).encode(),headers={'Content-Type':'application/json'}),timeout=180) as response:r=json.load(response)
        report=ResearchReport.model_validate_json(r['message']['content']);checked=verify_report(report,payload['pages'],sample['direction'],30)
        result={'sample':i,'seconds':time.monotonic()-start,'valid_schema':True,'evidence_returned':len(report.evidence),'evidence_exact_quotes':len(checked['evidence']),'signals':len(checked['signals']),'output_tokens':r.get('eval_count'),'tokens_per_second':r.get('eval_count',0)/max(r.get('eval_duration',1)/1e9,.001),'load_seconds':r.get('load_duration',0)/1e9,'response':report.model_dump(),'input_chars':sum(len(p['text']) for p in payload['pages'])}
    except Exception as e:result={'sample':i,'seconds':time.monotonic()-start,'valid_schema':False,'error':str(e)}
    results.append(result);Path('data/experiments/local_benchmark.json').write_text(json.dumps(results,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='response'},ensure_ascii=False),flush=True)
