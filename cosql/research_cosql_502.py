"""Research agent and evaluator additions for the CoSQL 502-turn replay."""

import json
from pathlib import Path

import research_cosql as base
import answer_submission_mcp
import run_answer_submission_pilot as pilot
from answer_submission_eval101 import column_key
from answer_submission_mcp_502 import ROW_LIMIT
from evaluation.contracts import Score
from run_harness_baseline import read_jsonl, result_eq
from utils.json_io import write_json


OPENING_RULES = (
    "补充答题约定。提交的 SQL 保留数据库计算的原始数值精度，只有本轮明确要求时才在 SQL 中取整或四舍五入。"
    "如果只是为了阅读方便，可以在文字说明中保留较少小数。"
    "题目清楚时直接查库并作答。只有会改变答案的关键信息缺失时，指出具体缺什么并提出一个澄清问题。"
    "如果按一种合理理解可以先给出有用答案，先说清按什么范围、对象或并列规则来答，再提交相应查询。"
    "如果查到的数据与提问前提不符，告诉用户哪里对不上，能查证时提交查询，同时在文字里解释。"
)


def configure_mcp():
    # Each launcher uses its own Python process. Apply the same limit in the
    # scorer and the stdio child to reproduce receipts from either side.
    answer_submission_mcp.ROW_LIMIT = ROW_LIMIT
    pilot.SERVER = base.ROOT / "answer_submission_mcp_502.py"


async def execute(*, instruction, **options):
    configure_mcp()
    dialogue = json.loads(instruction)
    first = dialogue["turns"][0]
    first["original_question"] = first["question"]
    first["question"] = f"{OPENING_RULES}\n\n{first['original_question']}"
    return await base.execute(instruction=json.dumps(dialogue, ensure_ascii=False), **options)


def check_alternatives(row):
    """Check the independently frozen answer values, including tied results."""
    options = row.get("acceptable_results")
    display = row["submission"]["user_answer"]
    if not options or display is None:
        return None
    good_names = set(options["aliases"])
    named = [i for i, column in enumerate(display["columns"]) if column_key(column) in good_names]
    positions = named if named else [0] if len(display["columns"]) == 1 else []
    if len(positions) != 1:
        return None
    observed = [(item[positions[0]],) for item in display["rows"]]
    expected = [tuple(item) for item in options["rows"]]
    if result_eq(expected, observed, False):
        return "all"
    if options["allow_one"] and len(observed) == 1 and observed[0] in expected:
        return "one_of_ties"
    return None


async def score_trial(context, agent_result):
    configure_mcp()
    base_score = await base.score_trial(context, agent_result)
    verifier = context.output_dir / "verifier"
    path = verifier / "turns.jsonl"
    rows = read_jsonl(path)
    flagged = []
    for row in rows:
        if row["category"] == "excluded":
            row["alternative_match"] = check_alternatives(row)
            flagged.append(row)
    base.write_records(path, rows)
    metrics = {**base_score.metrics, "flagged_turns": len(flagged),
               "alternative_matches": sum(row["alternative_match"] is not None for row in flagged)}
    write_json(verifier / "result.json", metrics)
    return Score(reward=base_score.reward, metrics=metrics)
