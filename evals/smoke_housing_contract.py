import json,time
from pathlib import Path
from relocation.config import Settings
from relocation.providers import Nebius
c=next(x for x in json.loads(Path('evals/golden_dataset.json').read_text(encoding='utf-8'))['cases'] if x['id']=='housing_grounded')
s=Settings(); start=time.perf_counter(); out=None; used='none'; failures=[]
for model in dict.fromkeys([s.research_model,s.nebius_model]):
    try:
        out=Nebius(s,model).json('housing',{'profile':c['profile'],'evidence':c['evidence'],'instructions':'Extract exactly two listings and no more than four concise steps. Keep JSON under 7000 characters.'},{x['id'] for x in c['evidence']},max_tokens=1800)
        used=model.split('/')[-1]; break
    except Exception as exc:
        failures.append(type(exc).__name__)
print('PASS' if out and len(out.get('candidates',[]))>=2 else 'FAIL','candidates',len((out or {}).get('candidates',[])),'steps',len((out or {}).get('steps',[])),'model',used,'seconds',round(time.perf_counter()-start,1),'failed_attempts',failures,flush=True)
raise SystemExit(0 if out and len(out.get('candidates',[]))>=2 else 1)
