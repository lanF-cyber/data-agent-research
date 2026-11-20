"""Run ordered, persistent CoSQL dialogues at the 101- and 300-turn checkpoints."""

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

from harness_sdk import MODEL, PROVIDER
from run_harness_baseline import (
    DATA, EVAL, PILOT_DIALOGUES, ROOT, dialogue_samples, evaluate, execute_sql,
    manifest_text, postprocess, read_jsonl, sampled_dialogue_order, schema_text,
    summarize, write_text,
)
from run_harness_dialogue_comparison import (
    DEFAULT_BASELINE, FEEDBACK_ROW_LIMIT, completed_dialogues, encode_sqlite_value,
    last_result, next_prompt, paired_counts, run_dialogues,
)


CHECKPOINT = 101
TARGET = 300
DEFAULT_OUTPUT = ROOT / "reports" / "2026-11-19_gpt6sol_continuous_300"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_line(record):
    return json.dumps(record, ensure_ascii=False, default=encode_sqlite_value) + "\n"


def experiment_plan(baseline):
    dev_file = DATA / "sql_state_tracking" / "cosql_dev.json"
    tables_file = DATA / "tables.json"
    dev = json.loads(dev_file.read_bytes())
    schemas = {item["db_id"]: schema_text(item) for item in json.loads(tables_file.read_bytes())}
    manifest = json.loads((baseline / "manifest.json").read_bytes())
    files = {"dev_sha256": digest(dev_file), "tables_sha256": digest(tables_file)}
    if any(manifest[key] != value for key, value in files.items()):
        raise ValueError("The CoSQL inputs differ from the 101-turn baseline")

    checkpoint = [row["dev_index"] for row in manifest["dialogues"]]
    ordering = sampled_dialogue_order(dev)
    if ordering[:len(checkpoint)] != checkpoint:
        raise ValueError("The sample order differs from the 101-turn baseline")
    total = sum(len(dev[index]["interaction"]) for index in checkpoint)
    if total != CHECKPOINT:
        raise ValueError(f"Expected {CHECKPOINT} baseline turns, found {total}")

    indices, skipped = list(checkpoint), []
    for index in ordering[len(checkpoint):]:
        length = len(dev[index]["interaction"])
        if total + length > TARGET:
            skipped.append(index)
            continue
        indices.append(index)
        total += length
        if total == TARGET:
            break
    if total != TARGET:
        raise ValueError(f"Could not reach {TARGET} with complete dialogues")

    samples, references, dialogues = {}, {}, []
    for position, index in enumerate(indices):
        dialogue = dev[index]
        database = dialogue["database_id"]
        partition = "pilot" if position < PILOT_DIALOGUES else "extension"
        samples[index] = []
        for sample, reference in dialogue_samples(index, dialogue, schemas[database], partition):
            samples[index].append(sample)
            references[sample["id"]] = reference
        dialogues.append({"dev_index": index, "database": database, "turns": len(samples[index])})

    expected = [sample for index in checkpoint for sample in samples[index]]
    if expected != read_jsonl(baseline / "prompts.jsonl"):
        raise ValueError("The paired checkpoint has different prompts or questions")
    scores = {row["id"]: row for row in read_jsonl(baseline / "scores.jsonl")}
    responses = {row["id"]: row for row in read_jsonl(baseline / "responses.jsonl")}
    for sample in expected:
        key = sample["id"]
        source = responses[key]["source"]
        if ((source["provider"], source["model"]) != (PROVIDER, MODEL)
                or type(scores[key]["matched"]) is not bool
                or scores[key]["reference_sql"] != references[key]):
            raise ValueError(f"Invalid baseline comparison for {key}")

    databases = sorted({row["database"] for row in dialogues})
    plan = {
        "dataset": manifest["dataset"], **files, "seed": manifest["seed"],
        "excluded_dev_indices": manifest["excluded_dev_indices"],
        "checkpoint_turns": CHECKPOINT, "target_turns": TARGET,
        "checkpoint_dialogues": len(checkpoint), "dialogues": dialogues,
        "skipped_when_too_long_to_fit": skipped,
        "selection": "Continue the original seeded database round-robin order. "
                     "Keep whole dialogues and skip those longer than the remaining budget.",
        "baseline": baseline.name,
        "baseline_sha256": {name: digest(baseline / name) for name in (
            "manifest.json", "prompts.jsonl", "scores.jsonl", "responses.jsonl")},
        "sqlite_sha256": {db: digest(DATA / "database" / db / f"{db}.sqlite") for db in databases},
        "evaluator_sha256": digest(EVAL / "exec_eval.py"),
        "provider": PROVIDER, "model": MODEL,
        "feedback_row_limit": FEEDBACK_ROW_LIMIT, "metric": manifest["metric"],
        "dialogue_policy": "One Harness session per dialogue. First prompt matches the baseline. "
                           "Wait for each answer, then send its actual SQLite result and the next "
                           "question. The reference SQL and result stay local.",
    }
    return plan, samples, references, scores, checkpoint, indices


def snapshot(output, stage, indices, completed, scores):
    entries = [entry for index in indices for entry in completed.get(index, [])]
    graded = [entry["grade"] for entry in entries]
    paired = [{"id": row["id"], "baseline_matched": scores[row["id"]]["matched"],
               "continuous_matched": row["matched"]} for row in graded if row["id"] in scores]
    previous_outcomes = {"after_pass": Counter(), "after_fail": Counter()}
    feedback = Counter()
    for index in indices:
        attempts = completed.get(index, [])
        for previous, current in zip(attempts, attempts[1:]):
            grade = previous["grade"]
            context = last_result(grade)
            feedback["followups"] += 1
            feedback["query_unavailable"] += int("status" in context)
            feedback["truncated"] += int(context.get("row_count", 0) > FEEDBACK_ROW_LIMIT)
            key = "after_pass" if grade["matched"] else "after_fail"
            previous_outcomes[key]["total"] += 1
            previous_outcomes[key]["passed"] += current["grade"]["matched"]
    summary = {
        "target_turns": stage, "target_dialogues": len(indices),
        "complete": all(index in completed for index in indices),
        "combined": summarize(graded),
        "first_turn": summarize([row for row in graded if row["turn"] == 1]),
        "subsequent_turns": summarize([row for row in graded if row["turn"] > 1]),
        "added_after_101": summarize([row for row in graded if row["id"] not in scores]),
        "paired_with_baseline": paired_counts(paired),
        "score_changes": [row for row in paired if row["baseline_matched"] != row["continuous_matched"]],
        "previous_outcomes": previous_outcomes, "feedback": feedback,
        "reported_total_tokens": sum(entry["usage"].get("totalTokens", 0) for entry in entries),
    }
    failures = [entry for entry in entries if not entry["grade"]["matched"]]
    write_text(output / f"summary_{stage}.json", manifest_text(summary))
    write_text(output / f"failures_{stage}.jsonl", "".join(map(json_line, failures)))
    return summary


def verify(completed, samples, references):
    for index, entries in completed.items():
        previous = None
        for sample, entry in zip(samples[index], entries, strict=True):
            source = entry["source"]
            if ((source["provider"], source["model"]) != (PROVIDER, MODEL)
                    or entry["tool_calls"] != 0
                    or entry["prompt"] != next_prompt(sample, previous)):
                raise ValueError(f"Session, route or feedback mismatch for {sample['id']}")
            grade = evaluate(sample, references[sample["id"]], entry, include_results=True)
            if json_line(grade) != json_line(entry["grade"]):
                raise ValueError(f"Local re-scoring differs for {sample['id']}")
            previous = grade
    print(f"Verified {sum(map(len, completed.values()))} turns from {len(completed)} dialogues.",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("plan", "checkpoint", "extend", "verify"))
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--cli", type=Path, help="Installed DeepSeek Harness bin.js")
    parser.add_argument("--credentials", type=Path, help="Existing DeepSeek Harness credentials store")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    output = args.run_dir.resolve()
    plan, samples, references, scores, checkpoint, indices = experiment_plan(args.baseline.resolve())
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".running"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()} {args.stage}\n")
    try:
        plan_path = output / "plan.json"
        if plan_path.exists():
            if json.loads(plan_path.read_bytes()) != plan:
                raise ValueError(f"Existing experiment plan differs: {plan_path}")
        else:
            write_text(plan_path, manifest_text(plan))
        if args.stage == "plan":
            for dialogue in samples.values():
                for sample in dialogue:
                    execute_sql(sample["database"], postprocess(references[sample["id"]]))
            print(f"Checked all {TARGET} reference queries across {len(indices)} dialogues. "
                  f"Checkpoint: {len(checkpoint)} dialogues, {CHECKPOINT} turns.", flush=True)
            return 0

        log = output / "turns.jsonl"
        records = read_jsonl(log) if log.exists() else []
        completed = completed_dialogues(records, samples)
        if args.stage == "verify":
            verify(completed, samples, references)
        if args.stage == "extend" and any(index not in completed for index in checkpoint):
            raise ValueError("Complete the 101-turn checkpoint before extending")
        stage = CHECKPOINT if args.stage == "checkpoint" else TARGET
        selection = checkpoint if stage == CHECKPOINT else indices
        pending = [index for index in selection if index not in completed]
        if args.stage != "verify" and pending:
            if not args.cli or not args.credentials or not args.cli.is_file() or not args.credentials.is_file():
                parser.error("Specify the existing --cli and --credentials files")
            args.cli, args.credentials = args.cli.resolve(), args.credentials.resolve()
            for _ in range(2):
                asyncio.run(run_dialogues(args, output, records, samples, references, pending,
                                          error_limit=max(2, args.workers)))
                completed = completed_dialogues(records, samples)
                remaining = [index for index in pending if index not in completed]
                if not remaining or remaining == pending:
                    break
                pending = remaining
        if args.stage == "verify":
            snapshot(output, CHECKPOINT, checkpoint, completed, scores)
        summary = snapshot(output, stage, selection, completed, scores)
        print(manifest_text({key: val for key, val in summary.items() if key != "score_changes"}))
        return 0 if summary["complete"] else 2
    finally:
        lock.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
