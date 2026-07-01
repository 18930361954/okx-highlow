"""
每日盈亏报告：23:55 UTC 自动跑，或手动 `python scripts/daily_report.py [--date YYYY-MM-DD]`
输出到 docs/daily_reports/report_YYYY-MM-DD.md
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UTC = timezone.utc


def generate_report(db, account, config, target_date: str | None = None) -> Path:
    today = target_date or datetime.now(UTC).date().isoformat()
    # 今日执行的交易在 db 里 signal_date=昨日（昨日 K → 今日单）
    sig_date = (datetime.fromisoformat(today).date() - timedelta(days=1)).isoformat()
    trades = db.list_trades_by_date(sig_date)

    start_balance_str = db.get_state(f"balance_start_{today}")
    end_balance = account.get_balance()

    filled = [t for t in trades if t.get("exit_price") is not None]
    wins = sum(1 for t in filled if (t.get("pnl") or 0) > 0)
    losses = sum(1 for t in filled if (t.get("pnl") or 0) < 0)
    total_pnl = sum((t.get("pnl") or 0) for t in filled)

    if start_balance_str is None:
        start_balance = end_balance - total_pnl
    else:
        start_balance = float(start_balance_str)

    pnl_pct = (total_pnl / start_balance * 100) if start_balance > 0 else 0

    mode = "FIXED" if account.is_fixed_mode() else "PCT"
    in_cd = account.is_in_cooldown()
    consec = account.get_consecutive_losses()
    max_losses = account.max_losses

    lines: list[str] = []
    lines.append(f"# 每日盈亏报告 {today}")
    lines.append("")
    lines.append("## 概览")
    lines.append(f"- 起始余额: {start_balance:.2f} USDT")
    lines.append(f"- 结束余额: {end_balance:.2f} USDT")
    lines.append(f"- 当日盈亏: {total_pnl:+.2f} USDT ({pnl_pct:+.2f}%)")
    lines.append(f"- 当日交易: {len(filled)} 笔（盈 {wins} / 亏 {losses}）")
    lines.append("")

    lines.append("## 交易明细")
    if filled:
        lines.append("| 时间 | 品种 | 方向 | 入场 | 出场 | 原因 | PnL |")
        lines.append("|---|---|---|---|---|---|---|")
        for t in filled:
            ts = (t.get("exit_time") or "")[:19]
            lines.append(
                f"| {ts} | {t.get('pair', '')} | {t.get('side', '')} | "
                f"{t.get('entry_price', '')} | {t.get('exit_price', '')} | "
                f"{t.get('exit_reason', '')} | {(t.get('pnl') or 0):+.2f} |"
            )
    else:
        lines.append("（无成交）")
    lines.append("")

    pending = [t for t in trades if t.get("exit_price") is None]
    if pending:
        lines.append("## 未触发挂单")
        lines.append("| 品种 | 方向 | 触发价 | 保证金 | mode |")
        lines.append("|---|---|---|---|---|")
        for t in pending:
            lines.append(
                f"| {t.get('pair', '')} | {t.get('side', '')} | "
                f"{t.get('entry_price', '')} | {(t.get('margin') or 0):.2f} | "
                f"{t.get('mode', '')} |"
            )
        lines.append("")

    lines.append("## 状态")
    lines.append(f"- 连亏计数: {consec}/{max_losses}")
    lines.append(f"- 熔断: {'是' if in_cd else '否'}")
    lines.append(f"- 模式: {mode}")

    out_dir = ROOT / "docs" / "daily_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report_{today}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def main():
    import yaml
    from data.db import DB
    from core.account_state import AccountState
    from utils.logger import get_logger

    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD（默认今日 UTC）")
    args = ap.parse_args()

    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    logger = get_logger("daily_report", level=config["system"]["log_level"])
    db = DB(ROOT / config["system"]["db_path"])
    account = AccountState(db, config, logger=logger)

    out = generate_report(db, account, config, target_date=args.date)
    print(f"report saved → {out}")


if __name__ == "__main__":
    main()
