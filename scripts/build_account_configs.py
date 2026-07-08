"""从 reports/grid_fine.csv 挑出每 signal_bar 下三币的最优参数,
生成"每账户三币组合"的 config.yaml 片段,同时输出中文命名的报告。

规则:
- MDD <= 30% 硬过滤
- 交易数 >= 200
- 每 (pair, signal_bar) 取 total_return 最优
- 三个 signal_bar 各是一套"三币组合",分配给不同账户
"""
import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


PAIR_ALIAS = {"BTC-USDT-SWAP": "BTC", "ETH-USDT-SWAP": "ETH", "SOL-USDT-SWAP": "SOL"}
BAR_ALIAS = {"4H": "4时", "6H": "6时", "12H": "12时", "1D": "日线"}


def _num(r, k):
    try:
        return float(r[k])
    except (ValueError, TypeError, KeyError):
        return 0.0


def load_best(csv_path: Path, max_dd: float, min_trades: int) -> dict[tuple[str, str], dict]:
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    sane = [r for r in rows if _num(r, "final") > 0
            and _num(r, "max_dd_pct") <= max_dd
            and int(float(r["trades"])) >= min_trades]
    best: dict[tuple[str, str], dict] = {}
    for r in sane:
        key = (r["pair"], r["signal_bar"])
        if key not in best or _num(r, "total_return_pct") > _num(best[key], "total_return_pct"):
            best[key] = r
    return best


def build_report(best: dict, out_md: Path, signals: list[str]) -> None:
    lines: list[str] = []
    lines.append("# 3 套账户级三币组合策略 (MDD ≤ 30% 精细回测)")
    lines.append("")
    lines.append("- 数据:2 年 730 天,BTC/ETH/SOL 各底粒度 5m/15m/30m")
    lines.append("- 初始 300 USDT,每笔 30 USDT 固定保证金,杠杆 100x,双向 taker 5bp × 2")
    lines.append("- 每套组合 = 一个信号周期 + 三币各自最优的 float/tp/sl")
    lines.append("")
    lines.append("| 组合 | 品种 | float | tp | sl | 收益率 | MDD | 胜率 | 笔数 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for sb in signals:
        for pair in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
            r = best.get((pair, sb))
            if not r:
                continue
            lines.append(
                f"| {BAR_ALIAS[sb]}三币 | {pair} | "
                f"{r['float_pct']} | {r['tp_pct']} | {r['sl_pct']} | "
                f"{_num(r, 'total_return_pct'):+.1f}% | "
                f"{_num(r, 'max_dd_pct'):.1f}% | "
                f"{_num(r, 'win_rate_pct'):.1f}% | {r['trades']} |"
            )
    lines.append("")

    lines.append("## 每套组合的 YAML 片段 (粘进 config.yaml 的 accounts[N].strategy)")
    lines.append("")
    for sb in signals:
        lines.append(f"### {BAR_ALIAS[sb]}三币组合 (signal_bar={sb})")
        lines.append("")
        lines.append("```yaml")
        lines.append(f"strategy:")
        lines.append(f"  signal_bar: {sb}")
        lines.append(f"  pair_overrides:")
        for pair in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
            r = best.get((pair, sb))
            if not r:
                continue
            lines.append(f"    {pair}:")
            lines.append(f"      float_pct: {r['float_pct']}")
            lines.append(f"      tp_pct: {r['tp_pct']}")
            lines.append(f"      sl_pct: {r['sl_pct']}")
        lines.append("```")
        lines.append("")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines), encoding="utf-8")


def build_configs(best: dict, signals: list[str]) -> dict[str, dict]:
    """返回 {signal_bar: strategy_config_dict}"""
    configs: dict[str, dict] = {}
    for sb in signals:
        strat = {"signal_bar": sb, "pair_overrides": {}}
        for pair in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
            r = best.get((pair, sb))
            if not r:
                continue
            strat["pair_overrides"][pair] = {
                "float_pct": float(r["float_pct"]),
                "tp_pct": float(r["tp_pct"]),
                "sl_pct": float(r["sl_pct"]),
            }
        configs[sb] = strat
    return configs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_csv", default=str(ROOT / "reports" / "grid_fine.csv"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "account_combos.md"))
    ap.add_argument("--max-dd", type=float, default=30.0)
    ap.add_argument("--min-trades", type=int, default=200)
    ap.add_argument("--signals", default="4H,6H,12H")
    args = ap.parse_args()

    signals = [s.strip() for s in args.signals.split(",") if s.strip()]
    best = load_best(Path(args.in_csv), args.max_dd, args.min_trades)
    build_report(best, Path(args.out), signals)

    configs = build_configs(best, signals)
    import yaml
    print("=" * 60)
    print("生成的 3 套 strategy 覆盖 (可直接粘进 accounts[].strategy):")
    print("=" * 60)
    for sb, c in configs.items():
        print(f"\n--- {sb} 三币组合 ---")
        print(yaml.dump(c, allow_unicode=True, sort_keys=False))
    print(f"\n报告 → {args.out}")


if __name__ == "__main__":
    main()
