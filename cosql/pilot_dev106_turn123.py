import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

root = Path(__file__).resolve().parent
data = root / "data" / "cosql_dataset"
reports = root / "reports"

dev_file = data / "sql_state_tracking" / "cosql_dev.json"
dialogue = json.loads(dev_file.read_text(encoding="utf-8"))[106]
reference = "\n".join(turn["query"] for turn in dialogue["interaction"])

prediction = reports / "pilot_dev106_turn123.sql"
prediction_sql = prediction.read_text(encoding="utf-8").strip()
gold = reports / "pilot_dev106_turn123_gold.txt"
gold.write_text(
    "".join(f"{sql}\t{dialogue['database_id']}\n" for sql in reference.splitlines()),
    encoding="utf-8",
)

evaluator = root / "upstream" / "test-suite-sql-eval" / "evaluation.py"
result = subprocess.run(
    [
        sys.executable, "-X", "utf8", str(evaluator),
        "--gold", str(gold),
        "--pred", str(prediction),
        "--db", str(data / "database"),
        "--etype", "exec", "--keep_distinct",
    ],
    stdout=subprocess.PIPE,
    encoding="utf-8",
)
print(result.stdout, end="")
result.check_returncode()

score_log = reports / "pilot_dev106_turn123_score.txt"
timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
with score_log.open("a", encoding="utf-8") as log:
    log.write(
        f"\n{timestamp} CoSQL dev[106] 第 1-3 轮（{dialogue['database_id']}）\n"
        f"\n预测 SQL\n{prediction_sql}\n"
        f"\n参考 SQL\n{reference}\n"
        f"\n{result.stdout.rstrip()}\n"
    )
print(f"评分记录已保存到 {score_log}")
