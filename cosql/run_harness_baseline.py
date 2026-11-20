"""Sample, predict and score complete CoSQL dev dialogues on the original SQLite databases."""

import argparse
import asyncio
import hashlib
import json
import random
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

from harness_sdk import API_KEY_REF, BASE_URL, MODEL, PROVIDER, Harness, diagnostic


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "cosql_dataset"
EVAL = ROOT / "upstream" / "test-suite-sql-eval"
sys.path.insert(0, str(EVAL))
from exec_eval import postprocess, result_eq


SEED = 20261118
PILOT_DIALOGUES = 10
EXPANSION_TARGET = 100
EARLIER_PRACTICE = {0, 106}
FENCED_SQL = re.compile(r"\x60{3}(?:sql|sqlite)?[ \t]*\r?\n(.*?)\r?\n?\x60{3}", re.I | re.S)
READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


def write_text(path, text):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def json_line(value):
    return json.dumps(value, ensure_ascii=False) + "\n"


def read_jsonl(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def schema_text(schema):
    tables = schema["table_names_original"]
    columns = schema["column_names_original"]
    keys = set(schema["primary_keys"])
    by_table = defaultdict(list)
    for index, ((table, name), sql_type) in enumerate(zip(columns, schema["column_types"], strict=True)):
        if table >= 0:
            marker = " [PK]" if index in keys else ""
            by_table[table].append(f"{name} {sql_type}{marker}")
    lines = [f"{table}({', '.join(by_table[index])})" for index, table in enumerate(tables)]
    for foreign, referenced in schema["foreign_keys"]:
        source_table, source_column = columns[foreign]
        target_table, target_column = columns[referenced]
        lines.append(f"{tables[source_table]}.{source_column} -> {tables[target_table]}.{target_column}")
    return "\n".join(lines)


def sampled_dialogue_order(dev):
    groups = defaultdict(list)
    for index, dialogue in enumerate(dev):
        if index not in EARLIER_PRACTICE:
            groups[dialogue["database_id"]].append(index)
    rng = random.Random(SEED)
    databases = sorted(groups)
    rng.shuffle(databases)
    for database in databases:
        rng.shuffle(groups[database])
    return [index for batch in zip_longest(*(groups[db] for db in databases))
            for index in batch if index is not None]


def dialogue_samples(index, dialogue, schema, partition):
    database = dialogue["database_id"]
    questions = []
    for number, turn in enumerate(dialogue["interaction"], start=1):
        questions.append(f"{number}. {turn['utterance']}")
        prompt = (
            "根据下面的 SQLite 表结构和对话，生成一条只读 SQL，回答最后一轮问题。"
            "前面的提问用于理解上下文。只输出 SQL。\n\n"
            f"数据库 {database}\n{schema}\n\n"
            f"用户问题\n" + "\n".join(questions)
        )
        yield {
            "id": f"dev{index}_turn{number}", "dev_index": index, "turn": number,
            "database": database, "partition": partition, "question": turn["utterance"],
            "prompt": prompt,
        }, turn["query"]


def prepare(output):
    dev_bytes = (DATA / "sql_state_tracking" / "cosql_dev.json").read_bytes()
    tables_bytes = (DATA / "tables.json").read_bytes()
    dev = json.loads(dev_bytes)
    schemas = {table["db_id"]: schema_text(table) for table in json.loads(tables_bytes)}
    dialogues = []
    samples = []
    references = {}
    for index in sampled_dialogue_order(dev):
        dialogue = dev[index]
        database = dialogue["database_id"]
        turns = dialogue["interaction"]
        partition = "pilot" if len(dialogues) < PILOT_DIALOGUES else "extension"
        dialogues.append({"dev_index": index, "database": database,
                          "turns": len(turns), "partition": partition})
        for sample, reference in dialogue_samples(index, dialogue, schemas[database], partition):
            samples.append(sample)
            references[sample["id"]] = reference
        if len(dialogues) >= PILOT_DIALOGUES and len(samples) >= EXPANSION_TARGET:
            break
    manifest = {
        "seed": SEED, "dataset": "CoSQL sql_state_tracking dev",
        "dev_sha256": hashlib.sha256(dev_bytes).hexdigest(),
        "tables_sha256": hashlib.sha256(tables_bytes).hexdigest(),
        "sampling": "Shuffle database order and each database's dialogues, then round-robin. "
                    "Use complete dialogues until reaching at least 100 SQL turns.",
        "excluded_dev_indices": sorted(EARLIER_PRACTICE),
        "model": MODEL, "provider": PROVIDER, "base_url": BASE_URL,
        "transport": "DeepSeek Harness SDK sdk-minimal, zero tools, fresh session per turn",
        "history": "SQLite schema and user questions through the current turn",
        "metric": "original SQLite execution accuracy with official result_eq comparison, "
                  "keep DISTINCT, score values, use reference ORDER BY for row order",
        "dialogues": dialogues,
    }
    files = {
        "manifest.json": manifest_text(manifest),
        "prompts.jsonl": "".join(map(json_line, samples)),
    }
    for name, contents in files.items():
        path = output / name
        if path.exists() and path.read_text(encoding="utf-8") != contents:
            raise RuntimeError(f"Existing run plan differs: {path}")
        if not path.exists():
            write_text(path, contents)
    return samples, references


def manifest_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def execute_sql(database, statement):
    path = DATA / "database" / database / f"{database}.sqlite"
    return execute_readonly_sql(path, statement)


def execute_readonly_sql(path, statement, *, row_limit=None):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in READ_ACTIONS
                                  else sqlite3.SQLITE_DENY)
        deadline = time.monotonic() + 10
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        cursor = connection.execute(statement)
        rows = cursor.fetchall() if row_limit is None else cursor.fetchmany(row_limit + 1)
        if row_limit is not None and len(rows) > row_limit:
            raise ValueError(f"Query returns more than {row_limit} rows")
        return [column[0] for column in cursor.description], rows


def evaluate(sample, reference, reply, *, include_results=False):
    row = {name: value for name, value in sample.items() if name != "prompt"}
    row["reference_sql"] = reference
    row["matched"] = None
    if not reply or "error" in reply:
        row.update(reason="pending" if not reply else "transport_error")
        return row
    reference_sql = postprocess(reference)
    if include_results:
        gold_columns, gold_rows = execute_sql(sample["database"], reference_sql)
        row["reference_result"] = {"columns": gold_columns, "rows": gold_rows}
    text = reply["raw_reply"].strip()
    fence = FENCED_SQL.fullmatch(text)
    sql = fence[1].strip() if fence else text
    row.update(predicted_sql=sql, matched=False)
    if reply["finish"] == "max-tokens":
        row["reason"] = "truncated_reply"
        return row
    if not re.match(r"^(?:SELECT|WITH)\b", sql, re.I):
        row["reason"] = "invalid_sql_reply"
        return row
    if not include_results:
        gold_columns, gold_rows = execute_sql(sample["database"], reference_sql)
    try:
        pred_columns, pred_rows = execute_sql(sample["database"], postprocess(sql))
    except (sqlite3.Error, TypeError) as exc:
        row.update(reason="sqlite_error", detail=str(exc))
        return row
    row.update(predicted_columns=pred_columns, reference_columns=gold_columns,
               predicted_rows=len(pred_rows), reference_rows=len(gold_rows))
    if include_results:
        row["predicted_result"] = {"columns": pred_columns, "rows": pred_rows}
    matched = result_eq(gold_rows, pred_rows, "order by" in reference_sql.lower())
    if matched:
        reason = "match"
    elif len(pred_columns) != len(gold_columns):
        reason = "different_columns"
    elif len(pred_rows) != len(gold_rows):
        reason = "different_row_count"
    else:
        reason = "different_values_or_order"
    row.update(matched=matched, reason=reason)
    return row


def summarize(rows):
    grouped = defaultdict(list)
    scored = [row for row in rows if row["matched"] is not None]
    for row in rows:
        grouped[row["dev_index"]].append(row["matched"])
    completed = [statuses for statuses in grouped.values() if None not in statuses]
    return {
        "planned_dialogues": len(grouped),
        "databases": len({row["database"] for row in rows}),
        "planned_turns": len(rows),
        "scored_turns": len(scored),
        "passed_turns": sum(row["matched"] for row in scored),
        "scored_dialogues": len(completed),
        "passed_dialogues": sum(all(statuses) for statuses in completed),
        "outcomes": dict(Counter(row["reason"] for row in rows)),
    }


def write_results(output, samples, references, replies):
    rows = [evaluate(sample, references[sample["id"]], replies.get(sample["id"]))
            for sample in samples]
    totals = {
        "pilot": summarize([row for row in rows if row["partition"] == "pilot"]),
        "extension_only": summarize([row for row in rows if row["partition"] == "extension"]),
        "combined": summarize(rows),
    }
    failures = [row for row in rows if row["matched"] is False]
    write_text(output / "scores.jsonl", "".join(map(json_line, rows)))
    write_text(output / "failures.jsonl", "".join(map(json_line, failures)))
    write_text(output / "summary.json", manifest_text(totals))
    return totals


async def run_model(args, targets, output, responses):
    response_path = output / "responses.jsonl"
    previous = {row["id"]: row for row in responses}
    pending = [sample for sample in targets
               if sample["id"] not in previous or "error" in previous[sample["id"]]]
    if args.stage == "smoke":
        pending = pending[:1]
    if not pending:
        return
    gate = asyncio.Semaphore(args.workers)
    errors = 0
    completed = 0
    with tempfile.TemporaryDirectory(prefix=".harness-", dir=output) as temp_dir:
        async with Harness(args.cli, args.credentials, Path(temp_dir)) as harness:
            print(f"Harness {harness.server_info['serverInfo']} · {PROVIDER}/{MODEL}", flush=True)

            async def predict(sample):
                nonlocal errors, completed
                async with gate:
                    if errors >= 2:
                        return
                    session_id = harness.session_id()
                    start = time.monotonic()
                    record = {"id": sample["id"], "session_id": session_id,
                              "started_at": datetime.now(timezone.utc).isoformat()}
                    try:
                        record.update(await harness.predict(sample["prompt"], session_id))
                    except (RuntimeError, TimeoutError, OSError) as exc:
                        errors += 1
                        record["error"] = diagnostic(exc) or type(exc).__name__
                    record["elapsed_seconds"] = round(time.monotonic() - start, 3)
                    responses.append(record)
                    write_text(response_path, "".join(map(json_line, responses)))
                    completed += 1
                    detail = record.get("error", record.get("finish"))
                    print(f"{completed:3}/{len(pending)} {sample['id']} {sample['database']} "
                          f"{detail} {record['elapsed_seconds']}s", flush=True)

            await asyncio.gather(*(predict(sample) for sample in pending))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("plan", "smoke", "pilot", "expand", "score"))
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cli", type=Path, help="Path to the installed DeepSeek Harness CLI bin.js")
    parser.add_argument("--credentials", type=Path, help=f"DSH store with {API_KEY_REF} configured")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    output = args.run_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.stage in {"smoke", "pilot", "expand"}:
        if not args.cli or not args.credentials or not args.cli.is_file() or not args.credentials.is_file():
            parser.error("Specify the existing --cli and --credentials files")
        args.cli, args.credentials = args.cli.resolve(), args.credentials.resolve()
    lock = output / ".running"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(f"{args.stage} started {datetime.now(timezone.utc).isoformat()}\n")
    try:
        samples, references = prepare(output)
        pilot = [sample for sample in samples if sample["partition"] == "pilot"]
        response_path = output / "responses.jsonl"
        responses = read_jsonl(response_path) if response_path.exists() else []
        if args.stage == "plan":
            for sample in samples:
                execute_sql(sample["database"], postprocess(references[sample["id"]]))
        elif args.stage != "score":
            latest = {row["id"]: row for row in responses}
            if args.stage == "expand" and any(
                sample["id"] not in latest or "error" in latest[sample["id"]] for sample in pilot
            ):
                raise RuntimeError("Finish the pilot before expanding")
            targets = samples if args.stage == "expand" else pilot
            asyncio.run(run_model(args, targets, output, responses))
        totals = write_results(output, samples, references, {row["id"]: row for row in responses})
        print(manifest_text(totals))
        if args.stage in {"pilot", "expand"}:
            result = totals["pilot" if args.stage == "pilot" else "combined"]
            if result["scored_turns"] != result["planned_turns"]:
                return 2
    finally:
        lock.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
