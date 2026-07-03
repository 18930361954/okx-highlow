"""联合回测参数寻优（145U 起始，2 年连续复利，全 maker 0.06% 成本）。

阶段 1：仓位百分比网格（BTC × ETH）
阶段 2：TP/SL 组合（分品种）
阶段 3：加仓/重挂参数（reentry_floats）
"""
import sys
import copy
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest import load_csv  # noqa: E402
from scripts.backtest_joint import joint_backtest, load_cfg  # noqa: E402


INITIAL = 145.0
FEE = 0.0004
SLIP = 0.0002
Y_START = pd.Timestamp("2024-07-01", tz="UTC")
Y_MID = pd.Timestamp("2025-07-01", tz="UTC")
Y_END = pd.Timestamp("2026-07-01", tz="UTC")


def load():
    return (load_csv(Path("csv_data/BTC_USDT_SWAP_1H_780d.csv")),
            load_csv(Path("csv_data/ETH_USDT_SWAP_1H_780d.csv")))


def run_one(cfg):
    df_btc, df_eth = load()
    return joint_backtest(df_btc.copy(), df_eth.copy(), cfg,
                          initial=INITIAL, start=Y_START, end=Y_END,
                          fee_rt=FEE, slippage_rt=SLIP, year1_end=Y_MID)


def stage_position_grid():
    print("=" * 100)
    print(f"[阶段 1] 仓位百分比网格（BTC pos × ETH pos，其他参数不变）")
    print(f"起始 {INITIAL:.0f}U 成本 0.06% (费{FEE*100:.3f}%+滑{SLIP*100:.3f}%)")
    print("=" * 100)

    base = load_cfg()
    btc_ps = [0.03, 0.05, 0.07, 0.10, 0.12, 0.15]
    eth_ps = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20]

    print(f"\n{'BTC%':>4} {'ETH%':>4} | {'2Y 收益%':>10} {'Y1%':>7} {'Y2%':>8} {'月化%':>6} "
          f"{'MDD%':>6} {'BTC PnL':>9} {'ETH PnL':>10} {'Y2末余额':>10}")
    print("-" * 100)

    results = []
    for bp in btc_ps:
        for ep in eth_ps:
            cfg = copy.deepcopy(base)
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["position_pct"] = bp
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["position_pct"] = ep
            r = run_one(cfg)
            y1 = r["year1_end_balance"]
            y1_ret = (y1 - INITIAL) / INITIAL * 100 if y1 else 0
            y2_ret = (r["final"] - y1) / y1 * 100 if y1 else 0
            print(f"{bp*100:>3.0f}% {ep*100:>3.0f}% | "
                  f"{r['total_return']*100:>+9.1f}% "
                  f"{y1_ret:>+6.1f}% {y2_ret:>+7.1f}% "
                  f"{r['monthly']*100:>+5.2f}% {r['max_dd']*100:>5.1f}% "
                  f"{r['per_pair']['BTC-USDT-SWAP']['pnl']:>+8.1f} "
                  f"{r['per_pair']['ETH-USDT-SWAP']['pnl']:>+9.1f} "
                  f"{r['final']:>10.2f}")
            results.append({"bp": bp, "ep": ep, "ret": r['total_return'],
                            "mdd": r['max_dd'], "final": r['final']})

    # 排序找最好的
    results.sort(key=lambda x: x['ret'], reverse=True)
    print("\n[Top 5 by 收益]")
    for r in results[:5]:
        print(f"  BTC={r['bp']*100:.0f}% ETH={r['ep']*100:.0f}%: 收益={r['ret']*100:+.1f}% "
              f"MDD={r['mdd']*100:.1f}% 末余={r['final']:.0f}")
    print("\n[Top 5 by Return/MDD]")
    results.sort(key=lambda x: (x['ret'] / max(x['mdd'], 0.01)), reverse=True)
    for r in results[:5]:
        rr = r['ret'] / max(r['mdd'], 0.01)
        print(f"  BTC={r['bp']*100:.0f}% ETH={r['ep']*100:.0f}%: 比率={rr:.2f} "
              f"收益={r['ret']*100:+.1f}% MDD={r['mdd']*100:.1f}%")
    return results


if __name__ == "__main__":
    stage_position_grid()
