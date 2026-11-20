"""Frozen answer roles and data-quality review for the original 101-turn sample."""

import re

from answer_submission_mcp import project_rows
from run_harness_baseline import result_eq


# List exact decisions before reading any new model output. These turns are
# replayed for context and for the historical execution score.
EXCLUSIONS = {
    "dev282_turn1": ("reference", "题目数养狗学生，参考数饲养记录。"),
    "dev282_turn3": ("premise", "追问说有三名学生，原库实际是两名不同学生。"),
    "dev97_turn1": ("reference", "题目要歌手编号，参考返回姓名。"),
    "dev292_turn1": ("reference", "题目要语言名单，参考只返回数量。"),
    "dev140_turn1": ("reference", "题目问本科，参考筛硕士。"),
    "dev215_turn3": ("reference", "男歌手字段存 T/F，参考用 True 字符串。"),
    "dev98_turn5": ("reference", "题目问最低净资产金额，参考返回人名。"),
    "dev25_turn2": ("context", "前三家取决于上一问的排序，参考还用机场代码回答国家。"),
    "dev254_turn3": ("context", "开发集回放缺了上文中的 Kyle。"),
    "dev10_turn3": ("premise", "原库记录店铺产品数，无法确定经理个人生产量。"),
    "dev280_turn1": ("ambiguous", "题目未明确要求全国的电视台都未播放，还是至少有一家未播放。"),
    "dev277_turn1": ("ambiguous", "题目未说明是否纳入零段落文档。"),
    "dev225_turn1": ("ambiguous", "所有成绩单日期并列最少，参考只认其中一个日期。"),
    "dev225_turn3": ("ambiguous", "所有成绩单日期并列最多，参考只认其中一个日期。"),
    "dev57_turn2": ("ambiguous", "所有选手编号的范围可能承接两州，或指整个投票表。"),
    "dev57_turn3": ("ambiguous", "上一问范围未定，选手 5 的全表与两州计数不同。"),
    "dev91_turn2": ("ambiguous", "题目的房间数限制是否同时约束住宅与公寓未讲清。"),
    "dev91_turn3": ("ambiguous", "指代上一问的房产，继承了上一问的范围歧义。"),
    "dev85_turn6": ("ambiguous", "会员等级是筛选条件还是排序条件未讲清。"),
    "dev268_turn2": ("ambiguous", "好友关系的查询方向没有明确。"),
    "dev268_turn5": ("reference", "指向与 Kyle 有关系的 Jordan，参考同时取了两位同名学生。"),
    "dev268_turn6": ("ambiguous", "有两位 Gabriel，题目未指定，参考也混合两位的记录。"),
    "dev31_turn1": ("ambiguous", "题目未明确列出匹配文档时需要整行记录还是文档名称。"),
}
CONTRACTS = {
    "dev123_turn2": "age",
    "dev123_turn3": "age",
    "dev125_turn2": "full_name",
    "dev98_turn4": "over_30_million",
}
ALIASES = {
    "sum(population)": ("population", "total_population", "population_total"),
    "avg(lifeexpectancy)": ("average_life_expectancy", "avg_life_expectancy", "life_expectancy"),
    "avg(agreed_selling_price)": ("average_agreed_selling_price", "avg_selling_price"),
    "avg(age)": ("average_age", "avg_age"),
    "name": ("singer_name", "student_name", "visitor_name", "conductor_name", "teacher_name",
             "player_name", "poker_player_name", "person_name", "customer_name", "battle_name",
             "highschooler_name", "full_name"),
    "id": ("school_id", "student_id", "highschooler_id", "kyle_id", "battle_id"),
    "age": ("age_years", "singer_age", "visitor_age", "conductor_age", "customer_age"),
    "orchestra": ("orchestra_name",),
    "citizenship": ("citizenship_country",),
    "contid": ("continent_id",),
    "title": ("cartoon_title", "episode_title"),
    "original_air_date": ("original_airdate", "air_date", "first_aired"),
    "written_by": ("writer", "writer_name"),
    "directed_by": ("director", "director_name"),
    "number_products": ("product_count", "number_of_products", "total_products"),
    "degree_summary_description": ("degree_summary",),
    "level_of_membership": ("membership_level",),
    "country_code": ("country",),
    "fname": ("first_name", "given_name"),
    "lname": ("last_name", "family_name", "surname"),
}
COUNT_ALIASES = {
    "dev57_turn1": ("total_votes", "vote_count", "total_vote_count", "total_number_of_votes"),
    "dev16_turn2": ("country_count", "number_of_countries", "num_countries", "countries"),
    "dev254_turn1": ("student_count", "number_of_students", "total_students"),
    "dev254_turn2": ("student_count", "grade_9_count", "ninth_grade_count"),
    "dev123_turn1": ("dog_count", "treated_dog_count", "dogs_treated"),
    "dev125_turn1": ("female_students", "female_student_count"),
    "dev285_turn1": ("course_id_count", "course_count"),
    "dev285_turn3": ("teacher_count",),
    "dev282_turn2": ("student_count", "cat_owner_count", "students_with_cats"),
    "dev277_turn3": ("paragraph_count",),
    "dev225_turn2": ("occurrence_count",),
    "dev215_turn2": ("singer_count", "us_singer_count", "american_singer_count"),
    "dev292_turn2": ("total_unique_official_languages", "language_count", "official_language_count"),
    "dev85_turn3": ("person_count", "member_count", "visitor_count"),
    "dev85_turn5": ("person_count", "member_count", "visitor_count"),
}


def column_key(value):
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def answer_fields(old):
    """Derive a named projection from the question's reviewed reference metadata."""
    key = old["id"]
    grade = old["grade"]
    contract = CONTRACTS.get(key, "rows")
    if contract == "age":
        aliases = ("age", "age_years", "min_age", "youngest_age", "min(age)") if key.endswith("turn2") else (
            "age", "age_years", "max_age", "oldest_age", "max(age)"
        )
        return [sorted({column_key(alias) for alias in aliases})]
    if contract == "over_30_million":
        return [[column_key(name) for name in (
            "over_30_million", "exceeds_30_million", "is_over_30_million", "answer",
            "above_30_million", "over_threshold", "exceeds_threshold",
        )]]
    columns = grade["reference_result"]["columns"]
    old_columns = (grade.get("predicted_result") or {}).get("columns", [])
    groups = []
    for name in columns:
        compact = re.sub(r"\s+", "", name.casefold())
        names = {name, *ALIASES.get(compact, ())}
        if compact.startswith("count("):
            names.update({"count", "total_count", *COUNT_ALIASES.get(key, ())})
        if grade["matched"] and len(columns) == len(old_columns) == 1:
            names.add(old_columns[0])
        groups.append(sorted({column_key(alias) for alias in names}))
    return groups


def bind_fields(display, aliases):
    selected = []
    for field in aliases:
        found = [name for name in display["columns"] if column_key(name) in field]
        if len(found) != 1:
            return []
        selected.extend(found)
    return selected if len(selected) == len(set(selected)) else []


def score_answer(case, display, reference, reference_sql, normalize):
    if display is None:
        return {"matched": False, "method": "no_answer"}
    expected = normalize(case["contract"], reference, reference=True)
    columns = display["columns"]
    order_matters = "order by" in reference_sql.lower()
    try:
        actual = normalize(case["contract"], display)
        if (case["contract"] != "rows" or len(columns) == len(reference["columns"])) and result_eq(
                expected, actual, order_matters):
            return {"matched": True, "method": "answer_fields", "columns": columns}
    except (AttributeError, TypeError, ValueError):
        pass
    if case["contract"] == "full_name":
        # Prefer a single explicitly named full-name field, then first and last.
        selected = bind_fields(display, [("fullname", "name", "studentname")])
    else:
        selected = []
    if not selected:
        selected = bind_fields(display, case["answer_fields"])
    if not selected:
        return {"matched": False, "method": "required_fields_unidentified"}
    projected = {"columns": selected, "rows": project_rows(columns, display["rows"], selected)}
    try:
        actual = normalize(case["contract"], projected)
        return {"matched": result_eq(expected, actual, order_matters),
                "method": "fixed_field_projection", "columns": selected}
    except (AttributeError, TypeError, ValueError):
        return {"matched": False, "method": "required_answer_differs", "columns": selected}
