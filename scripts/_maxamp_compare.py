"""对比 max_prev_amp 阈值组合对现有 HighLow 策略一年表现的影响。
用真实 config.yaml 里的 pair_overrides，只改 max_prev_amp。
"""
import sys
import io
from pathlib import Path
import copy
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest import simulate, load_csv  # noqa: E402


START = pd.Timestamp("2025-07-01", tz="UTC")
END = pd.Timestamp("2026-07-01", tz="UTC")


# 与 config.yaml 完全一致（pair_overrides 全套）
BASE_CFG = {
    "strategy": {
        "pairs": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        "position_pct": 0.10,
        "float_pct": 0.0015,
        "tp_pct": 0.012,
        "sl_pct": 0.005,
        "pair_overrides": {
            "BTC-USDT-SWAP": {
                "position_pct": 0.05,
                "tp_pct": 0.015,
                "sl_pct": 0.004,
                "float_pct": 0.002,
                "reentry_floats": [0.002, 0.004],
                "min_prev_amp": 0.01,
                "max_prev_amp": 0.04,
            },
            "ETH-USDT-SWAP": {
                "position_pct": 0.08,
                "tp_pct": 0.020,
                "sl_pct": 0.007,
                "reentry_floats": [0.0015, 0.006],
                "min_prev_amp": 0.01,
                "max_prev_amp": 0.04,
            },
        },
        "leverage": 100,
        "trend_filter": True,
        "max_consecutive_losses": 3,
        "cooldown_hours": 24,
        "fixed_mode_threshold": 800000,
        "fixed_mode_margin": 1000,
        "signal_time_utc": "00:00",
    }
}


def build_cfg(btc_cap, eth_cap):
    c = copy.deepcopy(BASE_CFG)
    c["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["max_prev_amp"] = btc_cap
    c["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["max_prev_amp"] = eth_cap
    return c


def load_slice(csv):
    df = load_csv(Path(csv))
    df = df[(df["ts"] >= START) & (df["ts"] < END)].reset_index(drop=True)
    df["date"] = df["ts"].dt.date
    return df


def run(pair, csv, cfg):
    df = load_slice(csv)
    # 用 reentry_floats：simulate() 支持传入 —— 但它只对第 pair 生效
    reentry = cfg["strategy"]["pair_overrides"][pair].get("reentry_floats")
    res = simulate(df, pair, cfg, initial_balance=75.0, days=None, reentry_floats=reentry)
    return res


def summarize(label, res_btc, res_eth):
    total_final = res_btc["final"] + res_eth["final"]
    total_init = res_btc["initial"] + res_eth["initial"]
    total_ret = (total_final - total_init) / total_init * 100
    print(f"\n=== {label} ===")
    print(f"{'':25} {'BTC':>10} {'ETH':>10} {'合计':>10}")
    print(f"{'初始':<25} {res_btc['initial']:>10.2f} {res_eth['initial']:>10.2f} {total_init:>10.2f}")
    print(f"{'结束':<25} {res_btc['final']:>10.2f} {res_eth['final']:>10.2f} {total_final:>10.2f}")
    print(f"{'总收益%':<25} {res_btc['total_return_pct']:>+9.2f}% {res_eth['total_return_pct']:>+9.2f}% {total_ret:>+9.2f}%")
    print(f"{'交易笔数':<25} {res_btc['trades']:>10} {res_eth['trades']:>10} {res_btc['trades']+res_eth['trades']:>10}")
    print(f"{'胜率%':<25} {res_btc['win_rate_pct']:>9.1f}% {res_eth['win_rate_pct']:>9.1f}%")
    print(f"{'MDD%':<25} {res_btc['max_dd_pct']:>9.1f}% {res_eth['max_dd_pct']:>9.1f}%")
    print(f"{'PF':<25} {res_btc['profit_factor']:>10.2f} {res_eth['profit_factor']:>10.2f}")


COMBOS = [
    ("现状: BTC 4% / ETH 4%",     0.04, 0.04),
    ("BTC 4% / ETH 8% (上次)",    0.04, 0.08),
    ("BTC 3% / ETH 8% (本次)",    0.03, 0.08),
    ("BTC 2% / ETH 8%",           0.02, 0.08),
    ("BTC 2.5% / ETH 8%",         0.025, 0.08),
    ("BTC 3.5% / ETH 8%",         0.035, 0.08),
    ("BTC 3% / ETH 6%",           0.03, 0.06),
    ("BTC 3% / ETH 10%",          0.03, 0.10),
]


def main():
    print("窗口: 2025-07-01 ~ 2026-07-01，各 75U 独立本金")
    for label, btc_cap, eth_cap in COMBOS:
        cfg = build_cfg(btc_cap, eth_cap)
        rb = run("BTC-USDT-SWAP", "csv_data/BTC_USDT_SWAP_1H_400d.csv", cfg)
        re = run("ETH-USDT-SWAP", "csv_data/ETH_USDT_SWAP_1H_400d.csv", cfg)
        summarize(label, rb, re)


if __name__ == "__main__":
    main()
