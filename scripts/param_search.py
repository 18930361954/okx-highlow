"""参数扫描：多维网格，找高复利参数组合。
默认扫 position_pct × tp_pct × sl_pct，浮动值固定为当前 config 里的。

  python scripts/param_search.py --pair ETH-USDT-SWAP --mode high-return --top 10

约束：position_pct × leverage × sl_pct ≤ single-loss-max（默认 0.20，即单笔最大亏损 20% 余额）。
评分模式：
  - "high-return" (默认)：总收益 60% + 正收益率 25% + 中位 15%（不看尾部）
  - "balanced"         ：总收益 30% + 正收益率 30% + 中位 25% + 尾部 15%
"""
import argparse
import statistics
import sys
from copy import deepcopy
from itertools import product
from pathlib import Path

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


def _build_cfg(base_cfg: dict, pair: str,
               position_pct: float, tp_pct: float, sl_pct: float,
               leverage: int) -> dict:
    """基于 base 复制并覆盖 pair 的关键参数。"""
    cfg = deepcopy(base_cfg)
    s = cfg["strategy"]
    s["position_pct"] = position_pct
    s["leverage"] = leverage
    ov = s.setdefault("pair_overrides", {}).setdefault(pair, {})
    ov["tp_pct"] = tp_pct
    ov["sl_pct"] = sl_pct
    # 顶层也覆盖一遍，兼容 backtest 里读取路径
    s["tp_pct"] = tp_pct
    s["sl_pct"] = sl_pct
    return cfg


def _floats_of(pair: str, cfg: dict) -> list[float] | None:
    ov = (cfg["strategy"].get("pair_overrides") or {}).get(pair) or {}
    seq = ov.get("reentry_floats") or []
    return list(seq) if seq else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="ETH-USDT-SWAP")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--window", type=int, default=30)
    ap.add_argument("--step", type=int, default=7)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--pos-pct", default="0.10,0.15,0.20,0.25",
                    help="仓位比例候选（逗号）")
    ap.add_argument("--tp-list", default=None,
                    help="止盈候选（逗号）；默认 pair 自适应")
    ap.add_argument("--sl-list", default=None,
                    help="止损候选（逗号）；默认 pair 自适应")
    ap.add_argument("--leverage", type=int, default=100)
    ap.add_argument("--single-loss-max", type=float, default=0.20,
                    help="单笔最大亏损占余额比例（约束：pos×lev×sl ≤ 此值）")
    ap.add_argument("--mode", default="high-return",
                    choices=["high-return", "balanced"],
                    help="评分模式")
    ap.add_argument("--fp1", type=float, default=None,
                    help="首次浮动。默认读 pair 当前配置")
    ap.add_argument("--fp2", type=float, default=None,
                    help="重挂浮动。默认读 pair 当前配置")
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
        base_cfg = yaml.safe_load(f)

    # pair-自适应默认 tp/sl 候选
    if args.tp_list:
        tp_grid = [float(x) for x in args.tp_list.split(",")]
    else:
        # ETH 更抗打（1H 波动大）→ tp 范围偏大
        if "ETH" in args.pair:
            tp_grid = [0.015, 0.020, 0.025, 0.030]
        else:
            tp_grid = [0.010, 0.012, 0.015, 0.020]
    if args.sl_list:
        sl_grid = [float(x) for x in args.sl_list.split(",")]
    else:
        if "ETH" in args.pair:
            sl_grid = [0.007, 0.010, 0.012]
        else:
            sl_grid = [0.004, 0.005, 0.007]

    pos_grid = [float(x) for x in args.pos_pct.split(",")]

    # 浮动值：默认走 config 里 pair 的重挂序列；若显式传 fp1/fp2 则用
    default_reentry = _floats_of(args.pair, base_cfg)
    if args.fp1 is not None and args.fp2 is not None:
        reentry = [args.fp1, args.fp2]
    elif default_reentry:
        reentry = default_reentry
    else:
        # pair 未配重挂 → 用最小启动组
        reentry = [0.0015, 0.006] if "ETH" in args.pair else [0.002, 0.004]

    combos = list(product(pos_grid, tp_grid, sl_grid))
    # 应用单笔亏损约束
    kept = [(p, t, s) for p, t, s in combos
             if p * args.leverage * s <= args.single_loss_max + 1e-9]
    dropped = len(combos) - len(kept)

    print(f"[scan] {args.pair} 组合={len(combos)}（保留 {len(kept)}，剔除 {dropped} 组超单笔亏损上限 {args.single_loss_max*100:g}%）")
    print(f"[fixed] reentry_floats={reentry}  leverage={args.leverage}x")

    df = load_csv(csv_path)

    rows = []
    for pos, tp, sl in kept:
        cfg = _build_cfg(base_cfg, args.pair, pos, tp, sl, args.leverage)
        total = simulate(df, args.pair, cfg, days=args.days, reentry_floats=reentry)
        wins = _rolling(df, args.pair, cfg, args.window, args.step, reentry=reentry)
        rets = [r["total_return_pct"] for r in wins]
        if not rets:
            continue
        pos_rate = sum(1 for x in rets if x > 0) / len(rets) * 100
        rows.append({
            "pos": pos, "tp": tp, "sl": sl,
            "single_loss": pos * args.leverage * sl,
            "total_ret": total["total_return_pct"],
            "total_win_rate": total["win_rate_pct"],
            "total_dd": total["max_dd_pct"],
            "total_pf": total["profit_factor"],
            "pos_rate": pos_rate,
            "median_ret": statistics.median(rets),
            "worst_ret": min(rets),
            "windows": len(wins),
        })

    if not rows:
        print("[scan] no results")
        return

    def _norm(vals):
        lo, hi = min(vals), max(vals)
        if hi == lo:
            return [0.5] * len(vals)
        return [(v - lo) / (hi - lo) for v in vals]

    tot = _norm([r["total_ret"] for r in rows])
    pos_n = _norm([r["pos_rate"] for r in rows])
    med = _norm([r["median_ret"] for r in rows])
    wst = _norm([-r["worst_ret"] for r in rows])

    if args.mode == "high-return":
        weights = (0.60, 0.25, 0.15, 0.00)  # tot, pos, med, wst
        label = "评分：总收益 60% + 正收益率 25% + 中位 15%"
    else:
        weights = (0.30, 0.30, 0.25, 0.15)
        label = "评分：总收益 30% + 正收益率 30% + 中位 25% + 尾部 15%"

    for i, r in enumerate(rows):
        r["score"] = (weights[0] * tot[i] + weights[1] * pos_n[i]
                       + weights[2] * med[i] + weights[3] * wst[i])
    rows.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n=== Top {args.top} 组合（{label}）===")
    print(f"{'pos%':>5} {'tp%':>5} {'sl%':>5} {'sl_bal%':>8} {'score':>6} "
          f"{'total%':>9} {'PF':>5} {'DD%':>7} {'win%':>6} {'pos%':>6} "
          f"{'med%':>7} {'worst%':>8}")
    for r in rows[:args.top]:
        print(f"{r['pos']*100:>5.0f} {r['tp']*100:>5.2f} {r['sl']*100:>5.2f} "
              f"{r['single_loss']*100:>7.1f}  {r['score']:>6.3f} "
              f"{r['total_ret']:>+9.2f} {r['total_pf']:>5.2f} {r['total_dd']:>7.2f} "
              f"{r['total_win_rate']:>6.2f} {r['pos_rate']:>6.1f} "
              f"{r['median_ret']:>+7.2f} {r['worst_ret']:>+8.2f}")


if __name__ == "__main__":
    main()
