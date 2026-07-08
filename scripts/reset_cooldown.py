"""紧急手动重置熔断 / 连亏计数。

用法:
  # 清 default 账户(单账户模式)
  python scripts/reset_cooldown.py

  # 清指定账户
  python scripts/reset_cooldown.py --account 实盘-bot14559

  # 清所有已配账户
  python scripts/reset_cooldown.py --all
"""
import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.db import DB, DEFAULT_ACCOUNT  # noqa: E402
from core.account_state import AccountState  # noqa: E402


def _reset(db, config, account_name: str) -> None:
    acc = AccountState(db, config, account=account_name)
    acc.reset_cooldown()
    print(f"[{account_name}] cooldown & consecutive_losses cleared")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=DEFAULT_ACCOUNT,
                    help=f"账户名(默认 {DEFAULT_ACCOUNT})")
    ap.add_argument("--all", action="store_true",
                    help="清 config.yaml accounts 段里所有账户 + db 里的其他账户")
    args = ap.parse_args()

    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    db = DB(ROOT / config["system"]["db_path"])

    if args.all:
        seen: set[str] = set()
        for raw in config.get("accounts") or []:
            name = str(raw.get("account_name") or raw.get("name") or "")
            if name and name not in seen:
                _reset(db, config, name)
                seen.add(name)
        # db 里可能有历史 account 也一并清(比如 default)
        for a in db.list_accounts():
            if a not in seen:
                _reset(db, config, a)
    else:
        _reset(db, config, args.account)


if __name__ == "__main__":
    main()
