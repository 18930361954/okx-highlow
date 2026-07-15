"""手动把本地余额同步到 OKX 真值 —— 充值/提现后跑一次。

正常情况不需要手动跑: reconciler 每次平仓结算后会自动对齐 OKX 余额。
本脚本用于「充值后想立刻生效」或「长期没有成交、余额一直没机会自动同步」的场景。

用法:
  # 同步 config.yaml 里所有 enabled 账户
  python scripts/sync_balance.py

  # 只同步指定账户
  python scripts/sync_balance.py --account 初级炼气士-实盘

  # 有持仓也强制同步 (注意: OKX eq 含未实现盈亏,会污染余额,慎用)
  python scripts/sync_balance.py --force
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from dotenv import load_dotenv

from core.account_state import AccountState
from core.multi_account import _build_account_config
from core.okx_client import OKXClient
from data.db import DB


def _sync_one(db, cfg, force: bool) -> None:
    name = cfg.name
    okx = OKXClient(
        cfg.api_key, cfg.secret_key, cfg.passphrase,
        env=cfg.env, proxy_url=cfg.proxy_url,
    )
    acc = AccountState(db, cfg.to_legacy_config(), account=name)

    try:
        held = [
            p["instId"] for p in okx.get_positions()
            if float(p.get("pos", 0) or 0) != 0
        ]
    except Exception as e:
        print(f"[{name}] get_positions 失败,跳过: {e}")
        return
    if held and not force:
        print(f"[{name}] 有持仓 {held},OKX 余额含未实现盈亏,跳过 (--force 可强制)")
        return

    try:
        okx_bal = float(okx.get_balance("USDT"))
    except Exception as e:
        print(f"[{name}] get_balance 失败,跳过: {e}")
        return
    if okx_bal <= 0:
        print(f"[{name}] OKX 余额 {okx_bal},疑似异常,跳过")
        return

    local = acc.get_balance()
    if abs(okx_bal - local) < 0.01:
        print(f"[{name}] 本地 {local:.2f} == OKX {okx_bal:.2f},无需同步")
        return
    acc.set_balance(okx_bal)
    print(f"[{name}] 本地 {local:.2f} → OKX {okx_bal:.2f} USDT (差 {okx_bal - local:+.2f}) 已同步")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", help="只同步该账户 (默认全部 enabled 账户)")
    ap.add_argument("--force", action="store_true",
                    help="有持仓也强制同步 (OKX eq 含未实现盈亏,慎用)")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    db = DB(ROOT / config["system"]["db_path"])

    matched = False
    for raw in config.get("accounts") or []:
        name = str(raw.get("account_name") or raw.get("name") or "")
        if args.account and name != args.account:
            continue
        cfg = _build_account_config(name, raw, config)
        if not args.account and not cfg.enabled:
            continue
        matched = True
        _sync_one(db, cfg, force=args.force)

    if not matched:
        print(f"没有匹配的账户: {args.account or '(enabled 全部为空)'}")
        sys.exit(1)


if __name__ == "__main__":
    main()
