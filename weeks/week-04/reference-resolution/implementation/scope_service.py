"""Run a scope canvas through the existing v3 agent and grade after delivery."""
import asyncio
import json
import re
import threading
from contextlib import closing

import httpx

from clarification import adaptive, candidate_pilot, data, retest_revisions
from clarification.formal_compare import csvrows, readonly
from clarification.original_score import grade_delivery
from clarification.scope_harness import GUIDE, OUTPUT_GUIDE, history_inputs, output_phrase
from week4 import transport
from week4.protocol import MODEL, ROOT, database_path, digest, read, save, schemas
from week4.transport import compact, fingerprint


OUT = ROOT / "reports/2026-11-26_范围承接实验"
RUNS = OUT / "画布运行"
ARCHIVES = (OUT / "候选基线/state.sqlite3",
            OUT / "隐式切换候选/state.sqlite3",
            ROOT / "reports/2026-11-26_指代消解任务盘点/抽测/state.sqlite3")
STATUS = ROOT / "reports/formal/912b51723212/原库状态/逐题状态.csv"
VARIANTS = {
    ("dev86_turn5", "among"): "Which of those countries are in North America?",
    ("dev86_turn6", "shown"): "What are the official languages of just the three countries you listed?",
}
LOCK = threading.Lock()
ACTIVE = {}


def target(canvas):
    match = re.fullmatch(r"dev(\d+)_turn([1-9]\d*)", canvas["questionId"])
    if not match:
        raise ValueError("Unknown turn ID")
    index, n = map(int, match.groups())
    turns = data.load_dataset().get(index, [])
    turn = next((t for t in turns if t["id"] == canvas["questionId"]), None)
    if not turn:
        raise ValueError("Unknown original turn")
    case = canvas["case"]
    if case == "original":
        user = turn["first"]
    elif (turn["id"], case) in VARIANTS:
        user = VARIANTS[turn["id"], case]
    else:
        raise ValueError("Unknown contrast sentence")
    return index, n, turn["db"], user


def archive_prefix(index, n, database):
    wanted = [f"dev{index}_turn{i}" for i in range(1, n)]
    for path in ARCHIVES:
        if not path.is_file():
            continue
        with readonly(path) as conn:
            records = {qid: json.loads(raw) for qid, raw in conn.execute(
                "SELECT id,record FROM turns WHERE dialogue=?", (index,))}
        if all(q in records and records[q]["db"] == database for q in wanted):
            prefix = [records[q] for q in wanted]
            if any(r.get("follow") for r in prefix):
                raise ValueError("This prefix contains two user messages within a benchmark turn")
            return prefix
    raise ValueError("No complete recorded prefix for this dialogue")


def get_prefix(canvas, index, n, database):
    parent = canvas.get("parent", "")
    if not parent:
        return archive_prefix(index, n, database)
    if canvas["case"] == "shown":
        raise ValueError("The three-displayed contrast is registered against the frozen three-country reply")
    if not re.fullmatch(r"[0-9a-f]{32}", parent):
        raise ValueError("Invalid parent ticket")
    parent_dir = RUNS / parent
    receipt = parent_dir / "receipt.json"
    if not receipt.is_file() or read(receipt)["state"] != "complete":
        raise ValueError("Parent has no completed delivery")
    saved = read(parent_dir / "delivery.json")
    prior = saved["canvas"]
    if (prior["questionId"] != f"dev{index}_turn{n-1}" or prior["arm"] != canvas["arm"]
            or saved["record"]["db"] != database):
        raise ValueError("Parent must be the preceding turn in this dialogue and arm")
    return [*saved["prefix"], saved["record"]]


def visible(record):
    msg = record["first"]
    actions = [{"tool": step["tool"], "args": step["args"],
                "receipt": {k: v for k, v in step["receipt"].items() if k != "rows"}}
               for step in msg["trace"]]
    return {"user": msg["user"], "actions": actions,
            "delivered_rows": msg["trace"][-1]["receipt"].get("rows", [])}


def references(qid, case):
    # Called only after model delivery. Synthetic controls never enter formal CoSQL scores.
    if case == "original":
        row = next(r for r in csvrows(STATUS) if r["题号"] == qid)
        return json.loads(row["参考SQL"])
    if (qid, case) == ("dev86_turn5", "among"):
        return ["""SELECT c.Name FROM country c WHERE c.Continent='North America' AND c.Code IN
                   (SELECT CountryCode FROM countrylanguage WHERE Language='Chinese')"""]
    if (qid, case) == ("dev86_turn6", "shown"):
        return ["""SELECT l.Language FROM countrylanguage l JOIN country c ON c.Code=l.CountryCode
                   WHERE l.IsOfficial='T' AND c.Name IN ('Canada','Costa Rica','United States')"""]
    raise ValueError("Unknown scoring case")


async def execute(canvas, directory):
    index, n, database, user = target(canvas)
    prefix = get_prefix(canvas, index, n, database)
    history = {int(r["id"].split("_turn")[1]): visible(r) for r in prefix}
    selected, refs = history_inputs(canvas, user, history)
    with readonly(retest_revisions.BASELINE / "state.sqlite3") as baseline, closing(adaptive.connect(directory)) as state:
        profile_raw, notes = baseline.execute("SELECT survey,notes FROM catalogs WHERE db=?", (database,)).fetchone()
        profile = json.loads(profile_raw)
        keys = candidate_pilot.identifier_notes(database, profile["tables"])
        opening = (candidate_pilot.WORK + "\n\nSQLite 数据库 " + database + "\n" + schemas()[database]
                   + "\n\n巡检交接\n" + notes + "\n\n" + keys)
        if canvas["arm"] != "full":
            opening += "\n\n" + GUIDE
        output = output_phrase(canvas, user)
        if output:
            opening += "\n\n" + OUTPUT_GUIDE + "\n本轮所问的输出原文：" + compact(output)
        retest_revisions.seed_memory(state, baseline, database, index)
        for record in prefix:
            adaptive.learn(state, database, record["first"], record["id"])
        facts = state.execute("SELECT * FROM facts ORDER BY db,table_name,column_name,term").fetchall()
        # All arms use this same renderer. Previous tool output is conversation data.
        wire = [{"type": "message", "role": "assistant", "content": compact(item)} for item in selected]
        save(directory / "input.json", {"model": MODEL, "qid": canvas["questionId"], "user": user,
             "opening": opening, "history_items": wire, "sources": refs, "facts_sha256": fingerprint(facts),
             "database_sha256": digest(database_path(database))})
        async with httpx.AsyncClient(timeout=httpx.Timeout(150, connect=25), follow_redirects=False) as http:
            client = candidate_pilot.Client(state, http, directory, {}, {database: keys})
            slot = client.credential_aliases[0]
            cid = await adaptive.Client.open_session(client, "scope", index, slot, opening)
            for i in range(0, len(wire), 20):
                await client.request(slot, "POST", f"/conversations/{cid}/items", {"items": wire[i:i+20]},
                                     identity=f"prefix/{i}")
            msg = await adaptive.answer(client, database, profile, slot, cid, user, canvas["questionId"])
        usage = [json.loads(r[0]) for r in state.execute("SELECT record FROM api")]
    event = msg["trace"][-1]
    record = {"id": canvas["questionId"], "db": database, "first": msg, "follow": None}
    grades = []
    for sql in references(record["id"], canvas["case"]):
        ok, detail = grade_delivery({"db": database, "final_reference": sql}, event)
        if event["tool"] == "submit_answer" and not detail["原库复跑与回执一致"]:
            raise RuntimeError("Delivery does not match the rerun on the original database")
        grades.append({"match": ok, "reference_sql": sql, **detail})
    save(directory / "delivery.json", {"canvas": canvas, "prefix": prefix, "record": record,
                                       "grades": grades, "source_turns": refs})
    return {"state": "complete", "case": record["id"], "variant": canvas["case"], "database": database,
            "arm": canvas["arm"], "parent": canvas.get("parent", ""), "sources": refs, "output_span": output,
            "history_turns": [item["turn"] for item in selected], "question": user, "sql": event["args"].get("sql", ""),
            "columns": event["receipt"].get("columns", []), "rows": event["receipt"].get("row_count", 0),
            "first_rows": event["receipt"].get("first_rows", []), "note": event["args"].get("text", ""),
            "followup": event["args"].get("followup", ""), "set_match": any(g["match"] for g in grades),
            "check_limit": 3, "checks_used": sum(s["tool"] in ("check_context", "lookup_value") for s in msg["trace"]),
            "model_calls": len(usage)}


def finish(canvas, key, directory):
    try:
        result = asyncio.run(execute(canvas, directory))
    except Exception as exc:
        result = {"state": "failed", "error": f"{type(exc).__name__}: {str(exc)[:250]}"}
    receipt = {**result, "mode": "live", "lookup": "on", "ticket": canvas["ticket"], "structure_sha256": key}
    with LOCK:
        try:
            save(directory / "receipt.tmp", receipt)
            (directory / "receipt.tmp").replace(directory / "receipt.json")
        finally:
            ACTIVE.pop(canvas["ticket"], None)


def dispatch(canvas):
    ticket = canvas.get("ticket", "")
    if (canvas.get("id") != "cosql-harness" or canvas.get("mode") != "live"
            or canvas.get("operation") not in ("start", "status") or not re.fullmatch(r"[0-9a-f]{32}", ticket)):
        raise ValueError("Invalid scope run")
    start = {**canvas, "operation": "start"}
    view = start.pop("view", None)
    if view is not None and (canvas["operation"] != "status" or type(view) is not int or view < 0):
        raise ValueError("A table page requires a nonnegative offset on a status request")
    key = fingerprint({k: start[k] for k in sorted(start)})
    directory = RUNS / ticket
    with LOCK:
        result = directory / "receipt.json"
        if result.is_file():
            receipt = read(result)
        elif ticket in ACTIVE:
            receipt = ACTIVE[ticket]
        elif canvas["operation"] == "status":
            raise ValueError("Unknown or interrupted ticket")
        else:
            if len(ACTIVE) >= 2:
                raise ValueError("Two scope runs are already active")
            if directory.exists():
                raise ValueError("An interrupted run used this ticket. Start with a new ticket.")
            index, n, db, user = target(canvas)
            prefix = get_prefix(canvas, index, n, db)
            history_inputs(canvas, user, {int(r["id"].split("_turn")[1]): visible(r) for r in prefix})
            directory.mkdir(parents=True)
            save(directory / "canvas.json", start)
            transport.LEDGER = OUT / "model-usage.sqlite3"
            receipt = {"state": "running", "ticket": ticket, "mode": "live", "lookup": "on", "structure_sha256": key}
            ACTIVE[ticket] = receipt
            threading.Thread(target=finish, args=(start, key, directory), daemon=True).start()
        if receipt["structure_sha256"] != key:
            raise ValueError("Ticket belongs to a different canvas")
    if view is not None and receipt["state"] == "complete":
        record = read(directory / "delivery.json")["record"]["first"]
        result = record["trace"][-1]["receipt"]
        rows = result.get("rows", [])
        return {**receipt, "table_page": {"offset": view, "total": len(rows), "rows": rows[view:view + 100]}}
    return receipt
