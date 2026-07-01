"""多 pair 联合回测：BTC + ETH 共享同一份账户余额。
真实场景：两个 pair 同一天都有信号时，各自按 pos_pct 从共享余额取保证金；
熔断计数跨 pair 共享（任一 pair 亏 → 账户级 consec_losses+1）。

  python scripts/multi_pair_backtest.py --days 180

支持每 pair 的重挂序列（读 config.yaml 的 pair_overrides.reentry_floats）。
同根 K 双穿保守：SL 优先（与 backtest.py 口径一致）。
"""
import argparse
import sys
from copy import deepcopy
from datetime import timezone
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy.high_low import HighLowStrategy  # noqa: E402

UTC = timezone.utc


def _load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    df["date"] = df["ts"].dt.date
    return df


def _pair_cfg(cfg: dict, pair: str) -> dict:
    ov = (cfg["strategy"].get("pair_overrides") or {}).get(pair) or {}
    strat = cfg["strategy"]
    return {
        "position_pct": float(ov.get("position_pct", strat["position_pct"])),
        "tp_pct": float(ov.get("tp_pct", strat["tp_pct"])),
        "sl_pct": float(ov.get("sl_pct", strat["sl_pct"])),
        "float_pct": float(ov.get("float_pct", strat["float_pct"])),
        "reentry_floats": list(ov.get("reentry_floats") or []),
    }


def _compute_daily_signal(strat: HighLowStrategy, pair: str, prev_day_df: pd.DataFrame,
                           sig_date) -> dict | None:
    candles = prev_day_df.to_dict("records")
    if len(candles) < 2:
        return None
    return strat.compute_signal(pair, [
        {"ts": int(c["timestamp"]), "open": c["open"], "high": c["high"],
         "low": c["low"], "close": c["close"]}
        for c in candles
    ], signal_date=sig_date)


def simulate_multi(dfs: dict[str, pd.DataFrame], cfg: dict,
                    initial_balance: float = 75.0,
                    days: int | None = None,
                    verbose: bool = False,
                    slippage_bps_in: float = 5.0,
                    slippage_bps_out: float = 5.0,
                    fee_rate_in: float = 0.0005,   # 0.05% taker
                    fee_rate_out: float = 0.0002,  # 0.02% maker
                    ) -> dict:
    """
    dfs: {pair: df}。所有 df 应覆盖同样的日期范围。
    slippage_bps_in/out：入场/出场滑点（bps，1bp=0.01%）
    fee_rate_in/out：入场/出场手续费率（占名义仓位）
    """
    strat = HighLowStrategy(cfg)
    leverage = int(cfg["strategy"]["leverage"])
    fixed_thr = float(cfg["strategy"]["fixed_mode_threshold"])
    fixed_margin = float(cfg["strategy"]["fixed_mode_margin"])
    max_losses = int(cfg["strategy"]["max_consecutive_losses"])
    cooldown_hours = int(cfg["strategy"]["cooldown_hours"])

    pair_cfgs = {p: _pair_cfg(cfg, p) for p in dfs}

    def close(pair, st, exit_price, exit_reason, bar, direction):
        _close(pair, st, exit_price, exit_reason, bar,
               direction, leverage, trades, sig_date, next_date,
               slip_in_bps=slippage_bps_in, slip_out_bps=slippage_bps_out,
               fee_in=fee_rate_in, fee_out=fee_rate_out)

    # 所有 pair 的日期集合（应一致，但取交集以防）
    date_sets = [set(df["date"].unique()) for df in dfs.values()]
    common_dates = sorted(set.intersection(*date_sets))
    if days:
        common_dates = common_dates[-(days + 1):]

    balance = initial_balance
    peak = balance
    max_dd = 0.0
    trades: list[dict] = []
    consec_losses = 0
    cooldown_until_ts = None
    fixed_locked = False
    # 记录一起持仓的天数（可诊断相关性影响）
    concurrent_trade_days = 0
    concurrent_sl_days = 0

    for i in range(len(common_dates) - 1):
        sig_date = common_dates[i]
        next_date = common_dates[i + 1]

        if cooldown_until_ts is not None:
            day_start = pd.Timestamp(next_date, tz=UTC)
            if day_start < cooldown_until_ts:
                continue
            cooldown_until_ts = None

        # 为每个 pair 生成信号
        pair_states: dict[str, dict] = {}
        for pair, df in dfs.items():
            prev = df[df["date"] == sig_date]
            sig = _compute_daily_signal(strat, pair, prev, sig_date)
            if not sig:
                continue
            pcfg = pair_cfgs[pair]
            floats_seq = pcfg["reentry_floats"] or [pcfg["float_pct"]]
            pair_states[pair] = {
                "sig": sig,
                "cfg": pcfg,
                "floats_seq": floats_seq,
                "attempt": 0,        # 已完成的入场次数
                "in_position": False,  # 是否持仓中
                "entry": None, "tp": None, "sl": None,
                "entry_price_actual": None,
                "margin": 0.0,
                "sl_bar_idx_last": -1,  # 上一次 SL 那根 K 的 idx，用于日内新高低点计算
                "day_sl_count": 0,
                "day_had_non_sl": False,  # TP/EOD/NO_ENTRY
                "next_bar_to_search": 0,   # 从哪根 K 开始找入场
            }

        if not pair_states:
            continue

        # 把 next_date 每 pair 的 K 线合并到统一时间轴
        bars_by_pair: dict[str, list] = {}
        for pair in pair_states:
            sub = dfs[pair][dfs[pair]["date"] == next_date]
            bars_by_pair[pair] = list(sub.itertuples())
        # 找到所有唯一 ts（一般两 pair 一致）
        all_ts_set = set()
        for bars in bars_by_pair.values():
            for b in bars:
                all_ts_set.add(int(b.ts.value // 10**6))  # ts 转 ms
        all_ts_sorted = sorted(all_ts_set)

        # 每 pair 建 ts->bar 快查
        bar_by_ts: dict[str, dict[int, object]] = {}
        for pair, bars in bars_by_pair.items():
            bar_by_ts[pair] = {int(b.ts.value // 10**6): b for b in bars}

        # 逐根 K 推进
        for tick_ms in all_ts_sorted:
            for pair, st in list(pair_states.items()):
                bar = bar_by_ts[pair].get(tick_ms)
                if bar is None:
                    continue

                direction = st["sig"]["direction"]

                # 状态机
                if not st["in_position"]:
                    # 若已用完次数或已 TP/EOD → 跳
                    if st["day_had_non_sl"]:
                        continue
                    if st["attempt"] >= len(st["floats_seq"]):
                        continue

                    # 计算本次入场价
                    fp = st["floats_seq"][st["attempt"]]
                    if st["attempt"] == 0:
                        prev_high = st["sig"]["day_high"]
                        prev_low = st["sig"]["day_low"]
                        if direction == "long":
                            entry = round(prev_low * (1 - fp), 6)
                        else:
                            entry = round(prev_high * (1 + fp), 6)
                    else:
                        # 用"日初到上次 SL"这段的日内高低
                        seg_bars = [b for b in bars_by_pair[pair]
                                    if int(b.ts.value // 10**6) < tick_ms]
                        if not seg_bars:
                            continue
                        day_high_so_far = max(b.high for b in seg_bars)
                        day_low_so_far = min(b.low for b in seg_bars)
                        if direction == "long":
                            entry = round(day_low_so_far * (1 - fp), 6)
                        else:
                            entry = round(day_high_so_far * (1 + fp), 6)

                    pcfg = st["cfg"]
                    if direction == "long":
                        tp = round(entry * (1 + pcfg["tp_pct"]), 6)
                        sl = round(entry * (1 - pcfg["sl_pct"]), 6)
                    else:
                        tp = round(entry * (1 - pcfg["tp_pct"]), 6)
                        sl = round(entry * (1 + pcfg["sl_pct"]), 6)

                    # 判断本根 K 是否触发入场
                    triggered = False
                    if direction == "long" and bar.low <= entry:
                        triggered = True
                    elif direction == "short" and bar.high >= entry:
                        triggered = True

                    if not triggered:
                        # 记录入场价，等下一根 K 判触发
                        st["entry"], st["tp"], st["sl"] = entry, tp, sl
                        continue

                    # 入场！扣保证金
                    if fixed_locked or balance >= fixed_thr:
                        fixed_locked = True
                        margin = fixed_margin
                        mode = "FIXED"
                    else:
                        margin = balance * pcfg["position_pct"]
                        mode = "PCT"

                    st["in_position"] = True
                    st["entry"], st["tp"], st["sl"] = entry, tp, sl
                    st["entry_price_actual"] = entry
                    st["margin"] = margin
                    st["mode"] = mode

                    # 入场根 K 内是否直接 TP/SL（同根双穿 → SL 优先）
                    exit_price = None
                    exit_reason = None
                    if direction == "long":
                        if bar.low <= sl:
                            exit_price, exit_reason = sl, "SL"
                        elif bar.high >= tp:
                            exit_price, exit_reason = tp, "TP"
                    else:
                        if bar.high >= sl:
                            exit_price, exit_reason = sl, "SL"
                        elif bar.low <= tp:
                            exit_price, exit_reason = tp, "TP"

                    if exit_price is not None:
                        close(pair, st, exit_price, exit_reason, bar, direction)
                        # 结算 balance
                        pnl = trades[-1]["pnl"]
                        balance += pnl
                        peak = max(peak, balance)
                        dd = (peak - balance) / peak if peak > 0 else 0
                        max_dd = max(max_dd, dd)
                        st["attempt"] += 1
                        if exit_reason == "SL":
                            st["day_sl_count"] += 1
                        else:
                            st["day_had_non_sl"] = True
                        st["in_position"] = False
                else:
                    # 已持仓中，看 TP/SL
                    tp = st["tp"]
                    sl = st["sl"]
                    exit_price = None
                    exit_reason = None
                    if direction == "long":
                        if bar.low <= sl:
                            exit_price, exit_reason = sl, "SL"
                        elif bar.high >= tp:
                            exit_price, exit_reason = tp, "TP"
                    else:
                        if bar.high >= sl:
                            exit_price, exit_reason = sl, "SL"
                        elif bar.low <= tp:
                            exit_price, exit_reason = tp, "TP"

                    if exit_price is not None:
                        close(pair, st, exit_price, exit_reason, bar, direction)
                        pnl = trades[-1]["pnl"]
                        balance += pnl
                        peak = max(peak, balance)
                        dd = (peak - balance) / peak if peak > 0 else 0
                        max_dd = max(max_dd, dd)
                        st["attempt"] += 1
                        if exit_reason == "SL":
                            st["day_sl_count"] += 1
                        else:
                            st["day_had_non_sl"] = True
                        st["in_position"] = False

        # 收盘处理：未闭合的用 EOD close 平
        for pair, st in pair_states.items():
            if st["in_position"]:
                last_bar = bars_by_pair[pair][-1]
                direction = st["sig"]["direction"]
                exit_price = last_bar.close
                close(pair, st, exit_price, "EOD", last_bar, direction)
                pnl = trades[-1]["pnl"]
                balance += pnl
                peak = max(peak, balance)
                dd = (peak - balance) / peak if peak > 0 else 0
                max_dd = max(max_dd, dd)
                st["day_had_non_sl"] = True
                st["in_position"] = False

        # 每日统计"两 pair 是否都在交易"
        pairs_with_trade = [p for p, st in pair_states.items()
                            if st["attempt"] > 0 or st["in_position"]]
        if len(pairs_with_trade) >= 2:
            concurrent_trade_days += 1
            # 是否两个 pair 当天最终都 SL 收场
            both_sl = all(
                pair_states[p]["day_sl_count"] > 0 and
                not pair_states[p]["day_had_non_sl"]
                for p in pairs_with_trade
            )
            if both_sl:
                concurrent_sl_days += 1

        # 账户级熔断计数：按当日累计亏损次数
        day_losses = sum(
            1 for t in trades[-10:]  # 只看最近的，估算
            if t["trade_date"] == str(next_date) and t["pnl"] < 0
        )
        # 更严谨：当日凡有亏损 trade → +1；简单实现：只看当日最终情况
        day_trades = [t for t in trades if t["trade_date"] == str(next_date)]
        if day_trades:
            day_pnl = sum(t["pnl"] for t in day_trades)
            if day_pnl < 0:
                consec_losses += 1
            else:
                consec_losses = 0

        if consec_losses >= max_losses:
            cooldown_until_ts = pd.Timestamp(next_date, tz=UTC) + \
                                pd.Timedelta(days=1) + pd.Timedelta(hours=cooldown_hours)
            consec_losses = 0
            if verbose:
                print(f"[熔断] {next_date} → 冷静到 {cooldown_until_ts}")

        if verbose:
            for pair, st in pair_states.items():
                if st["attempt"] > 0:
                    print(f"{next_date} {pair} attempts={st['attempt']} sl={st['day_sl_count']}")
            print(f"  balance={balance:.2f}")

    # 汇总
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    total = len(trades)
    win_rate = wins / total if total else 0
    gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    total_return = (balance - initial_balance) / initial_balance if initial_balance > 0 else 0

    by_pair_stats = {}
    for p in dfs:
        pt = [t for t in trades if t["pair"] == p]
        w = sum(1 for t in pt if t["pnl"] > 0)
        l = sum(1 for t in pt if t["pnl"] < 0)
        by_pair_stats[p] = {
            "n": len(pt), "wins": w, "losses": l,
            "pnl": sum(t["pnl"] for t in pt),
        }

    return {
        "initial": initial_balance,
        "final": balance,
        "total_return_pct": total_return * 100,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate * 100,
        "max_dd_pct": max_dd * 100,
        "profit_factor": pf,
        "concurrent_trade_days": concurrent_trade_days,
        "concurrent_sl_days": concurrent_sl_days,
        "trades_detail": trades,
        "by_pair": by_pair_stats,
    }


def _close(pair, st, exit_price, exit_reason, bar,
            direction, leverage, trades, sig_date, next_date,
            slip_in_bps=0.0, slip_out_bps=0.0,
            fee_in=0.0, fee_out=0.0):
    """计算 pnl（含滑点+手续费）并 append 一条 trade。
    滑点：入场价格对我方不利、出场价格对我方不利。
    手续费：入场 + 出场分别按名义仓位 = margin × leverage 扣除。
    """
    entry_signal = st["entry_price_actual"]
    margin = st["margin"]
    slip_in = slip_in_bps / 10000.0
    slip_out = slip_out_bps / 10000.0

    # 滑点：入场
    if direction == "long":
        entry_real = entry_signal * (1 + slip_in)     # 买贵
        exit_real = exit_price * (1 - slip_out)       # 卖便宜
    else:
        entry_real = entry_signal * (1 - slip_in)     # 卖便宜
        exit_real = exit_price * (1 + slip_out)       # 买贵

    if direction == "long":
        pct = (exit_real - entry_real) / entry_real
    else:
        pct = (entry_real - exit_real) / entry_real
    gross_pnl = margin * leverage * pct

    # 手续费：按名义仓位算
    notional_in = margin * leverage
    notional_out = notional_in * (exit_real / entry_real) if entry_real > 0 else notional_in
    fee = notional_in * fee_in + notional_out * fee_out

    pnl = gross_pnl - fee

    trades.append({
        "signal_date": str(sig_date),
        "trade_date": str(next_date),
        "pair": pair,
        "attempt": st["attempt"] + 1,
        "direction": direction,
        "entry_signal": entry_signal, "entry_real": entry_real,
        "exit_signal": exit_price, "exit_real": exit_real,
        "reason": exit_reason,
        "margin": margin, "mode": st.get("mode", "PCT"),
        "gross_pnl": gross_pnl, "fee": fee, "pnl": pnl,
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--balance", type=float, default=75.0)
    ap.add_argument("--pairs", default="BTC-USDT-SWAP,ETH-USDT-SWAP")
    ap.add_argument("--config", default=None)
    ap.add_argument("--slip-in", type=float, default=5.0, help="入场滑点（bps）")
    ap.add_argument("--slip-out", type=float, default=5.0, help="出场滑点（bps）")
    ap.add_argument("--fee-in", type=float, default=0.0005, help="入场费率")
    ap.add_argument("--fee-out", type=float, default=0.0002, help="出场费率")
    ap.add_argument("--no-cost", action="store_true", help="关闭滑点+手续费（对照）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    pairs = args.pairs.split(",")
    dfs = {}
    for pair in pairs:
        token = pair.replace("-USDT-SWAP", "")
        csv_path = ROOT / "csv_data" / f"{token}_USDT_SWAP_1H_12m.csv"
        if not csv_path.exists():
            print(f"[err] CSV not found: {csv_path}")
            sys.exit(1)
        dfs[pair] = _load_csv(csv_path)

    if args.no_cost:
        slip_in = slip_out = 0.0
        fee_in = fee_out = 0.0
    else:
        slip_in, slip_out = args.slip_in, args.slip_out
        fee_in, fee_out = args.fee_in, args.fee_out

    res = simulate_multi(dfs, cfg, initial_balance=args.balance,
                          days=args.days, verbose=args.verbose,
                          slippage_bps_in=slip_in, slippage_bps_out=slip_out,
                          fee_rate_in=fee_in, fee_rate_out=fee_out)

    total_fee = sum(t.get("fee", 0) for t in res["trades_detail"])
    total_gross = sum(t.get("gross_pnl", 0) for t in res["trades_detail"])
    print(f"\n[成本] 入场滑点={slip_in}bps 出场滑点={slip_out}bps  "
          f"入费率={fee_in*100:.3f}% 出费率={fee_out*100:.3f}%")
    print(f"[成本] 累计手续费={total_fee:.2f} USDT  "
          f"毛盈亏（不含费）={total_gross:.2f} USDT")

    print(f"\n=== 多 pair 联合回测 ({args.days}d, {'+'.join(pairs)}) ===")
    print(f"初始余额        : {res['initial']:.2f}")
    print(f"结束余额        : {res['final']:.2f}")
    print(f"总收益          : {res['total_return_pct']:+.2f}%")
    print(f"总笔数          : {res['trades']}  (W {res['wins']} / L {res['losses']})")
    print(f"胜率            : {res['win_rate_pct']:.2f}%")
    print(f"最大回撤        : {res['max_dd_pct']:.2f}%")
    print(f"盈亏比          : {res['profit_factor']:.2f}")
    print(f"两 pair 共同交易日: {res['concurrent_trade_days']}  (其中双 SL 日: {res['concurrent_sl_days']})")
    print(f"\n分 pair 细节:")
    for p, s in res["by_pair"].items():
        print(f"  {p}: {s['n']} 笔 (W{s['wins']}/L{s['losses']})  pnl={s['pnl']:+.2f}")


if __name__ == "__main__":
    main()
