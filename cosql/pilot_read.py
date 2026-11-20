import json
from pathlib import Path

dev_file = Path(__file__).parent / "data/cosql_dataset/sql_state_tracking/cosql_dev.json"
dialogues = json.loads(dev_file.read_text(encoding="utf-8"))

for number, turn in enumerate(dialogues[0]["interaction"], start=1):
    print(f"{number}. {turn['utterance']}")