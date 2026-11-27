"""Select the evidence a SQL generator receives from an executed dialogue."""
import re


GUIDE = ("先根据本轮原话确定对象、范围和交付列。问句本身完整时从本轮范围开始；"
         "有指代或省略时，补入所指的集合或操作。计数背后的集合可以从当时执行的查询恢复。"
         "若前次交付与当时问句冲突，告知遗漏；本轮若明确指定已展示的记录则沿用展示结果。")
OUTPUT_GUIDE = ("画布标出的问句片段是用户要看的输出。仅用于限定对象的字段不随结果交付。"
                "索取属性时，每条实际匹配的记录交付一次；问到不同值、每个对象或缺失项时再按原话处理。"
                "set 指来源问句要查的集合，已执行 SQL 是证据。发现无依据的旧筛选时按原意重查并说明遗漏；"
                "若本轮可能是在指上次展示行，交付暂定结果后询问所指。shown 明确指实际展示行。")
BASE_NODES = ("catalog", "generate", "lookup_value", "submit")
BASE_EDGES = (("catalog", "generate"), ("generate", "lookup_value"),
              ("lookup_value", "generate"), ("generate", "submit"))


def canvas_for(question_id, case, arm, ticket, *, source=None, carry="set", cue="", parent="", agreement=None,
               output_span=""):
    """Only the canvas edges and their user-language evidence select history."""
    elements = [{"id": name} for name in BASE_NODES]
    links = [{"fromElementId": a, "toElementId": b} for a, b in BASE_EDGES]
    if arm in ("full", "reminder"):
        elements.append({"id": "history"})
        links.append({"fromElementId": "history", "toElementId": "generate"})
    if output_span:
        elements.append({"id": "requested"})
        links.append({"fromElementId": "requested", "toElementId": "generate", "cue": output_span})
    sources = [(source, carry, cue)] if source is not None else []
    if agreement:
        sources.append((agreement[0], "agreement", agreement[1]))
    for n, kind, quote in sources:
        node = f"t{n}_{kind}"
        elements.append({"id": node})
        links.append({"fromElementId": node, "toElementId": "generate", "cue": quote})
    return {"id": "cosql-harness", "experiment": "scope", "mode": "live", "operation": "start",
            "questionId": question_id, "case": case, "arm": arm, "ticket": ticket, "parent": parent,
            "elements": elements, "links": links}


def output_phrase(canvas, user):
    for link in canvas["links"]:
        if link["fromElementId"] == "requested":
            return quoted(link, user)
    return ""


def quoted(link, utterance):
    text = link.get("cue")
    if not isinstance(text, str) or not text.strip() or text.strip().casefold() not in utterance.casefold():
        raise ValueError("The graph edge needs an exact phrase from its user utterance")
    return text.strip()


def bindings(canvas, user, history):
    arm = canvas["arm"]
    if arm not in ("full", "reminder", "selected"):
        raise ValueError("Unknown history policy")
    nodes = [node["id"] for node in canvas["elements"]]
    edges = [(e["fromElementId"], e["toElementId"]) for e in canvas["links"]]
    if len(set(nodes)) != len(nodes) or len(set(edges)) != len(edges):
        raise ValueError("Repeated graph element")
    required = set(BASE_NODES)
    expected = set(BASE_EDGES)
    if arm != "selected":
        required.add("history")
        expected.add(("history", "generate"))
    if output_phrase(canvas, user):
        required.add("requested")
        expected.add(("requested", "generate"))
    references = []
    for link in canvas["links"]:
        name = link["fromElementId"]
        match = re.fullmatch(r"t([1-9]\d*)_(set|shown|operation|agreement)", name)
        if not match:
            continue
        turn, kind = int(match[1]), match[2]
        if arm != "selected" or turn not in history or link["toElementId"] != "generate":
            raise ValueError("Reference must connect an earlier turn of this dialogue to generation")
        evidence = history[turn]["user"] if kind == "agreement" else user
        quote = quoted(link, evidence)
        if any((r["kind"] == "agreement") == (kind == "agreement") for r in references):
            raise ValueError("Select at most one antecedent and one active explicit agreement")
        required.add(name)
        expected.add((name, "generate"))
        references.append({"turn": turn, "kind": kind, "cue": quote})
    if set(nodes) != required or set(edges) != expected:
        raise ValueError("Unexpected graph node or edge")
    return references


def history_inputs(canvas, user, history):
    refs = bindings(canvas, user, history)
    if canvas["arm"] != "selected":
        return [{"turn": n, "user": item["user"], "actions": item["actions"]}
                for n, item in sorted(history.items())], refs
    selected = []
    for ref in refs:
        item = history[ref["turn"]]
        kind = ref["kind"]
        entry = {**ref, "user": item["user"]}
        if kind == "agreement":
            entry = {**ref, "user": ref["cue"]}
        elif kind != "operation":
            action = item["actions"][-1]
            if action["tool"] != "submit_answer" or not action["receipt"].get("accepted"):
                raise ValueError("Referenced turn has no executed delivery")
            entry["executed_sql"] = action["args"]["sql"]
            if kind == "shown":
                entry["columns"] = action["receipt"]["columns"]
                entry["rows"] = item["delivered_rows"]
        selected.append(entry)
    return selected, refs
