"""在强平安全线（>=5%）约束下，寻找最优三币仓位组合。

约束：
  1. 三对同 SL 失效时强平门槛 X >= 5%（即需三对同时反向暴跌 5%+ 才强平）
  2. 单对 SL 失效时强平门槛 >= 8%（可承受单币 flash crash 8%）
  3. 总保证金占用 <= 50%（保留缓冲）
"""
import sys, copy
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest import load_csv  # noqa: E402
from scripts.backtest_joint import joint_backtest_multi, load_cfg  # noqa: E402


INITIAL = 280.0
FEE = 0.0004
SLIP = 0.0002
Y_START = pd.Timestamp("2024-07-01", tz="UTC")
Y_MID = pd.Timestamp("2025-07-01", tz="UTC")
Y_END = pd.Timestamp("2026-07-01", tz="UTC")
MAINT = 0.005  # OKX cross 维持保证金率


def liquidation_analysis(bp, ep, sp, blev=100, elev=100, slev=50,
                          bsl=0.01, esl=0.008, ssl=0.02):
    """返回 (三对全SL失效强平门槛, ETH 最脆弱单币门槛, 单日SL总亏损)"""
    bn, en, sn = bp*blev, ep*elev, sp*slev
    total_n = bn + en + sn
    maint = total_n * MAINT
    # 三对同向失效
    liq_all = (1 - maint) / total_n
    # ETH 单独失效（其他 SL 触发）
    others_loss = bn*bsl + sn*ssl
    available = 1 - others_loss - maint
    eth_liq = available / en
    # 单日 SL 全触发亏损
    single_day_loss = bn*bsl + en*esl + sn*ssl + total_n*(FEE + SLIP)
    return liq_all, eth_liq, single_day_loss


DF_CACHE = None
def load_all():
    global DF_CACHE
    if DF_CACHE is None:
        DF_CACHE = {
            "BTC-USDT-SWAP": load_csv(Path("csv_data/BTC_USDT_SWAP_1H_780d.csv")),
            "ETH-USDT-SWAP": load_csv(Path("csv_data/ETH_USDT_SWAP_1H_780d.csv")),
            "SOL-USDT-SWAP": load_csv(Path("csv_data/SOL_USDT_SWAP_1H_780d.csv")),
        }
    return {k: v.copy() for k, v in DF_CACHE.items()}


def base_cfg():
    cfg = load_cfg()
    cfg["strategy"]["pairs"] = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
    cfg["strategy"]["pair_overrides"]["SOL-USDT-SWAP"] = {
        "position_pct": 0.15, "leverage": 50,
        "tp_pct": 0.010, "sl_pct": 0.020,
        "float_pct": 0.002, "reentry_floats": [0.002, 0.008],
        "min_prev_amp": 0.015, "max_prev_amp": 0.12,
    }
    return cfg


def run_one(bp, ep, sp):
    cfg = base_cfg()
    cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["position_pct"] = bp
    cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["position_pct"] = ep
    cfg["strategy"]["pair_overrides"]["SOL-USDT-SWAP"]["position_pct"] = sp
    r = joint_backtest_multi(load_all(), cfg, initial=INITIAL,
                              start=Y_START, end=Y_END,
                              fee_rt=FEE, slippage_rt=SLIP,
                              year1_end=Y_MID)
    return r


def main():
    print("=" * 105)
    print("三币安全仓位寻优：强平门槛 (三对全失效) >= 5% 且 (ETH 单独失效) >= 8%")
    print(f"起始 {INITIAL}U，2Y，全 maker 0.06% 成本")
    print("=" * 105)

    # 只测满足安全约束的组合
    candidates = []
    for bp in [0.02, 0.03, 0.04, 0.05]:
        for ep in [0.05, 0.06, 0.07, 0.08, 0.10]:
            for sp in [0.06, 0.08, 0.10, 0.12, 0.15]:
                liq_all, eth_liq, day_loss = liquidation_analysis(bp, ep, sp)
                total_notional = (bp*100 + ep*100 + sp*50)
                if liq_all >= 0.05 and eth_liq >= 0.08:
                    candidates.append((bp, ep, sp, liq_all, eth_liq, day_loss, total_notional))

    print(f"\n候选安全组合：{len(candidates)} 个（先按强平门槛过滤）\n")

    print(f"{'BTC':>4} {'ETH':>4} {'SOL':>4} {'总敞口':>7} {'全失效临界':>10} "
          f"{'ETH临界':>8} {'单日最大亏':>10} | {'2Y%':>10} {'Y1%':>7} {'Y2%':>8} "
          f"{'MDD%':>5} {'月化%':>6}")
    print("-" * 130)

    results = []
    for bp, ep, sp, liq_all, eth_liq, day_loss, total_n in candidates:
        r = run_one(bp, ep, sp)
        ret = r["total_return"]
        mdd = r["max_dd"]
        y1 = r["year1_end_balance"]
        y1_ret = (y1 - INITIAL) / INITIAL if y1 else 0
        y2_ret = (r["final"] - y1) / y1 if y1 else 0
        monthly = r["monthly"]
        results.append({
            "bp": bp, "ep": ep, "sp": sp,
            "ret": ret, "mdd": mdd, "y1": y1_ret, "y2": y2_ret,
            "monthly": monthly, "liq_all": liq_all, "eth_liq": eth_liq,
            "day_loss": day_loss, "final": r["final"],
            "total_n": total_n,
        })
        print(f"{bp*100:>3.0f}% {ep*100:>3.0f}% {sp*100:>3.0f}% "
              f"{total_n:>6.0f}% {liq_all*100:>9.2f}% "
              f"{eth_liq*100:>7.2f}% {day_loss*100:>9.1f}% | "
              f"{ret*100:>+9.1f}% {y1_ret*100:>+6.1f}% {y2_ret*100:>+7.1f}% "
              f"{mdd*100:>4.1f}% {monthly*100:>+5.2f}%")

    # 排序找最优
    print(f"\n{'=' * 60}")
    print("Top 5 by 收益（在安全约束下）：")
    results.sort(key=lambda x: x["ret"], reverse=True)
    for r in results[:5]:
        print(f"  BTC={r['bp']*100:.0f}% ETH={r['ep']*100:.0f}% SOL={r['sp']*100:.0f}%: "
              f"2Y={r['ret']*100:+.1f}% MDD={r['mdd']*100:.1f}% "
              f"全失效临界={r['liq_all']*100:.2f}% Y2末余={r['final']:.0f}U")

    print(f"\n{'=' * 60}")
    print("Top 5 by 收益/MDD（风险调整）：")
    results.sort(key=lambda x: x["ret"] / max(x["mdd"], 0.01), reverse=True)
    for r in results[:5]:
        rr = r["ret"] / max(r["mdd"], 0.01)
        print(f"  BTC={r['bp']*100:.0f}% ETH={r['ep']*100:.0f}% SOL={r['sp']*100:.0f}%: "
              f"比率={rr:.0f} 2Y={r['ret']*100:+.1f}% MDD={r['mdd']*100:.1f}%")


if __name__ == "__main__":
    main()
