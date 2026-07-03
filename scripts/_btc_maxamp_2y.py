"""BTC max_prev_amp 2%~5% 精细扫描，两年数据（2024-07-01 → 2026-07-01）。
ETH 固定 8%。分年度对比 + 两年合计 + BTC/ETH 组合总收益。
"""
import sys
import io
import copy
from pathlib import Path
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest import simulate, load_csv  # noqa: E402


Y0 = pd.Timestamp("2024-07-01", tz="UTC")
Y1 = pd.Timestamp("2025-07-01", tz="UTC")
Y2 = pd.Timestamp("2026-07-01", tz="UTC")

BASE_CFG = {
    "strategy": {
        "pairs": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        "position_pct": 0.10, "float_pct": 0.0015,
        "tp_pct": 0.012, "sl_pct": 0.005,
        "pair_overrides": {
            "BTC-USDT-SWAP": {
                "position_pct": 0.05, "tp_pct": 0.015, "sl_pct": 0.004,
                "float_pct": 0.002, "reentry_floats": [0.002, 0.004],
                "min_prev_amp": 0.01, "max_prev_amp": 0.04,
            },
            "ETH-USDT-SWAP": {
                "position_pct": 0.08, "tp_pct": 0.020, "sl_pct": 0.007,
                "reentry_floats": [0.0015, 0.006],
                "min_prev_amp": 0.01, "max_prev_amp": 0.08,
            },
        },
        "leverage": 100, "trend_filter": True,
        "max_consecutive_losses": 3, "cooldown_hours": 24,
        "fixed_mode_threshold": 800000, "fixed_mode_margin": 1000,
        "signal_time_utc": "00:00",
    }
}


def load_slice(csv, s, e):
    df = load_csv(Path(csv))
    df = df[(df["ts"] >= s) & (df["ts"] < e)].reset_index(drop=True)
    df["date"] = df["ts"].dt.date
    return df


def run(pair, csv, cfg, s, e):
    df = load_slice(csv, s, e)
    reentry = cfg["strategy"]["pair_overrides"][pair].get("reentry_floats")
    return simulate(df, pair, cfg, initial_balance=75.0, days=None, reentry_floats=reentry)


def main():
    csv_btc = "csv_data/BTC_USDT_SWAP_1H_780d.csv"
    csv_eth = "csv_data/ETH_USDT_SWAP_1H_780d.csv"

    caps = [round(x, 4) for x in [0.02, 0.0225, 0.025, 0.0275, 0.03, 0.0325,
                                   0.035, 0.0375, 0.04, 0.0425, 0.045, 0.0475, 0.05]]

    print(f"数据窗口：{Y0.date()} → {Y2.date()}（2 年）")
    print(f"ETH max_prev_amp 固定 8%")
    print()

    # ---- 先跑 ETH 一次（固定 8%），三个窗口 ----
    print("[ETH 端参考，固定 max=8%]")
    for lab, s, e in [("Y1 (24-07~25-07)", Y0, Y1),
                       ("Y2 (25-07~26-07)", Y1, Y2),
                       ("2Y 合计",           Y0, Y2)]:
        r = run("ETH-USDT-SWAP", csv_eth, BASE_CFG, s, e)
        print(f"  {lab:<20} 笔数={r['trades']:>3} 胜率={r['win_rate_pct']:>5.1f}% "
              f"PF={r['profit_factor']:>4.2f} 收益={r['total_return_pct']:>+9.2f}% "
              f"MDD={r['max_dd_pct']:>5.1f}% 结束={r['final']:>10.2f}")
    print()

    # ---- BTC 扫描 ----
    print("[BTC max_prev_amp 扫描]")
    print(f"{'cap':>6} | {'笔数':>4} {'胜率':>6} {'PF':>5} {'收益%':>8} {'MDD%':>6} {'结束':>8} | "
          f"{'Y1 收益%':>9} {'Y2 收益%':>9} 稳定性")
    print("-" * 105)
    rows = []
    for cap in caps:
        cfg = copy.deepcopy(BASE_CFG)
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["max_prev_amp"] = cap
        r_all = run("BTC-USDT-SWAP", csv_btc, cfg, Y0, Y2)
        r_y1  = run("BTC-USDT-SWAP", csv_btc, cfg, Y0, Y1)
        r_y2  = run("BTC-USDT-SWAP", csv_btc, cfg, Y1, Y2)
        stab = "两年皆盈" if (r_y1["total_return_pct"] > 0 and r_y2["total_return_pct"] > 0) \
               else ("两年皆亏" if (r_y1["total_return_pct"] < 0 and r_y2["total_return_pct"] < 0) \
                     else "不一致")
        rows.append((cap, r_all, r_y1, r_y2, stab))
        print(f"{cap*100:>5.2f}% | {r_all['trades']:>4} {r_all['win_rate_pct']:>5.1f}% "
              f"{r_all['profit_factor']:>5.2f} {r_all['total_return_pct']:>+7.2f}% "
              f"{r_all['max_dd_pct']:>5.1f}% {r_all['final']:>8.2f} | "
              f"{r_y1['total_return_pct']:>+8.2f}% {r_y2['total_return_pct']:>+8.2f}% {stab}")

    # ---- 组合合计（BTC + ETH，各 75U） ----
    print()
    print("[BTC + ETH 组合合计 (BTC 用扫描档 + ETH 8%)]")
    r_eth = run("ETH-USDT-SWAP", csv_eth, BASE_CFG, Y0, Y2)
    print(f"{'BTC cap':>7} | {'BTC 收益%':>10} {'ETH 收益%':>10} {'合计收益%':>11} "
          f"{'BTC MDD%':>9} {'合计结束':>10}")
    for cap, r_all, _, _, _ in rows:
        total_init = 150
        total_end = r_all["final"] + r_eth["final"]
        total_ret = (total_end - total_init) / total_init * 100
        print(f"{cap*100:>5.2f}% | {r_all['total_return_pct']:>+9.2f}% {r_eth['total_return_pct']:>+9.2f}% "
              f"{total_ret:>+10.2f}% {r_all['max_dd_pct']:>8.1f}% {total_end:>10.2f}")


if __name__ == "__main__":
    main()
