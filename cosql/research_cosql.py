"""CoSQL task, DSH, and scoring adapters for the Research evaluation runner."""

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from collections import Counter
from contextlib import aclosing
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UPSTREAM = ROOT / "upstream"
sys.path[:0] = [str(UPSTREAM / path) for path in (
    "research", "research/llm/src", "agent-session-analyzer/src",
)]

from agent_session_analyzer.recorders.dsh import DshRecorder
from datasets.package import read_package_index, resource_path
from environment.local import LocalEnvironment
from evaluation.contracts import PreparedTask, Score, Task
from harness.base import AgentResult
from utils.json_io import append_jsonl, redact, write_json

from harness_sdk import API_KEY_REF, BASE_URL, MODEL, PROVIDER
from record_research_usage import billable_usage
from run_answer_submission_pilot import grade_submission, iter_dialogue
from run_autonomous_db_pilot import DEV, TURN_TIMEOUT, digest, write_records
from run_harness_baseline import DATA, read_jsonl


FORMAT = "cosql-mcp-v2"
TOOLS = frozenset({"pwsh", "mcp__cosql__submit_answer", "list_mcp_resources",
                   "list_mcp_resource_templates", "read_mcp_resource"})


def load_tasks(index, ids=None):
    package = read_package_index(index, task_format=FORMAT)
    plan_path = resource_path(package.root, package.index["plan"])
    plan_hash = package.index["plan_sha256"]
    if digest(plan_path) != plan_hash:
        raise ValueError("Frozen CoSQL plan differs from the Research package index")
    dialogues = {row["dev_index"]: row for row in json.loads(plan_path.read_bytes())["dialogues"]}
    tasks = {}
    for entry in package.index["tasks"]:
        dialogue = dialogues[entry["dev_index"]]
        public = {key: dialogue[key] for key in ("dev_index", "database", "sha256")}
        public["turns"] = [{key: item[key] for key in ("id", "turn", "question")}
                           for item in dialogue["turns"]]
        tasks[entry["id"]] = Task(
            id=entry["id"], instruction=json.dumps(public, ensure_ascii=False),
            source={"plan": str(plan_path), "plan_sha256": plan_hash},
            metadata={"database": dialogue["database"], "turns": len(public["turns"])},
            agent_timeout_sec=150 + TURN_TIMEOUT * len(public["turns"]),
        )
    return list(tasks.values()) if ids is None else [tasks[task_id] for task_id in ids]


def prepare_task(task, destination, condition):
    dialogue = json.loads(task.instruction)
    database = dialogue["database"]
    original = DATA / "database" / database / f"{database}.sqlite"
    destination.mkdir()
    copy = destination / original.name
    shutil.copyfile(original, copy)
    copy.chmod(stat.S_IREAD)
    if digest(copy) != dialogue["sha256"]:
        raise RuntimeError(f"CoSQL database snapshot changed: {database}")
    return PreparedTask(task_dir=destination)


def create_environment(task_dir, *, output_dir):
    return LocalEnvironment(workdir=task_dir)


class Recorder(DshRecorder):
    """Feed native async DSH events to Research's standard trace writer."""

    def capture(self, session, events):
        self._session(session, session, {"provider": PROVIDER, "model": MODEL})
        self.last_path = self.paths[session]
        for event in events:
            row = self._adapters[session].feed(event)
            if row is not None:
                if row["event"] == "model.attempt":
                    row["data"]["usage"] = asdict(billable_usage(row["data"]["usage"]))
                self._write(session, row, sync=event["type"] == "turn/end")


def local_runtime():
    home = Path(os.environ["USERPROFILE"]) / ".dsh-upgrade-stage-20261020"
    return {
        "cli": home / "runtime/node_modules/@deepseek-ai/dsh/lib/bin.js",
        "credentials": home / "home/.credentials.yaml",
        "sqlite_cli": Path(shutil.which("sqlite3") or ""),
        "powershell": Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe",
    }


def load_dsh_key(cli, credentials):
    """Give Research the same secret DSH already resolves, in process memory only."""
    reader = """
        const req = require('module').createRequire(process.argv[1]);
        const yaml = req('yaml').parse(require('fs').readFileSync(process.argv[2], 'utf8'));
        const value = yaml.version === 1 && yaml.refs?.[process.argv[3]];
        if (typeof value !== 'string' || !value) process.exit(1);
        process.stdout.write(value);
    """
    completed = subprocess.run(["node", "-e", reader, str(cli), str(credentials), API_KEY_REF],
                               capture_output=True, encoding="utf-8", timeout=15)
    if completed.returncode or not completed.stdout:
        raise RuntimeError(f"DSH credential reference is unavailable: {API_KEY_REF}")
    return completed.stdout


async def execute(*, instruction, model, tools, environment, output_dir, workspace,
                  timeout_sec, skill_catalog, recorder):
    if (model["model"], model["api_key_env"], model["base_url"]) != (MODEL, API_KEY_REF, BASE_URL):
        raise ValueError("Research and DSH model routes disagree")
    if set(tools) != TOOLS:
        raise ValueError("Research and DSH tool lists disagree")
    workspace = Path(workspace) if workspace is not None else environment.workdir
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    secret = os.environ[API_KEY_REF]
    dialogue = json.loads(instruction)
    turns = iter_dialogue(dialogue, workspace, output / "runtime", **local_runtime(),
                          materialized=True, expected_tools=TOOLS)
    entries = []
    async with asyncio.timeout(timeout_sec), aclosing(turns):
        async for entry in turns:
            entry = redact(entry, secret)
            entries.append(entry)
            append_jsonl(output / "turns.jsonl", entry)
            recorder.capture(entry["session_id"], entry["events"])
            if "error" in entry:
                raise RuntimeError(entry["error"])
    if len(entries) != len(dialogue["turns"]):
        raise RuntimeError("DSH did not finish all turns in the dialogue")
    return AgentResult(entries[-1]["answer"], entries[-1]["finish"],
                       sum(row["tool_requests"] for row in entries),
                       {"reported_tokens": sum(row["reported_tokens"] for row in entries)},
                       str(recorder.last_path))


async def score_trial(context, agent_result):
    plan_path = Path(context.task.source["plan"])
    if digest(plan_path) != context.task.source["plan_sha256"]:
        raise RuntimeError("CoSQL scoring plan changed during the run")
    plan = json.loads(plan_path.read_bytes())
    if digest(DEV) != plan["dev_sha256"]:
        raise RuntimeError("CoSQL reference answers changed during the run")
    public = json.loads(context.task.instruction)
    index, database = public["dev_index"], public["database"]
    dialogue = next(item for item in plan["dialogues"] if item["dev_index"] == index)
    original_turns = json.loads(DEV.read_bytes())[index]["interaction"]
    workspace_db = context.prepared.task_dir / f"{database}.sqlite"
    if digest(workspace_db) != dialogue["sha256"]:
        raise RuntimeError("Agent workspace database no longer matches the original")

    raw = read_jsonl(context.output_dir / "agent" / "turns.jsonl")
    if ([item["id"] for item in raw] != [item["id"] for item in dialogue["turns"]]
            or len({item["session_id"] for item in raw}) != 1):
        raise RuntimeError("Research scoring needs the complete ordered dialogue in one session")
    graded = []
    for entry, case in zip(raw, dialogue["turns"], strict=True):
        entry.update(case)
        reference = original_turns[case["turn"] - 1]["query"]
        entry["grade"], entry["submission"] = grade_submission(entry, reference, workspace_db)
        graded.append(entry)

    primary = [row for row in graded if row["category"] == "primary"]
    metrics = {
        "turns": len(graded), "strict_correct": sum(row["grade"]["matched"] for row in graded),
        "primary_turns": len(primary),
        "primary_correct": sum(row["submission"]["core"]["matched"] for row in primary),
        "delivery": dict(Counter(row["submission"]["delivery_source"] if row["submission"]["verified"]
                                 else "unverified" for row in graded)),
    }
    verifier = context.output_dir / "verifier"
    verifier.mkdir(exist_ok=True)
    write_records(verifier / "turns.jsonl", graded)
    write_json(verifier / "result.json", metrics)
    reward = metrics["primary_correct"] / len(primary) if primary else 0.0
    return Score(reward=reward, metrics=metrics)
