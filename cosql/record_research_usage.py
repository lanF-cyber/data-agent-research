"""Preview or import CoSQL's existing DSH usage into a research ledger."""

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from harness_sdk import API_KEY_REF, BASE_URL, MODEL, PROVIDER


UPSTREAM = Path(__file__).resolve().parent / "upstream"
sys.path[:0] = [str(UPSTREAM / "research/llm/src"), str(UPSTREAM / "agent-session-analyzer/src")]

from agent_session_analyzer import DshAdapter
from research_llm import TokenUsage, UsageRecorder, load_prices
from research_llm.pricing import estimate_cost, select_rates
from research_llm.traces import summarize_calls


def billable_usage(raw):
    """Pi-ai reports uncached input separately, while research expects all input."""
    uncached = raw.get("input_tokens")
    read, write = raw.get("cache_read_tokens"), raw.get("cache_write_tokens")
    if type(uncached) is int and all(value is None or type(value) is int for value in (read, write)):
        # The DSH pi-ai adapter omits cache fields when their count is zero.
        read, write = read or 0, write or 0
        combined_input = uncached + read + write
    else:
        combined_input = None
    return TokenUsage(input_tokens=combined_input, output_tokens=raw.get("output_tokens"),
                      total_tokens=raw.get("total_tokens"), cache_read_tokens=read,
                      cache_write_tokens=write, reasoning_tokens=raw.get("reasoning_tokens"))


def attempts(run_dir):
    adapters = {}
    paths = [*sorted((run_dir / "attempts").glob("*.jsonl")), run_dir / "turns.jsonl"]
    for path in paths:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                turn = json.loads(line)
                session = turn["session_id"]
                if session not in adapters:
                    adapters[session] = DshAdapter(session)
                for native in turn["events"]:
                    row = adapters[session].feed(native)
                    if row is None or row["event"] != "model.attempt":
                        continue
                    model = row["model"]
                    provenance = (native["data"].get("message") or {}).get("source") or {}
                    if (provenance.get("provider", model.get("provider")),
                            provenance.get("model", model.get("model"))) != (PROVIDER, MODEL):
                        raise ValueError(f"Unexpected model route in turn {turn['id']}")
                    timestamp = datetime.fromtimestamp(row["time_ms"] / 1000, UTC)
                    yield turn["id"], row, timestamp, billable_usage(row["data"]["usage"])


def review(run_dir, *, prices_path=None, recorder=None):
    _, rates = load_prices(prices_path)[MODEL]
    costs = []
    counts = Counter()
    status_counts = Counter()
    for task, event, timestamp, usage in attempts(run_dir):
        status = event["data"]["status"]
        status_counts[status] += 1
        for name, value in vars(usage).items():
            if type(value) is int:
                counts[name] += value
        if recorder is None:
            selected, _, _ = select_rates(rates, timestamp, usage=usage)
            amount, _ = estimate_cost(usage, selected)
            costs.append({"estimated_cost": str(amount) if amount is not None else None,
                          "currency": rates.currency})
            continue
        call_id = "cosql-dsh-" + hashlib.sha256(event["event_id"].encode()).hexdigest()
        costs.append(recorder.record_usage(
            model=MODEL, api_key_env=API_KEY_REF, base_url=BASE_URL, response_model=MODEL,
            started_at=timestamp.isoformat(), finished_at=timestamp.isoformat(),
            time_source="trace_event", source="dsh", call_id=call_id, usage=usage,
            status="completed" if status == "committed" else status, source_status=status,
            metadata={"experiment_name": run_dir.name, "task_id": task,
                      "session_id": event["session_id"], "event_id": event["event_id"]},
        ))
    return {"run": run_dir.name, **summarize_calls(costs), "attempt_status": dict(status_counts),
            "reported_tokens_by_kind": dict(counts),
            "ledger": str(recorder.ledger_path) if recorder is not None else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--ledger", type=Path, help="Omit to preview only")
    parser.add_argument("--project-id", default="cosql")
    parser.add_argument("--user-id", help="User to attribute the ledger entries to")
    parser.add_argument("--prices", type=Path, help="Override the checked-out research price table")
    args = parser.parse_args()
    if args.ledger is not None and not args.user_id:
        parser.error("--ledger requires --user-id")
    if not (args.run_dir / "turns.jsonl").is_file():
        parser.error("The run has no saved model turns")
    recorder = None if args.ledger is None else UsageRecorder(
        project_id=args.project_id, user_id=args.user_id, ledger_path=args.ledger, prices_path=args.prices,
    )
    print(json.dumps(review(args.run_dir, prices_path=args.prices, recorder=recorder), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
