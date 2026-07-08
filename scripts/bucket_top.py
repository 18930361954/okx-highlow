"""从 bucket_grid 输出里选拔 Top 策略,生成中文报告。

选拔规则:
  硬过滤: final > 0 (未爆仓), max_dd_pct <= 60 (方案里 60% 是暂停线), trades >= 20
  排序:  total_return_pct 降序
  分组:  每 (pair, signal_bar) 取头 1 个,再全局 Top N

中文命名:每个策略给个可读别名,方便后续做「策略选择」。
  格式: {pair 别名} · {周期} 猎手 v{seq}
  eg  BTC · 4H 猎手 v1
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


PAIR_ALIAS = {
    "BTC-USDT-SWAP": "BTC",
    "ETH-USDT-SWAP": "ETH",
    "SOL-USDT-SWAP": "SOL",
}

BAR_ALIAS = {
    "5m": "5分",  "15m": "15分", "30m": "30分",
    "1H": "1时", "2H": "2时", "4H": "4时",
    "6H": "6时", "12H": "12时",
}


def _pct(x) -> float:
    try:
        return float(x)
    except (ValueError, TypeError):
        return 0.0


def _int(x) -> int:
    try:
        return int(float(x))
    except (ValueError, TypeError):
        return 0


def _load(csv_path: Path) -> list[dict]:
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows


def _filter(rows: list[dict], min_trades: int, max_dd: float) -> list[dict]:
    out = []
    for r in rows:
        if _pct(r.get("final")) <= 0:  # 爆仓
            continue
        if _int(r.get("trades")) < min_trades:
            continue
        if _pct(r.get("max_dd_pct")) > max_dd:
            continue
        out.append(r)
    return out


def _pair_signal_top(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """每 (pair, signal) 取该组内 total_return 最高的一条。"""
    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["pair"], r["signal_bar"])
        cur = best.get(key)
        if cur is None or _pct(r["total_return_pct"]) > _pct(cur["total_return_pct"]):
            best[key] = r
    return best


def _name_strategy(pair: str, signal_bar: str, seq: int) -> str:
    return f"{PAIR_ALIAS.get(pair, pair)}·{BAR_ALIAS.get(signal_bar, signal_bar)}猎手 v{seq}"


def build_report(rows: list[dict], top_n: int, min_trades: int, max_dd: float,
                 out_md: Path) -> None:
    kept = _filter(rows, min_trades=min_trades, max_dd=max_dd)
    kept.sort(key=lambda r: _pct(r["total_return_pct"]), reverse=True)

    # 每 (pair, signal) 头一名
    per_group = list(_pair_signal_top(kept).values())
    per_group.sort(key=lambda r: _pct(r["total_return_pct"]), reverse=True)

    # 全局 Top N
    global_top = kept[:top_n]

    lines: list[str] = []
    lines.append("# 高收益单策略选拔 (基于 2 年 730 天回测)")
    lines.append("")
    lines.append(f"- 数据:BTC/ETH/SOL 各 2 年 K 线 (来源 OKX live)")
    lines.append(f"- 初始余额: 300 USDT · 每笔固定保证金 30 USDT · 杠杆 100x")
    lines.append(f"- 手续费:双向 taker 0.05% × 2 = 每笔 0.10% notional 扣除")
    lines.append(f"- 桶回测:前一时间桶阴阳 → 下一桶浮动挂单 → 桶内逐 K 判 TP/SL(SL 优先保守)")
    lines.append(f"- 硬过滤: 未爆仓 · 交易 ≥ {min_trades} 笔 · MDD ≤ {max_dd:.0f}%")
    lines.append("")

    # 每 pair × signal 最优
    lines.append("## 每 (品种,周期) 组合的最佳策略")
    lines.append("")
    lines.append("| 策略名 | 品种 | 周期 | float | tp | sl | 收益率 | 月化 | 笔数 | 胜率 | MDD | PF |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(per_group, 1):
        name = _name_strategy(r["pair"], r["signal_bar"], i)
        pf = r.get("profit_factor", "")
        try:
            pf_str = f"{float(pf):.2f}"
        except (ValueError, TypeError):
            pf_str = str(pf)
        lines.append(
            f"| **{name}** | {r['pair']} | {r['signal_bar']} | "
            f"{r['float_pct']} | {r['tp_pct']} | {r['sl_pct']} | "
            f"{_pct(r['total_return_pct']):+.2f}% | "
            f"{_pct(r['monthly_pct']):+.2f}% | "
            f"{r['trades']} | {_pct(r['win_rate_pct']):.1f}% | "
            f"{_pct(r['max_dd_pct']):.1f}% | {pf_str} |"
        )
    lines.append("")

    # 全局 Top N
    lines.append(f"## 全局 Top {top_n}(按总收益率排序)")
    lines.append("")
    lines.append("| 名次 | 策略名 | 品种 | 周期 | float | tp | sl | 收益率 | MDD | 胜率 | 笔数 | 月化 | PF |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for rank, r in enumerate(global_top, 1):
        name = _name_strategy(r["pair"], r["signal_bar"], rank)
        pf = r.get("profit_factor", "")
        try:
            pf_str = f"{float(pf):.2f}"
        except (ValueError, TypeError):
            pf_str = str(pf)
        lines.append(
            f"| {rank} | **{name}** | {r['pair']} | {r['signal_bar']} | "
            f"{r['float_pct']} | {r['tp_pct']} | {r['sl_pct']} | "
            f"{_pct(r['total_return_pct']):+.2f}% | "
            f"{_pct(r['max_dd_pct']):.1f}% | "
            f"{_pct(r['win_rate_pct']):.1f}% | "
            f"{r['trades']} | {_pct(r['monthly_pct']):+.2f}% | {pf_str} |"
        )
    lines.append("")

    # 推荐上线组合(3-5 个策略,尽量分散 pair)
    lines.append("## 推荐上线组合(3-5 个,尽量分散品种/周期)")
    lines.append("")
    picked: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    # 优先每个 pair 各选一个最高的
    for pair in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
        for r in kept:
            if r["pair"] != pair:
                continue
            key = (r["pair"], r["signal_bar"])
            if key in seen_keys:
                continue
            picked.append(r)
            seen_keys.add(key)
            break
    # 再补 2 个全局最高,避免与已选重复
    for r in kept:
        if len(picked) >= 5:
            break
        key = (r["pair"], r["signal_bar"])
        if key in seen_keys:
            continue
        picked.append(r)
        seen_keys.add(key)

    for i, r in enumerate(picked, 1):
        name = _name_strategy(r["pair"], r["signal_bar"], i)
        pf = r.get("profit_factor", "")
        try:
            pf_str = f"{float(pf):.2f}"
        except (ValueError, TypeError):
            pf_str = str(pf)
        lines.append(f"### {i}. {name}")
        lines.append("")
        lines.append(f"- 品种/周期: {r['pair']} / {r['signal_bar']}")
        lines.append(f"- 参数: `float_pct={r['float_pct']}`, `tp_pct={r['tp_pct']}`, `sl_pct={r['sl_pct']}`")
        lines.append(f"- 表现: 总收益 **{_pct(r['total_return_pct']):+.2f}%**, "
                     f"月化 {_pct(r['monthly_pct']):+.2f}%, "
                     f"MDD {_pct(r['max_dd_pct']):.1f}%, "
                     f"胜率 {_pct(r['win_rate_pct']):.1f}% ({r['trades']} 笔), "
                     f"PF {pf_str}")
        lines.append("")
        lines.append(f"配置片段(可粘进 config.yaml 的 pair_overrides 或 strategy 覆盖):")
        lines.append("```yaml")
        lines.append(f"# {name}")
        lines.append(f"pairs: [{r['pair']}]")
        lines.append(f"strategy:")
        lines.append(f"  signal_bar: {r['signal_bar']}   # 需要新增运行时支持,目前策略仍是 1D")
        lines.append(f"  pair_overrides:")
        lines.append(f"    {r['pair']}:")
        lines.append(f"      float_pct: {r['float_pct']}")
        lines.append(f"      tp_pct: {r['tp_pct']}")
        lines.append(f"      sl_pct: {r['sl_pct']}")
        lines.append("```")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("> **重要提示**: 上表回测口径为「桶内 5m/15m K 逐根 SL 优先」,"
                 "已比方案 1.3 节乐观口径保守。实盘上线前建议:")
    lines.append("> 1. 用小额 demo 账户先跑 1-2 周确认信号一致性")
    lines.append("> 2. 若要在运行时启用非 1D 信号周期,需要扩展 strategy.compute_signal 支持任意 bar,当前只支持 1D")
    lines.append("> 3. 高杠杆(100x)在小周期上极易遇到爆仓,MDD > 40% 建议降杠杆/降仓")

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_csv", default=str(ROOT / "reports" / "grid_results.csv"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "backtest_top_strategies.md"))
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--max-dd", type=float, default=60.0)
    args = ap.parse_args()

    rows = _load(Path(args.in_csv))
    if not rows:
        print(f"[err] {args.in_csv} 无数据")
        sys.exit(1)
    build_report(rows, top_n=args.top, min_trades=args.min_trades,
                 max_dd=args.max_dd, out_md=Path(args.out))
    print(f"报告 → {args.out}")


if __name__ == "__main__":
    main()
