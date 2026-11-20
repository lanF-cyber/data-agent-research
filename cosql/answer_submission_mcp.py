"""Verify a structured answer against a dialogue's read-only SQLite copy."""

import argparse
import math
import re
import sqlite3
from pathlib import Path
from typing import TypedDict

from run_harness_baseline import FENCED_SQL, execute_readonly_sql


Cell = str | int | float | bool | None
ROW_LIMIT = 200
SERVER_NAME = "cosql"
TOOL_NAME = f"mcp__{SERVER_NAME}__submit_answer"


class Receipt(TypedDict):
    accepted: bool
    columns: list[str]
    rows: list[list[Cell]]
    error: str | None


def project_rows(columns, rows, answer_columns):
    names = [name.strip().casefold() for name in columns]
    requested = [name.strip().casefold() for name in answer_columns]
    if not requested or len(requested) != len(set(requested)):
        raise ValueError("Declare at least one distinct answer column.")
    positions = []
    for name, folded in zip(answer_columns, requested, strict=True):
        if names.count(folded) != 1:
            raise ValueError(f"Use an unambiguous SQL result alias for {name!r}.")
        positions.append(names.index(folded))
    return [[row[pos] for pos in positions] for row in rows]


def same_rows(submitted, executed):
    if len(submitted) != len(executed):
        return False
    for sent, actual in zip(submitted, executed, strict=True):
        if len(sent) != len(actual):
            return False
        for left, right in zip(sent, actual, strict=True):
            # SQLite returns 0/1 for Boolean expressions. Float values may
            # travel through a terminal's decimal representation first.
            if left == right:
                continue
            if type(left) is float and type(right) is float and math.isclose(left, right, rel_tol=1e-12):
                continue
            return False
    return True


def check_submission(database, sql, answer_columns=None, answer_rows=None) -> Receipt:
    try:
        sql = sql.strip()
        fence = FENCED_SQL.fullmatch(sql)
        if fence:
            sql = fence[1].strip()
        if not re.match(r"^(?:SELECT|WITH)\b", sql, flags=re.IGNORECASE):
            raise ValueError("Submit one read-only SQLite SELECT or WITH query.")
        columns, rows = execute_readonly_sql(database, sql, row_limit=ROW_LIMIT)
        if answer_columns is None:
            answer_columns = columns
        projection = project_rows(columns, rows, answer_columns)
        if any(type(cell) not in (str, int, float, type(None))
               or (type(cell) is float and not math.isfinite(cell))
               for row in projection for cell in row):
            raise ValueError("Answer columns must contain finite JSON scalar values.")
    except (ValueError, sqlite3.Error) as exc:
        return {"accepted": False, "columns": [], "rows": [], "error": str(exc)}

    accepted = answer_rows is None or same_rows(answer_rows, projection)
    return {
        "accepted": accepted,
        "columns": answer_columns,
        "rows": projection,
        "error": None if accepted else "Submitted answer rows differ from the selected SQL result rows.",
    }


def main():
    from mcp.server.fastmcp import FastMCP

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Path fixed by the harness for this dialogue")
    database = parser.parse_args().database.resolve(strict=True)
    if not database.is_file():
        parser.error("The dialogue database must be a file")

    server = FastMCP(SERVER_NAME)

    @server.tool()
    def submit_answer(sql: str, answer_columns: list[str] | None = None,
                      answer_rows: list[list[Cell]] | None = None) -> Receipt:
        """Run the final SQLite query and submit its actual result to the user.

        The tool obtains the rows directly from the session database. Optionally
        select which SQL result aliases to show in answer_columns. Otherwise all
        result columns are shown. Leave answer_rows unset to avoid copying data.
        If provided, it is checked against the actual rows in result order.
        """
        return check_submission(database, sql, answer_columns, answer_rows)

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
