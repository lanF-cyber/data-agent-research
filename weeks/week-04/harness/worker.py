"""Run one public CoSQL case from a validated RSI canvas. Python standard library only."""
import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import core


HERE = Path(__file__).resolve().parent
CASE = json.loads((HERE / "case.json").read_bytes())
GRAPH = json.loads((HERE / "workflow.json").read_bytes())
FLOW_ID = "cosql-harness"
MODEL = "gpt-6-sol"
MODEL_URL = "https://intlapi.thingo.com.cn/v1/chat/completions"
REFERENCE = """SELECT DISTINCT trim(f.SourceAirport) FROM flights AS f
WHERE f.Airline IN (SELECT uid FROM airlines WHERE trim(Airline) = 'United Airlines')"""
LOCK = threading.Lock()
ACTIVE = {}
RUNS = None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def model_decision(messages, choices):
    token = os.environ.get("RSI_HARNESS_TOKEN")
    if not token:
        raise ValueError("当前服务未配置现场生成，可选择记录动作复跑")
    tools = [{"type": "function", "function": {k: v for k, v in spec.items() if k != "type"}} for spec in choices]
    body = {"model": MODEL, "messages": messages, "tools": tools, "tool_choice": "required", "parallel_tool_calls": False}
    req = Request(MODEL_URL, data=core.compact(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token,
    })
    try:
        with build_opener(NoRedirect()).open(req, timeout=65) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"模型请求返回 HTTP {exc.code}") from None
    except (URLError, TimeoutError):
        raise RuntimeError("模型服务连接超时或不可达") from None
    calls = result["choices"][0]["message"].get("tool_calls", [])
    if len(calls) != 1:
        raise ValueError("模型没有返回唯一的工具调用")
    call = calls[0]
    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
    return call["function"]["name"], json.loads(call["function"]["arguments"]), call["id"]


class Session:
    def __init__(self, state, arm, mode, profile):
        self.db = state
        self.arm = arm
        self.mode = mode
        self.index = 0
        self.messages = [{"role": "system", "content": core.WORK + "\n\nSQLite flight_2\n" +
                          core.compact(profile["tables"]) + "\n巡检交接\n" + CASE["catalog_notes"]}]

    async def choose(self, slot, cid, task, inputs, tools):
        choices = [core.DELIVER if item["name"] == "submit_answer" else item for item in tools
                   if self.arm == "on" or item["name"] != "lookup_value"]
        if self.mode == "live":
            for item in inputs:
                if item.get("type") == "function_call_output":
                    self.messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
                else:
                    self.messages.append(item)
            name, args, call_id = await asyncio.to_thread(model_decision, self.messages, choices)
        else:
            recorded = CASE["recorded"][self.arm][self.index]
            name, args, call_id = recorded["tool"], recorded["args"], str(self.index + 1)
        if name not in {choice["name"] for choice in choices}:
            raise ValueError("动作超出该画布及剩余试查次数")
        self.index += 1
        return name, args, call_id, self.index

    async def deliver(self, slot, cid, call_id, receipt):
        if self.mode == "live":
            self.messages.append({"role": "tool", "tool_call_id": call_id, "content": core.compact(receipt)})


def graph_arm(canvas):
    nodes = [item["id"] for item in canvas["elements"]]
    links = [(item["fromElementId"], item["toElementId"]) for item in canvas["links"]]
    for arm in ("on", "off"):
        expected = {item["id"] for item in GRAPH["nodes"] if arm == "on" or item["id"] != "lookup_value"}
        edges = {(u, v) for u, v in GRAPH["edges"] if u in expected and v in expected}
        if len(nodes) == len(expected) and set(nodes) == expected and len(links) == len(edges) and set(links) == edges:
            return arm
    raise ValueError("画布与已登记的节点、连线组合不符")


def run(arm, mode):
    started = time.monotonic()
    profile = core.survey("flight_2")
    with closing(sqlite3.connect(":memory:")) as state:
        state.execute("""CREATE TABLE facts(db,table_name,column_name,term,value,evidence,learned_at,
                      PRIMARY KEY(db,table_name,column_name,term))""")
        session = Session(state, arm, mode, profile)
        msg = asyncio.run(core.answer(session, "flight_2", profile, 0, CASE["id"], CASE["question"], CASE["id"]))
        core.learn(state, "flight_2", msg, CASE["id"])
        facts = state.execute("SELECT count(*) FROM facts").fetchone()[0]
    steps = []
    for action in msg["trace"]:
        receipt, args = action["receipt"], action["args"]
        step = {"tool": action["tool"], "args": args}
        step["receipt"] = {k: receipt[k] for k in ("accepted","row_count","verified","source","field","values","has_more","error") if k in receipt}
        if "first_rows" in receipt:
            step["receipt"]["sample"] = receipt["first_rows"][:3]
        steps.append(step)
    final = msg["trace"][-1]
    receipt = final["receipt"]
    cols, rows = receipt.get("columns", []), receipt.get("rows", [])
    with core.query("flight_2", REFERENCE) as cursor:
        expected = {row[0] for row in cursor}
    # A live query may reorder its columns. Require one column to match the full distinct set.
    matched = len(rows) == len(expected) and any(
        {str(row[i]).strip() for row in rows} == expected for i in range(len(cols))
    )
    return {"case": CASE["id"], "database": "flight_2", "mode": mode, "lookup": arm,
            "state": "complete", "survey_tables": len(profile["tables"]), "check_limit": 3,
            "checks_used": sum(e["tool"] in ("lookup_value", "check_context") for e in steps),
            "actions": steps, "model_calls": session.index if mode == "live" else 0,
            "rows": len(rows), "reference_distinct_airports": len(expected), "set_match": matched,
            "memory_writes": facts, "elapsed_seconds": round(time.monotonic() - started, 3)}


def finish(ticket, arm, mode):
    try:
        receipt = run(arm, mode)
    except Exception as exc:
        receipt = {"state": "failed", "error": str(exc)[:180], "mode": mode, "lookup": arm}
    receipt["ticket"] = ticket
    path = RUNS / (ticket + ".json")
    tmp = path.with_suffix(".part")
    with LOCK:
        try:
            tmp.write_text(core.compact(receipt), encoding="utf-8")
            tmp.replace(path)
        finally:
            ACTIVE.pop(ticket, None)


def dispatch(canvas):
    ticket = canvas.get("ticket", "")
    if canvas.get("id") != FLOW_ID or not isinstance(ticket, str) or not re.fullmatch(r"[0-9a-f]{32}", ticket):
        raise ValueError("画布或运行编号无效")
    op = canvas.get("operation")
    if op not in ("start", "status"):
        raise ValueError("未知调度操作")
    arm, mode = graph_arm(canvas), canvas.get("mode")
    if mode not in ("live", "replay"):
        raise ValueError("请选择运行方式")
    with LOCK:
        path = RUNS / (ticket + ".json")
        if path.is_file():
            receipt = json.loads(path.read_bytes())
        elif ticket in ACTIVE:
            receipt = ACTIVE[ticket]
        elif op == "status":
            raise ValueError("找不到这次运行")
        else:
            if len(ACTIVE) >= 2:
                raise ValueError("已有两个任务运行中")
            receipt = {"state": "running", "mode": mode, "lookup": arm, "ticket": ticket}
            ACTIVE[ticket] = receipt
            threading.Thread(target=finish, args=(ticket, arm, mode), daemon=True).start()
        if receipt["mode"] != mode or receipt["lookup"] != arm:
            raise ValueError("运行编号和调度条件不相符")
        return receipt


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/dispatch":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 65536:
                raise ValueError("无效请求长度")
            response = dispatch(json.loads(self.rfile.read(size)))
        except (ValueError, KeyError, TypeError):
            self.send_error(400, "Invalid dispatch")
            return
        payload = core.compact(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    args = parser.parse_args()
    core.DATABASE = args.db.resolve(strict=True)
    if hashlib.sha256(core.DATABASE.read_bytes()).hexdigest() != core.DATABASE_SHA256:
        parser.error("flight_2.sqlite SHA-256 mismatch")
    RUNS = args.runs.resolve()
    RUNS.mkdir(parents=True, exist_ok=True)
    print("CoSQL harness ready on 127.0.0.1:3216", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 3216), Handler).serve_forever()
