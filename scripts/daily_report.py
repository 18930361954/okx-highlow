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


def _dd_flag(dd_pct: float) -> str:
    if dd_pct >= 60:
        return "🔴 严重"
    if dd_pct >= 40:
        return "🟡 警戒"
    if dd_pct >= 20:
        return "🟢 正常"
    return "✅ 良好"


def _build_balance_series(db, up_to_today: str) -> list[tuple[str, float]]:
    """从全部 trade 记录重建"每日结束时 balance"序列。
    只用 exit_time 有值的（已结算）trade。
    """
    all_trades = db.list_trades(limit=10000)
    filled = [t for t in all_trades if t.get("pnl") is not None]
    filled.sort(key=lambda t: (t.get("exit_time") or "", t.get("id", 0)))

    # 假设起始 balance = 当前 balance - sum(所有 pnl)
    cur = 0.0
    for t in filled:
        cur += t.get("pnl") or 0
    # 但这只给了"当前减去所有 pnl"，需要真实起点。用 state 里的 current_balance 反推
    real_current = float(db.get_state("current_balance") or "0")
    start = real_current - cur

    # 按 exit 日聚合
    by_day: dict[str, float] = {}
    running = start
    for t in filled:
        day = (t.get("exit_time") or "")[:10]
        if not day:
            continue
        running += t.get("pnl") or 0
        by_day[day] = running

    days = sorted(by_day.keys())
    return [(d, by_day[d]) for d in days if d <= up_to_today]


def _sparkline(vals: list[float]) -> str:
    """极简 ASCII 折线：8 级高度。"""
    if not vals or len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return "─" * len(vals)
    levels = "▁▂▃▄▅▆▇█"
    out = []
    for v in vals:
        idx = int((v - lo) / (hi - lo) * (len(levels) - 1))
        out.append(levels[idx])
    return "".join(out)


def generate_report(db, account, config, target_date: str | None = None) -> Path:
    today = target_date or datetime.now(UTC).date().isoformat()
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

    # === balance 曲线 + DD ===
    series = _build_balance_series(db, today)
    if series:
        vals = [v for _, v in series]
        peak = max(vals)
        cur_dd = ((peak - end_balance) / peak * 100) if peak > 0 else 0
        # 计算历史最大 DD
        max_dd = 0.0
        running_peak = vals[0]
        for v in vals:
            running_peak = max(running_peak, v)
            dd = (running_peak - v) / running_peak * 100 if running_peak > 0 else 0
            max_dd = max(max_dd, dd)
        # 最近 30 天
        recent = series[-30:] if len(series) > 30 else series
        recent_vals = [v for _, v in recent]
        spark = _sparkline(recent_vals)
        first_d, first_v = recent[0]
        last_d, last_v = recent[-1]
        recent_range = f"{first_d} {first_v:.1f} → {last_d} {last_v:.1f}"
    else:
        peak = end_balance
        cur_dd = 0.0
        max_dd = 0.0
        spark = ""
        recent_range = "（尚无历史数据）"

    # === 分 pair 当日汇总 ===
    by_pair: dict[str, dict] = {}
    for t in filled:
        p = t.get("pair", "?")
        d = by_pair.setdefault(p, {"n": 0, "w": 0, "l": 0, "pnl": 0.0})
        d["n"] += 1
        pnl = t.get("pnl") or 0
        d["pnl"] += pnl
        if pnl > 0:
            d["w"] += 1
        elif pnl < 0:
            d["l"] += 1

    lines: list[str] = []
    lines.append(f"# 每日盈亏报告 {today}")
    lines.append("")

    # === 总览（加 DD 警戒色）===
    lines.append("## 总览")
    lines.append(f"| 指标 | 值 | 状态 |")
    lines.append(f"|---|---|---|")
    lines.append(f"| 起始余额 | {start_balance:.2f} USDT | — |")
    lines.append(f"| 结束余额 | {end_balance:.2f} USDT | — |")
    lines.append(f"| 当日盈亏 | {total_pnl:+.2f} USDT ({pnl_pct:+.2f}%) | — |")
    lines.append(f"| 当日成交 | {len(filled)} 笔（盈 {wins} / 亏 {losses}）| — |")
    lines.append(f"| 历史峰值 | {peak:.2f} USDT | — |")
    lines.append(f"| **当前回撤** | **{cur_dd:.2f}%** | **{_dd_flag(cur_dd)}** |")
    lines.append(f"| 历史最大回撤 | {max_dd:.2f}% | {_dd_flag(max_dd)} |")
    lines.append("")
    lines.append("> **回撤** = 当前余额相比历史最高余额跌了多少百分比。例如"
                 "峰值 200U → 当前 120U，回撤 = (200-120)/200 = 40%。"
                 "**回撤 40% 建议减仓，60% 建议暂停策略手动复盘。**")
    lines.append(f"| 连亏计数 | {consec}/{max_losses} | {'🔴' if consec >= max_losses - 1 else '✅'} |")
    lines.append(f"| 熔断状态 | {'🔴 是' if in_cd else '✅ 否'} | — |")
    lines.append(f"| 保证金模式 | {mode} | — |")
    lines.append("")

    # === 分 pair 汇总 ===
    if by_pair:
        lines.append("## 分 pair 汇总（今日）")
        lines.append("| Pair | 笔数 | 盈 | 亏 | PnL |")
        lines.append("|---|---|---|---|---|")
        for p, d in by_pair.items():
            lines.append(f"| {p} | {d['n']} | {d['w']} | {d['l']} | {d['pnl']:+.2f} |")
        lines.append("")

    # === Balance 曲线 ===
    if spark:
        lines.append("## Balance 曲线（最近 30 交易日）")
        lines.append("```")
        lines.append(spark)
        lines.append(recent_range)
        lines.append("```")
        lines.append("")

    # === 明细 ===
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

    # === 未触发挂单 ===
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

    # === 风控警戒 ===
    warnings: list[str] = []
    if cur_dd >= 60:
        warnings.append(f"🔴 **当前回撤 {cur_dd:.1f}%（余额跌破历史峰值 6 成）** —— 强烈建议手动降仓，把 ETH 的 position_pct 改成 5%、BTC 改成 3%")
    elif cur_dd >= 40:
        warnings.append(f"🟡 **当前回撤 {cur_dd:.1f}%（余额跌破历史峰值 4 成）** —— 密切关注，若继续下沉请立即降仓")
    if consec >= max_losses - 1:
        warnings.append(f"🟡 连亏 {consec}/{max_losses} —— 下一笔亏损将触发 {config['strategy']['cooldown_hours']}h 熔断")
    if in_cd:
        warnings.append(f"🔴 熔断中 —— 系统暂停新单，直到熔断解除")
    if warnings:
        lines.append("## ⚠️  风控警戒")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

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
