"""Run the archived clarification pipeline or the existing v3 value graph through RSI."""
import asyncio
import json
import re
import subprocess
import sys
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import httpx

from clarification import adaptive, candidate_pilot, data, experiment, retest_revisions
from clarification.formal_compare import csvrows, readonly
from clarification.original_score import grade_delivery
from clarification.parallel_trial import graph
from week4 import transport
from week4.protocol import MODEL, ROOT, database_path, digest, read, save, schemas
from week4.transport import fingerprint

OUT = ROOT / "reports/2026-11-26_历代Harness合并"
RUNS = OUT / "平台实跑"
OLD = ROOT / "reports/2026-11-21_cosql_clarification_policy/prompt-contrast.json"
STATUS = ROOT / "reports/formal/912b51723212/原库状态/逐题状态.csv"
SLOT_POOL = ThreadPoolExecutor(max_workers=4)
LOCK = threading.Lock()
ACTIVE = {}


def view(arm):
    if arm == "contrast":
        # The old runner is one existing RSI action. Its own SQL and clarification tools run inside it.
        return [{"id": "contrast"}], []
    if arm == "v3":
        saved = graph(True)
        return ([{"id": n["id"]} for n in saved["nodes"]],
                [{"fromElementId": a, "toElementId": b} for a,b in saved["edges"]])
    raise ValueError("Unknown archived architecture")


class Legacy(experiment.Client):
    def __init__(self, state, http, ticket):
        super().__init__(state, http, 1)
        self.ticket = ticket

    async def request(self, slot, method, path, body=None, *, identity="", model_meta=None):
        return await super().request(slot, method, path, body, identity=self.ticket + "/" + identity,
                                     model_meta=model_meta)


async def generate(directory, canvas):
    idx, last, arm = canvas["dialogue"], canvas["last"], canvas["arm"]
    bank = data.load_dataset()[idx]
    pos = next(i for i,t in enumerate(bank) if t["id"] == f"dev{idx}_turn{last}")
    turns, schema = bank[:pos+1], schemas()
    db = turns[0]["db"]
    transport.LEDGER = directory / "model-usage.sqlite3"
    legacy = arm == "contrast"
    # This module runs in a process dedicated to one ticket. The old executor uses module paths for its store.
    if legacy:
        experiment.OUT, experiment.DB = directory, directory / "state.sqlite3"
        frozen = read(OLD)
        if (frozen["model"] != MODEL or frozen["prompt"] != experiment.PROMPTS["contrast"] or
            frozen["tools"] != [experiment.SUBMIT,experiment.CHECK,experiment.ASK,experiment.CLOSE]):
            raise ValueError("Frozen baseline does not match current executable")
        conn = experiment.connect()
    else:
        conn = adaptive.connect(directory)
    inputs = {"model": MODEL, "database": db, "db_sha256": digest(database_path(db)), "arm": arm,
              "script": [[t["id"], t["first"], t["followup"], t["replay"], t["later"]] for t in turns],
              "elements": canvas["elements"], "links": canvas["links"]}
    with closing(conn) as state:
        async with httpx.AsyncClient(timeout=httpx.Timeout(150, connect=25), follow_redirects=False) as http:
            if legacy:
                client = Legacy(state,http,canvas["ticket"])
                slot = client.credential_aliases[idx % len(client.credential_aliases)]
                inputs["opening"] = experiment.opening("contrast",db,schema[db])
                data.freeze(directory / "input.json",inputs)
                async with experiment.create_connected_server_and_client_session(experiment.server({db})) as mcp:
                    await experiment.dialogue(client,mcp,state,"contrast",idx,turns,slot,schema[db])
                records = [json.loads(r) for r, in state.execute("SELECT result FROM units ORDER BY turn")]
            else:
                with readonly(retest_revisions.BASELINE / "state.sqlite3") as baseline:
                    catalog = baseline.execute("SELECT * FROM catalogs WHERE db=?",(db,)).fetchone()
                state.execute("INSERT INTO catalogs VALUES(?,?,?)",catalog)
                keys = candidate_pilot.identifier_notes(db,json.loads(catalog[1])["tables"])
                rules = {(idx, t["first"]): candidate_pilot.REPLIES[t["id"]]
                         for t in turns if t["followup"] == "reply"}
                client = candidate_pilot.Client(state,http,directory,rules,{db:keys})
                slot = client.credential_aliases[idx % len(client.credential_aliases)]
                inputs["opening"] = candidate_pilot.WORK+"\n\nSQLite 数据库 "+db+"\n"+schema[db]+"\n\n巡检交接\n"+catalog[2]+"\n\n"+keys
                inputs["initial_facts"] = state.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                assert inputs["initial_facts"] == 0
                data.freeze(directory / "input.json",inputs)
                await adaptive.play(client,db,0,slot,[(idx,turns)],schema[db])
                records = [json.loads(r) for r, in state.execute("SELECT record FROM turns ORDER BY rowid")]
        assert [r["id"] for r in records] == [t["id"] for t in turns]
        current = {r["题号"]:r for r in csvrows(STATUS)}
        result = []
        for r in records:
            msg = r["follow"] or r["first"]
            event = msg["trace"][-1]
            rc = event["receipt"]
            if rc.get("accepted") and "handle" in rc:
                row = state.execute("SELECT data FROM result_tables WHERE handle=?",(rc["handle"],)).fetchone()
                rc["rows"] = json.loads(zlib.decompress(row[0]))
                assert rc["handle"] == fingerprint(rc["rows"])
            if r["id"] not in current:
                continue
            scores = [grade_delivery({"db":db,"final_reference":q},event)
                      for q in json.loads(current[r["id"]]["参考SQL"])]
            assert event["tool"] != "submit_answer" or all(v["原库复跑与回执一致"] for _,v in scores)
            checks = sum(a["tool"] in ("lookup_value","check_context") for a in msg["trace"])
            result.append({"id":r["id"],"user":r["first"]["user"],"follow_user":r["follow"]["user"] if r["follow"] else None,
                           "action":event["tool"],"sql":event["args"].get("sql",""),"set_match":any(p for p,_ in scores),
                           "columns":rc.get("columns",[]),"rows":rc.get("row_count",0),"preview":rc.get("first_rows",[])[:3],
                           "table_sha256":fingerprint(rc.get("rows")),"checks_used":checks,"call_id":event["call_id"]})
        calls = state.execute("SELECT count(*) FROM api WHERE json_extract(record,'$.status')='completed'").fetchone()[0]
        if not calls:
            raise ValueError("Missing live model receipts")
        save(directory/"deliveries.json",records)
        return {"database":db,"state":"complete","case":result[-1]["id"],"model":MODEL,"model_calls":calls,
                "checks_used":result[-1]["checks_used"],"rows":result[-1]["rows"],"set_match":result[-1]["set_match"],
                # The registered Java receipt requires the platform ceiling (3). The old runner itself permits 2.
                "check_limit":3,"turns":result}


def finish(canvas, directory, base):
    try:
        with (directory/"run.log").open("wb") as log:
            proc = subprocess.run([sys.executable,"-B","-X","utf8","-m","week4.version_pair",canvas["ticket"]],
                                  cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                  creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if proc.returncode:
            raise RuntimeError(f"Runner exited {proc.returncode}. See run.log")
        receipt = {**base, **read(directory/"answer.json")}
    except Exception as exc:
        receipt = {**base,"state":"failed","error":str(exc)}
    with LOCK:
        try:
            save(directory/"receipt.tmp",receipt)
            (directory/"receipt.tmp").replace(directory/"receipt.json")
        finally:
            ACTIVE.pop(canvas["ticket"],None)


def dispatch(canvas):
    ticket = canvas.get("ticket","")
    arm = canvas["arm"]
    if (canvas.get("id") != "cosql-harness" or canvas.get("mode") != "live" or
        not re.fullmatch(r"[0-9a-f]{32}",ticket) or canvas["operation"] not in ("start","status") or
        type(canvas["dialogue"]) is not int or type(canvas["last"]) is not int):
        raise ValueError("Invalid version comparison")
    if canvas.get("operation") not in ("start", "status"):
        raise ValueError("Unknown version comparison operation")
    if (canvas["elements"],canvas["links"]) != view(arm):
        raise ValueError("Canvas differs from the recorded workflow")
    if (str(canvas["dialogue"]),canvas["last"]) not in read(OUT/"平台计划.json")["prefixes"].items():
        raise ValueError("Dialogue prefix is not in the frozen comparison")
    start = {**canvas,"operation":"start"}
    key = fingerprint({k:start[k] for k in sorted(start)})
    base = {"ticket":ticket,"mode":"live","lookup":"on" if arm=="v3" else "off","arm":arm,"structure_sha256":key}
    directory = RUNS/ticket
    with LOCK:
        if (directory/"receipt.json").is_file():
            receipt = read(directory/"receipt.json")
        elif ticket in ACTIVE:
            receipt = ACTIVE[ticket]
        elif canvas["operation"] == "status" or directory.exists():
            raise ValueError("Unknown or interrupted ticket")
        else:
            directory.mkdir(parents=True)
            save(directory/"canvas.json",start)
            receipt = {**base,"state":"running"}
            ACTIVE[ticket] = receipt
            SLOT_POOL.submit(finish,start,directory,base)
        if key != receipt["structure_sha256"]:
            raise ValueError("Ticket belongs to another graph")
        return receipt


if __name__ == "__main__":
    if len(sys.argv)!=2 or not re.fullmatch(r"[0-9a-f]{32}",sys.argv[1]):
        raise SystemExit("Expected an existing ticket")
    path = RUNS/sys.argv[1]
    save(path/"answer.json",asyncio.run(generate(path,read(path/"canvas.json"))))
