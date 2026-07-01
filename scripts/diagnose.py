"""
HighLow 策略诊断：
  - 方向分布（多空各多少）
  - 触发率（多少信号实际成交）
  - TP/SL/EOD 占比
  - 平均持仓时长
  - 各方向胜率
"""
import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest import DEFAULT_CONFIG, load_csv, simulate  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="BTC-USDT-SWAP")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--days", type=int, default=180)
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
    res = simulate(df, args.pair, DEFAULT_CONFIG, days=args.days)
    trades = res["trades_detail"]

    if not trades:
        print("no trades to diagnose")
        return

    side_counter = Counter(t["direction"] for t in trades)
    reason_counter = Counter(t["reason"] for t in trades)

    side_wins: dict = defaultdict(int)
    side_total: dict = defaultdict(int)
    for t in trades:
        side_total[t["direction"]] += 1
        if t["pnl"] > 0:
            side_wins[t["direction"]] += 1

    print(f"=== Diagnose {args.pair} ({args.days}d) ===")
    print(f"Trades total      : {len(trades)}")
    print()
    print("Direction split:")
    for k, v in side_counter.items():
        wr = side_wins[k] / side_total[k] * 100 if side_total[k] else 0
        print(f"  {k:5s}: {v:3d}  win_rate={wr:5.2f}%")
    print()
    print("Exit reason split:")
    for k, v in reason_counter.items():
        print(f"  {k:5s}: {v:3d}  ({v / len(trades) * 100:5.2f}%)")
    print()
    print(f"Total return: {res['total_return_pct']:+.2f}%  |  Win rate: {res['win_rate_pct']:.2f}%  |  PF: {res['profit_factor']:.2f}")


if __name__ == "__main__":
    main()
