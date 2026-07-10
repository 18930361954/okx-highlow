"""一次性收拾脚本 —— 用于修完桶对齐/lotSz/funding 三件事后重启前。

做两件事:
  1. 撤 OKX 上 3 个模拟账户所有 pending algo(错桶挂的价 + 整张挂的单都作废)
  2. db 里所有未成交(entry_time=NULL AND exit_time=NULL)的 trades 标 ORPHAN
     不然重启后 reconciler 会去尝试改绑它们,徒劳且刷屏

跑完 db 干净、OKX 干净,可以放心 `python main.py` 重启。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from dotenv import load_dotenv

from core.okx_client import OKXClient
from data.db import DB

UTC = timezone.utc


def main() -> int:
    load_dotenv(ROOT / ".env")
    cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))

    db = DB(ROOT / cfg["system"]["db_path"])

    # 1) 撤 OKX pending
    accounts = cfg.get("accounts") or []
    for acc in accounts:
        if not acc.get("enabled", True):
            continue
        name = acc.get("account_name") or acc.get("name")
        env = acc.get("env_adapt") or acc.get("env") or "demo"
        okx = OKXClient(
            api_key=acc["api_key"],
            secret_key=acc["secret_key"],
            passphrase=acc["passphrase"],
            env=env,
        )
        try:
            pendings = okx.list_pending_algos(ordType="trigger")
        except Exception as e:
            print(f"[{name}] list_pending_algos 失败: {e}")
            continue
        if not pendings:
            print(f"[{name}] OKX 无 pending")
            continue
        cancelled = 0
        for o in pendings:
            algo_id = o.get("algoId")
            inst = o.get("instId")
            if not algo_id or not inst:
                continue
            try:
                okx.cancel_algo_order(algo_id, inst)
                cancelled += 1
                print(f"[{name}] 撤 {inst} algo={algo_id}")
            except Exception as e:
                print(f"[{name}] 撤 {inst} algo={algo_id} 失败: {e}")
        print(f"[{name}] 共撤 {cancelled}/{len(pendings)} 单")

    # 2) db 里 pending 标 ORPHAN
    now_iso = datetime.now(UTC).isoformat()
    import sqlite3
    con = sqlite3.connect(str(ROOT / cfg["system"]["db_path"]))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, account, pair, side, entry_price, signal_date "
        "FROM trades WHERE entry_time IS NULL AND exit_time IS NULL"
    ).fetchall()
    if not rows:
        print("db 无未成交 trade,跳过")
    else:
        print(f"标 ORPHAN: {len(rows)} 条")
        for r in rows:
            con.execute(
                "UPDATE trades SET exit_price=0, exit_reason='ORPHAN', "
                "pnl=0, fee=0, funding=0, exit_time=? WHERE id=?",
                (now_iso, r["id"]),
            )
            print(f"  #{r['id']} {r['pair']} {r['side']} @ {r['entry_price']}")
        con.commit()
    con.close()

    print("done. 可以重启 python main.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
