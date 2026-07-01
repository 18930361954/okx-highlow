"""
HighLow 策略回测：
  python scripts/backtest.py --pair BTC-USDT-SWAP --days 180
  python scripts/backtest.py --csv csv_data/BTC_USDT_SWAP_1H_12m.csv --days 180

输出：总收益率 / 胜率 / 最大回撤 / 总笔数 / 盈亏比
"""
import argparse
import sys
from datetime import timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy.high_low import HighLowStrategy  # noqa: E402


UTC = timezone.utc

DEFAULT_CONFIG = {
    "strategy": {
        "pairs": ["BTC-USDT-SWAP"],
        "position_pct": 0.10,
        "float_pct": 0.0015,
        "tp_pct": 0.012,
        "sl_pct": 0.005,
        "leverage": 100,
        "trend_filter": True,
        "max_consecutive_losses": 3,
        "cooldown_hours": 24,
        "fixed_mode_threshold": 800000,
        "fixed_mode_margin": 1000,
        "signal_time_utc": "00:00",
    }
}


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    df["date"] = df["ts"].dt.date
    return df


def _simulate_one_entry(bars, direction: str, entry: float, tp: float, sl: float,
                         start_idx: int = 0) -> tuple[float | None, str | None, object, int | None]:
    """
    从 bars[start_idx:] 开始，找到第一根 low<=entry (long) 或 high>=entry (short) 的 K；
    确定 entry_idx 后，逐根判 TP/SL（同根双触发保守 → SL 优先）。
    返回 (exit_price, exit_reason, exit_ts, entry_bar_idx)
    exit_reason: 'TP' / 'SL' / 'EOD' / 'NO_ENTRY'
    未触发入场：(None, 'NO_ENTRY', None, None)
    """
    entry_idx = None
    for j in range(start_idx, len(bars)):
        c = bars[j]
        if direction == "long" and c.low <= entry:
            entry_idx = j
            break
        if direction == "short" and c.high >= entry:
            entry_idx = j
            break
    if entry_idx is None:
        return None, "NO_ENTRY", None, None

    exit_price = None
    exit_reason = None
    exit_ts = None
    entry_bar = bars[entry_idx]
    # 入场这根 K：同根 SL+TP 同时穿 → SL 优先
    if direction == "long":
        if entry_bar.low <= sl:
            exit_price, exit_reason, exit_ts = sl, "SL", entry_bar.ts
        elif entry_bar.high >= tp:
            exit_price, exit_reason, exit_ts = tp, "TP", entry_bar.ts
    else:
        if entry_bar.high >= sl:
            exit_price, exit_reason, exit_ts = sl, "SL", entry_bar.ts
        elif entry_bar.low <= tp:
            exit_price, exit_reason, exit_ts = tp, "TP", entry_bar.ts

    if exit_price is None:
        for c in bars[entry_idx + 1:]:
            if direction == "long":
                if c.low <= sl:
                    exit_price, exit_reason, exit_ts = sl, "SL", c.ts
                    break
                if c.high >= tp:
                    exit_price, exit_reason, exit_ts = tp, "TP", c.ts
                    break
            else:
                if c.high >= sl:
                    exit_price, exit_reason, exit_ts = sl, "SL", c.ts
                    break
                if c.low <= tp:
                    exit_price, exit_reason, exit_ts = tp, "TP", c.ts
                    break

    if exit_price is None:
        last = bars[-1]
        exit_price = last.close
        exit_reason = "EOD"
        exit_ts = last.ts
    return exit_price, exit_reason, exit_ts, entry_idx


def simulate(
    df: pd.DataFrame,
    pair: str,
    config: dict,
    initial_balance: float = 75.0,
    days: int | None = None,
    verbose: bool = False,
    reentry_floats: list[float] | None = None,
) -> dict:
    """
    reentry_floats: 若为 None → 旧行为（每日只挂 1 单，用 config.float_pct）
                    若为 list → 每日按顺序用这些浮动值最多尝试 N 次入场，
                    前一次 SL 才继续；TP/EOD/NO_ENTRY 立即停。
                    N 次全 SL → consec_losses += 1（触发原熔断机制）
                    N 次中任意一次 TP/EOD/NO_ENTRY 归零/不动 consec_losses
    """
    strat = HighLowStrategy(config)
    leverage = int(config["strategy"]["leverage"])
    position_pct = float(config["strategy"]["position_pct"])
    fixed_thr = float(config["strategy"]["fixed_mode_threshold"])
    fixed_margin = float(config["strategy"]["fixed_mode_margin"])
    max_losses = int(config["strategy"]["max_consecutive_losses"])
    cooldown_hours = int(config["strategy"]["cooldown_hours"])

    # pair 级 tp/sl 覆盖：跟 HighLowStrategy 一致的读取
    ov = (config["strategy"].get("pair_overrides") or {}).get(pair, {})
    tp_pct = float(ov.get("tp_pct", config["strategy"]["tp_pct"]))
    sl_pct = float(ov.get("sl_pct", config["strategy"]["sl_pct"]))

    grouped = list(df.groupby("date"))
    if days:
        grouped = grouped[-(days + 1):]  # +1 因为信号日和挂单日错一天

    balance = initial_balance
    peak = balance
    max_dd = 0.0
    trades: list[dict] = []
    consec_losses = 0
    cooldown_until_ts = None  # pd.Timestamp | None
    fixed_locked = False

    for i in range(len(grouped) - 1):
        sig_date, sig_day_df = grouped[i]
        next_date, next_day_df = grouped[i + 1]

        if cooldown_until_ts is not None:
            day_start = pd.Timestamp(next_date, tz=UTC)
            if day_start < cooldown_until_ts:
                continue
            cooldown_until_ts = None

        candles = sig_day_df.to_dict("records")
        if len(candles) < 2:
            continue

        signal = strat.compute_signal(pair, [
            {"ts": int(c["timestamp"]), "open": c["open"], "high": c["high"],
             "low": c["low"], "close": c["close"]}
            for c in candles
        ], signal_date=sig_date)

        if not signal:
            continue

        direction = signal["direction"]
        # 用于日内重挂：需要"日内到 SL 时刻"的新高低点。而入场价公式在阴线用 high、阳线用 low。
        # 前日 high/low：用 signal 里带的
        prev_high = signal["day_high"]
        prev_low = signal["day_low"]

        # 每一次的入场浮动列表：默认走旧行为（单次，float_pct 来自 signal 已内嵌）
        floats_seq = reentry_floats if reentry_floats is not None else [float(config["strategy"]["float_pct"])]

        bars = list(next_day_df.itertuples())
        if not bars:
            continue

        # 逐次尝试入场
        attempt_idx = 0
        search_from = 0  # 下一次入场从哪根 K 开始搜 —— 上一次 SL 那根 K 之后
        day_sl_count = 0
        day_had_non_sl = False  # 只要出现 TP / EOD / NO_ENTRY 就置 True，当日不再重挂

        for attempt_idx, fp in enumerate(floats_seq):
            # 重新计算保证金（每次入场都按当前 balance 算 PCT / FIXED）
            if fixed_locked or balance >= fixed_thr:
                fixed_locked = True
                margin = fixed_margin
                mode = "FIXED"
            else:
                margin = balance * position_pct
                mode = "PCT"

            # 计算本次入场价
            if attempt_idx == 0:
                # 第 1 次：沿用旧口径 —— 用前日 high/low × float
                if direction == "long":
                    entry = round(prev_low * (1 - fp), 6)
                else:
                    entry = round(prev_high * (1 + fp), 6)
            else:
                # 重挂：用"当日日初 → 上一次 SL 那根 K"这段的新高低点
                # search_from 已在上一次 SL 时被更新为 SL 那根 K 的 index+1
                # 但"日内高低点"要用 bars[0..sl_bar_idx]（含 SL 那根 K）
                # 我们保留一个变量 sl_bar_idx 追踪
                # 为简单 —— 这里直接用 bars[:search_from] 作为"到 SL 为止"
                seg = bars[:search_from] if search_from > 0 else []
                if not seg:
                    break  # 理论上不会发生
                day_high_so_far = max(b.high for b in seg)
                day_low_so_far = min(b.low for b in seg)
                if direction == "long":
                    entry = round(day_low_so_far * (1 - fp), 6)
                else:
                    entry = round(day_high_so_far * (1 + fp), 6)

            tp = round(entry * (1 + tp_pct), 6) if direction == "long" else round(entry * (1 - tp_pct), 6)
            sl = round(entry * (1 - sl_pct), 6) if direction == "long" else round(entry * (1 + sl_pct), 6)

            exit_price, exit_reason, exit_ts, entry_bar_idx = _simulate_one_entry(
                bars, direction, entry, tp, sl, start_idx=search_from
            )

            if exit_reason == "NO_ENTRY":
                # 挂了没被触发 —— 当日不再重挂
                day_had_non_sl = True
                break

            # 记录本次 trade
            if direction == "long":
                pct = (exit_price - entry) / entry
            else:
                pct = (entry - exit_price) / entry
            pnl = margin * leverage * pct
            balance += pnl
            peak = max(peak, balance)
            dd = (peak - balance) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

            trades.append({
                "signal_date": str(sig_date),
                "trade_date": str(next_date),
                "attempt": attempt_idx + 1,
                "float_pct": fp,
                "direction": direction,
                "entry": entry, "exit": exit_price, "reason": exit_reason,
                "margin": margin, "mode": mode, "pnl": pnl, "balance_after": balance,
            })

            if verbose:
                print(f"{sig_date} #{attempt_idx+1} fp={fp} {direction} entry={entry} "
                      f"exit={exit_price} ({exit_reason}) pnl={pnl:+.2f} bal={balance:.2f}")

            if exit_reason == "SL":
                day_sl_count += 1
                # 找到 SL 那根 K 的 index —— exit_ts 就是它
                sl_bar_idx = None
                for k in range(entry_bar_idx, len(bars)):
                    if bars[k].ts == exit_ts:
                        sl_bar_idx = k
                        break
                if sl_bar_idx is None:
                    break
                search_from = sl_bar_idx + 1
                if search_from >= len(bars):
                    # 当日已无后续 K —— 无法重挂
                    break
                # 继续下一次尝试
                continue
            else:
                # TP 或 EOD → 收工
                day_had_non_sl = True
                break

        # 熔断计数：本日全 SL 且尝试次数 = 配置的次数 → +1，否则归零（若有 TP）
        if reentry_floats is not None:
            attempts_used = day_sl_count + (1 if day_had_non_sl else 0)
            if day_sl_count >= len(floats_seq) and not day_had_non_sl:
                # N 连 SL
                consec_losses += 1
            elif trades and trades[-1]["signal_date"] == str(sig_date):
                # 当日最后一笔是 TP → 归零；EOD 视为不亏不算连亏但也不清零
                last_reason = trades[-1]["reason"]
                if last_reason == "TP":
                    consec_losses = 0
        else:
            # 旧行为：单笔即算
            if trades and trades[-1]["signal_date"] == str(sig_date):
                if trades[-1]["pnl"] < 0:
                    consec_losses += 1
                else:
                    consec_losses = 0

        # 熔断触发：以 next_date 当日结束为基准 + cooldown_hours（回测粒度够用）
        if consec_losses >= max_losses:
            base_ts = pd.Timestamp(next_date, tz=UTC) + pd.Timedelta(days=1)
            cooldown_until_ts = base_ts + pd.Timedelta(hours=cooldown_hours)
            consec_losses = 0

    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    total = len(trades)
    win_rate = wins / total if total else 0
    gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    total_return = (balance - initial_balance) / initial_balance if initial_balance > 0 else 0
    n_days = days or (len(grouped) - 1)
    monthly = total_return / (n_days / 30) if n_days >= 30 else total_return

    return {
        "pair": pair,
        "initial": initial_balance,
        "final": balance,
        "total_return_pct": total_return * 100,
        "monthly_pct": monthly * 100,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate * 100,
        "max_dd_pct": max_dd * 100,
        "profit_factor": pf,
        "trades_detail": trades,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="BTC-USDT-SWAP")
    ap.add_argument("--csv", default=None, help="csv 路径；默认按 pair 推导")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--balance", type=float, default=75.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.csv:
        csv_path = Path(args.csv)
    else:
        token = args.pair.replace("-USDT-SWAP", "")
        csv_path = ROOT / "csv_data" / f"{token}_USDT_SWAP_1H_12m.csv"

    if not csv_path.exists():
        print(f"[err] CSV not found: {csv_path}")
        sys.exit(1)

    df = load_csv(csv_path)
    res = simulate(df, args.pair, DEFAULT_CONFIG, initial_balance=args.balance,
                   days=args.days, verbose=args.verbose)

    print()
    print(f"=== Backtest {res['pair']} ({args.days}d) ===")
    print(f"Initial      : {res['initial']:.2f}")
    print(f"Final        : {res['final']:.2f}")
    print(f"Total return : {res['total_return_pct']:+.2f}%")
    print(f"Monthly      : {res['monthly_pct']:+.2f}%")
    print(f"Trades       : {res['trades']} (W {res['wins']} / L {res['losses']})")
    print(f"Win rate     : {res['win_rate_pct']:.2f}%")
    print(f"Max DD       : {res['max_dd_pct']:.2f}%")
    print(f"Profit factor: {res['profit_factor']:.2f}")


if __name__ == "__main__":
    main()
