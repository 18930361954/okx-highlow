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


def simulate(
    df: pd.DataFrame,
    pair: str,
    config: dict,
    initial_balance: float = 75.0,
    days: int | None = None,
    verbose: bool = False,
) -> dict:
    strat = HighLowStrategy(config)
    leverage = int(config["strategy"]["leverage"])
    position_pct = float(config["strategy"]["position_pct"])
    fixed_thr = float(config["strategy"]["fixed_mode_threshold"])
    fixed_margin = float(config["strategy"]["fixed_mode_margin"])
    max_losses = int(config["strategy"]["max_consecutive_losses"])
    cooldown_hours = int(config["strategy"]["cooldown_hours"])

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

        # 计算保证金
        if fixed_locked or balance >= fixed_thr:
            fixed_locked = True
            margin = fixed_margin
            mode = "FIXED"
        else:
            margin = balance * position_pct
            mode = "PCT"

        entry = signal["entry_price"]
        tp = signal["tp_price"]
        sl = signal["sl_price"]
        direction = signal["direction"]

        # 在 next_day 的 1H 数据里看是否触发
        touched_entry = False
        entry_idx = None
        for j, c in enumerate(next_day_df.itertuples()):
            if direction == "long" and c.low <= entry:
                touched_entry = True
                entry_idx = j
                break
            if direction == "short" and c.high >= entry:
                touched_entry = True
                entry_idx = j
                break

        if not touched_entry:
            continue

        # 入场那根 K：入场后这根 K 内 close 已先穿到哪一侧用 close 判断；
        # 否则进入后续 K 线逐根判 TP/SL（同根内 TP+SL 同时穿 → SL 优先保守）。
        exit_price = None
        exit_reason = None
        exit_ts = None
        bars = list(next_day_df.itertuples())
        entry_bar = bars[entry_idx]
        # 入场这根 K 内：用 close 相对 entry 看朝哪边走，避免双触发歧义
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
            # 当日未触发 TP/SL → 用收盘价平
            last = list(next_day_df.itertuples())[-1]
            exit_price = last.close
            exit_reason = "EOD"
            exit_ts = last.ts

        # PnL
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
            "direction": direction,
            "entry": entry, "exit": exit_price, "reason": exit_reason,
            "margin": margin, "mode": mode, "pnl": pnl, "balance_after": balance,
        })

        if pnl < 0:
            consec_losses += 1
        else:
            consec_losses = 0

        if consec_losses >= max_losses:
            cooldown_until_ts = pd.Timestamp(exit_ts).tz_convert(UTC) + pd.Timedelta(hours=cooldown_hours)
            consec_losses = 0

        if verbose:
            print(f"{sig_date} {direction} entry={entry} exit={exit_price} ({exit_reason}) "
                  f"pnl={pnl:+.2f} bal={balance:.2f}")

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
