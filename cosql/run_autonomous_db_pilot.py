"""Prepare and run CoSQL dialogues with Harness's native PowerShell tool."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from harness_sdk import MODEL, PROVIDER, Harness, diagnostic
from run_harness_baseline import DATA, EVAL, ROOT, evaluate, manifest_text, read_jsonl, write_text
from run_harness_dialogue_comparison import encode_sqlite_value


INDICES = (106, 215, 268)
FOLLOWUP_INDICES = (275, 254, 280, 31)
LIMIT = 6
TURN_TIMEOUT = 120
OUT = ROOT / "reports" / "2026-11-19_gpt6sol_autonomous_pilot"
COMPARISON_OUT = ROOT / "reports" / "2026-11-19_gpt6sol_autonomous_101"
FOLLOWUP_OUT = ROOT / "reports" / "2026-11-19_gpt6sol_followup_14"
FOLLOWUP_101_OUT = ROOT / "reports" / "2026-11-19_gpt6sol_followup_101"
FIRST_MANIFEST = ROOT / "reports" / "2026-11-18_gpt6sol_harness_baseline" / "manifest.json"
OLD_LOG = ROOT / "reports" / "2026-11-19_gpt6sol_continuous_300" / "turns.jsonl"
OLD_PLAN = OLD_LOG.with_name("plan.json")
DEV = DATA / "sql_state_tracking" / "cosql_dev.json"
SQL_BLOCK = re.compile(r"```(?:sql|sqlite)[ \t]*\r?\n(.*?)```", re.S | re.I)
FOLLOWUP_RULES = (
    "接下来会按顺序发送同一段对话的问题。遇到追问，沿用上文已经确定的对象、范围和条件，"
    "按本轮要求调整。遇到新话题，按当前问题确定查询范围。"
    "结合上文仍无法确定指代时，先问一个具体的澄清问题。\n\n"
    "每个可回答的问题，先简短回答，再用一个 SQL 代码块附上一条可以直接运行的最终 SQLite 查询。"
    "查询结果只返回本轮要求的字段和记录，按题意保留顺序和数值精度。辅助信息可以写在正文里。"
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def comparison_source():
    first = json.loads(FIRST_MANIFEST.read_bytes())
    previous = json.loads(OLD_PLAN.read_bytes())
    selection = previous["dialogues"][:previous["checkpoint_dialogues"]]
    first_selection = [{field: row[field] for field in ("dev_index", "database", "turns")}
                       for row in first["dialogues"]]
    if (selection != first_selection or len(selection) != 29
            or sum(row["turns"] for row in selection) != 101):
        raise RuntimeError("The two baseline manifests disagree on the 101-question cohort")
    if previous["evaluator_sha256"] != digest(EVAL / "exec_eval.py"):
        raise RuntimeError("The execution evaluator differs from the previous run")
    return previous, selection


def reuse_pilot_records(plan, pilot_plan_path, pilot_log_path):
    pilot = json.loads(pilot_plan_path.read_bytes())
    for key, value in plan.items():
        if key != "dialogues" and pilot.get(key) != value:
            raise RuntimeError(f"The pilot's {key} differs from the comparison plan")
    pilot_dialogues = {dialogue["dev_index"]: dialogue for dialogue in pilot["dialogues"]}
    pilot_records = read_jsonl(pilot_log_path)
    seed = []
    reused = []
    for dialogue in plan["dialogues"]:
        index = dialogue["dev_index"]
        if index not in pilot_dialogues:
            continue
        expected = {f"dev{index}_turn{turn}": prompt
                    for turn, prompt in enumerate(dialogue["prompts"], 1)}
        records = [row for row in pilot_records if row["dev_index"] == index]
        if (dialogue != pilot_dialogues[index] or len(records) != len(expected)
                or {row["id"]: row["prompt"] for row in records} != expected
                or len({row["session_id"] for row in records}) != 1
                or any(row["database"] != dialogue["database"] or "error" in row
                       or row["harness_turn"] != turn or row["turn"] != turn
                       or row["finish"] != "completed" or type(row.get("grade", {}).get("matched")) is not bool
                       for turn, row in enumerate(records, 1))):
            raise RuntimeError(f"Pilot dialogue {index} cannot be reused as a complete session")
        reused.append(index)
        seed.extend(records)
    return seed, reused


def prepare_comparison(plan):
    pilot_plan_path = OUT / "plan.json"
    pilot_log_path = OUT / "turns.jsonl"
    seed, reused = reuse_pilot_records(plan, pilot_plan_path, pilot_log_path)
    all_ids = {f"dev{d['dev_index']}_turn{turn}" for d in plan["dialogues"]
               for turn in range(1, len(d["prompts"]) + 1)}
    baseline = [row for row in read_jsonl(OLD_LOG) if row["id"] in all_ids]
    if len(baseline) != len(all_ids) or {row["id"] for row in baseline} != all_ids:
        raise RuntimeError("The previous continuous run does not have all 101 paired results")
    paired_tokens = {row["id"]: row["usage"]["totalTokens"] for row in baseline}
    reused_tokens = sum(row["reported_tokens"] for row in seed)
    new_turns = len(all_ids) - len(seed)
    plan["comparison"] = {
        "harness_profile": "sdk-minimal",
        "baseline_plan": OLD_PLAN.relative_to(ROOT).as_posix(),
        "baseline_plan_sha256": digest(OLD_PLAN),
        "baseline_log": OLD_LOG.relative_to(ROOT).as_posix(),
        "baseline_log_sha256": digest(OLD_LOG),
        "baseline_matched": sum(row["grade"]["matched"] for row in baseline),
        "baseline_reported_tokens": sum(paired_tokens.values()),
        "pilot_plan": pilot_plan_path.relative_to(ROOT).as_posix(),
        "pilot_plan_sha256": digest(pilot_plan_path),
        "pilot_log": pilot_log_path.relative_to(ROOT).as_posix(),
        "pilot_log_sha256": digest(pilot_log_path),
        "planned_dialogues": len(plan["dialogues"]),
        "planned_turns": len(all_ids),
        "reused_dialogues": reused,
        "reused_turns": len(seed),
        "reused_reported_tokens": reused_tokens,
        "baseline_tokens_on_reused_turns": sum(paired_tokens[row["id"]] for row in seed),
        "new_dialogues": len(plan["dialogues"]) - len(reused),
        "new_turns": new_turns,
        "estimated_new_reported_tokens": round(reused_tokens * new_turns / len(seed)) if seed else None,
    }
    return seed


def write_records(path, records):
    write_text(path, "".join(json.dumps(row, ensure_ascii=False, default=encode_sqlite_value) + "\n"
                             for row in records))


def prepare(output, sqlite_cli, *, comparison=False, followup=False, followup_full=False):
    dataset = json.loads(DEV.read_bytes())
    plan = {
        "model": MODEL, "provider": PROVIDER, "dev_sha256": digest(DEV),
        "tool": "Harness native PowerShell invoking local SQLite",
        "workspace": "One regular Windows directory per dialogue with a read-only database copy.",
        "powershell_policy": "read-only (Windows file-write restriction; not filesystem read isolation)",
        "terminal_calls_limit_per_question": LIMIT, "question_timeout_seconds": TURN_TIMEOUT,
        "sqlite_cli_version": subprocess.run([str(sqlite_cli), "--version"], check=True,
                                             capture_output=True, text=True, timeout=10).stdout.strip(),
        "dialogues": [],
    }
    if comparison or followup:
        previous, selection = comparison_source()
        if (previous["dev_sha256"] != plan["dev_sha256"]
                or (previous["model"], previous["provider"]) != (MODEL, PROVIDER)):
            raise RuntimeError("Dataset or model route differs from the previous 101-question run")
        if followup and not followup_full:
            selection = [row for row in selection if row["dev_index"] in FOLLOWUP_INDICES]
            if len(selection) != 4 or sum(row["turns"] for row in selection) != 14:
                raise RuntimeError("The follow-up pilot must contain four complete dialogues and 14 questions")
    else:
        selection = [{"dev_index": index} for index in INDICES]
    for selected in selection:
        index = selected["dev_index"]
        dialogue = dataset[index]
        database = dialogue["database_id"]
        original = DATA / "database" / database / f"{database}.sqlite"
        sha256 = digest(original)
        questions = [item["utterance"] for item in dialogue["interaction"]]
        if (comparison or followup) and ((database, len(questions)) != (selected["database"], selected["turns"])
                                         or sha256 != previous["sqlite_sha256"][database]):
            raise RuntimeError(f"Dialogue {index} or its database differs from the previous run")
        workspace = output / "workspaces" / f"dev_{index}"
        workspace.mkdir(parents=True, exist_ok=True)
        copy = workspace / original.name
        if not copy.exists():
            shutil.copyfile(original, copy)
            copy.chmod(stat.S_IREAD)
        if digest(copy) != sha256:
            raise RuntimeError(f"Database copy differs from its source for dialogue {index}")
        initial = (
            f"数据库位于 `./{original.name}`。请在当前目录用终端对这个数据库做只读查询，"
            "回答每个问题，并用一个 sql 代码块附上你用来得出答案的最终 SQL。\n\n"
        )
        if followup:
            initial = f"数据库位于 `./{original.name}`。请在当前目录用终端做只读查询。\n\n{FOLLOWUP_RULES}\n\n"
        plan["dialogues"].append({
            "dev_index": index, "database": database, "sha256": sha256,
            "workspace": str(workspace.relative_to(output)), "prompts": [initial + questions[0], *questions[1:]],
        })
    seed = None
    if comparison:
        seed = prepare_comparison(plan)
    elif followup:
        prior_plan_path = COMPARISON_OUT / "plan.json"
        prior_log_path = COMPARISON_OUT / "turns.jsonl"
        prior_plan = json.loads(prior_plan_path.read_bytes())
        for key, value in plan.items():
            if key != "dialogues" and prior_plan.get(key) != value:
                raise RuntimeError(f"The original autonomous run's {key} differs from the follow-up plan")
        if followup_full:
            pilot_plan = FOLLOWUP_OUT / "plan.json"
            pilot_log = FOLLOWUP_OUT / "turns.jsonl"
            verified = json.loads((FOLLOWUP_OUT / "verification.json").read_bytes())
            if digest(pilot_plan) != verified["plan_sha256"] or digest(pilot_log) != verified["turns_sha256"]:
                raise RuntimeError("The verified 14-question pilot has changed")
            seed, reused = reuse_pilot_records(plan, pilot_plan, pilot_log)
            if len(seed) != 14 or set(reused) != set(FOLLOWUP_INDICES):
                raise RuntimeError("Exactly four pilot dialogues and 14 answers must be reusable")
        plan["comparison"] = {
            "harness_profile": "sdk-minimal", "prompt_version": "followup",
            "planned_dialogues": len(plan["dialogues"]),
            "planned_turns": sum(len(row["prompts"]) for row in plan["dialogues"]),
            "reused_turns": len(seed) if seed is not None else 0,
            "autonomous_reference_plan": prior_plan_path.relative_to(ROOT).as_posix(),
            "autonomous_reference_plan_sha256": digest(prior_plan_path),
            "autonomous_reference_log": prior_log_path.relative_to(ROOT).as_posix(),
            "autonomous_reference_log_sha256": digest(prior_log_path),
        }
        if followup_full:
            plan["comparison"].update({
                "reused_dialogues": reused, "new_dialogues": len(selection) - len(reused),
                "new_turns": plan["comparison"]["planned_turns"] - len(seed),
                "pilot_plan": pilot_plan.relative_to(ROOT).as_posix(), "pilot_plan_sha256": digest(pilot_plan),
                "pilot_log": pilot_log.relative_to(ROOT).as_posix(), "pilot_log_sha256": digest(pilot_log),
            })
    elif sum(len(dialogue["prompts"]) for dialogue in plan["dialogues"]) != 12:
        raise RuntimeError("Approved pilot must contain exactly 12 questions")
    file = output / "plan.json"
    if file.exists() and json.loads(file.read_bytes()) != plan:
        raise RuntimeError("Existing run has a different plan")
    if not file.exists():
        write_text(file, manifest_text(plan))
    if seed is not None:
        records_path = output / "turns.jsonl"
        if records_path.exists():
            reused = set(plan["comparison"]["reused_dialogues"])
            saved = [row for row in read_jsonl(records_path) if row["dev_index"] in reused]
            if saved != seed:
                raise RuntimeError("The reused records differ from the pilot log")
        else:
            write_records(records_path, seed)
    return plan, dataset


def terminal_patch(workspace, runtime, powershell, *, submit_tool=None):
    budget = {"workspace": str(workspace), "turnFile": str(runtime / "active_turn.txt"),
              "readyFile": str(runtime / "terminal_budget_ready"), "limit": LIMIT}
    if submit_tool is not None:
        budget.update(submitTool=submit_tool, submitLimit=2)
    return [
        {"id": "sandbox-policy", "config": {"mode": "read-only", "workspaceRoot": str(workspace)}},
        {"id": "agent-loop", "config": {"maxParallelToolCalls": 1}},
        {"insert": [
            {"id": "cosql-pwsh-shell", "name": "@deepseek-ai/dsh-pwsh-sandbox",
             "config": {"pwshPath": str(powershell), "cwd": str(workspace), "timeoutMs": 18000,
                        "maxTimeoutMs": 20000, "maxOutputBytes": 64000}},
            {"id": "cosql-shell-env", "name": "@deepseek-ai/dsh-shell-env"},
            {"id": "cosql-pwsh", "name": "@deepseek-ai/dsh-tool-pwsh",
             "config": {"enableRunInBackground": False, "promoteOnTimeout": False}},
            {"id": "cosql-budget", "name": (ROOT / "pilot_terminal_budget.mjs").as_uri(),
             "config": budget},
        ]},
    ]


def runtime_environment(sqlite_cli):
    keep = {
        "APPDATA", "COMSPEC", "LOCALAPPDATA", "PATH", "PATHEXT", "PROGRAMDATA",
        "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "SYSTEMDRIVE",
        "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "WINDIR",
    }
    environment = {key.upper(): val for key, val in os.environ.items() if key.upper() in keep}
    environment["PATH"] = str(sqlite_cli.parent) + os.pathsep + environment.get("PATH", "")
    return environment


async def prompt_agent(harness, text, session_id, events):
    harness.events[session_id] = events
    try:
        async with asyncio.timeout(TURN_TIMEOUT):
            receipt = await harness.call("session/prompt", {
                "sessionId": session_id, "contentBlocks": [{"type": "text", "text": text}],
            }, timeout=25)
            await harness.call("session/wait", {"sessionId": session_id})
    finally:
        harness.events.pop(session_id, None)
    end = [e["data"] for e in events if e["type"] == "turn/end"]
    replies = [e["data"] for e in events if e["type"] == "assistant/message"]
    if len(end) != 1 or not replies or end[0]["reason"]["kind"] != "completed":
        reason = end[0]["reason"] if end else "missing completion event"
        raise RuntimeError(f"Harness did not complete the answer: {diagnostic(reason)}")
    for item in replies:
        source = item["message"].get("source", {})
        if (source.get("provider"), source.get("model")) != (PROVIDER, MODEL):
            raise RuntimeError("Unexpected model route in Harness trace")
    text = "".join(b["text"] for b in replies[-1]["message"]["content"] if b["type"] == "text")
    return {
        "message_id": receipt["messageId"], "harness_turn": end[0]["turn"],
        "answer": text, "finish": end[0]["reason"]["kind"],
        "model_messages": len(replies),
        "reported_tokens": sum(item.get("usage", {}).get("totalTokens", 0) for item in replies),
        "tool_requests": sum(e["type"] == "tool/call" for e in events),
    }


def grade_answer(sample, reference, entry):
    matches = SQL_BLOCK.findall(entry["answer"])
    sql = matches[-1].strip() if matches else entry["answer"].strip()
    return evaluate(sample, reference, {"raw_reply": sql, "finish": entry["finish"]}, include_results=True)


async def run_pilot(args, plan, dataset, sqlite_cli, powershell):
    output = args.run_dir
    records_path = output / "turns.jsonl"
    records = read_jsonl(records_path) if records_path.exists() else []
    env = runtime_environment(sqlite_cli)
    workers = getattr(args, "workers", 1)
    pending = []
    for dialogue in plan["dialogues"]:
        index = dialogue["dev_index"]
        count = len(dialogue["prompts"])
        previous = [r for r in records if r["dev_index"] == index]
        if previous:
            if len(previous) == count and all("grade" in r and "error" not in r for r in previous):
                continue
            raise RuntimeError(f"Dialogue {index} has an incomplete attempt in this run directory")
        pending.append(dialogue)

    async def play(dialogue):
        index, database = dialogue["dev_index"], dialogue["database"]
        work = output / dialogue["workspace"]
        runtime = output / "runtime" / f"dev_{index}"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "active_turn.txt").write_text("", encoding="utf-8")
        async with Harness(args.cli, args.credentials, runtime, workspace=work,
                           patch_overrides=terminal_patch(work, runtime, powershell),
                           environment=env, initialize_timeout=90) as harness:
            if not (runtime / "terminal_budget_ready").is_file():
                raise RuntimeError(f"Tool budget policy failed to initialize; inspect {runtime / 'stderr.txt'}")
            session = harness.session_id()
            originals = dataset[index]["interaction"]
            for turn, (prompt, item) in enumerate(zip(dialogue["prompts"], originals, strict=True), 1):
                sample = {"id": f"dev{index}_turn{turn}", "dev_index": index, "database": database,
                          "turn": turn, "question": item["utterance"]}
                entry = {**sample, "prompt": prompt, "session_id": session, "events": []}
                started = time.monotonic()
                (runtime / "active_turn.txt").write_text(str(turn), encoding="utf-8")
                try:
                    entry.update(await prompt_agent(harness, prompt, session, entry["events"]))
                    if entry["harness_turn"] != turn:
                        raise RuntimeError("Harness turn number does not match the dialogue")
                    entry["grade"] = grade_answer(sample, item["query"], entry)
                    if sample["id"] == "dev215_turn3":
                        entry["reviewed_result"] = evaluate(sample, "SELECT COUNT(*) FROM singer WHERE Is_male = 'T'",
                                                          {"raw_reply": entry["grade"]["predicted_sql"],
                                                           "finish": entry["finish"]}, include_results=True)
                    if digest(work / f"{database}.sqlite") != dialogue["sha256"]:
                        raise RuntimeError("The database copy was modified")
                except (RuntimeError, TimeoutError, OSError, KeyError) as exc:
                    entry["error"] = diagnostic(exc) or type(exc).__name__
                entry["seconds"] = round(time.monotonic() - started, 3)
                # Commit synchronously in the shared event loop before another dialogue can append.
                records.append(entry)
                write_records(records_path, records)
                print(sample["id"], entry.get("grade", {}).get("reason", entry.get("error")),
                      f"tools={entry.get('tool_requests', 0)}", f"{entry['seconds']:.1f}s", flush=True)
                if "error" in entry:
                    return entry["error"]
        return None

    if workers == 1:
        for dialogue in pending:
            if error := await play(dialogue):
                raise RuntimeError(error)
    else:
        gate = asyncio.Semaphore(workers)
        failed = {}

        async def play_bounded(dialogue):
            async with gate:
                try:
                    error = await play(dialogue)
                except TimeoutError as exc:
                    error = diagnostic(exc) or "Harness initialization timed out"
                if error:
                    failed[dialogue["dev_index"]] = error

        async with asyncio.TaskGroup() as tasks:
            for dialogue in pending:
                tasks.create_task(play_bounded(dialogue))
        if failed:
            details = ", ".join(f"{index} ({error})" for index, error in sorted(failed.items()))
            raise RuntimeError(f"These dialogues did not finish: {details}")
    return records


def summarize(output, records):
    plan_file = output / "plan.json"
    comparison = json.loads(plan_file.read_bytes()).get("comparison") if plan_file.exists() else None
    reference = comparison.get("autonomous_reference_log") if comparison else None
    baseline_log = ROOT / reference if reference else OLD_LOG
    if reference and digest(baseline_log) != comparison["autonomous_reference_log_sha256"]:
        raise RuntimeError("The old autonomous reference log has changed")
    baseline = {row["id"]: row["grade"]["matched"] for row in read_jsonl(baseline_log)}
    compared = [{"id": r["id"], "previous": baseline[r["id"]], "autonomous": r["grade"]["matched"]}
                for r in records if r["id"] in baseline and "grade" in r]
    complete = [r for r in records if "grade" in r]
    result = {
        "questions": len(complete), "matched": sum(r["grade"]["matched"] for r in complete),
        "questions_with_tools": sum(r["tool_requests"] > 0 for r in complete),
        "tool_requests": sum(r["tool_requests"] for r in complete),
        "model_messages": sum(r["model_messages"] for r in complete),
        "reported_tokens": sum(r["reported_tokens"] for r in complete),
        "paired": compared,
    }
    if reference:
        result["paired_reference"] = reference
    if comparison:
        reused = comparison["reused_turns"]
        planned = comparison["planned_turns"]
        result["progress"] = {"planned_questions": planned, "reused_questions": reused,
                              "new_questions": len(complete) - reused,
                              "remaining_questions": planned - len(complete)}
    write_text(output / "summary.json", manifest_text(result))
    print(manifest_text(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "run", "summary"))
    output_dirs = {"pilot": OUT, "comparison101": COMPARISON_OUT,
                   "followup14": FOLLOWUP_OUT, "followup101": FOLLOWUP_101_OUT}
    parser.add_argument("--cohort", choices=tuple(output_dirs), default="pilot")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--workers", type=int, default=1, help="Maximum number of concurrent dialogues (default 1)")
    runtime = Path(os.environ["USERPROFILE"]) / ".dsh-upgrade-stage-20261020"
    parser.add_argument("--cli", type=Path, default=runtime / "runtime/node_modules/@deepseek-ai/dsh/lib/bin.js")
    parser.add_argument("--credentials", type=Path, default=runtime / "home/.credentials.yaml")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    comparison = args.cohort == "comparison101"
    followup_full = args.cohort == "followup101"
    followup = args.cohort == "followup14" or followup_full
    args.run_dir = (args.run_dir or output_dirs[args.cohort]).resolve()
    if args.stage == "summary":
        log = args.run_dir / "turns.jsonl"
        records = [] if followup and not log.exists() else read_jsonl(log)
        summarize(args.run_dir, records)
        return
    sqlite_cli = Path(shutil.which("sqlite3") or "")
    powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not sqlite_cli.is_file() or not powershell.is_file():
        parser.error("Local SQLite and PowerShell are required")
    args.run_dir.mkdir(parents=True, exist_ok=True)
    lock = args.run_dir / ".running"
    with lock.open("x", encoding="utf-8") as owner:
        owner.write(f"{os.getpid()}\n")
    started = time.monotonic()
    try:
        plan, dataset = prepare(args.run_dir, sqlite_cli, comparison=comparison,
                                followup=followup, followup_full=followup_full)
        if args.stage == "prepare":
            if comparison or followup_full:
                summarize(args.run_dir, read_jsonl(args.run_dir / "turns.jsonl"))
            elif followup:
                print("Ready: four Windows working directories, 14 questions and a fixed follow-up prompt.")
            else:
                print("Ready: three Windows working directories, 12 questions and verified SQLite copies.")
        else:
            if not args.cli.is_file() or not args.credentials.is_file():
                parser.error("Local Harness CLI and credentials are required")
            summarize(args.run_dir, asyncio.run(run_pilot(args, plan, dataset, sqlite_cli, powershell)))
    finally:
        lock.unlink()
        if args.stage == "run":
            print(f"Run elapsed with --workers={args.workers} was {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    sys.exit(main())
