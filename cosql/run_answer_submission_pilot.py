"""Replay selected CoSQL turns with verified MCP answer submissions."""

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import stat
import sys
import time
from collections import Counter
from contextlib import aclosing
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from answer_submission_mcp import SERVER_NAME, TOOL_NAME, check_submission, project_rows
from answer_submission_eval101 import CONTRACTS, EXCLUSIONS, answer_fields, score_answer
from harness_sdk import MODEL, PROVIDER, Harness, diagnostic
from run_autonomous_db_pilot import (
    DEV, FOLLOWUP_101_OUT, FOLLOWUP_RULES, LIMIT, digest, prompt_agent,
    runtime_environment, terminal_patch, write_records,
)
from run_harness_baseline import DATA, ROOT, evaluate, manifest_text, read_jsonl, result_eq, write_text


OUT = ROOT / "reports" / "2026-11-20_gpt6sol_answer_mcp"
OUT_101 = ROOT / "reports" / "2026-11-20_gpt6sol_answer_mcp_101"
SERVER = ROOT / "answer_submission_mcp.py"
RUBRIC = ROOT / "answer_submission_eval101.py"
# None is an earlier question supplied to keep the conversation intact.
COHORT = {
    57: {1: "rows"},
    167: {1: "rows"},
    123: {1: None, 2: "age", 3: "age"},
    125: {1: None, 2: "full_name"},
    98: {1: None, 2: None, 3: None, 4: "over_30_million"},
    268: {1: "rows", 3: "rows"},
}
LABELS = {
    "dev57_turn1": "纽约州和加州一共多少票",
    "dev167_turn1": "列出所有指挥对应的乐团",
    "dev123_turn2": "受过治疗的狗中最年轻的年龄",
    "dev123_turn3": "受过治疗的狗中最年长的年龄",
    "dev125_turn2": "年龄最小的学生全名",
    "dev98_turn4": "上一问的歌手净资产是否超过 3000 万",
    "dev268_turn1": "列出全体高中生姓名",
    "dev268_turn3": "Kyle 的学号",
}
# Score only the named requested field when these questions also display evidence.
REQUIRED_FIELD_ALIASES = {
    "dev167_turn1": ("orchestra", "orchestra_name"),
    "dev98_turn2": ("citizenship",),
    "dev98_turn3": ("singer_name", "name"),
}
SUBMISSION_RULES = (
    f"本轮请调用 {TOOL_NAME} 提交最终答案。先把能直接回答本问的只读 SQLite 查询放在 sql。"
    "SQL 可以包含辅助列。answer_columns 只列实际应展示给用户的结果列别名。"
    "answer_rows 逐行填入这些列的实际查询值，保留行数、顺序和数据库值。"
    "你可以用终端试查。提交被拒绝时，根据工具返回的实际查询结果修正一次。"
    "用户会看到通过核验的答案表，最终文字可简短确认。"
)
MATERIALIZED_RULES = (
    f"每轮请调用 {TOOL_NAME} 提交一条最终的只读 SQLite 查询。工具会执行 SQL 并直接展示真实结果，"
    "省略 answer_rows，避免手抄。若查了辅助列，可用 answer_columns 指定要显示的 SQL 别名。"
    "查询应让结果直接回答当前问句。问合计就返回一个合计值，问分项才给分项。"
    "追问中的名单顺序按实际展示的结果，指向同名的人时按已经确认的身份查询，"
    "信息不足时提问澄清。提交成功后简短确认。"
)


def prepare(output, *, full=False):
    source_log = FOLLOWUP_101_OUT / "turns.jsonl"
    source_plan = FOLLOWUP_101_OUT / "plan.json"
    verification = json.loads((FOLLOWUP_101_OUT / "verification.json").read_bytes())
    if digest(source_log) != verification["turns_sha256"] or digest(source_plan) != verification["plan_sha256"]:
        raise RuntimeError("Original verified experiment records have changed")
    baseline_plan = json.loads(source_plan.read_bytes())
    if ((baseline_plan["model"], baseline_plan["provider"]) != (MODEL, PROVIDER)
            or baseline_plan["dev_sha256"] != digest(DEV)):
        raise RuntimeError("The model route or CoSQL dev set differs from the original trial")
    baseline = {row["id"]: row for row in read_jsonl(source_log)}
    db_hashes = {item["database"]: item["sha256"] for item in baseline_plan["dialogues"]}
    dev = json.loads(DEV.read_bytes())
    plan = {
        "model": MODEL, "provider": PROVIDER, "harness_profile": "sdk-minimal",
        "mcp_sdk": version("mcp"), "mcp_tool": TOOL_NAME,
        "terminal_limit": LIMIT, "submission_limit": 2, "dev_sha256": digest(DEV),
        "baseline_log": source_log.relative_to(ROOT).as_posix(),
        "baseline_sha256": digest(source_log), "dialogues": [],
    }
    cohort = COHORT
    old_dialogues = {item["dev_index"]: item for item in baseline_plan["dialogues"]}
    if full:
        cohort = {index: dict.fromkeys(range(1, len(item["prompts"]) + 1))
                  for index, item in old_dialogues.items()}
        if len(cohort) != 29 or sum(map(len, cohort.values())) != 101 or len(EXCLUSIONS) != 23:
            raise RuntimeError("The original 29 dialogues, 101 turns or the frozen exclusion list differs")
        plan.update(protocol="materialized-v2", submission_rules=MATERIALIZED_RULES,
                    mcp_sha256=digest(SERVER), rubric_sha256=digest(RUBRIC), exclusions=EXCLUSIONS)
        if set(EXCLUSIONS) - set(baseline):
            raise RuntimeError("A reviewed exclusion is outside the baseline")
    for index, turns in cohort.items():
        interaction = dev[index]
        database = interaction["database_id"]
        original = DATA / "database" / database / f"{database}.sqlite"
        sha256 = digest(original)
        if sha256 != db_hashes[database]:
            raise RuntimeError(f"Original database has changed: {database}")
        workspace = output / "workspaces" / f"dev_{index}"
        workspace.mkdir(parents=True, exist_ok=True)
        copy = workspace / original.name
        if not copy.exists():
            shutil.copyfile(original, copy)
            copy.chmod(stat.S_IREAD)
        if digest(copy) != sha256:
            raise RuntimeError(f"Workspace database differs from original: {database}")
        dialogue = {"dev_index": index, "database": database, "sha256": sha256, "turns": []}
        for turn, contract in turns.items():
            item = interaction["interaction"][turn - 1]
            key = f"dev{index}_turn{turn}"
            old = baseline[key]
            if full:
                contract = CONTRACTS.get(key, "rows")
                category = "excluded" if key in EXCLUSIONS else "primary"
            else:
                category = "context" if contract is None else "delivery" if index == 268 else "prior_failure"
            if old["question"] != item["utterance"] or (
                category == "prior_failure" and old["grade"]["matched"]
            ):
                raise RuntimeError(f"The original question or result differs: {key}")
            turn_plan = {
                "id": key, "turn": turn, "question": item["utterance"], "category": category,
                "contract": contract or "rows", "old_strict": old["grade"]["matched"],
                "old_reason": old["grade"]["reason"],
            }
            if full:
                if turn > 1 and old_dialogues[index]["prompts"][turn - 1] != item["utterance"]:
                    raise RuntimeError(f"Previous follow-up was not the original utterance: {key}")
                turn_plan["answer_fields"] = answer_fields(old)
                record = old["grade"]
                turn_plan["old_core_correct"] = score_answer(
                    turn_plan, record.get("predicted_result"), record["reference_result"],
                    record["reference_sql"], answer_contract,
                )["matched"]
            dialogue["turns"].append(turn_plan)
        plan["dialogues"].append(dialogue)
    # Match JSON's container types so that resuming compares to the serialized plan.
    if full:
        plan["exclusions"] = {key: list(value) for key, value in EXCLUSIONS.items()}
    plan_file = output / "plan.json"
    if plan_file.exists() and json.loads(plan_file.read_bytes()) != plan:
        raise RuntimeError("The run directory already contains a different plan")
    if not plan_file.exists():
        write_text(plan_file, manifest_text(plan))
    return plan, dev


def answer_contract(contract, result, *, reference=False):
    columns, rows = result["columns"], result["rows"]
    if contract == "rows":
        return [tuple(row) for row in rows]
    if contract == "full_name":
        if len(columns) == 1 and all(isinstance(row[0], str) for row in rows):
            return [(row[0].strip(),) for row in rows]
        aliases = [re.sub(r"[^a-z]", "", name.lower()) for name in columns]
        first = [i for i, name in enumerate(aliases) if name in {"fname", "firstname", "givenname"}]
        last = [i for i, name in enumerate(aliases) if name in {"lname", "lastname", "surname", "familyname"}]
        if len(columns) == 2 and len(first) == len(last) == 1:
            return [(f"{row[first[0]].strip()} {row[last[0]].strip()}",) for row in rows]
        raise ValueError("Full name requires one name column or labeled first and last names")
    if contract == "over_30_million" and reference:
        pos = next(i for i, name in enumerate(columns) if name.casefold() == "net_worth_millions")
        return [(row[pos] > 30,) for row in rows]
    if len(columns) != 1:
        raise ValueError("This question requires a single answer column")
    normalized = []
    for (value,) in rows:
        if contract == "age":
            age = float(value)
            if isinstance(value, bool) or not math.isfinite(age):
                raise ValueError("Age must be a finite numeric value")
            normalized.append((age,))
        elif type(value) in (bool, int) and value in (0, 1):
            normalized.append((bool(value),))
        elif isinstance(value, str) and value.strip().casefold() in {"yes", "no", "true", "false"}:
            normalized.append((value.strip().casefold() in {"yes", "true"},))
        else:
            raise ValueError("The net-worth threshold question requires a yes/no answer")
    return normalized


def submissions(events):
    calls = [event["data"] for event in events
             if event["type"] == "tool/call" and event["data"]["name"] == TOOL_NAME]
    results = {event["data"]["message"].get("toolCallId"): event["data"]["message"]
               for event in events if event["type"] == "tool/result"}
    extracted = []
    for call in calls:
        try:
            args = json.loads(call["arguments"])
        except json.JSONDecodeError:
            args = {}
        message = results.get(call["callId"], {})
        receipt = message.get("structuredContent")
        if receipt is None and not message.get("isError"):
            for block in message.get("content", []):
                if block["type"] == "text":
                    try:
                        receipt = json.loads(block["text"])
                    except json.JSONDecodeError:
                        continue
                    break
        extracted.append({"arguments": args, "receipt": receipt})
    return extracted


def grade_submission(entry, reference, database_file):
    attempts = submissions(entry["events"])
    latest = attempts[-1] if attempts else {}
    args = latest.get("arguments") or {}
    receipt = latest.get("receipt")
    full = "answer_fields" in entry
    source = "mcp" if attempts else "missing"
    if full and not attempts and entry["answer"].strip():
        fallback = check_submission(database_file, entry["answer"])
        if fallback["accepted"]:
            args = {"sql": entry["answer"]}
            receipt = fallback
            source = "message_sql"
    sql = args.get("sql") if isinstance(args, dict) else None
    grade = evaluate({"database": entry["database"]}, reference,
                     {"raw_reply": sql if isinstance(sql, str) else "", "finish": entry["finish"]},
                     include_results=True)
    grade.pop("database")
    accuracy = {"attempts": attempts, "verified": False, "matched": False, "user_answer": None,
                "reason": "missing_submission" if not attempts else "submission_rejected"}
    if full:
        accuracy.update(delivery_source=source, core={"matched": False, "method": "no_answer"})
    if not isinstance(receipt, dict) or receipt.get("accepted") is not True:
        return grade, accuracy
    if check_submission(database_file, args["sql"], args.get("answer_columns"), args.get("answer_rows")) != receipt:
        raise RuntimeError("MCP receipt could not be independently reproduced from the workspace database")
    displayed = {"columns": receipt["columns"], "rows": receipt["rows"]}
    accuracy.update(verified=True, user_answer=displayed)
    try:
        actual = answer_contract(entry["contract"], displayed)
        expected = answer_contract(entry["contract"], grade["reference_result"], reference=True)
        matched = result_eq(expected, actual, "order by" in grade["reference_sql"].lower())
        accuracy.update(matched=matched, reason="match" if matched else "required_answer_differs")
    except (AttributeError, TypeError, ValueError):
        accuracy["reason"] = "answer_shape_differs"
    if full:
        accuracy["core"] = score_answer(entry, displayed, grade["reference_result"],
                                        grade["reference_sql"], answer_contract)
    return grade, accuracy


async def iter_dialogue(dialogue, workspace, runtime, *, cli, credentials, sqlite_cli, powershell,
                        materialized, expected_tools=None):
    index, database = dialogue["dev_index"], dialogue["database"]
    database_file = workspace / f"{database}.sqlite"
    runtime.mkdir(parents=True)
    turn_file = runtime / "active_turn.txt"
    turn_file.write_text("", encoding="utf-8")
    patch = terminal_patch(workspace, runtime, powershell, submit_tool=TOOL_NAME)
    patch.append({"insert": [{
        "id": "cosql-answer-mcp", "name": "@deepseek-ai/dsh-mcp-client",
        "config": {"serverName": SERVER_NAME, "transport": "stdio", "command": sys.executable,
                   "args": ["-B", "-X", "utf8", str(SERVER), str(database_file)],
                   "cwd": str(workspace), "failOnStartupError": True, "toolCallTimeoutMs": 15000},
    }]})
    async with Harness(cli, credentials, runtime, workspace=workspace, patch_overrides=patch,
                       environment=runtime_environment(sqlite_cli), initialize_timeout=90) as harness:
        if not (runtime / "terminal_budget_ready").is_file():
            raise RuntimeError("Terminal tool policy did not initialize")
        session = harness.session_id()
        for position, item in enumerate(dialogue["turns"], 1):
            entry = {**item, "dev_index": index, "database": database, "events": [], "session_id": session}
            prompt = item["question"]
            if position == 1:
                movement = FOLLOWUP_RULES.split("\n\n", 1)[0]
                rules = MATERIALIZED_RULES if materialized else SUBMISSION_RULES
                prompt = f"数据库位于 ./{database}.sqlite。请在当前目录做只读查询。\n\n{movement}\n\n{rules}\n\n{prompt}"
            entry["prompt"] = prompt
            turn_file.write_text(str(position), encoding="utf-8")
            started = time.monotonic()
            try:
                entry.update(await prompt_agent(harness, prompt, session, entry["events"]))
                if entry["harness_turn"] != position:
                    raise RuntimeError("The Harness turn number differs from the replay order")
                if position == 1 and not any(
                    tool["name"] == TOOL_NAME
                    for event in entry["events"] if event["type"] == "request/header"
                    for tool in event["data"]["header"].get("tools", [])
                ):
                    raise RuntimeError("The model request did not advertise the submission MCP")
                if expected_tools is not None and any(
                    {tool["name"] for tool in event["data"]["header"].get("tools", [])} != expected_tools
                    for event in entry["events"] if event["type"] == "request/header"
                ):
                    raise RuntimeError("The model request tools differ from the Research configuration")
                if digest(database_file) != dialogue["sha256"]:
                    raise RuntimeError("The dialogue database was modified")
            except (KeyError, OSError, RuntimeError, TimeoutError) as exc:
                entry["error"] = diagnostic(exc) or type(exc).__name__
            entry["seconds"] = round(time.monotonic() - started, 3)
            yield entry
            if "error" in entry:
                break


async def play(args, dialogue, dev, sqlite_cli, powershell, records):
    index, database = dialogue["dev_index"], dialogue["database"]
    workspace = args.run_dir / "workspaces" / f"dev_{index}"
    runtime = args.run_dir / "runtime" / f"dev_{index}_{uuid4().hex[:8]}"
    database_file = workspace / f"{database}.sqlite"
    turns = iter_dialogue(dialogue, workspace, runtime, cli=args.cli, credentials=args.credentials,
                          sqlite_cli=sqlite_cli, powershell=powershell,
                          materialized="answer_fields" in dialogue["turns"][0])
    async with aclosing(turns):
        async for entry in turns:
            grading_started = time.monotonic()
            if "error" not in entry:
                try:
                    reference = dev[index]["interaction"][entry["turn"] - 1]["query"]
                    entry["grade"], entry["submission"] = grade_submission(entry, reference, database_file)
                except (KeyError, OSError, RuntimeError, TimeoutError) as exc:
                    entry["error"] = diagnostic(exc) or type(exc).__name__
                entry["seconds"] = round(entry["seconds"] + time.monotonic() - grading_started, 3)
            records.append(entry)
            write_records(args.run_dir / "turns.jsonl", records)
            answer = entry.get("submission", {})
            result = "core_match" if answer.get("core", {}).get("matched") else answer.get("reason")
            print(entry["id"], entry.get("error", result),
                  f"{entry['seconds']:.1f}s", flush=True)
            if "error" in entry:
                raise RuntimeError(entry["error"])


async def run(args, plan, dev, records, sqlite_cli, powershell):
    gate = asyncio.Semaphore(args.workers)
    pending = []
    for dialogue in plan["dialogues"]:
        index = dialogue["dev_index"]
        if args.dialogue is not None and index != args.dialogue:
            continue
        previous = [row for row in records if row["dev_index"] == index]
        if [row["id"] for row in previous] == [item["id"] for item in dialogue["turns"]] and all(
            "error" not in row and "submission" in row for row in previous
        ):
            continue
        if previous:
            attempts = args.run_dir / "attempts"
            attempts.mkdir(exist_ok=True)
            write_records(attempts / f"dev_{index}_{uuid4().hex[:8]}.jsonl", previous)
            records[:] = [row for row in records if row["dev_index"] != index]
            write_records(args.run_dir / "turns.jsonl", records)
        pending.append(dialogue)

    async def bounded(dialogue):
        async with gate:
            await play(args, dialogue, dev, sqlite_cli, powershell, records)

    outcomes = await asyncio.gather(*(bounded(dialogue) for dialogue in pending), return_exceptions=True)
    errors = [f"dev{dialogue['dev_index']}: {diagnostic(outcome)}"
              for dialogue, outcome in zip(pending, outcomes, strict=True) if isinstance(outcome, Exception)]
    if errors:
        raise RuntimeError("\n".join(errors))


def required_answer_matches(row):
    submission = row["submission"]
    if submission["matched"]:
        return True
    aliases = REQUIRED_FIELD_ALIASES.get(row["id"])
    if not aliases or not submission["verified"]:
        return False
    display = submission["user_answer"]
    columns = [name for name in display["columns"] if name.casefold() in aliases]
    if len(columns) != 1:
        return False
    projected = project_rows(display["columns"], display["rows"], columns)
    grade = row["grade"]
    expected = [tuple(record) for record in grade["reference_result"]["rows"]]
    return result_eq(expected, [tuple(record) for record in projected],
                     "order by" in grade["reference_sql"].lower())


def summarize(output, plan, records):
    completed = {row["id"]: row for row in records if "submission" in row and "error" not in row}
    planned = [item for dialogue in plan["dialogues"] for item in dialogue["turns"]]
    if plan.get("protocol") == "materialized-v2":
        primary = [row for row in completed.values() if row["category"] == "primary"]
        planned_primary = [item for item in planned if item["category"] == "primary"]
        summary = {
            "planned": len(planned), "completed": len(completed),
            "strict_execution": {"old": sum(item["old_strict"] for item in planned),
                                 "new": (sum(row["grade"]["matched"] for row in completed.values())
                                         if completed else None)},
            "primary": {"planned": len(planned_primary), "completed": len(primary),
                        "old_core": sum(item["old_core_correct"] for item in planned_primary),
                        "new_core": sum(row["submission"]["core"]["matched"] for row in primary) if primary else None,
                        "recovered": [row["id"] for row in primary
                                      if not row["old_core_correct"] and row["submission"]["core"]["matched"]],
                        "regressed": [row["id"] for row in primary
                                      if row["old_core_correct"] and not row["submission"]["core"]["matched"]]},
            "excluded": dict(Counter(reason[0] for reason in plan["exclusions"].values())),
            "delivery": dict(Counter(row["submission"]["delivery_source"] if row["submission"]["verified"]
                                     else "unverified" for row in completed.values())),
            "mcp_calls": sum(len(row["submission"]["attempts"]) for row in completed.values()),
            "column_projections": [row["id"] for row in primary if row["submission"]["core"]["matched"]
                                   and row["submission"]["core"]["method"] == "fixed_field_projection"],
            "reported_tokens": sum(row.get("reported_tokens", 0) for row in records),
        }
        write_text(output / "summary.json", manifest_text(summary))
        print(manifest_text(summary), flush=True)
        return
    totals = {}
    for category in ("prior_failure", "delivery", "context"):
        items = [item for item in planned if item["category"] == category]
        scored = [completed[item["id"]] for item in items if item["id"] in completed]
        totals[category] = {
            "planned": len(items), "completed": len(scored), "old_strict": sum(row["old_strict"] for row in scored),
            "new_strict": sum(row["grade"]["matched"] for row in scored),
            "verified_submissions": sum(row["submission"]["verified"] for row in scored),
            "answer_correct": sum(row["submission"]["matched"] for row in scored),
            "required_answer_correct": sum(required_answer_matches(row) for row in scored),
        }
    summary = {"total_completed": len(completed), "groups": totals,
               "reported_tokens": sum(row.get("reported_tokens", 0) for row in records),
               "mcp_calls": sum(len(row["submission"]["attempts"]) for row in completed.values()),
               "required_field_aliases": REQUIRED_FIELD_ALIASES,
               "per_turn": [{"id": row["id"], "question": LABELS.get(row["id"], row["question"]),
                             "reference_compatible": row["grade"]["matched"],
                             "submission_verified": row["submission"]["verified"],
                             "declared_answer_correct": row["submission"]["matched"],
                             "required_answer_correct": required_answer_matches(row)}
                            for row in completed.values()]}
    write_text(output / "summary.json", manifest_text(summary))
    print(manifest_text(summary), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    runtime = Path(os.environ["USERPROFILE"]) / ".dsh-upgrade-stage-20261020"
    parser.add_argument("stage", choices=("prepare", "run", "summary"))
    parser.add_argument("--cohort", choices=("pilot13", "full101"), default="pilot13")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--dialogue", type=int)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--cli", type=Path, default=runtime / "runtime/node_modules/@deepseek-ai/dsh/lib/bin.js")
    parser.add_argument("--credentials", type=Path, default=runtime / "home/.credentials.yaml")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    full = args.cohort == "full101"
    args.run_dir = (args.run_dir or (OUT_101 if full else OUT)).resolve()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    lock = args.run_dir / ".running"
    with lock.open("x", encoding="utf-8") as owner:
        owner.write(f"{os.getpid()}\n")
    try:
        plan, dev = prepare(args.run_dir, full=full)
        if args.dialogue is not None and args.dialogue not in {item["dev_index"] for item in plan["dialogues"]}:
            parser.error("--dialogue is outside this cohort")
        path = args.run_dir / "turns.jsonl"
        records = read_jsonl(path) if path.exists() else []
        if args.stage == "run":
            sqlite_cli = Path(shutil.which("sqlite3") or "")
            powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            if not all(path.is_file() for path in (sqlite_cli, powershell, args.cli, args.credentials)):
                parser.error("Local Harness, credentials, sqlite3 and Windows PowerShell are required")
            try:
                asyncio.run(run(args, plan, dev, records, sqlite_cli, powershell))
            finally:
                summarize(args.run_dir, plan, records)
        else:
            summarize(args.run_dir, plan, records)
    finally:
        lock.unlink()


if __name__ == "__main__":
    main()
