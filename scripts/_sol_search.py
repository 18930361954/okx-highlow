"""SOL 单币回测寻优（280U 起，2Y，50x 杠杆，全 maker 0.06% 成本）。

阶段 A: max_prev_amp × min_prev_amp
阶段 B: TP × SL
阶段 C: position
阶段 D: reentry / float
阶段 E: 最终组合
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

DF_SOL = None


def load_sol():
    global DF_SOL
    if DF_SOL is None:
        DF_SOL = load_csv(Path("csv_data/SOL_USDT_SWAP_1H_780d.csv"))
    return DF_SOL.copy()


def make_sol_only_cfg(pos=0.08, lev=50, tp=0.02, sl=0.01, float_pct=0.002,
                     reentry=None, min_amp=0.01, max_amp=0.12):
    cfg = load_cfg()
    # 清空默认的 pair_overrides，只留 SOL
    cfg["strategy"]["pairs"] = ["SOL-USDT-SWAP"]
    cfg["strategy"]["pair_overrides"] = {
        "SOL-USDT-SWAP": {
            "position_pct": pos,
            "leverage": lev,
            "tp_pct": tp,
            "sl_pct": sl,
            "float_pct": float_pct,
            "reentry_floats": reentry or [float_pct, float_pct * 3],
            "min_prev_amp": min_amp,
            "max_prev_amp": max_amp,
        }
    }
    return cfg


def run_sol(cfg):
    dfs = {"SOL-USDT-SWAP": load_sol()}
    return joint_backtest_multi(dfs, cfg, initial=INITIAL,
                                 start=Y_START, end=Y_END,
                                 fee_rt=FEE, slippage_rt=SLIP,
                                 year1_end=Y_MID)


def summ(r):
    y1 = r["year1_end_balance"]
    y1_ret = (y1 - INITIAL) / INITIAL if y1 else 0
    y2_ret = (r["final"] - y1) / y1 if y1 else 0
    ps = r["per_pair"]["SOL-USDT-SWAP"]
    return {
        "ret": r["total_return"], "mdd": r["max_dd"], "final": r["final"],
        "y1": y1_ret, "y2": y2_ret,
        "n": ps["n"], "w": ps["w"], "l": ps["l"], "pnl": ps["pnl"],
        "wr": ps["w"]/max(ps["n"], 1),
    }


def stageA_amp():
    print("\n" + "=" * 90)
    print("[阶段 A] max_prev_amp × min_prev_amp（其他保持占位）")
    print("=" * 90)
    print(f"{'min':>5} {'max':>5} | {'2Y%':>9} {'Y1%':>7} {'Y2%':>8} {'MDD%':>5} {'胜率':>5} {'笔数':>4}")
    print("-" * 70)
    best = None
    for lo in [0.005, 0.01, 0.015, 0.02]:
        for hi in [0.06, 0.08, 0.10, 0.12, 0.15]:
            cfg = make_sol_only_cfg(min_amp=lo, max_amp=hi)
            r = run_sol(cfg); s = summ(r)
            print(f"{lo*100:>4.1f}% {hi*100:>4.1f}% | "
                  f"{s['ret']*100:>+8.1f}% {s['y1']*100:>+6.1f}% {s['y2']*100:>+7.1f}% "
                  f"{s['mdd']*100:>4.1f}% {s['wr']*100:>4.1f}% {s['n']:>4}")
            if best is None or s["ret"] > best["ret"]:
                best = {"min": lo, "max": hi, **s}
    print(f"\n>>> A 最优: min={best['min']*100:.1f}% max={best['max']*100:.1f}% 收益={best['ret']*100:+.1f}%")
    return best


def stageB_tpsl(best_a):
    print("\n" + "=" * 90)
    print(f"[阶段 B] TP × SL (min={best_a['min']*100:.1f}%, max={best_a['max']*100:.1f}%)")
    print("=" * 90)
    print(f"{'TP':>5} {'SL':>5} {'R:R':>5} | {'2Y%':>9} {'Y1%':>7} {'Y2%':>8} {'MDD%':>5} {'胜率':>5}")
    print("-" * 75)
    best = None
    for tp in [0.010, 0.0125, 0.015, 0.0175, 0.020, 0.025, 0.030]:
        for sl in [0.006, 0.008, 0.010, 0.012, 0.015, 0.020]:
            cfg = make_sol_only_cfg(tp=tp, sl=sl,
                                     min_amp=best_a["min"], max_amp=best_a["max"])
            r = run_sol(cfg); s = summ(r)
            print(f"{tp*100:>4.2f}% {sl*100:>4.2f}% {tp/sl:>4.2f} | "
                  f"{s['ret']*100:>+8.1f}% {s['y1']*100:>+6.1f}% {s['y2']*100:>+7.1f}% "
                  f"{s['mdd']*100:>4.1f}% {s['wr']*100:>4.1f}%")
            if best is None or s["ret"] > best["ret"]:
                best = {"tp": tp, "sl": sl, **s}
    print(f"\n>>> B 最优: TP={best['tp']*100:.2f}% SL={best['sl']*100:.2f}% 收益={best['ret']*100:+.1f}%")
    return best


def stageC_position(best_a, best_b):
    print("\n" + "=" * 90)
    print(f"[阶段 C] position × leverage (TP={best_b['tp']*100:.2f}% SL={best_b['sl']*100:.2f}%)")
    print("=" * 90)
    print(f"{'pos':>4} {'lev':>4} | {'2Y%':>9} {'MDD%':>5} {'笔数':>4}")
    print("-" * 55)
    best = None
    for pos in [0.03, 0.05, 0.07, 0.10, 0.12, 0.15]:
        cfg = make_sol_only_cfg(pos=pos, lev=50, tp=best_b["tp"], sl=best_b["sl"],
                                 min_amp=best_a["min"], max_amp=best_a["max"])
        r = run_sol(cfg); s = summ(r)
        print(f"{pos*100:>3.0f}% {50:>3}x | "
              f"{s['ret']*100:>+8.1f}% {s['mdd']*100:>4.1f}% {s['n']:>4}")
        if best is None or s["ret"] > best["ret"]:
            best = {"pos": pos, **s}
    print(f"\n>>> C 最优: pos={best['pos']*100:.0f}% 收益={best['ret']*100:+.1f}%")
    return best


def stageD_float_reentry(best_a, best_b, best_c):
    print("\n" + "=" * 90)
    print("[阶段 D] float / reentry")
    print("=" * 90)
    combos = [
        ("float=0.15% re=[.0015,.005]",  0.0015, [0.0015, 0.005]),
        ("float=0.2%  re=[.002,.006]",   0.002,  [0.002, 0.006]),
        ("float=0.25% re=[.0025,.006]",  0.0025, [0.0025, 0.006]),
        ("float=0.3%  re=[.003,.006]",   0.003,  [0.003, 0.006]),
        ("float=0.2%  re=[.002,.008]",   0.002,  [0.002, 0.008]),
        ("float=0.2%  re=[.002]",        0.002,  [0.002]),
        ("float=0.2%  re=[.002,.005,.008]", 0.002, [0.002, 0.005, 0.008]),
    ]
    print(f"{'name':<32} | {'2Y%':>9} {'MDD%':>5} {'胜率':>5} {'笔数':>4}")
    print("-" * 75)
    best = None
    for name, fp, re in combos:
        cfg = make_sol_only_cfg(pos=best_c["pos"], tp=best_b["tp"], sl=best_b["sl"],
                                 float_pct=fp, reentry=re,
                                 min_amp=best_a["min"], max_amp=best_a["max"])
        r = run_sol(cfg); s = summ(r)
        print(f"{name:<32} | "
              f"{s['ret']*100:>+8.1f}% {s['mdd']*100:>4.1f}% {s['wr']*100:>4.1f}% {s['n']:>4}")
        if best is None or s["ret"] > best["ret"]:
            best = {"fp": fp, "re": re, "name": name, **s}
    print(f"\n>>> D 最优: {best['name']} 收益={best['ret']*100:+.1f}%")
    return best


def stageE_final(best_a, best_b, best_c, best_d):
    print("\n" + "=" * 90)
    print("[阶段 E] SOL 最终参数验证")
    print("=" * 90)
    cfg = make_sol_only_cfg(pos=best_c["pos"], lev=50,
                             tp=best_b["tp"], sl=best_b["sl"],
                             float_pct=best_d["fp"], reentry=best_d["re"],
                             min_amp=best_a["min"], max_amp=best_a["max"])
    r = run_sol(cfg); s = summ(r)
    print(f"\nSOL 最优参数（单币回测 280U）：")
    print(f"  pos={best_c['pos']*100:.0f}%  leverage=50x  TP={best_b['tp']*100:.2f}%  SL={best_b['sl']*100:.2f}%")
    print(f"  float={best_d['fp']*100:.2f}%  reentry={best_d['re']}")
    print(f"  min_amp={best_a['min']*100:.1f}%  max_amp={best_a['max']*100:.1f}%")
    print(f"\n结果：")
    print(f"  2Y 收益: {s['ret']*100:+.2f}%   Y1: {s['y1']*100:+.2f}%  Y2: {s['y2']*100:+.2f}%")
    print(f"  MDD: {s['mdd']*100:.2f}%   胜率: {s['wr']*100:.2f}%   笔数: {s['n']}")
    print(f"  Y2 末余额: {s['final']:.2f} U")
    return {"pos": best_c["pos"], "tp": best_b["tp"], "sl": best_b["sl"],
            "fp": best_d["fp"], "re": best_d["re"],
            "min": best_a["min"], "max": best_a["max"]}


def main():
    print("=" * 90)
    print(f"SOL 单币寻优：起 {INITIAL}U，2Y，50x 杠杆，全 maker 0.06% 成本")
    print("=" * 90)
    a = stageA_amp()
    b = stageB_tpsl(a)
    c = stageC_position(a, b)
    d = stageD_float_reentry(a, b, c)
    stageE_final(a, b, c, d)


if __name__ == "__main__":
    main()
