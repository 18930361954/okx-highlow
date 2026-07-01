"""联合回测参数扫描：BTC + ETH 共享账户 + 滑点 + 手续费。
支持交替扫描：固定一个 pair 的参数扫另一个 pair。

  # 1) 固定 ETH 当前，扫 BTC
  python scripts/param_search_joint.py --scan BTC --top 10

  # 2) 用第 1 步 top1 固定 BTC，扫 ETH
  python scripts/param_search_joint.py --scan ETH --top 10 \
    --btc-pos 0.15 --btc-tp 0.015 --btc-sl 0.005

评分（含成本联合）：
  - "high-return" (默认)：总收益 70% + 最大回撤 30%（反向：DD 越小越好）
  - "balanced"          ：总收益 40% + DD 30% + PF 30%
"""
import argparse
import sys
from copy import deepcopy
from itertools import product
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.multi_pair_backtest import simulate_multi, _load_csv  # noqa: E402


def _build_cfg(base_cfg: dict, pair_params: dict) -> dict:
    """pair_params = {pair: {position_pct, tp_pct, sl_pct, ...}}"""
    cfg = deepcopy(base_cfg)
    ov = cfg["strategy"].setdefault("pair_overrides", {})
    for pair, params in pair_params.items():
        p_ov = ov.setdefault(pair, {})
        p_ov.update(params)
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", choices=["BTC", "ETH"], required=True,
                    help="扫描哪个 pair 的参数（另一个用 --btc-* / --eth-* 固定）")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--balance", type=float, default=75.0)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--single-loss-max", type=float, default=0.20,
                    help="单笔最大亏损占余额比例")
    ap.add_argument("--mode", default="high-return",
                    choices=["high-return", "balanced"])

    # 固定另一 pair 参数（不扫的那个）
    ap.add_argument("--btc-pos", type=float, default=0.20)
    ap.add_argument("--btc-tp", type=float, default=0.012)
    ap.add_argument("--btc-sl", type=float, default=0.007)
    ap.add_argument("--eth-pos", type=float, default=0.15)
    ap.add_argument("--eth-tp", type=float, default=0.020)
    ap.add_argument("--eth-sl", type=float, default=0.007)

    # 扫描网格
    ap.add_argument("--pos-grid", default="0.10,0.15,0.20,0.25",
                    help="要扫的 pair 的 position_pct")
    ap.add_argument("--tp-grid", default=None,
                    help="要扫的 pair 的 tp_pct；默认按 pair 自适应")
    ap.add_argument("--sl-grid", default=None,
                    help="要扫的 pair 的 sl_pct；默认按 pair 自适应")

    args = ap.parse_args()

    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    leverage = int(base_cfg["strategy"]["leverage"])

    # 加载 CSV
    dfs = {}
    for pair in ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]:
        token = pair.replace("-USDT-SWAP", "")
        csv_path = ROOT / "csv_data" / f"{token}_USDT_SWAP_1H_12m.csv"
        dfs[pair] = _load_csv(csv_path)

    # 扫描目标
    scan_pair = "BTC-USDT-SWAP" if args.scan == "BTC" else "ETH-USDT-SWAP"

    pos_grid = [float(x) for x in args.pos_grid.split(",")]
    if args.tp_grid:
        tp_grid = [float(x) for x in args.tp_grid.split(",")]
    elif args.scan == "BTC":
        tp_grid = [0.012, 0.015, 0.020, 0.025]
    else:
        tp_grid = [0.020, 0.025, 0.030, 0.035]
    if args.sl_grid:
        sl_grid = [float(x) for x in args.sl_grid.split(",")]
    elif args.scan == "BTC":
        sl_grid = [0.004, 0.005, 0.007]
    else:
        sl_grid = [0.005, 0.007, 0.010]

    combos = list(product(pos_grid, tp_grid, sl_grid))
    # 单笔亏损上限约束 + 盈亏比 >= 2:1
    kept = [
        (p, t, s) for p, t, s in combos
        if p * leverage * s <= args.single_loss_max + 1e-9
        and t / s >= 1.5  # 至少 1.5:1，含成本必须
    ]

    print(f"[scan] 扫 {scan_pair}  组合={len(combos)} 保留={len(kept)}")
    print(f"[fixed] 另一 pair: "
          f"BTC pos={args.btc_pos} tp={args.btc_tp} sl={args.btc_sl}  "
          f"ETH pos={args.eth_pos} tp={args.eth_tp} sl={args.eth_sl}")

    # 另一 pair 的固定参数（不扫的那个）
    fixed_pair = "ETH-USDT-SWAP" if args.scan == "BTC" else "BTC-USDT-SWAP"
    if fixed_pair == "ETH-USDT-SWAP":
        fixed_params = {
            "position_pct": args.eth_pos, "tp_pct": args.eth_tp, "sl_pct": args.eth_sl,
        }
    else:
        fixed_params = {
            "position_pct": args.btc_pos, "tp_pct": args.btc_tp, "sl_pct": args.btc_sl,
        }

    rows = []
    for pos, tp, sl in kept:
        scan_params = {"position_pct": pos, "tp_pct": tp, "sl_pct": sl}
        cfg = _build_cfg(base_cfg, {scan_pair: scan_params, fixed_pair: fixed_params})

        res = simulate_multi(dfs, cfg, initial_balance=args.balance, days=args.days)
        rows.append({
            "pos": pos, "tp": tp, "sl": sl,
            "final": res["final"],
            "total_ret": res["total_return_pct"],
            "dd": res["max_dd_pct"],
            "trades": res["trades"],
            "win_rate": res["win_rate_pct"],
            "pf": res["profit_factor"],
            "concurrent": res["concurrent_trade_days"],
            "double_sl": res["concurrent_sl_days"],
            "scan_pair_pnl": res["by_pair"].get(scan_pair, {}).get("pnl", 0),
            "fixed_pair_pnl": res["by_pair"].get(fixed_pair, {}).get("pnl", 0),
        })

    if not rows:
        print("no results")
        return

    # 归一化 + 综合评分
    def _norm(vals, reverse=False):
        lo, hi = min(vals), max(vals)
        if hi == lo:
            return [0.5] * len(vals)
        if reverse:
            return [(hi - v) / (hi - lo) for v in vals]
        return [(v - lo) / (hi - lo) for v in vals]

    tot = _norm([r["total_ret"] for r in rows])
    dd = _norm([r["dd"] for r in rows], reverse=True)
    pf = _norm([r["pf"] for r in rows])

    if args.mode == "high-return":
        weights = (0.70, 0.30, 0.00)
        label = "评分：总收益 70% + DD 30%"
    else:
        weights = (0.40, 0.30, 0.30)
        label = "评分：总收益 40% + DD 30% + PF 30%"

    for i, r in enumerate(rows):
        r["score"] = weights[0] * tot[i] + weights[1] * dd[i] + weights[2] * pf[i]
    rows.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n=== Top {args.top} {scan_pair}（{label}）===")
    print(f"{'pos%':>5} {'tp%':>5} {'sl%':>5} {'ratio':>5} {'score':>6} "
          f"{'total%':>9} {'final':>9} {'DD%':>7} {'n':>4} {'win%':>6} {'PF':>5} "
          f"{'scan_pnl':>9} {'other_pnl':>10} {'2sl':>4}")
    for r in rows[:args.top]:
        ratio = r["tp"] / r["sl"]
        print(f"{r['pos']*100:>5.0f} {r['tp']*100:>5.2f} {r['sl']*100:>5.2f} "
              f"{ratio:>5.2f} {r['score']:>6.3f} "
              f"{r['total_ret']:>+9.2f} {r['final']:>9.2f} {r['dd']:>7.2f} "
              f"{r['trades']:>4d} {r['win_rate']:>6.2f} {r['pf']:>5.2f} "
              f"{r['scan_pair_pnl']:>+9.2f} {r['fixed_pair_pnl']:>+10.2f} "
              f"{r['double_sl']:>4d}")


if __name__ == "__main__":
    main()
