"""Append complete CoSQL dialogues without changing the original 300-turn run."""
import argparse, asyncio, hashlib, json
from pathlib import Path
from types import SimpleNamespace
from run_harness_baseline import ROOT, DATA, dialogue_samples, schema_text, sampled_dialogue_order, read_jsonl, summarize, write_text, manifest_text
from run_harness_dialogue_comparison import run_dialogues, completed_dialogues, encode_sqlite_value
from harness_sdk import MODEL, PROVIDER

def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['plan','run','score']);p.add_argument('--target',type=int,default=500);p.add_argument('--workers',type=int,default=6);a=p.parse_args()
 old=ROOT/'reports/2026-11-19_gpt6sol_continuous_300'
 out=ROOT/'reports/2026-11-19_continuous_expanded_v2';out.mkdir(parents=True,exist_ok=True)
 dev=json.loads((DATA/'sql_state_tracking/cosql_dev.json').read_bytes()); tables={x['db_id']:schema_text(x) for x in json.loads((DATA/'tables.json').read_bytes())}
 source_plan=json.loads((old/'plan.json').read_bytes()); indices=[x['dev_index'] for x in source_plan['dialogues']];initial=list(indices)
 total=sum(len(dev[i]['interaction']) for i in indices)
 for i in sampled_dialogue_order(dev):
  if total>=a.target:break
  if i not in indices:indices.append(i);total+=len(dev[i]['interaction'])
 samples={};refs={}
 for pos,i in enumerate(indices):
  samples[i]=[]
  for sample,reference in dialogue_samples(i,dev[i],tables[dev[i]['database_id']], 'pilot' if pos<10 else 'extension'):
   samples[i].append(sample);refs[sample['id']]=reference
 plan={'target_at_least':a.target,'actual_planned_turns':total,'dialogue_indices':indices,'inherited_indices':initial,'inherited_turns':300,'new_planned_turns':total-300,'source_sha256':digest(old/'turns.jsonl'),'dev_sha256':digest(DATA/'sql_state_tracking/cosql_dev.json'),'provider':PROVIDER,'model':MODEL,'workers':a.workers,'method':'UNCHANGED continuous prompt/schema/previous actual execution feedback, max100 rows; whole dialogues, exclude dev0 and dev106','notes':'Extension at later time; model route unchanged, backend version unverified. Single run, not causal evidence.'}
 path=out/'plan.json'
 if path.exists() and json.loads(path.read_bytes())!=plan:raise ValueError('Existing plan differs')
 if not path.exists():write_text(path,manifest_text(plan))
 log=out/'turns.jsonl'
 if not log.exists():log.write_bytes((old/'turns.jsonl').read_bytes())
 records=read_jsonl(log);done=completed_dialogues(records,samples)
 if a.stage=='run':
  h=Path.home()/'.dsh-upgrade-stage-20261020';args=SimpleNamespace(workers=a.workers,cli=h/'runtime/node_modules/@deepseek-ai/dsh/lib/bin.js',credentials=h/'home/.credentials.yaml')
  assert args.cli.is_file() and args.credentials.is_file()
  pending=[i for i in indices if i not in done]
  asyncio.run(run_dialogues(args,out,records,samples,refs,pending,error_limit=3))
  done=completed_dialogues(records,samples)
 grades=[x['grade'] for i in indices for x in done.get(i,[])]
 summary={'complete':len(done)==len(indices),'combined':summarize(grades),'added_only':summarize([x for x in grades if x['dev_index'] not in initial]),'inherited_only':summarize([x for x in grades if x['dev_index'] in initial]),'new_reported_tokens':sum(x.get('usage',{}).get('totalTokens',0) for i in indices if i not in initial for x in done.get(i,[])),'pending_dialogues':[i for i in indices if i not in done]}
 write_text(out/'summary.json',manifest_text(summary));print(manifest_text({'plan':plan,'summary':summary}))
if __name__=='__main__':main()
