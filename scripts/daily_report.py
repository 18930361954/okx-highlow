"""
每日盈亏报告：23:55 UTC 自动跑，或手动 `python scripts/daily_report.py [--date YYYY-MM-DD]`
输出到 docs/daily_reports/report_YYYY-MM-DD.md
"""
import argparse
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UTC = timezone.utc


def _trunc2(x: float) -> float:
    """向零截断到 2 位小数, 与 OKX 界面显示口径一致。"""
    if x >= 0:
        return math.floor(x * 100) / 100
    return -math.floor(-x * 100) / 100


def _fmt2s(x: float) -> str:
    return f"{_trunc2(x):+.2f}"


def _fmt2u(x: float) -> str:
    return f"{_trunc2(x):.2f}"


def _dd_flag(dd_pct: float) -> str:
    if dd_pct >= 60:
        return "🔴 严重"
    if dd_pct >= 40:
        return "🟡 警戒"
    if dd_pct >= 20:
        return "🟢 正常"
    return "✅ 良好"


def _build_balance_series(db, up_to_today: str,
                           account: str = "default") -> list[tuple[str, float]]:
    """从全部 trade 记录重建"每日结束时 balance"序列。
    只用 exit_time 有值的（已结算）trade。多账户下按账户过滤。
    """
    all_trades = db.list_trades(limit=10000, account=account)
    filled = [t for t in all_trades if t.get("pnl") is not None]
    filled.sort(key=lambda t: (t.get("exit_time") or "", t.get("id", 0)))

    cur = 0.0
    for t in filled:
        cur += t.get("pnl") or 0
    real_current = float(db.get_state("current_balance", account=account) or "0")
    start = real_current - cur

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


def generate_report(db, account, config, target_date: str | None = None,
                     account_name: str = "default") -> Path:
    today = target_date or datetime.now(UTC).date().isoformat()
    sig_date = (datetime.fromisoformat(today).date() - timedelta(days=1)).isoformat()
    # 未触发挂单仍按 signal_date=昨日看
    trades_today_signal = db.list_trades_by_date(sig_date, account=account_name)

    # 「当日成交」按 exit_time 落在 today 聚合：一笔前几天挂的单可能今天才平仓，
    # 之前按 signal_date 分组会漏掉这类跨日成交（例如 07-05 signal 的 SOL 07-07 才平仓）。
    all_trades = db.list_trades(limit=10000, account=account_name)
    # 排除从未入场的挂单清扫记录(exit_price=0 说明未成交,只是 reconciler 撤单登记)
    filled = [t for t in all_trades
              if (t.get("exit_price") or 0) > 0
              and (t.get("exit_time") or "")[:10] == today]

    start_balance_str = db.get_state(f"balance_start_{today}", account=account_name)
    end_balance = account.get_balance()

    wins = sum(1 for t in filled if (t.get("pnl") or 0) > 0)
    losses = sum(1 for t in filled if (t.get("pnl") or 0) < 0)
    # db.pnl 已是净口径(reconciler 从 OKX positions-history.realizedPnl 写入);
    # 名义 = 净 + 手续费(反推展示,便于对账)
    total_net = sum((t.get("pnl") or 0) for t in filled)
    total_fee = sum((t.get("fee") or 0) for t in filled)
    total_pnl = total_net + total_fee

    if start_balance_str is None:
        start_balance = end_balance - total_net
    else:
        start_balance = float(start_balance_str)

    pnl_pct = (total_net / start_balance * 100) if start_balance > 0 else 0

    mode = "FIXED" if account.is_fixed_mode() else "PCT"
    in_cd = account.is_in_cooldown()
    consec = account.get_consecutive_losses()
    max_losses = account.max_losses

    # === balance 曲线 + DD ===
    series = _build_balance_series(db, today, account=account_name)
    if series:
        vals = [v for _, v in series]
        # 峰值同时纳入「当日结束余额」，防止 series 尾端还没落库、end_balance 已超过 series
        # 时算出负回撤（历史 bug：07-06 报告显示 -9.4%）
        peak = max(max(vals), end_balance)
        cur_dd = max(0.0, ((peak - end_balance) / peak * 100)) if peak > 0 else 0.0
        # 计算历史最大 DD
        max_dd = 0.0
        running_peak = vals[0]
        for v in vals:
            running_peak = max(running_peak, v)
            dd = (running_peak - v) / running_peak * 100 if running_peak > 0 else 0
            max_dd = max(max_dd, dd)
        # end_balance 也参与 running_peak/dd,处理 series 只有 1 点导致 max_dd 恒 0 的情况
        running_peak = max(running_peak, end_balance)
        dd = (running_peak - end_balance) / running_peak * 100 if running_peak > 0 else 0
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
    lines.append(f"| 当日名义盈亏 | {_fmt2s(total_pnl)} USDT | — |")
    lines.append(f"| 当日手续费 | {total_fee:.4f} USDT | — |")
    lines.append(f"| 当日净盈亏 | {_fmt2s(total_net)} USDT ({pnl_pct:+.2f}%) | — |")
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
        lines.append("| 时间 | 品种 | 方向 | 入场 | 出场 | 原因 | 名义 PnL | 手续费 | 净 PnL |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for t in filled:
            ts = (t.get("exit_time") or "")[:19]
            net = t.get("pnl") or 0
            fee = t.get("fee") or 0
            pnl = net + fee  # 名义 = 净 + 手续费(反推展示)
            lines.append(
                f"| {ts} | {t.get('pair', '')} | {t.get('side', '')} | "
                f"{t.get('entry_price', '')} | {t.get('exit_price', '')} | "
                f"{t.get('exit_reason', '')} | {_fmt2s(pnl)} | {fee:.4f} | {_fmt2s(net)} |"
            )
    else:
        lines.append("（无成交）")
    lines.append("")

    # === 未触发挂单 ===
    pending = [t for t in trades_today_signal if t.get("exit_price") is None]
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
    fname = f"report_{today}.md" if account_name == "default" else f"report_{today}_{account_name}.md"
    out_path = out_dir / fname
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


# ---------------- 多账户主报告 ----------------

def _summarize_account(db, rt_like, today: str) -> dict:
    """rt_like 需暴露 .name / .account (AccountState) / .cfg (提供 pairs 等)。
    这里不 import AccountRuntime 以避免循环依赖。"""
    name = rt_like.name
    account_state = rt_like.account
    sig_date = (datetime.fromisoformat(today).date() - timedelta(days=1)).isoformat()

    all_trades = db.list_trades(limit=10000, account=name)
    # 排除从未入场的挂单清扫记录(exit_price=0 说明未成交,只是 reconciler 撤单登记)
    today_filled = [t for t in all_trades
                    if (t.get("exit_price") or 0) > 0
                    and (t.get("exit_time") or "")[:10] == today]
    # db.pnl 已是净口径; 名义 = 净 + 手续费
    total_net = sum((t.get("pnl") or 0) for t in today_filled)
    total_fee = sum((t.get("fee") or 0) for t in today_filled)
    total_pnl = total_net + total_fee
    wins = sum(1 for t in today_filled if (t.get("pnl") or 0) > 0)
    losses = sum(1 for t in today_filled if (t.get("pnl") or 0) < 0)

    end_bal = account_state.get_balance()
    start_bal = end_bal - total_net

    # 回撤(带 end_bal 兜底防负)
    series = _build_balance_series(db, today, account=name)
    if series:
        vals = [v for _, v in series]
        peak = max(max(vals), end_bal)
        cur_dd = max(0.0, ((peak - end_bal) / peak * 100)) if peak > 0 else 0.0
    else:
        peak = end_bal
        cur_dd = 0.0

    pending = [t for t in db.list_trades_by_date(sig_date, account=name)
               if t.get("exit_price") is None]

    return {
        "name": name,
        "pairs": list(rt_like.cfg.pairs),
        "start_balance": start_bal,
        "end_balance": end_bal,
        "peak": peak,
        "pnl": total_pnl,
        "fee": total_fee,
        "net": total_net,
        "n_filled": len(today_filled),
        "wins": wins,
        "losses": losses,
        "cur_dd": cur_dd,
        "in_cooldown": account_state.is_in_cooldown(),
        "consec_losses": account_state.get_consecutive_losses(),
        "max_losses": account_state.max_losses,
        "mode": "FIXED" if account_state.is_fixed_mode() else "PCT",
        "filled": today_filled,
        "pending": pending,
    }


def generate_multi_account_report(runtimes, config, target_date: str | None = None) -> Path:
    """一份主报告 = 顶部总览 + 每账户拆分表。
    runtimes: list[AccountRuntime]。使用 rt.name / rt.account / rt.cfg / rt.db。
    """
    today = target_date or datetime.now(UTC).date().isoformat()

    if not runtimes:
        raise ValueError("runtimes 为空")

    db = runtimes[0].db  # 共享 db
    per_acc = [_summarize_account(db, rt, today) for rt in runtimes]

    total_end = sum(a["end_balance"] for a in per_acc)
    total_start = sum(a["start_balance"] for a in per_acc)
    total_pnl = sum(a["pnl"] for a in per_acc)
    total_fee = sum(a["fee"] for a in per_acc)
    total_net = sum(a["net"] for a in per_acc)
    total_filled = sum(a["n_filled"] for a in per_acc)
    total_wins = sum(a["wins"] for a in per_acc)
    total_losses = sum(a["losses"] for a in per_acc)
    pnl_pct = (total_net / total_start * 100) if total_start > 0 else 0.0

    lines: list[str] = []
    lines.append(f"# 每日盈亏报告 {today}（多账户汇总）")
    lines.append("")
    lines.append("## 总览（全账户）")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 账户数 | {len(per_acc)} |")
    lines.append(f"| 起始总余额 | {total_start:.2f} USDT |")
    lines.append(f"| 结束总余额 | {total_end:.2f} USDT |")
    lines.append(f"| 当日名义盈亏 | {_fmt2s(total_pnl)} USDT |")
    lines.append(f"| 当日手续费 | {total_fee:.4f} USDT |")
    lines.append(f"| 当日净盈亏 | {_fmt2s(total_net)} USDT ({pnl_pct:+.2f}%) |")
    lines.append(f"| 当日成交 | {total_filled} 笔（盈 {total_wins} / 亏 {total_losses}）|")
    lines.append("")

    # 按账户一览表
    lines.append("## 账户一览")
    lines.append("| 账户 | 品种 | 起始 | 结束 | 名义 PnL | 手续费 | 净 PnL | 成交 | 回撤 | 熔断 | 模式 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for a in per_acc:
        pairs_str = ",".join(a["pairs"]) or "-"
        cd = "🔴" if a["in_cooldown"] else "✅"
        lines.append(
            f"| {a['name']} | {pairs_str} | {a['start_balance']:.2f} | {a['end_balance']:.2f} | "
            f"{_fmt2s(a['pnl'])} | {a['fee']:.4f} | {_fmt2s(a['net'])} | "
            f"{a['n_filled']}(盈{a['wins']}/亏{a['losses']}) | "
            f"{a['cur_dd']:.2f}% {_dd_flag(a['cur_dd'])} | {cd} | {a['mode']} |"
        )
    lines.append("")

    # 各账户拆分
    for a in per_acc:
        lines.append(f"## 账户 [{a['name']}]")
        lines.append(f"- 品种: {', '.join(a['pairs']) or '-'}")
        lines.append(f"- 余额: {a['start_balance']:.2f} → {a['end_balance']:.2f} "
                     f"(名义 {_fmt2s(a['pnl'])} · 费 {a['fee']:.4f} · "
                     f"净 {_fmt2s(a['net'])} USDT)")
        lines.append(f"- 连亏计数: {a['consec_losses']}/{a['max_losses']} · "
                     f"熔断: {'是' if a['in_cooldown'] else '否'} · 模式: {a['mode']}")
        lines.append(f"- 历史峰值: {a['peak']:.2f} · 当前回撤: {a['cur_dd']:.2f}% {_dd_flag(a['cur_dd'])}")
        lines.append("")

        if a["filled"]:
            lines.append("### 成交明细")
            lines.append("| 时间 | 品种 | 方向 | 入场 | 出场 | 原因 | 名义 PnL | 手续费 | 净 PnL |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
            for t in a["filled"]:
                ts = (t.get("exit_time") or "")[:19]
                net = t.get("pnl") or 0
                fee = t.get("fee") or 0
                pnl = net + fee  # 名义 = 净 + 手续费(反推展示)
                lines.append(
                    f"| {ts} | {t.get('pair', '')} | {t.get('side', '')} | "
                    f"{t.get('entry_price', '')} | {t.get('exit_price', '')} | "
                    f"{t.get('exit_reason', '')} | {_fmt2s(pnl)} | {fee:.4f} | {_fmt2s(net)} |"
                )
            lines.append("")

        if a["pending"]:
            lines.append("### 未触发挂单")
            lines.append("| 品种 | 方向 | 触发价 | 保证金 |")
            lines.append("|---|---|---|---|")
            for t in a["pending"]:
                lines.append(
                    f"| {t.get('pair', '')} | {t.get('side', '')} | "
                    f"{t.get('entry_price', '')} | {(t.get('margin') or 0):.2f} |"
                )
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
