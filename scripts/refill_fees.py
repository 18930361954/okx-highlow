"""从 OKX 补拉历史 trade 的 pnl 与 fee 真值。

背景:
  reconciler 之前会在 OKX 无 pnl 字段时按 margin*lev*pct 本地估算,与 OKX 真值有差异。
  现在已移除估算,但历史 db 里可能仍有本地估算值或 fee=0 的记录。
  本脚本从 OKX orders-history 补拉真值,同时修正 db.balance。

用法:
  python scripts/refill_fees.py                          # 处理所有账户
  python scripts/refill_fees.py --account 实盘-主账户    # 只处理指定账户
  python scripts/refill_fees.py --dry-run                # 只显示不写入
  python scripts/refill_fees.py --force                  # 强制刷新所有已闭合 trade (即使 fee != 0)
"""
import argparse
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.db import DB  # noqa: E402
from core.okx_client import OKXClient  # noqa: E402
from core.account_state import AccountState  # noqa: E402


def _num(o: dict, k: str) -> float:
    v = o.get(k)
    if v in (None, ""):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _find_related_orders(okx: OKXClient, pair: str, algo_id: str,
                          entry_ts_ms: int) -> list[dict]:
    """返回该 trade 全部相关 orders:
    - 主 algoId 下的全部 orders (entry + partial fills)
    - 时间窗口 + reduceOnly=true 匹配的 exit orders (TP/SL attach algoId 独立)
    """
    try:
        orders = okx.list_order_history(instId=pair, state="filled", limit=100)
    except Exception as e:
        print(f"  ! list_order_history({pair}) 失败: {e}")
        return []

    related: list[dict] = []
    # a) 主 algoId 下的全部
    for o in orders:
        if o.get("algoId") == algo_id:
            related.append(o)

    # b) 时间窗口匹配的 exit (algoId 可能不同)
    if entry_ts_ms > 0:
        for o in orders:
            aid = o.get("algoId") or ""
            if aid == algo_id:
                continue  # 已经在 a 里加过
            try:
                ft = int(o.get("fillTime") or o.get("uTime") or 0)
            except (TypeError, ValueError):
                ft = 0
            if ft <= entry_ts_ms:
                continue
            if str(o.get("reduceOnly", "")).lower() != "true":
                continue
            # 只取一个(第一个符合的)。分批平仓的场景暂不处理
            related.append(o)
            break

    return related


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
    ap.add_argument("--force", action="store_true",
                    help="即使 db 里 fee!=0 也重新拉 OKX 覆盖")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    db = DB(ROOT / cfg["system"]["db_path"])

    acc_raw: dict[str, dict] = {}
    for raw in cfg.get("accounts") or []:
        name = str(raw.get("account_name") or raw.get("name") or "")
        if name:
            acc_raw[name] = raw

    if args.account:
        target_accounts = [args.account]
    else:
        target_accounts = list(acc_raw.keys())

    total_updated = 0
    total_balance_delta = 0.0

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

        # 需要补的 trade
        import sqlite3
        c = sqlite3.connect(str(db.path))
        c.row_factory = sqlite3.Row
        if args.force:
            where = ("SELECT id, pair, entry_time, exit_time, okx_order_id, pnl, fee "
                     "FROM trades WHERE account=? AND exit_price IS NOT NULL ORDER BY id")
        else:
            where = ("SELECT id, pair, entry_time, exit_time, okx_order_id, pnl, fee "
                     "FROM trades WHERE account=? AND exit_price IS NOT NULL "
                     "AND (fee IS NULL OR fee = 0.0) ORDER BY id")
        rows = c.execute(where, (name,)).fetchall()
        c.close()

        if not rows:
            print(f"[{name}] 无需补 (0 条)")
            continue

        print(f"[{name}] 找到 {len(rows)} 条待处理 trade")
        acc_state = AccountState(db, {"strategy": cfg["strategy"]}, account=name)
        acc_delta = 0.0

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

            related = _find_related_orders(okx, pair, algo_id, entry_ts_ms)
            if not related:
                print(f"  #{trade_id} {pair} algoId={algo_id[:12]}: OKX 未找到相关 orders,跳过")
                continue

            pnl_new = sum(_num(o, "pnl") for o in related)
            fee_raw = sum(_num(o, "fee") for o in related)  # 负值
            fee_new = abs(fee_raw)
            net_new = pnl_new + fee_raw  # fee 负值直接加

            pnl_old = r["pnl"] or 0
            fee_old = r["fee"] or 0
            net_old = pnl_old - fee_old  # 旧口径是 pnl - abs(fee)
            # balance 之前按 net_old 加过 → 补差价
            delta = net_new - net_old

            print(f"  #{trade_id} {pair}: "
                  f"pnl {pnl_old:+.4f}→{pnl_new:+.4f}, "
                  f"fee {fee_old:.4f}→{fee_new:.4f}, "
                  f"net {net_old:+.4f}→{net_new:+.4f} "
                  f"(balance Δ {delta:+.4f}, 合并 {len(related)} 个 OKX orders)")

            if args.dry_run:
                continue

            with db._conn() as cc:
                cc.execute(
                    "UPDATE trades SET pnl=?, fee=? WHERE id=?",
                    (pnl_new, fee_new, trade_id),
                )
            # balance 加上差价
            cur_bal = acc_state.get_balance()
            acc_state.set_balance(cur_bal + delta)
            acc_delta += delta
            total_updated += 1

        total_balance_delta += acc_delta
        if not args.dry_run:
            print(f"[{name}] 更新完成,余额修正 Δ = {acc_delta:+.4f} USDT")
        else:
            print(f"[{name}] --dry-run,未写入")

    if not args.dry_run:
        print(f"\n=== 共更新 {total_updated} 条,总余额修正 {total_balance_delta:+.4f} USDT ===")


if __name__ == "__main__":
    main()
