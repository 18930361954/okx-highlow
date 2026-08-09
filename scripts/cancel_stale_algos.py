"""精准撤掉指定 trade 的过期挂单 —— 断网导致 daily_cancel 失败时的补救。

与 cleanup_before_restart.py 的区别: 那个无差别撤全部 pending 并标 ORPHAN;
这个只动命令行显式点名的 trade id, 且撤前逐项校验(桶已过期、未成交、algoId 匹配),
任一不符就跳过。持仓与 OCO 保护单绝不触碰。

源码模式下 APP_ROOT 是项目根, 但正在运行的 config/db 通常在某个 dist/hlbot-vX 目录下,
所以要用 --root 显式指向那个目录, 否则会对着空库空转。

用法:
    python scripts/cancel_stale_algos.py --root dist/hlbot-v1.0.3 894 897          # 预演
    python scripts/cancel_stale_algos.py --root dist/hlbot-v1.0.3 894 897 --apply  # 执行
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
from utils.paths import APP_ROOT as ROOT  # noqa: E402 frozen 下指向 exe 旁

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from core.okx_client import OKXClient  # noqa: E402

UTC = timezone.utc

BAR_HOURS = {"1H": 1, "2H": 2, "4H": 4, "6H": 6, "12H": 12, "1D": 24}


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    argv = [a for a in argv if a != "--apply"]

    root = ROOT
    if "--root" in argv:
        i = argv.index("--root")
        if i + 1 >= len(argv):
            print("--root 后面要跟目录")
            return 2
        root = Path(argv[i + 1]).resolve()
        argv = argv[:i] + argv[i + 2:]
    if not (root / "config.yaml").exists():
        print(f"{root} 下没有 config.yaml —— 用 --root 指向正在运行的 dist 目录")
        return 2

    if not argv:
        print(__doc__)
        return 2
    try:
        ids = [int(a) for a in argv]
    except ValueError:
        print(f"trade id 必须是整数: {argv}")
        return 2

    load_dotenv(root / ".env")
    cfg = yaml.safe_load(open(root / "config.yaml", encoding="utf-8"))
    db_path = root / cfg["system"]["db_path"]
    print(f"目标目录: {root}")
    print(f"数据库:   {db_path}\n")

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    clients: dict[str, OKXClient] = {}
    for acc in cfg.get("accounts") or []:
        if not acc.get("enabled", True):
            continue
        name = acc.get("account_name") or acc.get("name")
        clients[name] = OKXClient(
            api_key=acc["api_key"], secret_key=acc["secret_key"],
            passphrase=acc["passphrase"],
            env=acc.get("env_adapt") or acc.get("env") or "demo",
        )

    now = datetime.now(UTC)
    done = 0
    for tid in ids:
        r = con.execute(
            "SELECT id, account, pair, side, signal_date, signal_bar, "
            "entry_time, exit_time, okx_order_id FROM trades WHERE id=?", (tid,)
        ).fetchone()
        if r is None:
            print(f"#{tid} 库里没有,跳过")
            continue
        tag = f"#{tid} {r['account']} {r['pair']} {r['side']}"
        if r["entry_time"]:
            print(f"{tag} 已成交(entry_time={r['entry_time']}),拒绝撤单")
            continue
        if r["exit_time"]:
            print(f"{tag} 已是终态,跳过")
            continue
        algo_id = r["okx_order_id"]
        if not algo_id:
            print(f"{tag} 无 algoId,跳过")
            continue

        # 桶必须真的过期了才撤 —— 防手滑撤掉当前桶的有效挂单
        try:
            start = datetime.strptime(r["signal_date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)
            end = start + timedelta(hours=BAR_HOURS[r["signal_bar"]])
        except (ValueError, KeyError) as e:
            print(f"{tag} 桶信息无法解析({r['signal_date']}/{r['signal_bar']}): {e},跳过")
            continue
        if now < end:
            print(f"{tag} 桶 {r['signal_date']}({r['signal_bar']}) 尚未结束(至 {end:%m-%d %H:%M}),跳过")
            continue

        okx = clients.get(r["account"])
        if okx is None:
            print(f"{tag} 账户不在启用列表,跳过")
            continue

        over_h = (now - end).total_seconds() / 3600
        if not apply:
            print(f"{tag} 桶 {r['signal_date']}({r['signal_bar']}) 超期 {over_h:.1f}h "
                  f"algo={algo_id} → 待撤 [预演]")
            continue

        try:
            okx.cancel_algo_order(algo_id, r["pair"])
        except Exception as e:
            # 已被 OKX 撤掉/不存在也算达成目的, 继续同步 db
            print(f"{tag} 撤单返回异常(可能已不存在): {e}")
        con.execute(
            "UPDATE trades SET exit_price=0, exit_reason='CANCELLED', pnl=0, fee=0, "
            "funding=0, exit_time=? WHERE id=? AND entry_time IS NULL AND exit_time IS NULL",
            (now.isoformat(), tid),
        )
        con.commit()
        print(f"{tag} 桶超期 {over_h:.1f}h algo={algo_id} → 已撤 + db 标 CANCELLED")
        done += 1

    con.close()
    if not apply:
        print("\n以上为预演。确认无误后加 --apply 执行。")
    else:
        print(f"\n完成: {done} 张已撤。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
