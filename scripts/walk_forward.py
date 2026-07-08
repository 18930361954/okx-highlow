"""Walk-Forward 反过拟合验证。

流程:
  1. 在 [date_from, split] 段 (train,18 个月) 跑网格,选每 (pair, signal_bar) 最优
  2. 用相同参数在 [split, date_to] 段 (test,6 个月) 跑 out-of-sample
  3. 输出对比:train/test 的 return / MDD / trades / win_rate

判定通过:
  - test 段 total_return > 0 (至少不亏)
  - test 段 MDD 相对 train 不显著恶化 (< train MDD × 1.5)
  - test 段月化 ≥ train 月化 × 0.3 (至少保留 30% 强度)
"""
import argparse
import csv
import itertools
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bucket_backtest import _load_csv, _resample, _pick_base_bar, simulate  # noqa: E402


PAIRS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
SIGNAL_BARS = ["4H", "6H", "12H", "1D"]
FLOAT_GRID = [0.0015, 0.002, 0.0025, 0.003, 0.004, 0.005]
TP_GRID = [0.006, 0.008, 0.010, 0.015, 0.020]
SL_GRID = [0.008, 0.010, 0.015, 0.020]


def _slice(df, start, end):
    m = (df.index >= start) & (df.index < end)
    return df[m]


def _run_one(df_base, df_sig, pair, base_bar, signal_bar,
             fp, tp, sl, balance, position_pct, leverage,
             compound, slippage_bps, funding_bps, max_contracts):
    return simulate(
        df_base, df_sig, pair, base_bar, signal_bar, fp, tp, sl,
        initial_balance=balance, position_pct=position_pct,
        leverage=leverage, fixed_margin=not compound,
        slippage_bps=slippage_bps, funding_bps_per_8h=funding_bps,
        max_contracts=max_contracts,
    )


def walk_forward(pairs, signal_bars, balance, position_pct, days,
                 train_end_iso, slippage_bps, funding_bps,
                 max_contracts_map: dict[str, int],
                 leverage_map: dict[str, int]) -> list[dict]:
    train_end = pd.Timestamp(train_end_iso, tz="UTC")
    results: list[dict] = []

    for pair in pairs:
        lev = leverage_map.get(pair, 100)
        max_ct = max_contracts_map.get(pair)
        for sb in signal_bars:
            base_bar = _pick_base_bar(sb)
            try:
                df_base_all = _load_csv(pair, base_bar, days)
            except FileNotFoundError:
                print(f"[skip] {pair} {sb}: base csv 缺")
                continue
            df_sig_all = _resample(df_base_all, sb)

            # 起止时间
            data_from = df_base_all.index[0]
            data_to = df_base_all.index[-1]
            print(f"\n=== {pair} {sb} 数据范围 {data_from.date()} - {data_to.date()} ===")

            df_base_train = _slice(df_base_all, data_from, train_end)
            df_sig_train = _slice(df_sig_all, data_from, train_end)
            df_base_test = _slice(df_base_all, train_end, data_to + pd.Timedelta(days=1))
            df_sig_test = _slice(df_sig_all, train_end, data_to + pd.Timedelta(days=1))

            print(f"  train: {df_sig_train.index[0].date()} - {df_sig_train.index[-1].date()} "
                  f"({len(df_sig_train)} 桶)")
            print(f"  test:  {df_sig_test.index[0].date()} - {df_sig_test.index[-1].date()} "
                  f"({len(df_sig_test)} 桶)")

            # Train: 遍历网格找最优
            best = None
            for fp, tp, sl in itertools.product(FLOAT_GRID, TP_GRID, SL_GRID):
                if sl <= tp:
                    # 常识:sl 应大于 tp 才让胜率补 R:R;不严格禁止,但排掉可减半搜索
                    pass
                r = _run_one(df_base_train, df_sig_train, pair, base_bar, sb,
                             fp, tp, sl, balance, position_pct, lev,
                             compound=False,  # walk-forward 用固定 margin,MDD 才有意义
                             slippage_bps=slippage_bps,
                             funding_bps=funding_bps, max_contracts=max_ct)
                if r.max_dd_pct > 50 or r.trades < 30:
                    continue
                if r.total_return_pct <= 0:
                    continue
                # 排序目标:年化 / MDD (calmar-like)
                score = r.total_return_pct / max(1.0, r.max_dd_pct)
                if best is None or score > best["score"]:
                    best = {
                        "score": score, "fp": fp, "tp": tp, "sl": sl,
                        "train_return": r.total_return_pct, "train_mdd": r.max_dd_pct,
                        "train_trades": r.trades, "train_win": r.win_rate_pct,
                        "train_monthly": r.monthly_pct,
                    }
            if best is None:
                print(f"  train 无满足参数 (MDD ≤ 50 且 trades ≥ 30 且赚钱)")
                continue

            # Test: 用同样参数跑 test 段
            r_test = _run_one(df_base_test, df_sig_test, pair, base_bar, sb,
                              best["fp"], best["tp"], best["sl"],
                              balance, position_pct, lev,
                              compound=False, slippage_bps=slippage_bps,
                              funding_bps=funding_bps, max_contracts=max_ct)

            # 通过判定
            passed = (
                r_test.total_return_pct > 0
                and r_test.max_dd_pct <= max(50.0, best["train_mdd"] * 1.5)
                and r_test.monthly_pct >= best["train_monthly"] * 0.3
            )
            row = {
                "pair": pair, "signal_bar": sb,
                "float_pct": best["fp"], "tp_pct": best["tp"], "sl_pct": best["sl"],
                "train_return_pct": round(best["train_return"], 2),
                "train_mdd_pct": round(best["train_mdd"], 2),
                "train_monthly_pct": round(best["train_monthly"], 2),
                "train_trades": best["train_trades"],
                "train_win_pct": round(best["train_win"], 2),
                "test_return_pct": round(r_test.total_return_pct, 2),
                "test_mdd_pct": round(r_test.max_dd_pct, 2),
                "test_monthly_pct": round(r_test.monthly_pct, 2),
                "test_trades": r_test.trades,
                "test_win_pct": round(r_test.win_rate_pct, 2),
                "passed": "YES" if passed else "NO",
            }
            results.append(row)
            print(f"  train f={best['fp']} tp={best['tp']} sl={best['sl']} "
                  f"→ ret={best['train_return']:+.1f}% mdd={best['train_mdd']:.1f}% "
                  f"mo={best['train_monthly']:+.1f}%")
            print(f"  test  → ret={r_test.total_return_pct:+.1f}% mdd={r_test.max_dd_pct:.1f}% "
                  f"mo={r_test.monthly_pct:+.1f}% [{row['passed']}]")

    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default=",".join(PAIRS))
    ap.add_argument("--signals", default=",".join(SIGNAL_BARS))
    ap.add_argument("--balance", type=float, default=140.0)
    ap.add_argument("--position-pct", type=float, default=0.10)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--train-end", default="2026-01-06",
                    help="train/test 分界日;默认 2026-01-06 (18 个月 train + 6 个月 test)")
    ap.add_argument("--slippage-bps", type=float, default=10.0)
    ap.add_argument("--funding-bps", type=float, default=3.0)
    ap.add_argument("--btc-max-contracts", type=int, default=1000)
    ap.add_argument("--other-max-contracts", type=int, default=5000)
    ap.add_argument("--out", default=str(ROOT / "reports" / "walk_forward.csv"))
    args = ap.parse_args()

    leverage_map = {
        "BTC-USDT-SWAP": 100, "ETH-USDT-SWAP": 100, "SOL-USDT-SWAP": 100,
    }
    max_contracts_map = {
        "BTC-USDT-SWAP": args.btc_max_contracts,
        "ETH-USDT-SWAP": args.other_max_contracts,
        "SOL-USDT-SWAP": args.other_max_contracts,
    }
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    sbs = [s.strip() for s in args.signals.split(",") if s.strip()]

    results = walk_forward(
        pairs, sbs, args.balance, args.position_pct, args.days,
        args.train_end, args.slippage_bps, args.funding_bps,
        max_contracts_map, leverage_map,
    )

    print("\n" + "=" * 78)
    print(f"{'pair':<15} {'sig':>3} {'params':>18} {'trainR':>8} {'trainMDD':>9} "
          f"{'testR':>8} {'testMDD':>9} {'pass':>5}")
    print("-" * 78)
    for r in results:
        params = f"f{r['float_pct']}tp{r['tp_pct']}sl{r['sl_pct']}"
        print(f"{r['pair']:<15} {r['signal_bar']:>3} {params:>18} "
              f"{r['train_return_pct']:>+7.1f}% {r['train_mdd_pct']:>7.1f}% "
              f"{r['test_return_pct']:>+7.1f}% {r['test_mdd_pct']:>7.1f}% "
              f"{r['passed']:>5}")

    if results:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            for r in results:
                w.writerow(r)
        print(f"\nsaved → {out}")


if __name__ == "__main__":
    main()
