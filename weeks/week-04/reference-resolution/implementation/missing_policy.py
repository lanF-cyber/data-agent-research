"""Optional coverage handling for the v3 CoSQL query client."""

import json
import re

from clarification import adaptive, answer_first, candidate_pilot, experiment
from week4.session_api import tool
from week4.transport import compact


GUIDANCE = """缺项处理适用于统计依赖关联明细，且部分对象可能没有明细或可用值的查询。
先确定用户要的对象范围，再区分确实无记录、记录存在但指标为空、真实的零。连接键或存值可疑时先沿用查值工具。
没有另定口径时，各对象的平均值、总量，以及从已有明细比较多少的主表只纳入有可用记录的对象。
如用户明确要求全量、要保留无记录者，或明确对上一张完整名册加字段，保留那些对象。无记录的平均值为 NULL；
在完整事件记录可核实的情况下，零次事件可以在全量统计中计 0。用户专问没有关联记录的人或物时只列缺项。
只补或删一列时承接上一轮所选集合和缺项口径。独立新问或明确改范围时按本轮判断，不继承前一份报表的缺项条件。
口径未明且缺项可能影响交付时，用一次 check_context 的 coverage 查询核实同一对象范围的三个计数：
全体对象数 scope_total、有匹配明细的对象数 with_records、有可用指标的对象数 with_value。每个对象只数一次，
按此顺序返回一行三个非负整数。只有一条空关联行的对象没有匹配明细，真实零值仍是可用值。
覆盖核验和通常的 SQL 试查共用原有预算。前轮已核验同一范围且本轮只改列时，可以复用。用户已明确范围时可直接执行。
主表只交用户要求的列和行。缺项计数由已验证的回执另附，text 仅说明必要的口径，别抄写未经核验的数。
提交时用 missing_policy 标明采用的范围，用 scope_source 标明这个范围是本轮原话、默认口径还是承接先前交付。
普通查询填 ordinary。"""

REFINED_GUIDANCE = GUIDANCE + """

用户说“每个”“每位”“each”“per”是在指定按什么对象分组。仅有这些词时仍按默认的有可用记录口径。
用户说无记录的也要、明确要完整名册，或要求给上一张已确定的全量名册追加列时，才覆盖为 all。
上一轮问过一类对象有几个，并不表示此后对它的独立统计都要带上无记录的对象。
存值格式核验可用于连接和筛选。所交付的直接属性保留原库值及原有空格，除非用户明确要清理显示。
说明中说的“留空”或 blank 一律交付 SQL NULL 对应的缺值，除非用户明确要求空字符串。"""

COVERAGE_COLUMNS = ["scope_total", "with_records", "with_value"]

CHECK = tool(
    "check_context", experiment.CHECK["description"],
    {
        **experiment.CHECK["parameters"]["properties"],
        "purpose": {"type": "string", "enum": ["fact", "coverage"],
                    "description": "Use coverage only for the three coverage counts in the instructions."},
    },
)

SUBMIT = tool(
    "submit_answer", answer_first.DELIVER["description"],
    {
        **answer_first.DELIVER["parameters"]["properties"],
        "missing_policy": {"type": "string", "enum": ["ordinary", "available", "all", "missing_only"],
                           "description": "Whether the result uses ordinary, observed, full, or missing-only scope."},
        "scope_source": {"type": "string", "enum": ["ordinary", "current_question", "default", "prior_delivery"],
                         "description": "The evidence for the chosen scope in this turn."},
    },
)


def coverage_counts(receipt):
    if not receipt.get("accepted") or receipt.get("columns") != COVERAGE_COLUMNS or receipt.get("has_more"):
        return None
    rows = receipt.get("first_rows", [])
    if len(rows) != 1 or len(rows[0]) != 3 or any(type(n) is not int for n in rows[0]):
        return None
    total, recorded, usable = rows[0]
    if not 0 <= usable <= recorded <= total:
        return None
    return {"total": total, "with_records": recorded, "with_value": usable,
            "no_record": total - recorded, "missing_value": recorded - usable}


def coverage_note(counts, user):
    if counts is None or counts["total"] == counts["with_value"]:
        return ""
    if re.search(r"[\u3400-\u9fff]", user):
        return (f"当前范围共 {counts['total']:,} 个对象，{counts['no_record']:,} 个无匹配记录，"
                f"{counts['missing_value']:,} 个有记录但指标缺失。")
    return (f"Of {counts['total']:,} entities in scope, {counts['no_record']:,} had no matching record "
            f"and {counts['missing_value']:,} had records but no usable value.")


class Client(candidate_pilot.Client):
    """Run the v3 session with coverage checks inside the existing query allowance."""

    def __init__(self, state, http, out, rules, identifiers, *, guidance=REFINED_GUIDANCE):
        super().__init__(state, http, out, rules, identifiers)
        self.guidance = guidance
        self.users = {}
        self.probes = {}
        self.check_calls = {}
        state.execute("""CREATE TABLE IF NOT EXISTS missing_policy_receipts(
            call_id TEXT PRIMARY KEY, cid TEXT NOT NULL, payload TEXT NOT NULL)""")

    async def open_session(self, phase, number, slot, opening):
        if phase == "work":
            opening += "\n\n" + self.guidance
        return await super().open_session(phase, number, slot, opening)

    async def choose(self, slot, cid, task, inputs, tools):
        item, = inputs
        if item.get("role") == "user":
            self.users[cid] = item["content"]
            self.probes.pop(cid, None)
        elif item.get("type") == "function_call_output":
            check = self.check_calls.pop(item["call_id"], None)
            if check:
                self.probes[cid] = {**check, "counts": coverage_counts(json.loads(item["output"]))}

        replacements = {"submit_answer": SUBMIT, "check_context": CHECK}
        choices = [replacements.get(t["name"], t) for t in tools if t["name"] != "ask_user"]
        result = await adaptive.Client.choose(self, slot, cid, f"{answer_first.SOURCE}/{task}", inputs, choices)
        name, args, call_id, _ = result
        if name == "submit_answer":
            self.pending[call_id] = args
        if name == "check_context" and args["purpose"] == "coverage":
            self.check_calls[call_id] = {"call_id": call_id, "sql": args["sql"]}
        return result

    async def deliver(self, slot, cid, call_id, receipt):
        args = self.pending.get(call_id)
        if args is None or not receipt.get("accepted"):
            return await super().deliver(slot, cid, call_id, receipt)

        policy, source = args["missing_policy"], args["scope_source"]
        probe = self.probes.get(cid)
        inherited = None
        if probe is None and source == "prior_delivery" and policy != "ordinary":
            row = self.db.execute(
                "SELECT call_id,payload FROM missing_policy_receipts WHERE cid=? AND call_id<>? ORDER BY rowid DESC LIMIT 1",
                (cid, call_id),
            ).fetchone()
            if row:
                prior = json.loads(row[1])
                if prior["policy"] == policy and prior["counts"] is not None:
                    inherited = row[0]
                    probe = prior["probe"]

        counts = probe.get("counts") if probe and policy != "ordinary" else None
        details = {"policy": policy, "scope_source": source, "counts": counts,
                   "probe": probe, "inherited_from": inherited}
        note = coverage_note(counts, self.users[cid])
        if note:
            args["text"] = " ".join(s for s in (args["text"].strip(), note) if s)
        await super().deliver(slot, cid, call_id, {**receipt, "coverage": details})
        self.db.execute("INSERT OR IGNORE INTO missing_policy_receipts VALUES (?,?,?)",
                        (call_id, cid, compact(details)))
