"""参数扫描：对指定 pair 扫 (fp1, fp2) 两维网格，180d 总盘 + 30d 滚动窗口指标。
按综合评分排序输出 top-K。

  python scripts/param_search.py --pair ETH-USDT-SWAP --top 10

评分：0.4*正收益窗口占比 + 0.3*中位窗口收益 + 0.2*180d 总收益 + 0.1*(-最差窗口)
所有指标归一化到 [0,1]。仅供参考，不是绝对排序。
"""
import argparse
import statistics
import sys
from itertools import product
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest import load_csv, simulate  # noqa: E402


def _rolling(df, pair, cfg, window, step, reentry):
    dates = sorted(df["date"].unique())
    n = len(dates)
    results = []
    for start in range(0, n - window, step):
        end = start + window
        sub_dates = set(dates[start:end + 1])
        sub_df = df[df["date"].isin(sub_dates)].reset_index(drop=True)
        if len(sub_df) < window * 20:
            continue
        r = simulate(sub_df, pair, cfg, reentry_floats=reentry)
        results.append(r)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="ETH-USDT-SWAP")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--window", type=int, default=30)
    ap.add_argument("--step", type=int, default=7)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--fp1", default="0.001,0.0015,0.002,0.0025,0.003",
                    help="第1次浮动候选（逗号）")
    ap.add_argument("--fp2", default="0.004,0.005,0.006,0.007,0.008",
                    help="第2次浮动候选（逗号）")
    args = ap.parse_args()

    if args.csv:
        csv_path = Path(args.csv)
    else:
        token = args.pair.replace("-USDT-SWAP", "")
        csv_path = ROOT / "csv_data" / f"{token}_USDT_SWAP_1H_12m.csv"
    if not csv_path.exists():
        print(f"[err] CSV not found: {csv_path}")
        sys.exit(1)

    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    fp1_grid = [float(x) for x in args.fp1.split(",")]
    fp2_grid = [float(x) for x in args.fp2.split(",")]
    combos = [(a, b) for a, b in product(fp1_grid, fp2_grid) if b > a]  # 第2次浮动应比第1次大

    print(f"[scan] {args.pair}  fp1={fp1_grid}  fp2={fp2_grid}  组合数={len(combos)}")
    df = load_csv(csv_path)

    rows = []
    for fp1, fp2 in combos:
        total = simulate(df, args.pair, cfg, days=args.days,
                          reentry_floats=[fp1, fp2])
        wins = _rolling(df, args.pair, cfg, args.window, args.step,
                         reentry=[fp1, fp2])
        rets = [r["total_return_pct"] for r in wins]
        if not rets:
            continue
        pos_rate = sum(1 for x in rets if x > 0) / len(rets) * 100
        median_ret = statistics.median(rets)
        worst_ret = min(rets)
        rows.append({
            "fp1": fp1, "fp2": fp2,
            "total_ret": total["total_return_pct"],
            "total_win_rate": total["win_rate_pct"],
            "total_dd": total["max_dd_pct"],
            "total_pf": total["profit_factor"],
            "pos_rate": pos_rate,
            "median_ret": median_ret,
            "worst_ret": worst_ret,
            "windows": len(wins),
        })

    if not rows:
        print("[scan] no results")
        return

    # 归一化后综合评分
    def _norm(vals):
        lo, hi = min(vals), max(vals)
        if hi == lo:
            return [0.5] * len(vals)
        return [(v - lo) / (hi - lo) for v in vals]

    pos = _norm([r["pos_rate"] for r in rows])
    med = _norm([r["median_ret"] for r in rows])
    tot = _norm([r["total_ret"] for r in rows])
    wst = _norm([-r["worst_ret"] for r in rows])  # worst 越大（越负）分越低
    for i, r in enumerate(rows):
        r["score"] = 0.4 * pos[i] + 0.3 * med[i] + 0.2 * tot[i] + 0.1 * wst[i]

    rows.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n=== Top {args.top} 组合（评分：正收益率 40% + 中位收益 30% + 总收益 20% + 尾部 10%）===")
    print(f"{'fp1':>7} {'fp2':>7} {'score':>7} {'total%':>8} {'PF':>5} {'DD%':>7} "
          f"{'win%':>6} {'pos%':>6} {'med%':>7} {'worst%':>8}")
    for r in rows[:args.top]:
        print(f"{r['fp1']*100:>6.2f}% {r['fp2']*100:>6.2f}% {r['score']:>7.3f} "
              f"{r['total_ret']:>+8.2f} {r['total_pf']:>5.2f} {r['total_dd']:>7.2f} "
              f"{r['total_win_rate']:>6.2f} {r['pos_rate']:>6.1f} "
              f"{r['median_ret']:>+7.2f} {r['worst_ret']:>+8.2f}")


if __name__ == "__main__":
    main()
