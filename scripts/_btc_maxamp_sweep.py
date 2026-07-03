"""BTC max_prev_amp 精细扫描 2%~5% 步长 0.25%，ETH 固定 8%。
一年真实数据 + 稳健性检查（分半段回测）。
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

START = pd.Timestamp("2025-07-01", tz="UTC")
MID   = pd.Timestamp("2026-01-01", tz="UTC")   # 用于分半段稳健性
END   = pd.Timestamp("2026-07-01", tz="UTC")

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


def sweep():
    csv_btc = "csv_data/BTC_USDT_SWAP_1H_400d.csv"
    # step 0.25% 从 2% 到 5%
    caps = [round(x, 4) for x in [0.02, 0.0225, 0.025, 0.0275, 0.03, 0.0325,
                                   0.035, 0.0375, 0.04, 0.0425, 0.045, 0.0475, 0.05]]

    print("BTC max_prev_amp 精细扫描（ETH 固定 8%）")
    print("窗口：2025-07-01 → 2026-07-01（全年）")
    print()
    print(f"{'BTC 上限':>9} | {'笔数':>4} {'胜率':>6} {'PF':>5} "
          f"{'收益%':>8} {'MDD%':>7} {'结束余额':>9} | "
          f"{'H1 收益%':>9} {'H2 收益%':>9} {'稳定性'}")
    print("-" * 105)

    for cap in caps:
        cfg = copy.deepcopy(BASE_CFG)
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["max_prev_amp"] = cap

        # 全年
        r = run("BTC-USDT-SWAP", csv_btc, cfg, START, END)
        # 上半年
        rh1 = run("BTC-USDT-SWAP", csv_btc, cfg, START, MID)
        # 下半年
        rh2 = run("BTC-USDT-SWAP", csv_btc, cfg, MID, END)

        # 稳定性：两个半年收益同号 = 稳；否则 = 不稳
        both_pos = rh1["total_return_pct"] > 0 and rh2["total_return_pct"] > 0
        stab = "两半年皆盈" if both_pos else (
            "两半年皆亏" if rh1["total_return_pct"] < 0 and rh2["total_return_pct"] < 0
            else "不一致")

        print(f"{cap*100:>7.2f}% | {r['trades']:>4} {r['win_rate_pct']:>5.1f}% "
              f"{r['profit_factor']:>5.2f} {r['total_return_pct']:>+7.2f}% "
              f"{r['max_dd_pct']:>6.1f}% {r['final']:>9.2f} | "
              f"{rh1['total_return_pct']:>+8.2f}% {rh2['total_return_pct']:>+8.2f}% "
              f"{stab}")


if __name__ == "__main__":
    sweep()
