"""滑点敏感性分析:对固定参数,在多档滑点下跑,看多脆弱。

输入:一组 (pair, signal_bar, float, tp, sl) 参数;或从 walk_forward.csv 读通过的组合。
输出:每组参数 × [0, 10, 30, 50, 100] bp 滑点下的 return / MDD / 通过与否。

判定稳健:50bp 滑点下仍盈利且 MDD ≤ 60% → PASS
"""
import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bucket_backtest import _load_csv, _resample, _pick_base_bar, simulate  # noqa: E402


def sweep(rows: list[dict], balance: float, position_pct: float, days: int,
          leverage_map: dict[str, int], funding_bps: float, max_margin: float,
          slippage_grid: list[float]) -> list[dict]:
    """rows 里每条含 pair/signal_bar/float_pct/tp_pct/sl_pct,对每个滑点跑一次。"""
    out: list[dict] = []
    for src in rows:
        pair = src["pair"]
        sb = src["signal_bar"]
        fp, tp, sl = float(src["float_pct"]), float(src["tp_pct"]), float(src["sl_pct"])
        lev = leverage_map.get(pair, 100)
        base_bar = _pick_base_bar(sb)
        try:
            df_base = _load_csv(pair, base_bar, days)
        except FileNotFoundError:
            print(f"[skip] {pair} {sb}: base csv 缺")
            continue
        df_sig = _resample(df_base, sb)

        row_data = {
            "pair": pair, "signal_bar": sb,
            "float_pct": fp, "tp_pct": tp, "sl_pct": sl,
        }
        pass_50 = False
        for slip in slippage_grid:
            r = simulate(df_base, df_sig, pair, base_bar, sb, fp, tp, sl,
                         initial_balance=balance, position_pct=position_pct,
                         leverage=lev, fixed_margin=True,
                         slippage_bps=slip, funding_bps_per_8h=funding_bps,
                         max_margin=max_margin)
            row_data[f"slip{int(slip)}_return"] = round(r.total_return_pct, 2)
            row_data[f"slip{int(slip)}_mdd"] = round(r.max_dd_pct, 2)
            row_data[f"slip{int(slip)}_win"] = round(r.win_rate_pct, 2)
            if int(slip) == 50:
                pass_50 = r.total_return_pct > 0 and r.max_dd_pct <= 60
        row_data["pass_50bp"] = "YES" if pass_50 else "NO"
        out.append(row_data)
        print(f"{pair} {sb} f={fp} tp={tp} sl={sl}: " +
              " | ".join(f"{s}bp={row_data[f'slip{int(s)}_return']:+.0f}%/mdd{row_data[f'slip{int(s)}_mdd']:.0f}%"
                         for s in slippage_grid) +
              f" [pass50={row_data['pass_50bp']}]")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_csv", default=str(ROOT / "reports" / "walk_forward.csv"),
                    help="输入 CSV,取 pair/signal_bar/float_pct/tp_pct/sl_pct")
    ap.add_argument("--balance", type=float, default=140.0)
    ap.add_argument("--position-pct", type=float, default=0.10)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--funding-bps", type=float, default=3.0)
    ap.add_argument("--max-margin", type=float, default=1500.0)
    ap.add_argument("--slippages", default="0,10,30,50,100")
    ap.add_argument("--only-passed", action="store_true",
                    help="只对 walk_forward 里 passed=YES 的做敏感性")
    ap.add_argument("--out", default=str(ROOT / "reports" / "slippage_sweep.csv"))
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.in_csv, encoding="utf-8")))
    if args.only_passed:
        rows = [r for r in rows if r.get("passed") == "YES"]
    if not rows:
        print("[err] 输入为空")
        sys.exit(1)

    leverage_map = {
        "BTC-USDT-SWAP": 100, "ETH-USDT-SWAP": 100, "SOL-USDT-SWAP": 50,
    }
    slippage_grid = [float(x) for x in args.slippages.split(",")]

    results = sweep(rows, args.balance, args.position_pct, args.days,
                    leverage_map, args.funding_bps, args.max_margin, slippage_grid)

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
