"""一次性收尾:把 2026-07-09 事故遗留的未成交 db trade 安全平掉。

背景:
  20:00 UTC+8 事故触发老 reconciler 的错绑逻辑,把 trade#8 (T00 short) 和
  trade#14 (T04 long) 错绑到了 20:00 timeout 生成的 2 张 long 孤儿 algoId
  (clOrdId=hlETH20260709T080l1)。trade#16 (T08 long) 的 51149 回查失败直接
  存了 algoId=None。

  经查:
  - trade#8 原始 algoId 3726796959718457344: state=canceled, triggerTime=0, ordIdList=[]
  - trade#8 错绑的 algoId 3727763277330419712: state=canceled, triggerTime=0, ordIdList=[]
  - trade#14 原始 3727280207024259072: state=canceled, triggerTime=0, ordIdList=[]
  - trade#14 错绑的 3727763279544991744: state=canceled, triggerTime=0, ordIdList=[]
  - trade#16 algoId=None,同桶的 clOrdId=hlETH20260709T080l1 那两张也 canceled
  - positions-history 里没有对应桶 (T00/T04/T08) 的开平仓记录

  结论:三条 trade 从未成交。db 里挂着 exit_price=None 让 reconciler 每轮都
  当"open trade"扫,占位没意义。→ 标 exit_reason='ORPHAN' pnl=0 fee=0 收干净。

用法:
  python scripts/fix_orphan_trades.py                       # dry-run,只验证
  python scripts/fix_orphan_trades.py --apply               # 真写入
  python scripts/fix_orphan_trades.py --trade-ids 8,14,16   # 默认这三个
  python scripts/fix_orphan_trades.py --account 模拟盘-主账户

流程:
  1) 对每条 trade,拉 okx_order_id 对应 algo 的当前状态
     - state 必须是 canceled/order_failed,且 ordIdList 为空/triggerTime=0
     - 否则中止:说明其实成交了,不该走 ORPHAN 路径
  2) --apply 时,update_trade_exit(exit_price=0, exit_reason='ORPHAN', pnl=0, fee=0)
  3) 不改 balance
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.db import DB  # noqa: E402
from core.okx_client import OKXClient  # noqa: E402

UTC = timezone.utc


def _build_okx_for(cfg_raw: dict) -> OKXClient:
    env_raw = str(cfg_raw.get("env_adapt") or cfg_raw.get("env") or "demo").lower()
    env = "live" if env_raw in ("real", "live", "prod", "production") else "demo"
    return OKXClient(
        cfg_raw["api_key"], cfg_raw["secret_key"], cfg_raw["passphrase"], env=env,
    )


def verify_and_orphan(db: DB, okx: OKXClient, account: str, trade_id: int,
                      apply: bool) -> bool:
    """校验一条 db trade 确实未成交,--apply 时标 ORPHAN 平掉。返回是否处理成功。"""
    with db._conn() as c:
        row = c.execute(
            "SELECT * FROM trades WHERE id=? AND account=?", (trade_id, account),
        ).fetchone()
    if not row:
        print(f"  #{trade_id}: 未找到该账户下的 trade,跳过")
        return False
    r = dict(row)
    if r.get("exit_price") is not None:
        print(f"  #{trade_id}: 已闭合 (exit_price={r['exit_price']}),跳过")
        return False
    if r.get("entry_time"):
        print(f"  #{trade_id}: entry_time={r['entry_time']} 已入场,是活持仓,"
              f"拒绝标 ORPHAN")
        return False

    pair = r["pair"]
    side = r["side"]
    signal_date = r["signal_date"]
    algo_id = r.get("okx_order_id") or ""

    print(f"\n  #{trade_id} {pair} db side={side} sig={signal_date} "
          f"algoId={algo_id or 'NONE'}")

    # 无 algoId (trade#16 情况):不能验证 OKX 状态,但既然 51149 timeout 就没拿到 id
    # 且当天日志/positions-history 都没成交,视为未成交
    if not algo_id:
        print(f"    (algoId=None: 挂单时 51149 timeout 回查未命中,视为未成交)")
    else:
        try:
            r_okx = okx.get_algo_order(algoId=algo_id)
        except Exception as e:
            print(f"    [X] get_algo_order 失败: {e}")
            return False
        if r_okx is None:
            print(f"    [X] OKX 未找到该 algoId (可能已被清理),仍按未成交处理")
        else:
            state = r_okx.get("state") or ""
            trigger_time = r_okx.get("triggerTime") or "0"
            ord_ids = r_okx.get("ordIdList") or []
            print(f"    OKX state={state}  triggerTime={trigger_time}  ordIdList={ord_ids}")
            if state not in ("canceled", "order_failed"):
                print(f"    [X] state={state} 不是 canceled/order_failed,拒绝标 ORPHAN"
                      f"(可能已成交,请手动核对)")
                return False
            if str(trigger_time) != "0" or ord_ids:
                print(f"    [X] triggerTime/ordIdList 显示有过成交痕迹,拒绝标 ORPHAN")
                return False

    if not apply:
        print(f"    [dry-run] 将标 exit_reason=ORPHAN, pnl=0, fee=0")
        return True

    exit_time = datetime.now(UTC).isoformat()
    try:
        db.update_trade_exit(
            trade_id=trade_id,
            exit_price=0.0,
            exit_reason="ORPHAN",
            pnl=0.0,
            exit_time=exit_time,
            fee=0.0,
        )
        print(f"    [OK] 已标 ORPHAN,exit_time={exit_time}")
        return True
    except Exception as e:
        print(f"    [X] 写入 db 失败: {e}")
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="模拟盘-主账户", help="账户名")
    ap.add_argument("--trade-ids", default="8,14,16", help="逗号分隔 trade id 列表")
    ap.add_argument("--apply", action="store_true", help="真写入 (默认 dry-run)")
    args = ap.parse_args()

    trade_ids = [int(x.strip()) for x in args.trade_ids.split(",") if x.strip()]

    load_dotenv(ROOT / ".env")
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    db = DB(ROOT / cfg["system"]["db_path"])

    acc_raw = None
    for raw in cfg.get("accounts") or []:
        name = str(raw.get("account_name") or raw.get("name") or "")
        if name == args.account:
            acc_raw = raw
            break
    if not acc_raw:
        print(f"[{args.account}] 未在 config.yaml 找到")
        sys.exit(1)
    if not acc_raw.get("api_key"):
        print(f"[{args.account}] 无 api_key")
        sys.exit(1)

    okx = _build_okx_for(acc_raw)
    print(f"[{args.account}] 校验并标 ORPHAN: trade ids = {trade_ids}  "
          f"mode = {'APPLY' if args.apply else 'DRY-RUN'}")

    ok_count = 0
    for tid in trade_ids:
        if verify_and_orphan(db, okx, args.account, tid, args.apply):
            ok_count += 1

    print(f"\n=== 处理成功 {ok_count}/{len(trade_ids)} 条 ===")
    if not args.apply:
        print("--dry-run:未写入。加 --apply 真执行。")


if __name__ == "__main__":
    main()
