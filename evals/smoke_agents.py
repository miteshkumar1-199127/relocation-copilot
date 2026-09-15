import json,time
from pathlib import Path
from relocation.config import Settings
from relocation.providers import Nebius
cases=json.loads(Path('evals/golden_dataset.json').read_text(encoding='utf-8'))['cases']
s=Settings(); failed=[]
for case in [c for c in cases if c['case_type']=='specialist']:
    role=case['role']; started=time.perf_counter(); out=None; used=None
    for model in dict.fromkeys([s.research_model,s.nebius_model]):
        try:
            out=Nebius(s,model).json(role,{'profile':case['profile'],'evidence':case['evidence'],'instructions':'Return concise practical sourced output.'},{x['id'] for x in case['evidence']},max_tokens={'housing':900,'finance':1100}.get(role,1000)); used=model.split('/')[-1]; break
        except Exception: pass
    useful=bool(out and (out.get('candidates') or out.get('options') or out.get('steps'))); elapsed=time.perf_counter()-started
    print(f'{role}: {"PASS" if useful else "FAIL"} {elapsed:.1f}s via {used or "none"}',flush=True)
    if not useful: failed.append(role)
print('RESULT:', 'PASS' if not failed else 'FAIL '+','.join(failed),flush=True)
raise SystemExit(1 if failed else 0)
