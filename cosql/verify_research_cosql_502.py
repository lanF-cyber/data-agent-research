"""Replay the completed Research experiment locally and export the submission evidence."""

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from statistics import median

from research_cosql import TOOLS
from research_cosql_502 import OPENING_RULES, check_alternatives, configure_mcp
from run_research_cosql_502 import BASELINE, OUT, OUT_101, prepare_package
from answer_submission_eval101 import score_answer
from answer_submission_mcp import check_submission
from run_answer_submission_pilot import answer_contract, grade_submission
from run_autonomous_db_pilot import DEV, digest, write_records
from run_harness_baseline import DATA, evaluate, read_jsonl
from run_harness_dialogue_comparison import encode_sqlite_value
from harness_sdk import MODEL, PROVIDER
from utils.json_io import write_json


def normalized(value):
    return json.loads(json.dumps(value, default=encode_sqlite_value))


def trial_paths(schedule):
    trials, manifests = {}, []
    for shard in schedule["shards"]:
        root = Path(shard["result_path"])
        manifests.append(json.loads((root / "manifest.json").read_bytes()))
        for path in root.glob("trials/*/result.json"):
            result = json.loads(path.read_bytes())
            if result["status"] != "completed" or result["task_id"] in trials:
                raise ValueError(f"The Research trial is incomplete or duplicated: {path}")
            trials[result["task_id"]] = path.parent
    return trials, manifests


def audit_turns(plan, expected, paths):
    development = json.loads(DEV.read_bytes())
    all_sessions = set()
    for dialogue in plan["dialogues"]:
        index, database = dialogue["dev_index"], dialogue["database"]
        trial = paths[f"dev{index}"]
        db = trial / "task" / f"{database}.sqlite"
        if digest(db) != dialogue["sha256"]:
            raise ValueError(f"Trial database changed: {trial}")
        raw = read_jsonl(trial / "agent" / "turns.jsonl")
        graded = read_jsonl(trial / "verifier" / "turns.jsonl")
        if len(raw) != len(graded) or len(raw) != len(dialogue["turns"]):
            raise ValueError(f"Missing questions in trial: {trial}")
        sessions = {r["session_id"] for r in raw}
        if len(sessions) != 1 or all_sessions & sessions:
            raise ValueError(f"Dialogues must use independent sessions: {trial}")
        all_sessions.update(sessions)
        for item, stored, case in zip(raw, graded, dialogue["turns"], strict=True):
            key = case["id"]
            source = development[index]["interaction"][case["turn"] - 1]
            original = source["utterance"]
            assert stored == expected[key] and item["id"] == key and case["question"] == original, key
            assert item["harness_turn"] == case["turn"] and "error" not in item, key
            assert all(field not in item for field in ("grade", "issue", "answer_fields", "acceptable_results")), key
            if case["turn"] == 1:
                assert item["original_question"] == original and item["prompt"].endswith(original), key
                assert OPENING_RULES in item["prompt"], key
            else:
                assert item["prompt"] == original, key
            for event in item["events"]:
                if event["type"] == "assistant/message":
                    route = event["data"]["message"]["source"]
                    assert (route["model"], route["provider"]) == (MODEL, PROVIDER), key
                if event["type"] == "request/header":
                    assert {tool["name"] for tool in event["data"]["header"].get("tools", [])} == TOOLS, key
            grade, receipt = grade_submission({**item, **case}, source["query"], db)
            assert normalized(grade) == stored["grade"] and normalized(receipt) == stored["submission"], key
            if "issue" in case:
                assert check_alternatives(stored) == stored["alternative_match"], key
    return len(all_sessions)


def verify_usage(output, summary):
    usage = read_jsonl(output / "usage.jsonl")
    assert len(usage) == len({r["call_id"] for r in usage}) == summary["research_usage"]["calls"]
    assert sum(row["total_tokens"] or 0 for row in usage) == summary["tokens"]["total_tokens"]
    cost = sum(Decimal(r["estimated_cost"]) for r in usage if r["estimated_cost"] is not None)
    assert cost == Decimal(summary["research_usage"]["estimated_totals"]["CNY"])
    ledger = OUT.parents[1] / "upstream" / "research" / "logs" / "model-usage.sqlite3"
    with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        columns = ("call_id", "project_id", "user_id", "status", "estimated_cost", "total_tokens")
        for row in usage:
            hit = db.execute(f"SELECT {', '.join(columns)} FROM model_calls WHERE call_id = ?", (row["call_id"],)).fetchone()
            assert dict(hit) == {key: row[key] for key in columns}, row["call_id"]
    return {"entries_checked": len(usage), "statuses": dict(Counter(r["status"] for r in usage)),
            "estimated_cny": str(cost)}


def compare(rows):
    clear = [r for r in rows if r["category"] == "primary"]
    groups = defaultdict(list)
    for row in rows:
        groups[row["dev_index"]].append(row)
    return {
        "all_questions": len(rows),
        "strict_old": sum(r["old_strict"] for r in rows),
        "strict_new": sum(r["grade"]["matched"] for r in rows),
        "strict_dialogues_old": sum(all(r["old_strict"] for r in group) for group in groups.values()),
        "strict_dialogues_new": sum(all(r["grade"]["matched"] for r in group) for group in groups.values()),
        "clear_questions": len(clear),
        "clear_old": sum(r["old_core_correct"] for r in clear),
        "clear_new": sum(r["submission"]["core"]["matched"] for r in clear),
        "clear_recovered": [r["id"] for r in clear if r["submission"]["core"]["matched"] and not r["old_core_correct"]],
        "clear_regressed": [r["id"] for r in clear if r["old_core_correct"] and not r["submission"]["core"]["matched"]],
        "delivery": dict(Counter(r["submission"]["delivery_source"] if r["submission"]["verified"]
                                 else "unverified" for r in rows)),
    }


def latency(values):
    ordered = sorted(values)
    return {"median_sec": round(median(ordered), 3), "p90_sec": round(ordered[math.ceil(len(ordered) * .9) - 1], 3)}


def export_predictions(path, rows, verdicts):
    records = []
    for row in rows:
        verdict = verdicts.get(row["id"])
        if verdict is None and row.get("alternative_match") == "all":
            verdict = {"verdict": "appropriate", "reason": "结果与冻结的独立数据库查询一致"}
        records.append({
            **{key: row[key] for key in ("id", "dev_index", "turn", "database", "question", "answer")},
            "sql": row["grade"]["predicted_sql"], "display": row["submission"]["user_answer"],
            "delivery": row["submission"]["delivery_source"], "verified_receipt": row["submission"]["verified"],
            "strict_match": row["grade"]["matched"],
            "clear_match": row["submission"]["core"]["matched"] if row["category"] == "primary" else None,
            "issue": row.get("issue"), "issue_review": {k: verdict[k] for k in ("verdict", "reason")} if verdict else None,
        })
    write_records(path, records)


def verify_predictions(path):
    plan = prepare_package()
    dev = json.loads(DEV.read_bytes())
    cases = {case["id"]: (dialogue, case) for dialogue in plan["dialogues"] for case in dialogue["turns"]}
    baseline = {row["id"]: row["grade"] for row in read_jsonl(BASELINE / "turns.jsonl")}
    rows = read_jsonl(path)
    assert [row["id"] for row in rows] == list(cases)
    totals = Counter()
    for row in rows:
        dialogue, case = cases[row["id"]]
        index, database = dialogue["dev_index"], dialogue["database"]
        assert row["question"] == case["question"] and row["database"] == database
        assert row["dev_index"] == index and row["turn"] == case["turn"]
        reference = dev[index]["interaction"][case["turn"] - 1]["query"]
        old = baseline[row["id"]]
        old_grade = evaluate({"database": database}, reference,
                             {"raw_reply": old["predicted_sql"], "finish": "completed"})
        assert old_grade["matched"] == old["matched"], row["id"]
        totals["baseline_strict_correct"] += old_grade["matched"]
        grade = evaluate({"database": database}, reference,
                         {"raw_reply": row["sql"], "finish": "completed"}, include_results=True)
        assert grade["matched"] == row["strict_match"], row["id"]
        totals["strict_correct"] += grade["matched"]
        display = row["display"]
        if row["verified_receipt"]:
            db = DATA / "database" / database / f"{database}.sqlite"
            verified = check_submission(db, row["sql"], display["columns"])
            assert verified["accepted"] and verified["rows"] == display["rows"], row["id"]
            totals["verified_receipts"] += 1
        else:
            assert display is None, row["id"]
        if case["category"] == "primary":
            core = score_answer(case, display, grade["reference_result"], reference, answer_contract)
            assert core["matched"] == row["clear_match"], row["id"]
            totals["clear_correct"] += core["matched"]
    print(json.dumps({"turns_checked": len(rows), **totals}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, help="Verify an exported predictions file on the original databases")
    args = parser.parse_args()
    configure_mcp()
    if args.predictions:
        verify_predictions(args.predictions)
        return
    output = OUT / "research"
    summary = json.loads((output / "summary.json").read_bytes())
    if (summary["completed_dialogues"], summary["completed_turns"]) != (143, 502):
        raise ValueError("The 502-turn experiment is still incomplete")
    if summary["issue_handling"]["accuracy"] is None or summary["issue_handling"]["unnecessary_clarification"]["rate"] is None:
        raise ValueError("The issue review is still incomplete. Finish review and run summary")
    plan = prepare_package()
    schedule = json.loads((output / "plan.json").read_bytes())
    rows = read_jsonl(output / "turns.jsonl")
    by_id = {row["id"]: row for row in rows}
    assert len(by_id) == len(rows) == 502
    trials, manifests = trial_paths(schedule)
    assert len(trials) == 143
    assert audit_turns(plan, by_id, trials) == len(trials)
    ledger = verify_usage(output, summary)
    all_metrics = compare(rows)
    assert all_metrics["strict_new"] == summary["strict_execution"]["new_correct"]
    assert all_metrics["clear_new"] == summary["primary"]["new_correct"]
    previous = json.loads((OUT_101 / "plan.json").read_bytes())
    used_for_design = {case["id"] for d in previous["dialogues"] for case in d["turns"]}
    old = read_jsonl(BASELINE / "turns.jsonl")
    verdicts = {row["id"]: row for row in read_jsonl(output / "review-decisions.jsonl")}
    export_predictions(output / "predictions.jsonl", rows, verdicts)
    manifest = {
        "dialogues": len(trials), "questions": len(rows), "verbatim_followups": len(rows) - len(trials),
        "started_at": min(m["started_at"] for m in manifests), "finished_at": max(m["finished_at"] for m in manifests),
        "launches": read_jsonl(output / "launches.jsonl"), "ledger": ledger,
        "metrics": {"whole_sample": all_metrics,
                    "earlier_101_used_in_design": compare([r for r in rows if r["id"] in used_for_design]),
                    "other_401": compare([r for r in rows if r["id"] not in used_for_design])},
        "per_turn_latency": {"old": latency([r["elapsed_seconds"] for r in old]),
                             "new": latency([r["seconds"] for r in rows])},
        "old_recorded_tokens": sum(r["usage"].get("totalTokens", 0) for r in old),
        "new_terminal_calls": sum(event["type"] == "tool/call" and event["data"]["name"] == "pwsh"
                                  for r in rows for event in r["events"]),
        "issue_reviews_from_reading_responses": len(verdicts),
        "issue_reviews_by_database_match": sum(r.get("alternative_match") == "all" and r["id"] not in verdicts for r in rows),
        "sha256": {name: digest(output / name) for name in (
            "summary.json", "turns.jsonl", "usage.jsonl", "review-decisions.jsonl", "predictions.jsonl",
        )},
    }
    write_json(output / "verification.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
