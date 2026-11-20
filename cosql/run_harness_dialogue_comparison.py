"""Replay complete CoSQL dialogues in persistent sessions and compare paired scores."""

import argparse
import asyncio
import hashlib
import json
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from harness_sdk import Harness, MODEL, PROVIDER, diagnostic
from run_harness_baseline import ROOT, evaluate, manifest_text, read_jsonl, summarize, write_text


DEFAULT_BASELINE = ROOT / "reports" / "2026-11-18_gpt6sol_harness_baseline"
# These previously reviewed dialogues exercise references to earlier people,
# animals, films and properties. Reuse the same complete dialogue in both arms.
DIALOGUES = (268, 98, 282, 85, 30, 91)
FEEDBACK_ROW_LIMIT = 100


def encode_sqlite_value(value):
    if isinstance(value, bytes):
        return {"sqlite_blob_hex": value.hex()}
    raise TypeError(f"Cannot encode {type(value).__name__}")


def cohort(baseline):
    samples = defaultdict(list)
    for item in read_jsonl(baseline / "prompts.jsonl"):
        if item["dev_index"] in DIALOGUES:
            samples[item["dev_index"]].append(item)
    for index in DIALOGUES:
        samples[index].sort(key=lambda item: item["turn"])
        if not samples[index] or [item["turn"] for item in samples[index]] != list(
            range(1, len(samples[index]) + 1)
        ):
            raise ValueError(f"Incomplete baseline dialogue {index}")
    scores = {row["id"]: row for row in read_jsonl(baseline / "scores.jsonl")}
    baseline_responses = {row["id"]: row for row in read_jsonl(baseline / "responses.jsonl")}
    for case in (item for index in DIALOGUES for item in samples[index]):
        row = scores[case["id"]]
        source = baseline_responses[case["id"]]["source"]
        if type(row["matched"]) is not bool or (source["provider"], source["model"]) != (PROVIDER, MODEL):
            raise ValueError(f"Missing or incompatible baseline score for {case['id']}")
    return samples, scores


def last_result(grade):
    if "predicted_result" not in grade:
        return {"status": "未得到可执行的查询结果", "reason": grade.get("detail", grade["reason"])}
    result = grade["predicted_result"]
    return {"columns": result["columns"], "rows": result["rows"][:FEEDBACK_ROW_LIMIT],
            "row_count": len(result["rows"]),
            "shown_rows": min(len(result["rows"]), FEEDBACK_ROW_LIMIT)}


def next_prompt(sample, previous):
    if previous is None:
        return sample["prompt"]
    # The model can see its previous SQL through the Harness session. The
    # extra text is only what its SQL actually returned from this database.
    feedback = json.dumps(last_result(previous), ensure_ascii=False, default=encode_sqlite_value)
    return (
        f"上一轮 SQL 的实际执行结果\n{feedback}\n\n"
        f"本轮用户问题\n{sample['question']}\n\n"
        "请生成一条只读 SQLite SQL，回答本轮问题。只输出 SQL。"
    )


def completed_dialogues(records, samples):
    by_session = defaultdict(list)
    for entry in records:
        by_session[entry["session_id"]].append(entry)
    completed = {}
    for entries in by_session.values():
        index = entries[0]["dev_index"]
        expected = [item["id"] for item in samples[index]]
        if [entry["id"] for entry in entries] != expected:
            continue
        if not all(type(entry.get("grade", {}).get("matched")) is bool for entry in entries):
            continue
        if [entry["harness_turn"] for entry in entries] != list(range(1, len(expected) + 1)):
            raise RuntimeError(f"Harness used unexpected session turns for dialogue {index}")
        if index in completed:
            raise RuntimeError(f"Multiple complete attempts for dialogue {index}")
        completed[index] = entries
    return completed


def write_summary(output, records, samples, baseline_scores):
    completed = completed_dialogues(records, samples)
    paired = []
    for index in DIALOGUES:
        for entry in completed.get(index, []):
            baseline = baseline_scores[entry["id"]]
            paired.append({"id": entry["id"], "dev_index": index,
                           "turn": entry["turn"], "database": entry["database"],
                           "baseline_matched": baseline["matched"],
                           "continuous_matched": entry["grade"]["matched"]})
    first = [row for row in paired if row["turn"] == 1]
    followups = [row for row in paired if row["turn"] > 1]
    baseline = [baseline_scores[row["id"]] for row in paired]
    continuous = [entry["grade"] for index in DIALOGUES for entry in completed.get(index, [])]
    summary = {
        "planned_dialogues": len(samples),
        "planned_turns": sum(map(len, samples.values())),
        "completed_dialogues": list(completed),
        "baseline": summarize(baseline),
        "continuous": summarize(continuous),
        "first_turn": paired_counts(first),
        "followup_turns": paired_counts(followups),
        "all_turns": paired_counts(paired),
        "turns": paired,
    }
    write_text(output / "summary.json", manifest_text(summary))
    return summary


def paired_counts(rows):
    return {
        "turns": len(rows),
        "baseline_passes": sum(row["baseline_matched"] for row in rows),
        "continuous_passes": sum(row["continuous_matched"] for row in rows),
        "baseline_only": sum(row["baseline_matched"] and not row["continuous_matched"] for row in rows),
        "continuous_only": sum(row["continuous_matched"] and not row["baseline_matched"] for row in rows),
    }


async def run_dialogues(args, output, records, samples, references, pending, *, error_limit=None):
    gate = asyncio.Semaphore(args.workers)
    errors = 0
    with tempfile.TemporaryDirectory(prefix=".harness-", dir=output) as temp_dir:
        async with Harness(args.cli, args.credentials, Path(temp_dir)) as harness:
            print(f"Harness {harness.server_info['serverInfo']} · {PROVIDER}/{MODEL}", flush=True)

            async def play(index):
                nonlocal errors
                async with gate:
                    if error_limit is not None and errors >= error_limit:
                        return
                    session_id = harness.session_id()
                    previous = None
                    for sample in samples[index]:
                        prompt = next_prompt(sample, previous)
                        entry = {"id": sample["id"], "dev_index": index, "turn": sample["turn"],
                                 "database": sample["database"], "prompt": prompt,
                                 "session_id": session_id, "started_at": datetime.now(timezone.utc).isoformat()}
                        start = time.monotonic()
                        try:
                            entry.update(await harness.predict(prompt, session_id))
                            if (entry["source"].get("provider"), entry["source"].get("model")) != (PROVIDER, MODEL):
                                raise RuntimeError("Harness routed the request to a different model")
                            if entry["harness_turn"] != sample["turn"]:
                                raise RuntimeError("Harness did not continue the expected turn")
                            previous = evaluate(sample, references[sample["id"]],
                                                entry, include_results=True)
                            entry["grade"] = previous
                        except (RuntimeError, TimeoutError, OSError) as exc:
                            errors += 1
                            entry["error"] = diagnostic(exc) or type(exc).__name__
                        entry["elapsed_seconds"] = round(time.monotonic() - start, 3)
                        records.append(entry)
                        write_text(output / "turns.jsonl", "".join(
                            json.dumps(row, ensure_ascii=False, default=encode_sqlite_value) + "\n"
                            for row in records
                        ))
                        outcome = entry.get("error", entry.get("grade", {}).get("reason"))
                        print(f"{sample['id']} turn {entry.get('harness_turn')} {outcome} "
                              f"{entry['elapsed_seconds']}s", flush=True)
                        if "error" in entry:
                            break

            await asyncio.gather(*(play(index) for index in pending))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("plan", "trial", "run"))
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cli", type=Path, help="Installed DeepSeek Harness bin.js")
    parser.add_argument("--credentials", type=Path, help="Existing DeepSeek Harness credentials store")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    args.baseline, output = args.baseline.resolve(), args.run_dir.resolve()
    samples, baseline_scores = cohort(args.baseline)
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".running"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(f"{args.stage}\n")
    try:
        plan = {
            "dataset": "CoSQL dev sql_state_tracking",
            "baseline": args.baseline.name,
            "baseline_hashes": {name: hashlib.sha256((args.baseline / name).read_bytes()).hexdigest()
                                for name in ("manifest.json", "prompts.jsonl", "responses.jsonl", "scores.jsonl")},
            "selection": "Six complete previously reviewed dialogues with context-dependent questions.",
            "dialogues": [{"dev_index": idx, "database": samples[idx][0]["database"], "turns": len(samples[idx])}
                          for idx in DIALOGUES],
            "provider": PROVIDER, "model": MODEL,
            "continuous": "One Harness session per dialogue. The first prompt is identical to baseline. "
                          "Each next user message contains only the previous SQL's actual result and "
                          "the current question. Reference SQL and result stay offline.",
            "feedback_row_limit": FEEDBACK_ROW_LIMIT,
            "scoring": "Original SQLite execution match, using the baseline evaluator.",
        }
        path = output / "plan.json"
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != plan:
            raise RuntimeError(f"Existing comparison plan differs: {path}")
        if not path.exists():
            write_text(path, manifest_text(plan))
        responses = output / "turns.jsonl"
        records = read_jsonl(responses) if responses.exists() else []
        completed = completed_dialogues(records, samples)
        targets = () if args.stage == "plan" else (DIALOGUES[:1] if args.stage == "trial" else DIALOGUES)
        pending = [idx for idx in targets if idx not in completed]
        if pending:
            if not args.cli or not args.credentials or not args.cli.is_file() or not args.credentials.is_file():
                parser.error("Specify the existing --cli and --credentials files")
            args.cli, args.credentials = args.cli.resolve(), args.credentials.resolve()
            references = {key: row["reference_sql"] for key, row in baseline_scores.items()}
            asyncio.run(run_dialogues(args, output, records, samples, references, pending))
        summary = write_summary(output, records, samples, baseline_scores)
        print(json.dumps({name: value for name, value in summary.items() if name != "turns"},
                         ensure_ascii=False, indent=2))
        return 0 if all(idx in summary["completed_dialogues"] for idx in targets) else 2
    finally:
        lock.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
