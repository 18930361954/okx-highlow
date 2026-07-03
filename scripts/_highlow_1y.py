"""用真实 1H 数据回测现有 HighLow 策略，窗口锁定 2025-07-01 → 2026-07-01。"""
import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest import simulate, DEFAULT_CONFIG, load_csv  # noqa: E402


START = pd.Timestamp("2025-07-01", tz="UTC")
END = pd.Timestamp("2026-07-01", tz="UTC")


def run(pair, csv):
    df = load_csv(Path(csv))
    df = df[(df["ts"] >= START) & (df["ts"] < END)].reset_index(drop=True)
    df["date"] = df["ts"].dt.date
    print(f"数据 {csv}: {len(df)} 行, {df.ts.min()} ~ {df.ts.max()}")
    res = simulate(df, pair, DEFAULT_CONFIG, initial_balance=75.0, days=None)
    print(f"\n=== {pair} ===")
    print(f"Initial      : {res['initial']:.2f}")
    print(f"Final        : {res['final']:.2f}")
    print(f"Total return : {res['total_return_pct']:+.2f}%")
    print(f"Monthly      : {res['monthly_pct']:+.2f}%")
    print(f"Trades       : {res['trades']} (W {res['wins']} / L {res['losses']})")
    print(f"Win rate     : {res['win_rate_pct']:.2f}%")
    print(f"Max DD       : {res['max_dd_pct']:.2f}%")
    print(f"Profit factor: {res['profit_factor']:.2f}")

    df_t = pd.DataFrame(res["trades_detail"])
    if len(df_t):
        df_t["ym"] = pd.to_datetime(df_t["trade_date"]).dt.strftime("%Y-%m")
        by_m = df_t.groupby("ym").agg(pnl=("pnl", "sum"), n=("pnl", "count")).round(2)
        print("\n月度分布：")
        print(by_m.to_string())
    return res


def main():
    run("BTC-USDT-SWAP", "csv_data/BTC_USDT_SWAP_1H_400d.csv")
    print()
    run("ETH-USDT-SWAP", "csv_data/ETH_USDT_SWAP_1H_400d.csv")


if __name__ == "__main__":
    main()
