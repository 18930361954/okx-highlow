"""在总保证金占用 = 30% 的约束下，扫描 BTC/ETH/SOL 分配方案，
   同时满足强平安全（三对全失效临界 >=5% 且 ETH 单失效 >=8%）。
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
MAINT = 0.005
TOTAL_MARGIN = 0.30  # 硬约束

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


def safety_check(bp, ep, sp, blev=100, elev=100, slev=50, bsl=0.01, esl=0.008, ssl=0.02):
    bn, en, sn = bp*blev, ep*elev, sp*slev
    total_n = bn + en + sn
    maint = total_n * MAINT
    liq_all = (1-maint) / total_n
    others = bn*bsl + sn*ssl
    eth_liq = (1 - others - maint) / en if en > 0 else 99
    day_loss = bn*bsl + en*esl + sn*ssl + total_n*(FEE+SLIP)
    return liq_all, eth_liq, day_loss, total_n


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
    print(f"总保证金约束 = 30%，扫描 BTC/ETH/SOL 所有分配方案（步长 1%）")
    print(f"起始 {INITIAL}U，2Y")
    print("=" * 105)

    # 枚举所有 sum=30% 的三元组
    all_combos = []
    for bp_pct in range(1, 15):  # BTC 1-14%
        for ep_pct in range(1, 15):
            sp_pct = 30 - bp_pct - ep_pct
            if sp_pct < 3 or sp_pct > 25:
                continue
            all_combos.append((bp_pct/100, ep_pct/100, sp_pct/100))

    # 分类
    safe = []
    marginal = []
    unsafe = []
    for bp, ep, sp in all_combos:
        liq_all, eth_liq, day_loss, total_n = safety_check(bp, ep, sp)
        info = (bp, ep, sp, liq_all, eth_liq, day_loss, total_n)
        if liq_all >= 0.05 and eth_liq >= 0.08:
            safe.append(info)
        elif liq_all >= 0.04 and eth_liq >= 0.06:
            marginal.append(info)
        else:
            unsafe.append(info)

    print(f"\n所有满足 sum=30% 的组合: {len(all_combos)} 个")
    print(f"  ✅ 安全 (liq_all>=5% & eth_liq>=8%): {len(safe)} 个")
    print(f"  ⚠️  警戒 (4-5% / 6-8%): {len(marginal)} 个")
    print(f"  ❌ 危险 (<4% 或 <6%): {len(unsafe)} 个")

    # 跑安全组合的回测
    if not safe:
        print("\n没有满足安全约束的组合！可能是 ETH 100x 太重，需要降杠杆或改仓位比例")
        return

    print(f"\n{'BTC':>4} {'ETH':>4} {'SOL':>4} | {'liq_all':>7} {'eth_liq':>7} {'day_loss':>8} "
          f"{'总敞口':>7} | {'2Y%':>10} {'Y1%':>7} {'Y2%':>8} {'MDD%':>5} {'月化%':>6}")
    print("-" * 120)
    results = []
    for bp, ep, sp, liq_all, eth_liq, day_loss, total_n in sorted(safe, key=lambda x: -x[3]):
        r = run_one(bp, ep, sp)
        y1 = r["year1_end_balance"]
        y1_ret = (y1 - INITIAL) / INITIAL if y1 else 0
        y2_ret = (r["final"] - y1) / y1 if y1 else 0
        results.append({"bp": bp, "ep": ep, "sp": sp, "ret": r["total_return"],
                        "mdd": r["max_dd"], "y1": y1_ret, "y2": y2_ret,
                        "monthly": r["monthly"], "final": r["final"],
                        "liq_all": liq_all, "eth_liq": eth_liq, "day_loss": day_loss})
        print(f"{bp*100:>3.0f}% {ep*100:>3.0f}% {sp*100:>3.0f}% | "
              f"{liq_all*100:>6.2f}% {eth_liq*100:>6.2f}% {day_loss*100:>7.1f}% "
              f"{total_n*100:>6.0f}% | {r['total_return']*100:>+9.1f}% "
              f"{y1_ret*100:>+6.1f}% {y2_ret*100:>+7.1f}% {r['max_dd']*100:>4.1f}% "
              f"{r['monthly']*100:>+5.2f}%")

    print(f"\n{'=' * 60}")
    print("Top 5 by 收益（30% 保证金 + 安全约束）：")
    results.sort(key=lambda x: -x["ret"])
    for r in results[:5]:
        print(f"  BTC={r['bp']*100:.0f}% ETH={r['ep']*100:.0f}% SOL={r['sp']*100:.0f}%: "
              f"2Y={r['ret']*100:+.1f}% MDD={r['mdd']*100:.1f}% "
              f"Y2 末余={r['final']:.0f}U (liq_all={r['liq_all']*100:.2f}%)")


if __name__ == "__main__":
    main()
