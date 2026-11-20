"""Score saved CoSQL answers against the question and database evidence."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

import sqlparse
from sqlparse.sql import Identifier, IdentifierList
from sqlparse.tokens import DML, Keyword

from answer_submission_eval101 import answer_fields, column_key, score_answer
from run_answer_submission_pilot import answer_contract
from run_harness_baseline import DATA, ROOT, execute_sql, postprocess, read_jsonl, result_eq, write_text


RUN = ROOT / "reports" / "2026-11-20_gpt6sol_answer_mcp_502"
BASELINE = ROOT / "reports" / "2026-11-19_continuous_expanded_v2" / "turns.jsonl"
RULES = ROOT / "research_config_502" / "answer_review.json"
ORDER_REQUEST = re.compile(
    r"\b(?:ascending|descending|increasing|decreasing|alphabetical(?:ly)?|chronological(?:ly)?"
    r"|(?:order|sort|arrange|rank)\s+(?:by|the|this|these|it|them)"
    r"|(?:ordered|sorted|arranged|ranked)\s+by|starting with (?:the )?(?:earliest|latest|oldest|youngest))\b",
    re.I,
)
INHERITED_ORDER = re.compile(r"\b(?:same list|keep (?:it |them )?in order|same order)\b", re.I)
DIRECT_COLUMN = re.compile(r'(?:(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|\w+)\s*\.\s*)?'
                           r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|\w+)')


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sql_sources(sql, columns):
    """Recover direct columns behind aliases. Leave expressions for review."""
    if not sql:
        return {}
    statements = sqlparse.parse(sql)
    if len(statements) != 1:
        return {}
    output, selecting = [], False
    for token in statements[0].tokens:
        if token.ttype is DML and token.normalized == "SELECT":
            selecting = True
        elif selecting and token.ttype is Keyword and token.normalized == "FROM":
            break
        elif selecting and not token.is_whitespace and token.normalized not in {"DISTINCT", "ALL"}:
            output.extend(token.get_identifiers() if isinstance(token, IdentifierList) else [token])
    if len(output) != len(columns):
        return {}
    sources = {}
    for column, token in zip(columns, output, strict=True):
        if isinstance(token, Identifier):
            expr = token.value
            if token.get_alias():
                expr = re.split(r"\s+(?:AS\s+)?", expr.strip(), maxsplit=1, flags=re.I)[0]
            if DIRECT_COLUMN.fullmatch(expr.strip()):
                sources[column] = column_key(token.get_real_name())
    return sources


def required_positions(columns, sources, groups):
    positions = []
    for group in groups:
        candidates = [
            i for i, name in enumerate(columns)
            if column_key(name) in group or sources.get(name) in group
        ]
        if not candidates:
            return []
        if len(candidates) > 1 or candidates[0] in positions:
            return None
        positions.extend(candidates)
    return positions


def ordered(question, prior, configured):
    if configured is not None:
        return configured
    return (bool(ORDER_REQUEST.search(question))
            or bool(prior and INHERITED_ORDER.search(question) and ORDER_REQUEST.search(prior[-1])))


def grade_answer(display, sql, sql_columns, reference, groups, order_matters, contract):
    if display is None:
        return {"points": None, "basis": "answer_missing"}
    columns = display["columns"]
    actual = [tuple(row) for row in display["rows"]]
    expected = [tuple(row) for row in reference["rows"]]
    if contract != "rows":
        specialized = score_answer(
            {"contract": contract, "answer_fields": groups}, display, reference,
            "ORDER BY" if order_matters else "", answer_contract,
        )
        if specialized["matched"]:
            return {"points": 1, "basis": "answer_contract", "fields": specialized["columns"]}
        return {"points": None, "basis": "content_needs_review"}
    positions = required_positions(columns, sql_sources(sql, sql_columns), groups)
    if positions is None:
        return {"points": None, "basis": "ambiguous_answer_fields"}
    if positions:
        projected = [tuple(row[i] for i in positions) for row in actual]
        equal = projected == expected if order_matters else Counter(projected) == Counter(expected)
        if equal:
            return {"points": 1, "basis": "required_fields", "fields": [columns[i] for i in positions]}
    elif len(columns) == len(reference["columns"]) and result_eq(expected, actual, order_matters):
        return {"points": 1, "basis": "complete_result", "fields": columns}
    reason = "content_needs_review" if positions else "required_fields_need_review"
    return {"points": None, "basis": reason}


def checked_read(path, digest):
    if sha256(path) != digest:
        raise ValueError(f"Source differs from the verified run: {path}")
    return read_jsonl(path)


def portable_turns(current, proof, old, cases, reviews):
    rows = []
    matches = checked_read(current / "predictions.jsonl", proof["sha256"]["predictions.jsonl"])
    for item in matches:
        key = item["id"]
        grade = old[key]["grade"]
        sql = item["sql"]
        columns = execute_sql(item["database"], postprocess(sql))[0] if item["verified_receipt"] else []
        rows.append({
            **cases[key],
            **{field: item[field] for field in ("id", "dev_index", "turn", "database", "question")},
            "grade": {
                "reference_result": grade["reference_result"], "reference_sql": grade["reference_sql"],
                "predicted_sql": sql, "predicted_columns": columns,
            },
            "submission": {"user_answer": item["display"]},
            "alternative_match": (
                "all" if item["issue_review"] and key not in reviews
                and item["issue_review"]["verdict"] == "appropriate" else None
            ),
        })
    return rows


def read_run(run, baseline, archive=None):
    current = archive or run / "research"
    plan_path = current / "frozen-plan.json" if archive else run / "research-package" / "plan.json"
    plan = json.loads(plan_path.read_bytes())
    proof = json.loads((current / "verification.json").read_bytes())
    old_record = "reports/2026-11-19_continuous_expanded_v2/turns.jsonl"
    previous = checked_read(baseline, plan["sources_sha256"][old_record])
    old = {r["id"]: r for r in previous}
    reviews = {r["id"]: r for r in checked_read(current / "review-decisions.jsonl",
                                              proof["sha256"]["review-decisions.jsonl"])}
    databases = {dialogue["database"]: dialogue["sha256"] for dialogue in plan["dialogues"]}
    for database, digest in databases.items():
        path = DATA / "database" / database / f"{database}.sqlite"
        if sha256(path) != digest:
            raise ValueError(f"SQLite data changed: {database}")
    cases = {item["id"]: item for dialogue in plan["dialogues"] for item in dialogue["turns"]}
    rows = (portable_turns(current, proof, old, cases, reviews) if archive
            else checked_read(current / "turns.jsonl", proof["sha256"]["turns.jsonl"]))
    ids = [r["id"] for r in rows]
    if len(cases) != len(previous) or ids != list(cases) or set(old) != cases.keys():
        raise ValueError("The two Research logs must contain every planned question exactly once")
    return old, rows, reviews, plan


def recorded_review(review):
    if (set(review) != {"points", "reason"} or type(review["points"]) is not int or review["points"] not in (0, 1)
            or not isinstance(review["reason"], str) or not review["reason"].strip()):
        raise ValueError(f"Invalid review: {review!r}")
    return {"points": review["points"], "basis": "question_review", "reason": review["reason"]}


def score_pair(row, baseline, question, prior, rule, issue_review):
    raw_grade, old_grade = row["grade"], baseline["grade"]
    reference = raw_grade["reference_result"]
    if "reference_sql" in rule:
        columns, values = execute_sql(row["database"], rule["reference_sql"])
        reference = {"columns": columns, "rows": values}
    groups = answer_fields({"id": row["id"], "grade": {"reference_result": reference, "matched": False}})
    if "aliases" in rule:
        if len(rule["aliases"]) != len(groups):
            raise ValueError(f"Wrong number of answer fields: {row['id']}")
        groups = [
            set(aliases) | {column_key(name) for name in extra}
            for aliases, extra in zip(groups, rule["aliases"], strict=True)
        ]
    show = row["submission"]["user_answer"]
    candidates = {
        "baseline": (old_grade.get("predicted_result"), old_grade),
        "mcp": (show, raw_grade),
    }
    ambiguous = row.get("issue") and not rule.get("reference_sql")
    results = {}
    for version, (display, grade) in candidates.items():
        if version in rule:
            results[version] = recorded_review(rule[version])
        elif ambiguous and version == "mcp" and issue_review:
            results[version] = {
                "points": int(issue_review["verdict"] == "appropriate"), "basis": "earlier_question_review",
                "reason": issue_review["reason"],
            }
        elif ambiguous and version == "mcp" and row.get("alternative_match") == "all":
            results[version] = {"points": 1, "basis": "verified_alternative", "reason": row["issue"]["reason"]}
        elif ambiguous:
            results[version] = {"points": None, "basis": "flagged_question_needs_review"}
        else:
            results[version] = grade_answer(
                display, grade.get("predicted_sql"), grade.get("predicted_columns", []), reference, groups,
                ordered(question, prior, rule.get("order")), rule.get("contract", row["contract"]),
            )
    return results


def grade_run(run, baseline, rules_path, archive=None):
    old, rows, earlier_reviews, plan = read_run(run, baseline, archive)
    review = json.loads(rules_path.read_bytes())
    rules = review["questions"]
    expected_sources = review["sources_sha256"]
    current_proof = json.loads(((archive or run / "research") / "verification.json").read_bytes())
    sources = {"baseline": sha256(baseline), "mcp": current_proof["sha256"]["turns.jsonl"]}
    if expected_sources != sources or rules.keys() - old.keys():
        raise ValueError("Question reviews must cite this exact pair of completed runs")
    cases = {item["id"]: item for dialogue in plan["dialogues"] for item in dialogue["turns"]}
    decisions, prior = [], {}
    for row in rows:
        key = row["id"]
        question = cases[key]["question"]
        if (old[key]["grade"]["question"] != question
                or row.get("original_question", row["question"]) != question
                or (row["dev_index"], row["turn"], row["database"]) != (
                    old[key]["dev_index"], old[key]["turn"], old[key]["database"],
                )):
            raise ValueError(f"Question or database differs between runs: {key}")
        rule = rules.get(key, {})
        history = prior.setdefault(row["dev_index"], [])
        result = score_pair(row, old[key], question, history, rule, earlier_reviews.get(key))
        decisions.append({
            "id": key, "database": row["database"], "question": question,
            "issue": (row.get("issue") or {}).get("kind") or rule.get("issue"),
            "initial_question_group": row["category"],
            "assessment": rule.get("reason"),
            "reference_sql": rule.get("reference_sql", row["grade"]["reference_sql"]),
            **result,
        })
        history.append(question)
    return decisions, sources


def summarize(decisions):
    subsets = {
        "all": decisions,
        "original_422": [d for d in decisions if d["initial_question_group"] == "primary"],
        "original_80": [d for d in decisions if d["initial_question_group"] == "excluded"],
    }
    totals = {
        name: {
            version: {
                "questions": len(rows),
                "passed": sum(row[version]["points"] == 1 for row in rows),
                "failed": sum(row[version]["points"] == 0 for row in rows),
                "pending": sum(row[version]["points"] is None for row in rows),
            }
            for version in ("baseline", "mcp")
        }
        for name, rows in subsets.items()
    }
    for group in totals.values():
        for score in group.values():
            score["rate"] = score["passed"] / score["questions"] if not score["pending"] else None
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=RUN)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--rules", type=Path, default=RULES)
    parser.add_argument("--evidence", type=Path, help="Use the public evidence directory from the submission archive")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    decisions, sources = grade_run(args.run, args.baseline, args.rules, args.evidence)
    totals = summarize(decisions)
    summary = {"scores": totals, "sources_sha256": sources, "rules_sha256": sha256(args.rules)}
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        write_text(args.output / "decisions.jsonl", "".join(
            json.dumps(row, ensure_ascii=False) + "\n" for row in decisions
        ))
        write_text(args.output / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
