"""多周期回测:遍历 pair × K 线粒度,用同一套 HighLow 策略 (前日阴阳 → 次日浮动挂单)
但把「日内粒度」从 1H 换成 5m/15m/30m/1H/2H/4H/6H/12H,评估哪个粒度回测表现最好。

不影响运行时:策略层完全不改;这是一个纯离线脚本。

用法:
  python scripts/multi_tf_backtest.py \\
      --pairs BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \\
      --bars 5m,15m,30m,1H,2H,4H,6H,12H \\
      --days 730 --balance 300 \\
      [--out reports/multi_tf.csv]

评估口径:
  - 沿用 backtest.simulate 的 SL 优先保守口径
  - reentry_floats 沿用 config.yaml 里 pair_overrides 的配置(有则用,无则单次入场)
  - 每 pair×bar 输出: total_return / monthly / trades / win_rate / max_dd / profit_factor
"""
import argparse
import csv
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest import load_csv, simulate  # noqa: E402


BAR_ORDER = ["1D", "12H", "6H", "4H", "2H", "1H", "30m", "15m", "5m"]


def _build_config(top_cfg: dict, pair: str) -> dict:
    """把顶层 config 塑成 backtest.simulate 需要的形状。保留 pair_overrides。"""
    strat = dict(top_cfg["strategy"])
    strat["pairs"] = [pair]
    return {"strategy": strat}


def _csv_for(pair: str, bar: str, days: int, csv_dir: Path) -> Path | None:
    coin = pair.split("-")[0]
    p = csv_dir / f"{coin}_USDT_SWAP_{bar}_{days}d.csv"
    return p if p.exists() else None


def run_matrix(pairs: list[str], bars: list[str], days: int, balance: float,
                csv_dir: Path, top_cfg: dict) -> list[dict]:
    """跑 pairs × bars 矩阵,返回结果 list。"""
    results: list[dict] = []
    for pair in pairs:
        cfg = _build_config(top_cfg, pair)
        # 尊重 pair_overrides.reentry_floats
        ov = (cfg["strategy"].get("pair_overrides") or {}).get(pair, {}) or {}
        reentry = ov.get("reentry_floats") or None

        for bar in bars:
            csv_path = _csv_for(pair, bar, days, csv_dir)
            if csv_path is None:
                print(f"[skip] {pair} {bar} {days}d: CSV 不存在")
                continue
            if bar == "1D":
                # 日频信号 + 日内粒度需要 >= 2 根/日的粒度才能做入场判断
                print(f"[skip] {pair} 1D: 日频信号需要日内 K,1D 粒度不能作为「日内粒度」")
                continue

            df = load_csv(csv_path)
            try:
                res = simulate(df, pair, cfg, initial_balance=balance, days=None,
                               reentry_floats=reentry)
            except Exception as e:
                print(f"[fail] {pair} {bar}: {e}")
                continue

            row = {
                "pair": pair,
                "bar": bar,
                "n_bars": len(df),
                "reentry": ",".join(str(x) for x in reentry) if reentry else "-",
                "final": round(res["final"], 2),
                "total_return_pct": round(res["total_return_pct"], 2),
                "monthly_pct": round(res["monthly_pct"], 2),
                "trades": res["trades"],
                "win_rate_pct": round(res["win_rate_pct"], 2),
                "max_dd_pct": round(res["max_dd_pct"], 2),
                "profit_factor": (
                    round(res["profit_factor"], 3)
                    if res["profit_factor"] != float("inf") else "inf"
                ),
            }
            results.append(row)
            print(
                f"[{pair}/{bar}] return={row['total_return_pct']:+.2f}% "
                f"trades={row['trades']} win={row['win_rate_pct']:.1f}% "
                f"mdd={row['max_dd_pct']:.1f}% pf={row['profit_factor']}"
            )
    return results


def _print_ranked(results: list[dict]) -> None:
    if not results:
        return
    print("\n=== ranked by total_return_pct ===")
    ranked = sorted(results, key=lambda r: r["total_return_pct"], reverse=True)
    print(f"{'pair':<18} {'bar':<5} {'return':>10} {'trades':>7} "
          f"{'win%':>6} {'mdd%':>6} {'pf':>6}")
    for r in ranked:
        print(f"{r['pair']:<18} {r['bar']:<5} {r['total_return_pct']:>9.2f}% "
              f"{r['trades']:>7} {r['win_rate_pct']:>6.1f} {r['max_dd_pct']:>6.1f} "
              f"{str(r['profit_factor']):>6}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP")
    ap.add_argument("--bars", default="12H,6H,4H,2H,1H,30m,15m,5m")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--balance", type=float, default=300.0)
    ap.add_argument("--csv-dir", default=str(ROOT / "csv_data"))
    ap.add_argument("--out", default=str(ROOT / "reports" / "multi_tf.csv"))
    args = ap.parse_args()

    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        top_cfg = yaml.safe_load(f)

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    bars = [b.strip() for b in args.bars.split(",") if b.strip()]

    results = run_matrix(pairs, bars, args.days, args.balance,
                         Path(args.csv_dir), top_cfg)

    _print_ranked(results)

    if results and args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            for r in results:
                w.writerow(r)
        print(f"\nsaved → {out_path}")


if __name__ == "__main__":
    main()
