"""补拉历史成交的手续费(fee=0.0 但 exit_price 已成交的 trade)。

对每条 fee=0 的已闭合 trade:
  1. 用 okx_order_id (主 algoId) 查 OKX orders-history 找 entry order 的 fee
  2. 若拿到 exit_time,再在附近的 orders-history 找 reduceOnly=true 的 exit order fee
  3. 累加两个 fee (取绝对值)
  4. 更新 db.trades.fee
  5. 同时调整 account_state.balance = balance - 补的 fee (因 OKX 实际已扣,但 db 之前只加了名义 pnl)

用法:
  python scripts/refill_fees.py                          # 处理所有账户
  python scripts/refill_fees.py --account 实盘-主账户    # 只处理指定账户
  python scripts/refill_fees.py --dry-run                # 只显示不写入
"""
import argparse
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.db import DB  # noqa: E402
from core.okx_client import OKXClient  # noqa: E402
from core.account_state import AccountState  # noqa: E402


def _fee_of(o: dict | None) -> float:
    if not o:
        return 0.0
    v = o.get("fee")
    if v in (None, ""):
        return 0.0
    try:
        return abs(float(v))
    except (TypeError, ValueError):
        return 0.0


def _find_fees(okx: OKXClient, pair: str, algo_id: str, entry_ts_ms: int) -> tuple[float, dict]:
    """返回 (总 fee, breakdown)。"""
    breakdown = {"entry": 0.0, "exit": 0.0, "matched_entry_ord": None,
                 "matched_exit_ord": None}
    try:
        orders = okx.list_order_history(instId=pair, state="filled", limit=100)
    except Exception as e:
        print(f"  ! list_order_history({pair}) 失败: {e}")
        return 0.0, breakdown

    entry_fee = 0.0
    exit_fee = 0.0

    for o in orders:
        aid = o.get("algoId") or ""
        if aid != algo_id:
            continue
        reduce_only = str(o.get("reduceOnly", "")).lower() == "true"
        if reduce_only:
            exit_fee += _fee_of(o)
            breakdown["matched_exit_ord"] = o.get("ordId")
        else:
            entry_fee += _fee_of(o)
            breakdown["matched_entry_ord"] = o.get("ordId")

    # 主 algo 上找不到 exit (TP/SL 独立 algoId) → 按时间窗口 + reduceOnly 兜底
    if exit_fee == 0.0 and entry_ts_ms > 0:
        for o in orders:
            try:
                ft = int(o.get("fillTime") or o.get("uTime") or 0)
            except (TypeError, ValueError):
                ft = 0
            if ft <= entry_ts_ms:
                continue
            if str(o.get("reduceOnly", "")).lower() != "true":
                continue
            exit_fee += _fee_of(o)
            breakdown["matched_exit_ord"] = o.get("ordId")
            break

    breakdown["entry"] = entry_fee
    breakdown["exit"] = exit_fee
    return entry_fee + exit_fee, breakdown


def _adjust_balance(account_state: AccountState, delta_fee: float) -> None:
    """补 fee 时,db balance 之前是按名义 pnl 加的,现在减去补的 fee。"""
    cur = account_state.get_balance()
    account_state.set_balance(cur - delta_fee)


def _build_okx_for(cfg_raw: dict) -> OKXClient:
    env_raw = str(cfg_raw.get("env_adapt") or cfg_raw.get("env") or "demo").lower()
    env = "live" if env_raw in ("real", "live", "prod", "production") else "demo"
    return OKXClient(
        cfg_raw["api_key"], cfg_raw["secret_key"], cfg_raw["passphrase"],
        env=env,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=None, help="只处理指定账户名")
    ap.add_argument("--dry-run", action="store_true", help="只显示不写入")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    db = DB(ROOT / cfg["system"]["db_path"])

    # 账户 → OKXClient 映射
    acc_raw: dict[str, dict] = {}
    for raw in cfg.get("accounts") or []:
        name = str(raw.get("account_name") or raw.get("name") or "")
        if not name:
            continue
        acc_raw[name] = raw

    if args.account:
        target_accounts = [args.account]
    else:
        target_accounts = list(acc_raw.keys())

    total_updated = 0
    total_fee_added = 0.0

    for name in target_accounts:
        if name not in acc_raw:
            print(f"[{name}] 未在 config.yaml 找到,跳过")
            continue
        raw = acc_raw[name]
        if not raw.get("api_key"):
            print(f"[{name}] 无 api_key,跳过")
            continue

        try:
            okx = _build_okx_for(raw)
        except Exception as e:
            print(f"[{name}] 建 OKXClient 失败: {e}")
            continue

        # 拉 fee=0 且 exit_price 已成交的 trade
        import sqlite3
        c = sqlite3.connect(str(db.path))
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id, pair, entry_time, exit_time, okx_order_id, pnl, fee "
            "FROM trades WHERE account=? AND exit_price IS NOT NULL AND "
            "(fee IS NULL OR fee = 0.0) ORDER BY id",
            (name,)
        ).fetchall()
        c.close()

        if not rows:
            print(f"[{name}] 无需补 fee(fee=0 且已闭合的 0 条)")
            continue

        print(f"[{name}] 找到 {len(rows)} 条待补 fee 的 trade")
        acc_state = AccountState(db, {"strategy": cfg["strategy"]}, account=name)
        acc_fee_added = 0.0

        for r in rows:
            trade_id = r["id"]
            pair = r["pair"]
            algo_id = r["okx_order_id"] or ""
            if not algo_id:
                print(f"  #{trade_id} {pair}: 无 algoId,跳过")
                continue

            entry_ts_ms = 0
            if r["entry_time"]:
                from datetime import datetime
                try:
                    entry_ts_ms = int(
                        datetime.fromisoformat(r["entry_time"]).timestamp() * 1000
                    )
                except Exception:
                    entry_ts_ms = 0

            fee, bd = _find_fees(okx, pair, algo_id, entry_ts_ms)
            if fee <= 0:
                print(f"  #{trade_id} {pair} algoId={algo_id[:12]}: OKX 未匹配到 fee,跳过")
                continue

            pnl = r["pnl"] or 0
            net = pnl - fee
            print(f"  #{trade_id} {pair}: pnl={pnl:+.4f} fee={fee:.4f} net={net:+.4f} "
                  f"(entry {bd['entry']:.4f} + exit {bd['exit']:.4f})")

            if args.dry_run:
                continue

            # 更新 db
            with db._conn() as cc:
                cc.execute("UPDATE trades SET fee=? WHERE id=?", (fee, trade_id))
            _adjust_balance(acc_state, fee)
            acc_fee_added += fee
            total_updated += 1

        total_fee_added += acc_fee_added
        if not args.dry_run:
            print(f"[{name}] 补 fee 合计: {acc_fee_added:.4f} USDT,余额已扣减")
        else:
            print(f"[{name}] --dry-run,未写入")

    if not args.dry_run:
        print(f"\n=== 共补 {total_updated} 条,总 fee 扣减 {total_fee_added:.4f} USDT ===")
    else:
        print(f"\n=== dry-run: 预计补 {len([r for r in rows])} 条 ===")


if __name__ == "__main__":
    main()
