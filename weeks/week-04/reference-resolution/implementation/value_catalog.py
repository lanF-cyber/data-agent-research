"""Read-only database survey and verified value memory for the CoSQL agent."""

import json
import sqlite3
import unicodedata
from contextlib import closing
from difflib import get_close_matches

from week4.db_tools import quoted
from week4.paged_results import json_cell, query
from week4.protocol import database_path
from week4.transport import compact


def match_key(text):
    return "".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def survey(database):
    """Inspect all table structures once and retain compact storage observations."""
    uri = database_path(database).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.text_factory = lambda value: value.decode("utf-8", "surrogateescape")
        tables = {}
        findings = {}
        relations = {}
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall():
            label = quoted(table)
            columns = conn.execute(f"PRAGMA table_info({label})").fetchall()
            tables[table] = {row[1]: row[2] for row in columns}
            links = conn.execute(f"PRAGMA foreign_key_list({label})").fetchall()
            if links:
                relations[table] = [[row[3], row[2], row[4]] for row in links]
            for column, kind in tables[table].items():
                name = quoted(column)
                stats = conn.execute(
                    f"SELECT typeof({name}), COUNT(*), "
                    f"SUM(CASE WHEN typeof({name})='text' AND {name}<>trim({name}) THEN 1 ELSE 0 END), "
                    f"SUM(CASE WHEN typeof({name})='text' AND trim({name})='' THEN 1 ELSE 0 END) "
                    f"FROM {label} WHERE {name} IS NOT NULL GROUP BY typeof({name})"
                ).fetchall()
                field = {"types": {storage: count for storage, count, _, _ in stats}}
                padded = sum(row[2] for row in stats)
                blank = sum(row[3] for row in stats)
                if padded:
                    field["edge_spaces"] = padded
                if blank:
                    field["blank_text"] = blank
                if len(stats) > 1 or padded or blank or "text" in field["types"] or "bool" in kind.lower():
                    sample = conn.execute(
                        f"SELECT DISTINCT {name} FROM {label} "
                        f"WHERE {name} IS NOT NULL AND length({name})<=72 LIMIT 9"
                    ).fetchall()
                    if len(sample) < 9 or padded or blank or len(stats) > 1:
                        field["examples"] = [json_cell(row[0]) for row in sample[:8]]
                if len(stats) > 1 or padded or blank or "examples" in field:
                    findings[f"{table}.{column}"] = field
    return {"tables": tables, "foreign_keys": relations, "observations": findings}


def find_field(profile, table, column):
    table = next((name for name in profile["tables"] if name.casefold() == table.casefold()), None)
    if table is None:
        return None
    found = next((name for name in profile["tables"][table] if name.casefold() == column.casefold()), None)
    return (table, found) if found else None


def lookup(state, database, profile, args):
    field = find_field(profile, args["table"], args["column"])
    term = match_key(args["term"])
    if not field or not term:
        return {"source": "input", "error": "Provide a table and column from the schema and a nonempty value."}
    table, column = field
    saved = state.execute(
        "SELECT value, learned_at FROM facts WHERE db=? AND table_name=? AND column_name=? AND term=?",
        (database, table, column, term),
    ).fetchone()
    if saved and not args["refresh"]:
        return {"source": "memory", "field": [table, column], "values": [json.loads(saved[0])],
                "learned_at": saved[1]}
    label = quoted(column)
    sql = f"SELECT DISTINCT {label} FROM {quoted(table)} WHERE {label} IS NOT NULL AND length({label})<=96"
    with query(database, sql) as cur:
        candidates = {}
        for (value,) in cur:
            original = json_cell(value)
            if isinstance(original, (str, int, float)):
                candidates.setdefault(match_key(original), []).append(original)
    values = candidates.get(term, [])
    near = [] if values else [
        value for key in get_close_matches(term, candidates, n=4, cutoff=0.66)
        for value in candidates[key][:2]
    ]
    return {"source": "database", "field": [table, column], "term": term, "values": values[:12],
            "candidates": near[:5], "verified": len(values) == 1, "inspection_sql": sql}


def learn(state, database, message, turn_id):
    final = message["trace"][-1]
    if final["tool"] != "submit_answer" or not final["receipt"].get("accepted"):
        return
    for action in message["trace"]:
        result = action["receipt"]
        if action["tool"] == "lookup_value" and result.get("verified"):
            state.execute(
                """INSERT INTO facts VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(db,table_name,column_name,term) DO UPDATE SET
                     value=excluded.value, evidence=excluded.evidence, learned_at=excluded.learned_at""",
                (database, *result["field"], result["term"], compact(result["values"][0]),
                 compact({"lookup": result["inspection_sql"], "args": action["args"],
                          "verified_value": result["values"][0]}), turn_id),
            )
