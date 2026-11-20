"""Run the existing answer MCP with room for the 502-turn cohort's large tables."""

import answer_submission_mcp


ROW_LIMIT = 4096


if __name__ == "__main__":
    answer_submission_mcp.ROW_LIMIT = ROW_LIMIT
    answer_submission_mcp.main()
