"""Prepare, launch, and score the paired 502-turn CoSQL Research experiment."""

import argparse
import asyncio
import json
import os
import shutil
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from research_cosql_502 import OPENING_RULES, ROW_LIMIT
from research_cosql import API_KEY_REF, FORMAT, ROOT, local_runtime, load_dsh_key
from answer_submission_eval101 import CONTRACTS, answer_fields, column_key, score_answer
from run_answer_submission_pilot import OUT_101, MATERIALIZED_RULES, answer_contract
from run_autonomous_db_pilot import DEV, digest, write_records
from run_harness_baseline import DATA, execute_sql, postprocess, read_jsonl
from run_research_cosql import (
    CONFIGS, REVISIONS, SHARDS, freeze_json, run_shards, summarize, verify_upstream,
)
from evaluation.config import experiment_directory, load_config
from evaluation.dataset import load_tasks
from evaluation.experiment import experiment_lock
from harness_sdk import MODEL, PROVIDER
from utils.json_io import append_jsonl, write_json


OUT = ROOT / "reports" / "2026-11-20_gpt6sol_answer_mcp_502"
BASELINE = ROOT / "reports" / "2026-11-19_continuous_expanded_v2"
AUDIT = ROOT / "reports" / "2026-11-19_attribution_v2"
PROFILE = ROOT / "research_config_502"
NAME = "mcp-502"
EXPECTED_SIZE = (143, 502)
RUBRIC = {
    "ambiguous": "指出具体歧义并追问，或明确说出采用的合理理解并提供符合该理解的数据库结果。返回全部有依据的并列答案也可以通过。",
    "context": "说明缺少哪个对象或范围并追问，或用可见对话和数据库证据明确补足指代后再作答。",
    "premise": "具体指出题目哪项前提与库中事实不符，并给出查询证据或提出相关澄清。",
    "reference": "按题目和库里的事实回答。即使参考 SQL 有问题，模型也可以通过给出有数据库证据的正确结果得分。",
}


def prepare_package(*, refresh=False):
    source_plan = json.loads((BASELINE / "plan.json").read_bytes())
    preflight = json.loads((AUDIT / "final_summary.json").read_bytes())
    verified = {key.replace("\\", "/"): value for key, value in preflight["sources_sha256"].items()}
    for path in (BASELINE / "turns.jsonl", DEV):
        if digest(path) != verified[path.relative_to(ROOT).as_posix()]:
            raise ValueError(f"Previously verified source changed: {path}")
    if ((source_plan["model"], source_plan["provider"]) != (MODEL, PROVIDER)
            or source_plan["dev_sha256"] != digest(DEV)):
        raise ValueError("The historical comparison does not match the model and dev dataset")
    old_rows = read_jsonl(BASELINE / "turns.jsonl")
    old = {row["id"]: row for row in old_rows}
    indices = source_plan["dialogue_indices"]
    if ((len(set(indices)), len(old)) != EXPECTED_SIZE or len(old_rows) != len(old)
            or sum(row["grade"]["matched"] for row in old_rows) != 343):
        raise ValueError("Expected the original 143-dialogue, 502-turn comparison")

    previous = json.loads((OUT_101 / "plan.json").read_bytes())
    annotation = PROFILE / "issue_review.json"
    review = json.loads(annotation.read_bytes())
    exclusions = dict(previous["exclusions"])
    if set(exclusions) & review["additional_issues"].keys():
        raise ValueError("The expansion review repeats an already labeled turn")
    exclusions.update(review["additional_issues"])
    if exclusions.keys() - old.keys() or review["acceptable_queries"].keys() - exclusions.keys():
        raise ValueError("A review label is outside the frozen sample or has no issue to review")
    hashes = {item["database"]: item["sha256"] for item in previous["dialogues"]}
    prior_turns = {item["id"]: item for dialogue in previous["dialogues"] for item in dialogue["turns"]}
    if (len(hashes) != 20 or len(prior_turns) != 101 or prior_turns.keys() - old.keys()
            or previous["dev_sha256"] != source_plan["dev_sha256"]):
        raise ValueError("The 101-turn audit and the previous 502-turn sample no longer align")
    for database, expected in hashes.items():
        if digest(DATA / "database" / database / f"{database}.sqlite") != expected:
            raise ValueError(f"SQLite file changed since the 101-turn run: {database}")

    dev = json.loads(DEV.read_bytes())
    dialogues, keys = [], set()
    for index in indices:
        item = dev[index]
        database = item["database_id"]
        dialogue = {"dev_index": index, "database": database, "sha256": hashes[database], "turns": []}
        for turn, raw in enumerate(item["interaction"], 1):
            key = f"dev{index}_turn{turn}"
            keys.add(key)
            row = old[key]
            grade = row["grade"]
            if (row["dev_index"], row["turn"], row["database"], grade["question"], grade["reference_sql"]) != (
                index, turn, database, raw["utterance"], raw["query"],
            ):
                raise ValueError(f"Question or reference changed: {key}")
            columns, rows = execute_sql(database, postprocess(raw["query"]))
            if grade["reference_result"] != {"columns": columns, "rows": [list(record) for record in rows]}:
                raise ValueError(f"Historical reference result did not reproduce: {key}")
            if len(rows) > ROW_LIMIT:
                raise ValueError(f"Known reference table exceeds the expanded MCP limit: {key}")
            case = {
                "id": key, "turn": turn, "question": raw["utterance"],
                "category": "excluded" if key in exclusions else "primary",
                "contract": CONTRACTS.get(key, "rows"), "old_strict": grade["matched"],
                "reference_row_count": len(rows), "answer_fields": (
                    prior_turns[key]["answer_fields"] if key in prior_turns else answer_fields(row)
                ),
            }
            case["old_core_correct"] = score_answer(
                case, grade.get("predicted_result"), grade["reference_result"], raw["query"], answer_contract,
            )["matched"]
            if key in exclusions:
                kind, reason = exclusions[key]
                case["issue"] = {"kind": kind, "reason": reason, "acceptable_behavior": RUBRIC[kind]}
            if (rule := review["acceptable_queries"].get(key)) is not None:
                alt_columns, alt_rows = execute_sql(database, rule["sql"])
                if len(alt_columns) != 1 or not alt_rows or (rule["allow_one"] and len(alt_rows) < 2):
                    raise ValueError(f"Independent answer evidence did not reproduce: {key}")
                case["acceptable_results"] = {**rule, "rows": [list(record) for record in alt_rows],
                                              "aliases": sorted(set(map(column_key, rule["aliases"])))}
            dialogue["turns"].append(case)
        dialogues.append(dialogue)
    if keys != set(old):
        raise ValueError("The Research plan differs from the old 502 questions")

    plan = {
        "model": MODEL, "provider": PROVIDER, "protocol": NAME, "dev_sha256": digest(DEV),
        "sources_sha256": {path.relative_to(ROOT).as_posix(): digest(path) for path in (
            BASELINE / "plan.json", BASELINE / "turns.jsonl", OUT_101 / "plan.json", annotation,
        )},
        "exclusions": exclusions, "dialogues": dialogues,
        "opening_rules": [MATERIALIZED_RULES, OPENING_RULES], "mcp_row_limit": ROW_LIMIT,
    }
    package = OUT / "research-package"
    path = package / "plan.json"
    save = write_json if refresh else freeze_json
    save(path, plan)
    save(package / "dataset.json", {
        "schema_version": 1, "id": "cosql-dev-502", "version": "1", "task_format": FORMAT,
    })
    save(package / "index.json", {
        "schema_version": 1, "plan": path.name, "plan_sha256": digest(path),
        "task_count": len(dialogues),
        "tasks": [{"id": f"dev{row['dev_index']}", "dev_index": row["dev_index"]} for row in dialogues],
    })
    return plan


def configuration():
    return load_config(name=NAME, environment=CONFIGS / "environment.toml",
                       **{key: PROFILE / f"{key}.toml" for key in ("dataset", "agent", "evaluation")})


def partition(config):
    tasks = load_tasks(config["dataset"])
    groups = [[] for _ in range(SHARDS)]
    counts = [0] * SHARDS
    for task in sorted(tasks, key=lambda item: -item.metadata["turns"]):
        slot = counts.index(min(counts))
        groups[slot].append(task.id)
        counts[slot] += task.metadata["turns"]
    if (len(tasks), sum(counts)) != EXPECTED_SIZE:
        raise ValueError("Research must schedule all 502 questions in their complete dialogues")
    return [{**config, "name": f"{NAME}-{slot:02d}", "dataset": {**config["dataset"], "task_ids": ids}}
            for slot, ids in enumerate(groups, 1)]


def summarize_issues(output, rows, planned):
    verdict_path = output / "review-decisions.jsonl"
    records = read_jsonl(verdict_path) if verdict_path.exists() else []
    verdicts = {row["id"]: row for row in records}
    if len(verdicts) != len(records):
        raise ValueError("Review decisions must mention each question only once")
    pending, reviewed, false_alarms, seen = [], Counter(), 0, set()
    # Store only the visible conversation so reviewers can judge the answer in context.
    history = {}
    for row in rows:
        prior = history.setdefault(row["dev_index"], [])
        issued = row.get("issue")
        if issued or not row["submission"]["verified"]:
            review = verdicts.get(row["id"])
            allowed = {"appropriate", "inappropriate"} if issued else {"unnecessary_clarification", "ordinary_failure"}
            if review is not None:
                if (review["session_id"] != row["session_id"] or review["verdict"] not in allowed
                        or not isinstance(review["reason"], str) or not review["reason"].strip()):
                    raise ValueError(f"Invalid verdict or stale response: {row['id']}")
                seen.add(row["id"])
                outcome = review["verdict"]
            elif issued and row.get("alternative_match") == "all":
                outcome = "appropriate"
            else:
                outcome = "pending"
                pending.append({
                    "id": row["id"], "session_id": row["session_id"], "database": row["database"],
                    "visible_history": prior.copy(), "question": row["question"], "issue": issued,
                    "answer": row["answer"], "display": row["submission"]["user_answer"],
                    "alternative_match": row.get("alternative_match"), "verdict_options": sorted(allowed),
                })
            if issued:
                reviewed[(issued["kind"], outcome)] += 1
            elif outcome == "unnecessary_clarification":
                false_alarms += 1
        prior.append({"question": row["question"], "answer": row["answer"],
                      "display": row["submission"]["user_answer"]})
    if set(verdicts) != seen:
        raise ValueError("A review decision does not match an available result in this run")
    if rows:
        write_records(output / "review-pending.jsonl", pending)

    by_kind = {}
    for kind in RUBRIC:
        total = planned[kind]
        success, fail = reviewed[(kind, "appropriate")], reviewed[(kind, "inappropriate")]
        by_kind[kind] = {"planned": total, "reviewed": success + fail, "appropriate": success,
                         "pending_review": reviewed[(kind, "pending")],
                         "accuracy": success / total if total and success + fail == total else None}
    reviewed_total = sum(group["reviewed"] for group in by_kind.values())
    successes = sum(group["appropriate"] for group in by_kind.values())
    total = planned.total()
    visible = {key: sum(group[key] for kind, group in by_kind.items() if kind != "reference")
               for key in ("planned", "reviewed", "appropriate", "pending_review")}
    visible["accuracy"] = (visible["appropriate"] / visible["planned"]
                           if visible["planned"] and visible["reviewed"] == visible["planned"] else None)
    clean_planned = EXPECTED_SIZE[1] - total
    clean_finished = sum(row["category"] == "primary" for row in rows)
    pending_normal = sum(row["issue"] is None for row in pending)
    return {
        "planned": total, "reviewed": reviewed_total, "appropriate": successes,
        "accuracy": successes / total if total and reviewed_total == total else None,
        "by_kind": by_kind,
        "visible_issue_handling": visible,
        "unnecessary_clarification": {"count": false_alarms, "pending_review": pending_normal,
                                      "planned_clear_questions": clean_planned,
                                      "rate": false_alarms / clean_planned
                                      if clean_finished == clean_planned and not pending_normal else None},
    }


def summarize_batch(output, shards, plan):
    results = summarize(output, shards, plan)
    rows = read_jsonl(output / "turns.jsonl") if results["completed_turns"] else []
    results["issue_handling"] = summarize_issues(output, rows, Counter(kind for kind, _ in plan["exclusions"].values()))
    results["large_reference_tables"] = sum(turn["reference_row_count"] > 200 for dialogue in plan["dialogues"]
                                           for turn in dialogue["turns"])
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
    with experiment_lock(OUT / "research-launcher"):
        staged_output = OUT / "research"
        refresh = (args.stage == "prepare" and not (staged_output / "run-identity.json").exists()
                   and not any(staged_output.glob("**/manifest.json")))
        plan = prepare_package(refresh=refresh)
        config = configuration()
        shards = partition(config)
        output = Path(config["output_dir"])
        save = write_json if refresh else freeze_json
        save(output / "plan.json", {
            "name": NAME, "dialogues": EXPECTED_SIZE[0], "turns": EXPECTED_SIZE[1],
            "upstream_revisions": REVISIONS, "research_config": config,
            "shards": [{"name": item["name"], "task_ids": item["dataset"]["task_ids"],
                        "result_path": str(experiment_directory(item))} for item in shards],
        })
        if args.stage != "run":
            print(json.dumps(summarize_batch(output, shards, plan), ensure_ascii=False, indent=2), flush=True)
            return

        identity = output / "run-identity.json"
        if any(experiment_directory(item).is_dir() for item in shards) and not args.resume:
            parser.error("This Research batch has started. Use --resume to continue it")
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
                "run_research_cosql_502.py", "research_cosql_502.py", "answer_submission_mcp_502.py",
                "run_research_cosql.py", "research_cosql.py", "run_answer_submission_pilot.py",
                "answer_submission_mcp.py", "answer_submission_eval101.py", "run_autonomous_db_pilot.py",
                "harness_sdk.py", "pilot_terminal_budget.mjs", "run_harness_baseline.py", "record_research_usage.py",
            )},
            "dsh_cli_sha256": digest(runtime["cli"]),
        })
        started = time.monotonic()
        when = datetime.now().astimezone().isoformat(timespec="seconds")
        errors = {}
        try:
            errors = asyncio.run(run_shards(shards, args.workers, args.resume))
        finally:
            result = summarize_batch(output, shards, plan)
            append_jsonl(output / "launches.jsonl", {"started_at": when, "workers": args.workers,
                         "elapsed_sec": round(time.monotonic() - started, 3), "shard_errors": errors})
        if errors or result["completed_turns"] != EXPECTED_SIZE[1]:
            parser.exit(1, f"Research batch incomplete. See {output / 'summary.json'}.\n")
        print(f"Research batch complete. See {output / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
