"""
30 天滚动窗口测试：
  以 7 天为步长，每个窗口跑 30 天回测，看胜率/收益的分布。
  用于监控边际衰减。

  python scripts/rolling_window.py --pair BTC-USDT-SWAP --window 30 --step 7
"""
import argparse
import statistics
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest import DEFAULT_CONFIG, load_csv, simulate  # noqa: E402


def _load_config(path: Path | None) -> dict:
    """优先从 config.yaml 读；找不到就 fallback 到 backtest 的 DEFAULT_CONFIG。"""
    if path is None:
        path = ROOT / "config.yaml"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return DEFAULT_CONFIG


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="BTC-USDT-SWAP")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--window", type=int, default=30)
    ap.add_argument("--step", type=int, default=7)
    ap.add_argument("--config", default=None,
                    help="config.yaml 路径；默认项目根 config.yaml。传 'default' 强制走 backtest DEFAULT_CONFIG")
    ap.add_argument("--float-pct", type=float, default=None,
                    help="临时覆盖 float_pct（pair_overrides 里的也一并覆盖），用于 A/B 对比")
    ap.add_argument("--reentry-floats", default=None,
                    help='逗号分隔的浮动列表如 "0.002,0.005,0.008"，启用日内重挂')
    args = ap.parse_args()

    if args.csv:
        csv_path = Path(args.csv)
    else:
        token = args.pair.replace("-USDT-SWAP", "")
        csv_path = ROOT / "csv_data" / f"{token}_USDT_SWAP_1H_12m.csv"

    if not csv_path.exists():
        print(f"[err] CSV not found: {csv_path}")
        sys.exit(1)

    # 加载 config
    if args.config == "default":
        cfg = DEFAULT_CONFIG
    else:
        cfg_path = Path(args.config) if args.config else None
        cfg = _load_config(cfg_path)

    # 临时覆盖 float_pct（同时清掉 pair_overrides 里的 float_pct 以免冲突）
    if args.float_pct is not None:
        cfg = {**cfg, "strategy": {**cfg["strategy"], "float_pct": args.float_pct}}
        # 清理 pair_overrides.float_pct 避免覆盖回去
        ov = dict(cfg["strategy"].get("pair_overrides") or {})
        for p, po in ov.items():
            if "float_pct" in po:
                ov[p] = {k: v for k, v in po.items() if k != "float_pct"}
        cfg["strategy"]["pair_overrides"] = ov

    reentry = None
    if args.reentry_floats:
        reentry = [float(x) for x in args.reentry_floats.split(",")]

    df = load_csv(csv_path)
    dates = sorted(df["date"].unique())
    n = len(dates)

    if n < args.window + 2:
        print(f"[err] not enough days ({n})")
        sys.exit(1)

    # 显示实际用到的关键参数（对每 pair 唯一有效值）
    strat_cfg = cfg["strategy"]
    ov_for_pair = (strat_cfg.get("pair_overrides") or {}).get(args.pair, {})
    eff_float = ov_for_pair.get("float_pct", strat_cfg["float_pct"])
    eff_sl = ov_for_pair.get("sl_pct", strat_cfg["sl_pct"])
    eff_tp = ov_for_pair.get("tp_pct", strat_cfg["tp_pct"])
    print(f"[config] {args.pair}  float={eff_float*100:g}%  sl={eff_sl*100:g}%  tp={eff_tp*100:g}%"
          + (f"  reentry={reentry}" if reentry else ""))

    results: list[dict] = []
    for start in range(0, n - args.window, args.step):
        end = start + args.window
        sub_dates = set(dates[start:end + 1])
        sub_df = df[df["date"].isin(sub_dates)].reset_index(drop=True)
        if len(sub_df) < args.window * 20:
            continue
        res = simulate(sub_df, args.pair, cfg, reentry_floats=reentry)
        results.append({
            "from": str(dates[start]),
            "to": str(dates[end]),
            "return_pct": res["total_return_pct"],
            "win_rate_pct": res["win_rate_pct"],
            "trades": res["trades"],
            "max_dd_pct": res["max_dd_pct"],
        })

    if not results:
        print("no windows produced")
        return

    print(f"=== Rolling {args.window}d / step {args.step}d / {args.pair} ===")
    print(f"{'from':<12} {'to':<12} {'ret%':>8} {'win%':>7} {'n':>4} {'dd%':>7}")
    for r in results:
        print(f"{r['from']:<12} {r['to']:<12} {r['return_pct']:>+8.2f} "
              f"{r['win_rate_pct']:>7.2f} {r['trades']:>4d} {r['max_dd_pct']:>7.2f}")

    rets = [r["return_pct"] for r in results]
    wins = [r["win_rate_pct"] for r in results]
    pos = sum(1 for r in rets if r > 0)
    print()
    print(f"Windows total: {len(results)}")
    print(f"Positive    : {pos} ({pos / len(results) * 100:.1f}%)")
    print(f"Median ret  : {statistics.median(rets):+.2f}%")
    print(f"Worst ret   : {min(rets):+.2f}%")
    print(f"Best ret    : {max(rets):+.2f}%")
    print(f"Avg win rate: {statistics.mean(wins):.2f}%")


if __name__ == "__main__":
    main()
