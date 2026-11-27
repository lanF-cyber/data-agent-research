"""Replay the 14 archived repairs and the five failed or regressed turns using Python and original SQLite files."""
import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from itertools import permutations
from pathlib import Path

from scope_harness import history_inputs, output_phrase

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE.parents[2] / "evidence/week-04/reference-resolution.json"


def require(ok, label):
    if not ok:
        raise ValueError(f"Evidence mismatch: {label}")


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table_sha(rows):
    content = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode()).hexdigest()


def text_value(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"sqlite_text_hex": raw.hex()}


def execute(root, db, sql):
    path = (root / db / (db + ".sqlite")).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.text_factory = text_value
        conn.execute("PRAGMA query_only = ON")
        cursor = conn.execute(sql)
        rows = [[{"sqlite_blob_hex": v.hex()} if isinstance(v, bytes) else v for v in r] for r in cursor]
        return [c[0] for c in cursor.description], rows


def same_table(expected, actual, ordered):
    if len(actual) != len(expected):
        return False
    if not expected:
        return True
    width = len(expected[0])
    if len(actual[0]) != width:
        return False
    canonicalize = list if ordered else Counter
    def row_key(row):
        return tuple(tuple(v.items()) if isinstance(v, dict) else v for v in row)
    wanted = canonicalize(row_key(row) for row in expected)
    for perm in permutations(range(width)):
        aligned = [row_key(row[i] for i in perm) for row in actual]
        if wanted == canonicalize(aligned):
            return True
    return False


def verify_case(root, c):
    db, key = c["database"], c["id"]
    published = c["published"]
    for t in [*c["expected"], published]:
        cols, rows = execute(root, db, t["executable_sql"])
        require((cols, rows) == (t["columns"], t["rows"]), key + "/reference")
    for (version, passed), release_score in zip([("before", False), ("after", True)], published["passes"], strict=True):
        t = c[version]
        cols, rows = execute(root, db, t["sql"])
        require((cols, rows) == (t["columns"], t["rows"]), key + "/" + version)
        match = any(same_table(ref["rows"], rows, "order by" in ref["sql"].lower()) for ref in c["expected"])
        require(match == t["passed"] == passed, key + "/score/" + version)
        require(same_table(published["rows"], rows, "order by" in published["sql"].lower()) == release_score,
                key + "/published/" + version)
    if "selected_canvas" in c:
        prior = c["frozen_last_answer"]
        _, rows = execute(root, db, prior["sql"])
        require(rows == prior["rows"], key + "/frozen")
        user = c["users"][-1][1]
        t = int(c["users"][-2][0].split("_turn")[1])
        source = {"user": c["users"][-2][1], "actions": [{"tool":"submit_answer", "args":{"sql":prior["sql"]},
                  "receipt":{"columns":prior["columns"], "accepted":True}}], "delivered_rows":rows}
        chosen, _ = history_inputs(c["selected_canvas"], user, {t:source})
        require(chosen == c["selected_history"], key + "/selected-input")
        require(output_phrase(c["selected_canvas"], user) == "official languages", key + "/output")


def verify_failures(root, records):
    for r in records:
        key, db = r["题号"], r["数据库"]
        ref = r["参考SQL"]
        _, gold = execute(root, db, ref)
        require(table_sha(gold) == r["期望整表SHA256"], key + "/reference")
        for side in ("旧", "新"):
            cols, rows = execute(root, db, r[side + "版SQL"])
            require(cols == json.loads(r[side+"版列"]) and table_sha(rows) == r[side+"整表SHA256"], key + "/" + side)
            passed = same_table(gold, rows, "order by" in ref.lower())
            require(passed == (side == "旧" and r["判定"] == "对转错"), key + "/score/" + side)


def verify_platform(root, pack):
    archive = json.loads(EVIDENCE.with_name("platform-runs.json").read_bytes())
    require(archive["scoring_sha256"] == pack["scoring_sha256"], "platform scoring edition")
    dialogues, scope = archive["dialogues"], archive["scope"]
    receipts = {r["ticket"]: r for r in [*dialogues, *scope]}
    require((len(dialogues), len(scope), len(receipts)) == (32, 6, 38), "distinct platform runs")
    require(all(r["receipt_checked"] and r["state"] == "complete" and r["mode"] == "live"
                for r in receipts.values()), "completed live runs")
    by_arm = {"contrast": {}, "v3": {}}
    for r in dialogues:
        arm = by_arm[r["arm"]]
        for t in r["turns"]:
            require(t["id"] not in arm, t["id"] + "/duplicate")
            arm[t["id"]] = (r["ticket"], t)
    old, new = by_arm["contrast"], by_arm["v3"]
    require(old.keys() == new.keys(), "dialogue turns")
    for key in old:
        require(all(old[key][1][k] == new[key][1][k] for k in ("user", "follow_user")), key + "/input")
    summary = pack["summary"]["同题整段重跑"]
    for label, ids in (("目标", archive["targets"]), ("连同前文", list(old))):
        scores = [(old[k][1]["set_match"], new[k][1]["set_match"]) for k in ids]
        stats = summary[label]
        require(len(set(ids)) == len(ids) == stats["题数"] and
                sum(a for a, _ in scores) == stats["旧版对"] and sum(b for _, b in scores) == stats["新版对"] and
                sum(not a and b for a, b in scores) == stats["错转对"] and
                sum(a and not b for a, b in scores) == stats["对转错"], label)
    for c in pack["cases"]:
        if "replications" in c:
            for pair in c["replications"]:
                for ticket, arm, count, passed in zip(pair["tickets"], ("reminder", "selected"),
                                                       pair["rows"], pair["passes"], strict=True):
                    r = receipts[ticket]
                    cols, rows = execute(root, c["database"], r["sql"])
                    matches = any(same_table(ref["rows"], rows, "order by" in ref["sql"].lower()) for ref in c["expected"])
                    require(r["arm"] == arm and r["case"] == c["id"] and r["rows"] == len(rows) == count and
                            r["columns"] == cols and r["first_rows"] == rows[:len(r["first_rows"])] and
                            r["set_match"] == passed == matches, ticket + "/scope")
        else:
            for side, runs in (("before", old), ("after", new)):
                sample = c[side]
                ticket, turn = runs[c["id"]]
                require(ticket == sample["ticket"] and turn["sql"] == sample["sql"] and
                        turn["columns"] == sample["columns"] and turn["set_match"] == sample["passed"] and
                        turn["table_sha256"] == table_sha(sample["rows"]), ticket + "/" + c["id"])
    return len(receipts)


def verify_coverage(root):
    with EVIDENCE.with_name("coverage-cases.csv").open(encoding="utf-8-sig", newline="") as source:
        deliveries = list(csv.DictReader(source))
    verified = set()
    for record in deliveries:
        db = record["数据库"]
        key = "/".join(record[col] for col in ("批次", "案例", "题号"))
        identity = (db, record["原库SHA256"])
        if identity not in verified:
            require(file_sha(root / db / (db + ".sqlite")) == identity[1], db)
            verified.add(identity)
        references = [(execute(root, db, sql)[1], "order by" in sql.lower())
                      for sql in json.loads(record["复算执行的参考SQL"])]
        require(any(len(rows) == int(record["原库期望行数"]) for rows, _ in references), key + "/reference")
        outcomes = {"都对": (True, True), "都错": (False, False), "错转对": (False, True), "对转错": (True, False)}
        for arm, passed in zip(("原v3", "缺项版"), outcomes[record["结果"]], strict=True):
            _, rows = execute(root, db, record[arm + "SQL"])
            require(len(rows) == int(record[arm + "行数"]) and
                    table_sha(rows) == record[arm + "完整表指纹"], key + "/" + arm)
            require(any(same_table(ref, rows, ordered) for ref, ordered in references) == passed, key + "/score/" + arm)
    require(len(deliveries) == 95, "coverage deliveries")
    return len(deliveries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-root", type=Path, required=True, help="CoSQL original database directory")
    parser.add_argument("--coverage", action="store_true", help="also replay the separate Python follow-up")
    args = parser.parse_args()
    pack = json.loads(EVIDENCE.read_bytes())
    for filename, sha in pack["source_sha256"].items():
        require(file_sha(HERE / filename) == sha, filename)
    for db in {c["database"] for c in pack["cases"]} | {r["数据库"] for r in pack["failed"]}:
        require(file_sha(args.database_root / db / (db+".sqlite")) == pack["database_sha256"][db], db)
    ids = [c["id"] for c in pack["cases"]]
    require(len(set(ids)) == len(ids) == 14 and ids == pack["summary"]["合并后不同修复题号"], "case list")
    for c in pack["cases"]:
        verify_case(args.database_root, c)
    verify_failures(args.database_root, pack["failed"])
    count = verify_platform(args.database_root, pack)
    print(f"Verified {len(ids)} distinct wrong-to-right turns, {len(pack['failed'])} failed or regressed turns, "
          f"{count} platform receipts, and the selected history for dev86_turn6.")
    if args.coverage:
        pairs = verify_coverage(args.database_root)
        print(f"Replayed the SQL for {pairs} paired deliveries in the Python follow-up.")


if __name__ == "__main__":
    main()
