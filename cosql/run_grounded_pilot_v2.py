"""Controlled diagnostic CoSQL pilot: real DB copies, bounded SQL tool, no filesystem tools.
Logical isolation only, not an OS security sandbox. Never exposes gold to the model.
"""
import argparse, asyncio, hashlib, json, re, shutil, sqlite3, tempfile, time
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from run_harness_baseline import ROOT, DATA, dialogue_samples, schema_text, evaluate, summarize, postprocess, write_text
from run_harness_dialogue_comparison import last_result, encode_sqlite_value
from harness_sdk import Harness, MODEL, PROVIDER, diagnostic
DIALOGUES=(215,85,91,39,98,252)
ARMS=('G0','G1','G2','G3')
MAX_TOOL_QUERIES=2
ROW_LIMIT=50
OUT=ROOT/'reports/2026-11-19_grounded_pilot_v2'

def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def dumps(x):return json.dumps(x,ensure_ascii=False,default=encode_sqlite_value)
def quote(s):return '"'+s.replace('"','""')+'"'

def safe_query(path,sql,limit=ROW_LIMIT):
 if len(sql)>20000:raise ValueError('Query too long')
 if not re.match(r'^\s*(SELECT|WITH)\b',sql,re.I):raise ValueError('Only SELECT/WITH allowed')
 allowed={sqlite3.SQLITE_READ,sqlite3.SQLITE_SELECT,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}
 with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
  c.execute('PRAGMA query_only=ON')
  def authorize(action,a,b,*_):
   if action==sqlite3.SQLITE_FUNCTION and str(b or a).lower() in {'load_extension','readfile','writefile','fts3_tokenizer'}:return sqlite3.SQLITE_DENY
   return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
  c.set_authorizer(authorize);deadline=time.monotonic()+5
  c.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
  cur=c.execute(postprocess(sql));rows=cur.fetchmany(limit+1)
  data={'columns':[x[0] for x in cur.description],'rows':rows[:limit],'truncated':len(rows)>limit,'row_limit':limit}
  while len(dumps(data))>12000 and data['rows']:
   data['rows']=data['rows'][:len(data['rows'])//2];data['truncated']=True
  return data

def metadata(path):
 with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
  ddl={x[0]:x[1] for x in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")}
  profile={}
  for table in ddl:
   columns=[x[1] for x in c.execute('PRAGMA table_info('+quote(table)+')')]
   profile[table]={}
   for column in columns:
    try:
     result=safe_query(path,f'SELECT DISTINCT {quote(column)} FROM {quote(table)} ORDER BY {quote(column)} LIMIT 9',9)
     vals=[x[0] for x in result['rows']]
     profile[table][column]={'sample_values':vals[:8],'more_distinct_values':len(vals)>8}
    except Exception as exc:profile[table][column]={'sample_error':diagnostic(exc)}
 return '\n\n'.join(x for x in ddl.values() if x),profile

def tool_contract(remaining):
 return ('\n\n受控数据库工具协议：这是实际SQLite副本，不是模拟表。你不能读取本地文件或参考答案。'
         '需要探索时仅返回JSON {"action":"query","sql":"SELECT ..."}；'
         '最终答案仅返回JSON {"action":"final","sql":"SELECT ..."}。'
         f'本轮最多还可查询{remaining}次。查询只读、限时5秒，最多展示50行；截断不等于完整结果。'
         '无论是否查询，最终必须输出完整SQL，不能仅输出结果。')

def parse_reply(text):
 text=text.strip()
 if text.startswith('```'):text=re.sub(r'^```(?:json|sql|sqlite)?\s*','',text,flags=re.I);text=re.sub(r'\s*```$','',text)
 if re.match(r'^(SELECT|WITH)\b',text,re.I):return {'action':'final','sql':text}
 value=json.loads(text)
 if not isinstance(value,dict) or value.get('action') not in {'query','final'} or not isinstance(value.get('sql'),str):raise ValueError('Invalid action JSON')
 return value

def self_test():
 OUT.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory(prefix='isolation-test-',dir=OUT) as tmp:
  a=Path(tmp)/'a.sqlite';b=Path(tmp)/'b.sqlite';source=DATA/'database/concert_singer/concert_singer.sqlite'
  shutil.copy2(source,a);shutil.copy2(source,b);before=digest(source);denied=[]
  for sql in ['DELETE FROM singer',"ATTACH DATABASE 'other.sqlite' AS x",'PRAGMA writable_schema=ON',"SELECT load_extension('x')",'SELECT 1; DELETE FROM singer']:
   try:safe_query(a,sql);denied.append(False)
   except Exception:denied.append(True)
  assert all(denied)
  assert digest(a)==digest(b)==before==digest(source)
  safe_query(a,'SELECT COUNT(*) FROM singer')
  result={'write_attach_pragma_extension_multistatement_denied':denied,'source_and_copies_unchanged':True,'boundary':'logical SQL capability isolation, not OS sandbox'}
  write_text(OUT/'isolation_test.json',dumps(result)+'\n');return result

async def run(args):
 OUT.mkdir(parents=True,exist_ok=True);self_test()
 dev=json.loads((DATA/'sql_state_tracking/cosql_dev.json').read_bytes());schemas={x['db_id']:schema_text(x) for x in json.loads((DATA/'tables.json').read_bytes())}
 plan={'dialogues':list(DIALOGUES),'turns_per_arm':sum(len(dev[i]['interaction']) for i in DIALOGUES),'arms':{'G0':'Original schema serialization, continuous, previous executed result','G1':'G0 plus original SQLite DDL','G2':'G1 plus fixed per-column distinct-value samples (up to8)','G3':'G1 plus model-requested bounded read-only SQL tool; no initial value profile'},'selection':'Purposive diagnosis cohort from reviewed original300; NOT random or held-out','tool_budget_per_question':MAX_TOOL_QUERIES,'tool_row_limit':ROW_LIMIT,'model':MODEL,'provider':PROVIDER,'temperature_seed':'Existing SDK defaults, not explicitly controlled','native_filesystem_and_shell_tools':'disabled for all arms','reference_visibility':'Never sent; scorer lives outside dialogue copy','dev_hash':digest(DATA/'sql_state_tracking/cosql_dev.json'),'db_hashes':{dev[i]['database_id']:digest(DATA/f"database/{dev[i]['database_id']}/{dev[i]['database_id']}.sqlite") for i in DIALOGUES}}
 path=OUT/'plan.json'
 if path.exists() and json.loads(path.read_bytes())!=plan:raise ValueError('Plan mismatch')
 if not path.exists():write_text(path,json.dumps(plan,ensure_ascii=False,indent=2))
 if args.stage=='plan':print(dumps(plan));return
 logfile=OUT/'turns.jsonl';records=[json.loads(l) for l in logfile.read_text(encoding='utf-8').splitlines()] if logfile.exists() else []
 done=set()
 for arm in ARMS:
  for idx in DIALOGUES:
   entries=[x for x in records if x['arm']==arm and x['dev_index']==idx]
   if len(entries)==len(dev[idx]['interaction']) and all('grade' in x for x in entries):done.add((arm,idx))
 if args.stage=='run':
  h=Path.home()/'.dsh-upgrade-stage-20261020';gate=asyncio.Semaphore(4);fatal=[]
  with tempfile.TemporaryDirectory(prefix='.runtime-',dir=OUT) as runtime:
   async with Harness(h/'runtime/node_modules/@deepseek-ai/dsh/lib/bin.js',h/'home/.credentials.yaml',Path(runtime)) as model:
    async def play(arm,idx):
     async with gate:
      if fatal:return
      db=dev[idx]['database_id'];source=DATA/f'database/{db}/{db}.sqlite';session=model.session_id();previous=None;model_round=0
      with tempfile.TemporaryDirectory(prefix=f'{arm}-dev{idx}-',dir=OUT) as work:
       copy=Path(work)/'dataset.sqlite';shutil.copy2(source,copy);sha=digest(copy);assert sha==plan['db_hashes'][db]
       ddl,values=metadata(copy) if arm!='G0' else ('',{})
       samples=list(dialogue_samples(idx,dev[idx],schemas[db],'diagnostic_pilot'))
       for sample,reference in samples:
        prompt=sample['prompt'] if previous is None else '上一轮 SQL 的实际执行结果\n'+dumps(last_result(previous))+'\n\n本轮用户问题\n'+sample['question']+'\n\n请生成一条只读 SQLite SQL，回答本轮问题。只输出 SQL。'
        if previous is None and arm!='G0':prompt=prompt.replace('\n\n用户问题\n','\n\n从真实SQLite提取的建表DDL（不包含表中数据）\n'+ddl+'\n\n用户问题\n')
        if previous is None and arm=='G2':prompt=prompt.replace('\n\n用户问题\n','\n\n固定列值样本（排序后的前8个不同值，不是完整数据）\n'+dumps(values)+'\n\n用户问题\n')
        if arm=='G3':prompt=prompt.replace('只输出 SQL。','按下面的工具协议输出。')+tool_contract(MAX_TOOL_QUERIES)
        entry={'arm':arm,'id':sample['id'],'dev_index':idx,'turn':sample['turn'],'database':db,'session_id':session,'db_sha256':sha,'initial_prompt':prompt,'started_at':datetime.now(timezone.utc).isoformat(),'rounds':[],'sql_tool_calls':0}
        start=time.monotonic();answer=None
        try:
         for attempt in range(1 if arm!='G3' else MAX_TOOL_QUERIES+1):
          reply=await model.predict(prompt,session);model_round+=1
          assert reply['harness_turn']==model_round
          assert (reply['source']['provider'],reply['source']['model'])==(PROVIDER,MODEL)
          event={'prompt':prompt,'response':reply};entry['rounds'].append(event)
          if arm!='G3':answer=reply;break
          action=parse_reply(reply['raw_reply']);event['action']=action
          if action['action']=='final':answer={**reply,'raw_reply':action['sql']};break
          if entry['sql_tool_calls']>=MAX_TOOL_QUERIES:break
          entry['sql_tool_calls']+=1
          try:result={'status':'ok',**safe_query(copy,action['sql'])}
          except Exception as exc:result={'status':'error','detail':diagnostic(exc)}
          event['tool_result']=result;prompt='数据库工具的真实执行反馈\n'+dumps(result)+tool_contract(MAX_TOOL_QUERIES-entry['sql_tool_calls'])
         if answer is None:answer={**entry['rounds'][-1]['response'],'raw_reply':'NO_FINAL_SQL'}
         grade=evaluate(sample,reference,answer,include_results=True);previous=grade;entry['grade']=grade
         assert digest(copy)==sha and digest(source)==sha
        except Exception as exc:
         entry['error']=diagnostic(exc);fatal.append(entry['error'])
        entry['elapsed_seconds']=round(time.monotonic()-start,3);records.append(entry)
        write_text(logfile,''.join(dumps(x)+'\n' for x in records))
        print(arm,sample['id'],entry.get('grade',{}).get('reason',entry.get('error')),entry['sql_tool_calls'],flush=True)
        if 'error' in entry:break
    # Rotate arm order across dialogues to reduce gross ordering bias.
    jobs=[(ARMS[(j+k)%len(ARMS)],idx) for j,idx in enumerate(DIALOGUES) for k in range(len(ARMS)) if (ARMS[(j+k)%len(ARMS)],idx) not in done]
    await asyncio.gather(*(play(arm,idx) for arm,idx in jobs))
 summary={}
 for arm in ARMS:
  entries=[x for x in records if x['arm']==arm and 'grade' in x]
  summary[arm]={'scores':summarize([x['grade'] for x in entries]),'sql_tool_calls':sum(x['sql_tool_calls'] for x in entries),'model_calls':sum(len(x['rounds']) for x in entries),'reported_tokens':sum(y['response']['usage'].get('totalTokens',0) for x in entries for y in x['rounds'])}
 result={'complete':all(v['scores']['scored_turns']==plan['turns_per_arm'] for v in summary.values()),'arms':summary,'interpretation':'Small purposively selected diagnostic cohort; G3 adds inference budget, not isolated pure tool effect. No corrected gold.'}
 write_text(OUT/'summary.json',json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))
def main():
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['plan','run','score','self-test']);a=p.parse_args()
 if a.stage=='self-test':print(dumps(self_test()))
 else:asyncio.run(run(a))
if __name__=='__main__':main()
