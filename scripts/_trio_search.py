"""BTC + ETH + SOL 三币联合回测 + 联合寻优（280U 起，2Y，全 maker 0.06% 成本）。

单币各自最优（起点）：
  BTC:  pos=5%,  lev=100x, TP=1.00%, SL=1.00%, max_amp=4.75%
  ETH:  pos=12%, lev=100x, TP=1.40%, SL=0.80%, max_amp=8%
  SOL:  pos=15%, lev=50x,  TP=1.00%, SL=2.00%, max_amp=12%

阶段 F：直接联合（用单币最优参数），看基线
阶段 G：SOL 加入后，仓位是否需要重分配？扫每对 pos
阶段 H：最终整合
"""
import sys
import copy
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


DFS = None

def load_all():
    global DFS
    if DFS is None:
        DFS = {
            "BTC-USDT-SWAP": load_csv(Path("csv_data/BTC_USDT_SWAP_1H_780d.csv")),
            "ETH-USDT-SWAP": load_csv(Path("csv_data/ETH_USDT_SWAP_1H_780d.csv")),
            "SOL-USDT-SWAP": load_csv(Path("csv_data/SOL_USDT_SWAP_1H_780d.csv")),
        }
    return {k: v.copy() for k, v in DFS.items()}


def base_cfg():
    """三币各自单币寻优后的最优参数"""
    cfg = load_cfg()
    cfg["strategy"]["pairs"] = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
    ov = cfg["strategy"]["pair_overrides"]
    # BTC / ETH 保持 config.yaml 现值（档 A 参数）
    # SOL 用刚寻优的最优参数
    ov["SOL-USDT-SWAP"] = {
        "position_pct": 0.15,
        "leverage": 50,
        "tp_pct": 0.010,
        "sl_pct": 0.020,
        "float_pct": 0.002,
        "reentry_floats": [0.002, 0.008],
        "min_prev_amp": 0.015,
        "max_prev_amp": 0.12,
    }
    return cfg


def run(cfg, dfs_subset=None):
    dfs = dfs_subset if dfs_subset is not None else load_all()
    return joint_backtest_multi(dfs, cfg, initial=INITIAL,
                                 start=Y_START, end=Y_END,
                                 fee_rt=FEE, slippage_rt=SLIP,
                                 year1_end=Y_MID)


def summ(r):
    y1 = r["year1_end_balance"]
    y1_ret = (y1 - INITIAL) / INITIAL if y1 else 0
    y2_ret = (r["final"] - y1) / y1 if y1 else 0
    return {"ret": r["total_return"], "mdd": r["max_dd"], "final": r["final"],
            "y1": y1_ret, "y2": y2_ret, "monthly": r["monthly"],
            "per_pair": r["per_pair"], "holds": r["days_hold_count"]}


def stageF_baseline():
    print("\n" + "=" * 90)
    print("[阶段 F] 三币基线（各自单币最优参数直接联合）")
    print("=" * 90)
    cfg = base_cfg()
    r = run(cfg); s = summ(r)
    print(f"起始: {INITIAL}U")
    print(f"Y1 结束: {r['year1_end_balance']:.2f}U ({s['y1']*100:+.2f}%)")
    print(f"Y2 结束: {s['final']:.2f}U (相对 Y1 {s['y2']*100:+.2f}%)")
    print(f"2Y 总收益: {s['ret']*100:+.2f}%")
    print(f"月化: {s['monthly']*100:+.2f}%")
    print(f"MDD: {s['mdd']*100:.2f}%")
    print(f"持仓分布: 0对={s['holds'].get(0,0)}日 1对={s['holds'].get(1,0)} 2对={s['holds'].get(2,0)} 3对={s['holds'].get(3,0)}")
    print(f"分币种贡献:")
    for p, ps in s["per_pair"].items():
        wr = ps["w"]/max(ps["n"],1)*100
        print(f"  {p}: 笔数={ps['n']:>3} 胜率={wr:>5.1f}% PnL={ps['pnl']:+.2f}U 费用={ps['cost']:.1f}U")
    return s


def stageG_position(baseline_s):
    print("\n" + "=" * 90)
    print("[阶段 G] 三币仓位再平衡（考虑同时持仓时资金压力）")
    print("=" * 90)
    combos = [
        # (BTC, ETH, SOL)
        (0.05, 0.12, 0.15),  # 基线
        (0.05, 0.10, 0.12),  # 全降
        (0.05, 0.10, 0.10),
        (0.05, 0.10, 0.15),
        (0.05, 0.12, 0.10),
        (0.05, 0.12, 0.12),
        (0.05, 0.15, 0.10),
        (0.05, 0.15, 0.12),
        (0.05, 0.15, 0.15),
        (0.03, 0.12, 0.15),
        (0.04, 0.12, 0.15),
        (0.05, 0.08, 0.15),
        (0.05, 0.10, 0.20),
        (0.05, 0.12, 0.20),  # 激进
        (0.03, 0.10, 0.20),
    ]
    print(f"{'BTC':>4} {'ETH':>4} {'SOL':>4} | {'2Y%':>10} {'Y1%':>7} {'Y2%':>8} {'MDD%':>5} "
          f"{'BTC PnL':>9} {'ETH PnL':>9} {'SOL PnL':>9}")
    print("-" * 105)
    best = None
    for bp, ep, sp in combos:
        cfg = base_cfg()
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["position_pct"] = bp
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["position_pct"] = ep
        cfg["strategy"]["pair_overrides"]["SOL-USDT-SWAP"]["position_pct"] = sp
        r = run(cfg); s = summ(r)
        btc_p = s["per_pair"]["BTC-USDT-SWAP"]["pnl"]
        eth_p = s["per_pair"]["ETH-USDT-SWAP"]["pnl"]
        sol_p = s["per_pair"]["SOL-USDT-SWAP"]["pnl"]
        marker = ""
        if bp == 0.05 and ep == 0.12 and sp == 0.15:
            marker = " ← 基线"
        print(f"{bp*100:>3.0f}% {ep*100:>3.0f}% {sp*100:>3.0f}% | "
              f"{s['ret']*100:>+9.1f}% {s['y1']*100:>+6.1f}% {s['y2']*100:>+7.1f}% "
              f"{s['mdd']*100:>4.1f}% {btc_p:>+9.0f} {eth_p:>+9.0f} {sol_p:>+9.0f}{marker}")
        if best is None or s["ret"] > best["ret"]:
            best = {"bp": bp, "ep": ep, "sp": sp, **s}
    print(f"\n>>> G 最优: BTC={best['bp']*100:.0f}% ETH={best['ep']*100:.0f}% SOL={best['sp']*100:.0f}% "
          f"收益={best['ret']*100:+.1f}%")
    return best


def stageH_bysolo(best_g):
    """对比：BTC+ETH（无 SOL）vs BTC+ETH+SOL"""
    print("\n" + "=" * 90)
    print("[阶段 H] SOL 加入的边际贡献")
    print("=" * 90)

    # 无 SOL（BTC+ETH）
    cfg = base_cfg()
    cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["position_pct"] = best_g["bp"]
    cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["position_pct"] = best_g["ep"]
    dfs = load_all()
    dfs_no_sol = {k: v for k, v in dfs.items() if k != "SOL-USDT-SWAP"}
    r_no = run(cfg, dfs_no_sol); s_no = summ(r_no)

    # 有 SOL
    cfg["strategy"]["pair_overrides"]["SOL-USDT-SWAP"]["position_pct"] = best_g["sp"]
    r_yes = run(cfg); s_yes = summ(r_yes)

    print(f"\n{'':<18} {'BTC+ETH':>15} {'BTC+ETH+SOL':>15} {'差异':>15}")
    print("-" * 70)
    print(f"{'2Y 收益':<15} {s_no['ret']*100:>+14.1f}% {s_yes['ret']*100:>+14.1f}% {(s_yes['ret']-s_no['ret'])*100:>+14.1f}pt")
    print(f"{'Y2 末余额':<15} {s_no['final']:>14.0f}U {s_yes['final']:>14.0f}U {(s_yes['final']-s_no['final']):>+14.0f}U")
    print(f"{'MDD':<15} {s_no['mdd']*100:>14.1f}% {s_yes['mdd']*100:>14.1f}% {(s_yes['mdd']-s_no['mdd'])*100:>+14.1f}pt")
    print(f"{'月化':<15} {s_no['monthly']*100:>+14.2f}% {s_yes['monthly']*100:>+14.2f}%")


def stageI_final(best_g):
    print("\n" + "=" * 90)
    print("[阶段 I] 三币最终推荐配置")
    print("=" * 90)
    print(f"\n统一起始 {INITIAL}U，2Y (2024-07-01 → 2026-07-01)，全 maker 成本 0.06%")
    print(f"\n【BTC-USDT-SWAP】")
    print(f"  position_pct: {best_g['bp']}")
    print(f"  leverage: 100")
    print(f"  tp_pct: 0.010    # 止盈 1.00%")
    print(f"  sl_pct: 0.010    # 止损 1.00%")
    print(f"  float_pct: 0.002")
    print(f"  reentry_floats: [0.002, 0.004]")
    print(f"  min_prev_amp: 0.01")
    print(f"  max_prev_amp: 0.0475")
    print(f"\n【ETH-USDT-SWAP】")
    print(f"  position_pct: {best_g['ep']}")
    print(f"  leverage: 100")
    print(f"  tp_pct: 0.014    # 止盈 1.40%")
    print(f"  sl_pct: 0.008    # 止损 0.80%")
    print(f"  reentry_floats: [0.0015, 0.006]")
    print(f"  min_prev_amp: 0.01")
    print(f"  max_prev_amp: 0.08")
    print(f"\n【SOL-USDT-SWAP】")
    print(f"  position_pct: {best_g['sp']}")
    print(f"  leverage: 50    # 用户指定 SOL 50x")
    print(f"  tp_pct: 0.010    # 止盈 1.00%")
    print(f"  sl_pct: 0.020    # 止损 2.00%（高胜率补偿低 R:R）")
    print(f"  float_pct: 0.002")
    print(f"  reentry_floats: [0.002, 0.008]")
    print(f"  min_prev_amp: 0.015")
    print(f"  max_prev_amp: 0.12")


def main():
    print("=" * 90)
    print(f"三币联合回测寻优：起 {INITIAL}U，2Y")
    print("=" * 90)
    baseline = stageF_baseline()
    best_g = stageG_position(baseline)
    stageH_bysolo(best_g)
    stageI_final(best_g)


if __name__ == "__main__":
    main()
