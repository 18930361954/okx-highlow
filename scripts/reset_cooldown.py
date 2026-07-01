"""紧急手动重置熔断 / 连亏计数。"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.db import DB  # noqa: E402
from core.account_state import AccountState  # noqa: E402


def main():
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    db = DB(ROOT / config["system"]["db_path"])
    account = AccountState(db, config)
    account.reset_cooldown()
    print("cooldown & consecutive_losses cleared")


if __name__ == "__main__":
    main()
