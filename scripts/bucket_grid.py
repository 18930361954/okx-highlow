"""参数网格搜索:找出各周期下的高收益策略。

维度:
  pair          BTC / ETH / SOL
  signal_bar    5m / 15m / 30m / 1H / 2H / 4H / 6H / 12H(1D 不做,策略语义就是 1D)
  float_pct     0.0010 / 0.0015 / 0.0020 / 0.0030 / 0.0050
  tp_pct        0.008  / 0.012 / 0.020 / 0.030
  sl_pct        0.005  / 0.008 / 0.010 / 0.015

= 3 × 8 × 5 × 4 × 4 = 1920 组

底粒度 K 按 signal 自动选(见 bucket_backtest._pick_base_bar)。
每个 pair 预加载 base df 与 resample 后的 sig df,避免重复 IO。
"""
from __future__ import annotations

import argparse
import csv
import itertools
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bucket_backtest import (  # noqa: E402
    BASE_BAR_SECS, _load_csv, _pick_base_bar, _resample, simulate,
)


PAIRS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
SIGNAL_BARS = ["5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H"]
FLOAT_GRID = [0.0010, 0.0015, 0.0020, 0.0030, 0.0050]
TP_GRID = [0.008, 0.012, 0.020, 0.030]
SL_GRID = [0.005, 0.008, 0.010, 0.015]


def run_grid(pairs: list[str], signal_bars: list[str],
             floats: list[float], tps: list[float], sls: list[float],
             balance: float, days: int, position_pct: float,
             leverage: int) -> list[dict]:
    results: list[dict] = []
    total_cases = len(pairs) * len(signal_bars) * len(floats) * len(tps) * len(sls)
    print(f"[grid] total cases: {total_cases}")
    t_start = time.time()
    done = 0

    for pair in pairs:
        # 每个 pair × signal 预加载并 resample 一次
        for signal_bar in signal_bars:
            base_bar = _pick_base_bar(signal_bar)
            try:
                df_base = _load_csv(pair, base_bar, days)
            except FileNotFoundError as e:
                print(f"[skip] {pair} {signal_bar}: base csv 缺 {e}")
                # 跳过该 signal 的所有参数组
                done += len(floats) * len(tps) * len(sls)
                continue
            df_sig = _resample(df_base, signal_bar)

            for fp, tp, sl in itertools.product(floats, tps, sls):
                try:
                    r = simulate(df_base, df_sig, pair, base_bar, signal_bar,
                                 fp, tp, sl, initial_balance=balance,
                                 position_pct=position_pct, leverage=leverage)
                except Exception as e:
                    print(f"[fail] {pair} {signal_bar} f={fp} tp={tp} sl={sl}: {e}")
                    done += 1
                    continue
                results.append(r.as_row())
                done += 1

            elapsed = time.time() - t_start
            eta = elapsed / done * (total_cases - done) if done else 0
            print(f"  [{done}/{total_cases}] {pair} {signal_bar} done "
                  f"(elapsed={elapsed:.0f}s eta={eta:.0f}s)")
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default=",".join(PAIRS))
    ap.add_argument("--signals", default=",".join(SIGNAL_BARS))
    ap.add_argument("--floats", default=",".join(str(x) for x in FLOAT_GRID))
    ap.add_argument("--tps", default=",".join(str(x) for x in TP_GRID))
    ap.add_argument("--sls", default=",".join(str(x) for x in SL_GRID))
    ap.add_argument("--balance", type=float, default=300.0)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--position-pct", type=float, default=0.10)
    ap.add_argument("--leverage", type=int, default=100)
    ap.add_argument("--out", default=str(ROOT / "reports" / "grid_results.csv"))
    args = ap.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    signals = [s.strip() for s in args.signals.split(",") if s.strip()]
    floats = [float(x) for x in args.floats.split(",") if x.strip()]
    tps = [float(x) for x in args.tps.split(",") if x.strip()]
    sls = [float(x) for x in args.sls.split(",") if x.strip()]

    results = run_grid(pairs, signals, floats, tps, sls,
                       args.balance, args.days, args.position_pct, args.leverage)

    if results:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            for r in results:
                w.writerow(r)
        print(f"\nsaved {len(results)} rows → {out}")


if __name__ == "__main__":
    main()
