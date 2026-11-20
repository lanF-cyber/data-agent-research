"""Launch the frozen 101-turn CoSQL experiment through Research."""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from research_cosql import API_KEY_REF, FORMAT, ROOT, UPSTREAM, local_runtime, load_dsh_key
from evaluation.config import experiment_directory, load_config
from evaluation.dataset import load_tasks
from evaluation.experiment import completed_trial, experiment_lock, load_run, trial_directory
from evaluation.runner import run_evaluation
from research_llm.traces import summarize_calls
from utils.json_io import append_jsonl, redact, write_json

from run_answer_submission_pilot import OUT_101, prepare
from run_autonomous_db_pilot import digest, write_records
from run_harness_baseline import read_jsonl


CONFIGS = ROOT / "research_config"
NAME = "mcp-v2-101"
SHARDS = 4
REVISIONS = {
    "research": "e52531bb2fdb0d3b9ff4ffac32514034110f5d54",
    "agent-session-analyzer": "502da5b899667bd525241b4e3d0878f9e83540e8",
}


def freeze_json(path, value):
    if path.exists():
        if json.loads(path.read_bytes()) != value:
            raise ValueError(f"A different run definition is already saved: {path}")
    else:
        write_json(path, value)


def prepare_package():
    plan, _ = prepare(OUT_101, full=True)
    package = OUT_101 / "research-package"
    plan_path = package / "plan.json"
    freeze_json(plan_path, plan)
    freeze_json(package / "dataset.json", {
        "schema_version": 1, "id": "cosql-dev-101", "version": "1", "task_format": FORMAT,
    })
    freeze_json(package / "index.json", {
        "schema_version": 1, "plan": plan_path.name, "plan_sha256": digest(plan_path),
        "task_count": len(plan["dialogues"]),
        "tasks": [{"id": f"dev{row['dev_index']}", "dev_index": row["dev_index"]}
                  for row in plan["dialogues"]],
    })
    return plan


def configuration():
    return load_config(name=NAME, **{key: CONFIGS / f"{key}.toml"
                                    for key in ("dataset", "environment", "agent", "evaluation")})


def partitions(config):
    tasks = load_tasks(config["dataset"])
    groups = [[] for _ in range(SHARDS)]
    weights = [0] * SHARDS
    # Keep complete conversations together, distributing their turns across the workers.
    for task in sorted(tasks, key=lambda item: -item.metadata["turns"]):
        slot = weights.index(min(weights))
        groups[slot].append(task.id)
        weights[slot] += task.metadata["turns"]
    if sum(weights) != 101 or len(tasks) != 29:
        raise ValueError("The Research experiment must cover the frozen 29 dialogues and 101 turns")
    return [{**config, "name": f"{config['name']}-{slot:02d}",
             "dataset": {**config["dataset"], "task_ids": ids}}
            for slot, ids in enumerate(groups, 1)]


def verify_upstream():
    for name, expected in REVISIONS.items():
        base = ["git", "-C", str(UPSTREAM / name)]
        revision = subprocess.check_output([*base, "rev-parse", "HEAD"], text=True).strip()
        tracked = subprocess.check_output([*base, "status", "--porcelain", "--untracked-files=no"], text=True)
        if revision != expected or tracked:
            raise ValueError(f"{name} differs from the verified upstream checkout at {expected}")


async def run_shards(configs, workers, resume):
    gate = asyncio.Semaphore(workers)

    async def run(config):
        async with gate:
            manifest = experiment_directory(config) / "manifest.json"
            return await run_evaluation(config, resume=resume and manifest.is_file())

    values = await asyncio.gather(*(run(config) for config in configs), return_exceptions=True)
    secret = os.environ[API_KEY_REF]
    return {config["name"]: redact(f"{type(value).__name__}: {value}", secret)
            for config, value in zip(configs, values, strict=True) if isinstance(value, BaseException)}


def summarize(output, configs, plan):
    completed, graded, usage, failures, stages = [], {}, {}, {}, Counter()
    for config in configs:
        shard = experiment_directory(config)
        if not (shard / "manifest.json").is_file():
            continue
        _, _, results = load_run(config, resume=True)
        for trial_id, result in results.items():
            for stage, seconds in result.get("stage_durations_sec", {}).items():
                stages[stage] += seconds or 0
            if not completed_trial(result):
                failures[result["task_id"]] = {"status": result["status"], "errors": result["errors"]}
                continue
            completed.append(result["task_id"])
            path = trial_directory(shard, trial_id) / "verifier" / "turns.jsonl"
            for row in read_jsonl(path):
                if row["id"] in graded:
                    raise RuntimeError(f"A turn was scored twice: {row['id']}")
                graded[row["id"]] = row
        for folder, pattern in (("trials", "*/agent/usage.jsonl"), ("attempts", "*/*/agent/usage.jsonl")):
            for path in (shard / folder).glob(pattern):
                for call in read_jsonl(path):
                    usage[call["call_id"]] = call

    ordered = [item for dialogue in plan["dialogues"] for item in dialogue["turns"]]
    rows = [graded[item["id"]] for item in ordered if item["id"] in graded]
    primary = [row for row in rows if row["category"] == "primary"]
    old_primary = [row for row in ordered if row["category"] == "primary"]
    results = {
        "planned_dialogues": len(plan["dialogues"]), "completed_dialogues": len(completed),
        "planned_turns": len(ordered), "completed_turns": len(rows),
        "failed_dialogues": failures,
        "strict_execution": {"old_correct": sum(row["old_strict"] for row in ordered),
                             "new_correct": sum(row["grade"]["matched"] for row in rows) if rows else None},
        "primary": {
            "planned": len(old_primary), "completed": len(primary),
            "old_correct": sum(row["old_core_correct"] for row in old_primary),
            "new_correct": sum(row["submission"]["core"]["matched"] for row in primary) if primary else None,
            "recovered": [row["id"] for row in primary if row["submission"]["core"]["matched"]
                          and not row["old_core_correct"]],
            "regressed": [row["id"] for row in primary if not row["submission"]["core"]["matched"]
                          and row["old_core_correct"]],
        },
        "excluded": dict(Counter(reason[0] for reason in plan["exclusions"].values())),
        "delivery": dict(Counter(row["submission"]["delivery_source"] if row["submission"]["verified"]
                                 else "unverified" for row in rows)),
        "research_usage": summarize_calls(list(usage.values())),
        "tokens": {key: sum(row.get(key) or 0 for row in usage.values()) for key in (
            "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens",
        )},
        "stage_seconds_across_dialogues": {key: round(seconds, 3) for key, seconds in stages.items()},
    }
    if rows:
        write_records(output / "turns.jsonl", rows)
    if usage:
        write_records(output / "usage.jsonl", list(usage.values()))
    write_json(output / "summary.json", results)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "summary", "run"))
    parser.add_argument("--workers", type=int, choices=range(1, SHARDS + 1), default=SHARDS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--project-id", default=os.environ.get("RESEARCH_PROJECT_ID", "cosql"))
    parser.add_argument("--user-id", default=os.environ.get("RESEARCH_USER_ID"))
    args = parser.parse_args()
    if args.stage == "run" and (not args.user_id or not args.user_id.strip() or not args.project_id.strip()):
        parser.error("Specify the Research ledger identity with --user-id and --project-id")

    verify_upstream()
    with experiment_lock(OUT_101 / "research-launcher"):
        plan = prepare_package()
        config = configuration()
        configs = partitions(config)
        output = Path(config["output_dir"])
        freeze_json(output / "plan.json", {
            "name": config["name"], "dialogues": 29, "turns": 101,
            "upstream_revisions": REVISIONS, "research_config": config,
            "shards": [{"name": item["name"], "task_ids": item["dataset"]["task_ids"],
                        "result_path": str(experiment_directory(item))} for item in configs],
        })
        if args.stage != "run":
            result = summarize(output, configs, plan)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return

        existing = any(experiment_directory(item).is_dir() for item in configs)
        identity = output / "run-identity.json"
        if existing and not args.resume:
            parser.error("This Research batch has already started. Use --resume to continue it")
        if args.resume and not identity.is_file():
            parser.error("There is no previous batch to resume")
        runtime = local_runtime()
        if not shutil.which("node") or not all(path.is_file() for path in runtime.values()):
            parser.error("Local DSH, its credential store, SQLite, Node and Windows PowerShell are required")
        secret = load_dsh_key(runtime["cli"], runtime["credentials"])
        if os.environ.get(API_KEY_REF, secret) != secret:
            parser.error("Research and DSH point to different credentials")
        os.environ.update({API_KEY_REF: secret, "RESEARCH_USER_ID": args.user_id.strip(),
                           "RESEARCH_PROJECT_ID": args.project_id.strip()})
        freeze_json(identity, {
            "user_id": args.user_id.strip(), "project_id": args.project_id.strip(),
            "code_sha256": {name: digest(ROOT / name) for name in (
                "run_research_cosql.py", "research_cosql.py", "run_answer_submission_pilot.py",
                "answer_submission_mcp.py", "answer_submission_eval101.py", "run_autonomous_db_pilot.py",
                "harness_sdk.py", "pilot_terminal_budget.mjs", "run_harness_baseline.py", "record_research_usage.py",
            )},
            "dsh_cli_sha256": digest(runtime["cli"]),
        })
        started = time.monotonic()
        when = datetime.now().astimezone().isoformat(timespec="seconds")
        failed_shards = {}
        try:
            failed_shards = asyncio.run(run_shards(configs, args.workers, args.resume))
        finally:
            summary = summarize(output, configs, plan)
            append_jsonl(output / "launches.jsonl", {"started_at": when, "workers": args.workers,
                         "elapsed_sec": round(time.monotonic() - started, 3), "shard_errors": failed_shards})
        if failed_shards or summary["completed_dialogues"] != summary["planned_dialogues"]:
            parser.exit(1, f"Research batch incomplete. See {output / 'summary.json'}.\n")
        print(f"Research batch complete. See {output / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
