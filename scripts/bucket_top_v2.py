"""从 bucket_grid v2 输出(含年度分解、滑点/funding/复利)里选拔 Top 策略。

规则:
  硬过滤: MDD ≤ max_dd, trades ≥ min_trades, final > initial (至少赚回本金)
  排序:   total_return_pct 降序
  输出:   每 (pair, signal_bar) 最优 + 全局 Top N + 年度余额曲线
"""
import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


PAIR_ALIAS = {"BTC-USDT-SWAP": "BTC", "ETH-USDT-SWAP": "ETH", "SOL-USDT-SWAP": "SOL"}
BAR_ALIAS = {"4H": "4时", "6H": "6时", "12H": "12时", "1D": "日线", "2H": "2时", "1H": "1时"}


def _num(r, k, default=0.0):
    try:
        return float(r.get(k) or default)
    except (ValueError, TypeError):
        return default


def _load(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        if not p.exists():
            print(f"[warn] {p} 不存在,跳过")
            continue
        with open(p, encoding="utf-8") as f:
            rows.extend(csv.DictReader(f))
    return rows


def _year_columns(rows: list[dict]) -> list[str]:
    """从 rows 里发现所有 yYYYY_end 列。"""
    cols = set()
    for r in rows:
        for k in r.keys():
            if k.startswith("y") and k.endswith("_end"):
                cols.add(k)
    return sorted(cols)


def _sane_filter(rows: list[dict], min_trades: int, max_dd: float,
                 initial: float) -> list[dict]:
    out = []
    for r in rows:
        if _num(r, "final") <= initial:
            continue  # 亏本或不赚
        if int(float(r.get("trades", 0) or 0)) < min_trades:
            continue
        if _num(r, "max_dd_pct") > max_dd:
            continue
        out.append(r)
    return out


def build_report(rows: list[dict], out_md: Path, top_n: int,
                 min_trades: int, max_dd: float, initial: float,
                 mode_desc: str) -> None:
    kept = _sane_filter(rows, min_trades, max_dd, initial)
    kept.sort(key=lambda r: _num(r, "total_return_pct"), reverse=True)

    year_cols = _year_columns(rows)

    # 每 (pair, signal_bar) 头一名
    best_by_group: dict[tuple[str, str], dict] = {}
    for r in kept:
        k = (r["pair"], r["signal_bar"])
        if k not in best_by_group or _num(r, "total_return_pct") > _num(best_by_group[k], "total_return_pct"):
            best_by_group[k] = r
    per_group = sorted(best_by_group.values(),
                       key=lambda r: _num(r, "total_return_pct"), reverse=True)

    global_top = kept[:top_n]

    lines: list[str] = []
    lines.append("# 高收益单策略选拔 (悲观口径回测)")
    lines.append("")
    lines.append(f"- {mode_desc}")
    lines.append(f"- 初始 {initial:.0f} USDT · 每笔仓位比例 10% (三币满仓 30%)")
    lines.append(f"- 手续费 taker 5bp × 2/笔 · 滑点 10bp/笔 · funding 3bp/8h")
    lines.append(f"- 桶回测:前桶阴阳 → 下桶浮动挂单 → 桶内逐 K 判 TP/SL,SL 优先")
    lines.append(f"- 硬过滤:未亏本 · trades ≥ {min_trades} · MDD ≤ {max_dd:.0f}%")
    lines.append("")

    lines.append("## 每 (品种,周期) 最佳")
    lines.append("")
    header_extra = " | ".join(y.replace("_end", "").replace("y", "") + "末" for y in year_cols)
    lines.append(f"| 品种 | 周期 | float | tp | sl | 收益率 | MDD | 胜率 | 笔数 | {header_extra} |")
    lines.append("|" + "---|" * (9 + len(year_cols)))
    for r in per_group:
        pf = r.get("profit_factor", "")
        try:
            pf_str = f"{float(pf):.2f}"
        except (ValueError, TypeError):
            pf_str = str(pf)
        yr_cells = " | ".join(f"{_num(r, y):.0f}" for y in year_cols)
        lines.append(
            f"| {PAIR_ALIAS.get(r['pair'], r['pair'])} | {r['signal_bar']} | "
            f"{r['float_pct']} | {r['tp_pct']} | {r['sl_pct']} | "
            f"{_num(r, 'total_return_pct'):+.1f}% | "
            f"{_num(r, 'max_dd_pct'):.1f}% | {_num(r, 'win_rate_pct'):.1f}% | "
            f"{r['trades']} | {yr_cells} |"
        )
    lines.append("")

    lines.append(f"## 全局 Top {top_n}")
    lines.append("")
    lines.append(f"| # | 品种 | 周期 | float | tp | sl | 收益率 | MDD | 胜率 | 笔数 | 月化 | {header_extra} |")
    lines.append("|" + "---|" * (11 + len(year_cols)))
    for i, r in enumerate(global_top, 1):
        yr_cells = " | ".join(f"{_num(r, y):.0f}" for y in year_cols)
        lines.append(
            f"| {i} | {PAIR_ALIAS.get(r['pair'], r['pair'])} | {r['signal_bar']} | "
            f"{r['float_pct']} | {r['tp_pct']} | {r['sl_pct']} | "
            f"{_num(r, 'total_return_pct'):+.1f}% | "
            f"{_num(r, 'max_dd_pct'):.1f}% | {_num(r, 'win_rate_pct'):.1f}% | "
            f"{r['trades']} | {_num(r, 'monthly_pct'):+.1f}% | {yr_cells} |"
        )
    lines.append("")

    # 推荐组合:每 signal_bar 下三币最优
    lines.append("## 推荐上线组合 (每 signal_bar 三币最优)")
    lines.append("")
    for sb in ("4H", "6H", "12H", "1D"):
        picks = [best_by_group.get((p, sb)) for p in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")]
        picks = [p for p in picks if p]
        if not picks:
            continue
        avg_ret = sum(_num(p, "total_return_pct") for p in picks) / len(picks)
        max_mdd = max(_num(p, "max_dd_pct") for p in picks)
        lines.append(f"### {BAR_ALIAS.get(sb, sb)}三币组合")
        lines.append("")
        lines.append(f"- 三币平均收益 ≈ **{avg_ret:+.1f}%** · 三币最大 MDD = {max_mdd:.1f}%")
        lines.append("")
        lines.append(f"| Pair | float | tp | sl | 收益率 | MDD | 胜率 | {header_extra} |")
        lines.append("|" + "---|" * (7 + len(year_cols)))
        for r in picks:
            yr_cells = " | ".join(f"{_num(r, y):.0f}" for y in year_cols)
            lines.append(
                f"| {r['pair']} | {r['float_pct']} | {r['tp_pct']} | {r['sl_pct']} | "
                f"{_num(r, 'total_return_pct'):+.1f}% | "
                f"{_num(r, 'max_dd_pct'):.1f}% | "
                f"{_num(r, 'win_rate_pct'):.1f}% | {yr_cells} |"
            )
        lines.append("")
        lines.append("YAML 覆盖:")
        lines.append("```yaml")
        lines.append(f"strategy:")
        lines.append(f"  signal_bar: {sb}")
        lines.append(f"  pair_overrides:")
        for r in picks:
            leverage_line = ", leverage: 50" if r["pair"] == "SOL-USDT-SWAP" else ""
            lines.append(
                f"    {r['pair']}: {{ float_pct: {r['float_pct']}, "
                f"tp_pct: {r['tp_pct']}, sl_pct: {r['sl_pct']}{leverage_line} }}"
            )
        lines.append("```")
        lines.append("")

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_csvs", nargs="+",
                    default=[str(ROOT / "reports" / "grid_btc_eth_v2.csv"),
                             str(ROOT / "reports" / "grid_sol_v2.csv")])
    ap.add_argument("--out", default=str(ROOT / "docs" / "backtest_v2.md"))
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-trades", type=int, default=100)
    ap.add_argument("--max-dd", type=float, default=30.0)
    ap.add_argument("--initial", type=float, default=140.0)
    ap.add_argument("--mode-desc", default="悲观口径: 复利模式 + 手续费/滑点/funding 全部扣除")
    args = ap.parse_args()

    rows = _load([Path(p) for p in args.in_csvs])
    if not rows:
        print("[err] 无数据")
        sys.exit(1)
    build_report(rows, Path(args.out), args.top,
                 args.min_trades, args.max_dd, args.initial, args.mode_desc)
    print(f"报告 → {args.out}")


if __name__ == "__main__":
    main()
