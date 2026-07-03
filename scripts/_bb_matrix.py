"""BB 策略在 5m / 15m / 30m / 1h × BTC/ETH 上的一年表现。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_bb import Params, load_ohlcv, backtest, report  # noqa: E402

START, END = "2025-07-01", "2026-07-01"

CFGS = [
    # (label, csv_bar_tag, order_ttl_bars ≈ 4小时)
    ("BTC-5m",  "5m",   48),
    ("BTC-15m", "15m",  16),
    ("BTC-30m", "30m",   8),
    ("BTC-1H",  "1H",    4),
    ("ETH-5m",  "5m",   48),
    ("ETH-15m", "15m",  16),
    ("ETH-30m", "30m",   8),
    ("ETH-1H",  "1H",    4),
]


def one(label, csv, ttl):
    df = load_ohlcv(Path(csv), START, END)
    p = Params(order_ttl_bars=ttl)
    trades, cap, ec = backtest(df, p)
    return report(trades, cap, ec, p, label)


def main():
    results = []
    for label, tag, ttl in CFGS:
        pair = label.split("-")[0]
        csv = f"csv_data/{pair}_USDT_SWAP_{tag}_400d.csv"
        r = one(label, csv, ttl)
        results.append(r)

    print("\n\n========= 汇总 =========")
    print(f"{'label':<10} {'n':>4} {'wr':>6} {'pf':>5} {'ret':>7} {'monthly':>8} {'mdd':>6} {'final':>7}")
    for r in results:
        if r["n"] == 0:
            print(f"{r['label']:<10} 无成交")
            continue
        print(f"{r['label']:<10} {r['n']:>4} {r['wr']*100:>5.1f}% {r['pf']:>5.2f} "
              f"{r['ret']*100:>+6.1f}% {r['monthly']*100:>+7.2f}% "
              f"{r['mdd']*100:>5.1f}% {r['final']:>7.1f}")


if __name__ == "__main__":
    main()
