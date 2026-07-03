"""BTC + ETH 联合组合回测（真实场景：共享资金池，两个交易对可能同时持仓）。

用当前 config.yaml 的完整 pair_overrides（BTC max_prev_amp=4.75%、ETH=8%）。

模型：
  - 共享资金池，起始 150U
  - 每日 00:00 UTC 触发：
      · BTC 用前一日 24 根 1H 算信号 → 得挂单价 + reentry_floats
      · ETH 同理
  - 交易日：分别按各自的 reentry 链模拟入场/退出
      · **两笔仓位互相独立**：BTC 亏损时 ETH 仓位不变
      · 两笔保证金分别 = 当日日初余额 × 各自 position_pct
        （即 BTC 用 5%、ETH 用 8%，都按同一份日初余额算 —— 现实等价）
  - 日终把两笔 PnL 加回共享余额，进入下一日
  - 每对独立跟踪 consec_losses 和 cooldown
  - 组合级回撤按 equity curve = 150 + 累计 PnL 追踪
"""
from __future__ import annotations

import sys
import io
import copy
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest import _simulate_one_entry, load_csv  # noqa: E402
from strategy.high_low import HighLowStrategy  # noqa: E402

UTC_LABEL = "UTC"


# ---------- 单对单日模拟 ----------

@dataclass
class DayResult:
    pnl: float = 0.0
    n_trades: int = 0
    n_win: int = 0
    n_loss: int = 0
    last_reason: str = ""  # TP / SL / EOD / NONE
    consec_from: int = 0   # 传入的 consec_losses
    consec_to: int = 0     # 更新后的 consec_losses
    trades: list = None


def simulate_pair_day(
    strat: HighLowStrategy,
    pair: str,
    cfg: dict,
    prev_day_df: pd.DataFrame,
    trade_day_df: pd.DataFrame,
    sig_date,
    trade_date,
    day_start_balance: float,
    consec_losses: int,
    cooldown_until_ts,
    fee_rt: float = 0.0007,     # 挂单进 0.02% + 市价出 0.05%
    slippage_rt: float = 0.001, # 每笔来回 10bp 滑点预算
) -> DayResult:
    """针对一个交易对的单日模拟。返回当日 PnL 和状态更新。"""
    r = DayResult(consec_from=consec_losses, consec_to=consec_losses, trades=[])
    s = cfg["strategy"]
    ov = (s.get("pair_overrides") or {}).get(pair, {})
    leverage = int(ov.get("leverage", s["leverage"]))  # per-pair 优先
    position_pct = float(ov.get("position_pct", s["position_pct"]))
    fixed_thr = float(s["fixed_mode_threshold"])
    fixed_margin = float(s["fixed_mode_margin"])
    tp_pct = float(ov.get("tp_pct", s["tp_pct"]))
    sl_pct = float(ov.get("sl_pct", s["sl_pct"]))
    reentry = list(ov.get("reentry_floats") or [ov.get("float_pct", s["float_pct"])])

    # 冷却期
    if cooldown_until_ts is not None:
        day_start = pd.Timestamp(trade_date, tz=UTC_LABEL)
        if day_start < cooldown_until_ts:
            return r

    # 计算信号
    candles = prev_day_df.to_dict("records")
    if len(candles) < 2:
        return r
    signal = strat.compute_signal(pair, [
        {"ts": int(c["timestamp"]), "open": c["open"], "high": c["high"],
         "low": c["low"], "close": c["close"]}
        for c in candles
    ], signal_date=sig_date)
    if not signal:
        return r
    direction = signal["direction"]
    prev_high = signal["day_high"]
    prev_low = signal["day_low"]

    bars = list(trade_day_df.itertuples())
    if not bars:
        return r

    # 保证金：按日初余额计算（是否切固定档以余额判定）
    if day_start_balance >= fixed_thr:
        margin = fixed_margin
    else:
        margin = day_start_balance * position_pct

    search_from = 0
    day_sl_count = 0
    day_had_non_sl = False

    for attempt_idx, fp in enumerate(reentry):
        # 入场价
        if attempt_idx == 0:
            entry = round(prev_low * (1 - fp), 6) if direction == "long" else round(prev_high * (1 + fp), 6)
        else:
            seg = bars[:search_from]
            if not seg:
                break
            day_high = max(b.high for b in seg)
            day_low = min(b.low for b in seg)
            entry = round(day_low * (1 - fp), 6) if direction == "long" else round(day_high * (1 + fp), 6)

        tp = round(entry * (1 + tp_pct), 6) if direction == "long" else round(entry * (1 - tp_pct), 6)
        sl = round(entry * (1 - sl_pct), 6) if direction == "long" else round(entry * (1 + sl_pct), 6)

        exit_price, exit_reason, exit_ts, entry_bar_idx = _simulate_one_entry(
            bars, direction, entry, tp, sl, start_idx=search_from
        )
        if exit_reason == "NO_ENTRY":
            day_had_non_sl = True
            break

        pct = (exit_price - entry) / entry if direction == "long" else (entry - exit_price) / entry
        notional = margin * leverage
        gross_pnl = notional * pct
        cost = notional * (fee_rt + slippage_rt)
        pnl = gross_pnl - cost
        r.pnl += pnl
        r.n_trades += 1
        if pnl > 0:
            r.n_win += 1
        else:
            r.n_loss += 1
        r.last_reason = exit_reason
        r.trades.append({
            "date": str(trade_date), "pair": pair, "direction": direction,
            "attempt": attempt_idx + 1, "entry": entry, "exit": exit_price,
            "reason": exit_reason, "margin": margin, "pnl": pnl,
        })

        if exit_reason == "SL":
            day_sl_count += 1
            sl_bar_idx = None
            for k in range(entry_bar_idx, len(bars)):
                if bars[k].ts == exit_ts:
                    sl_bar_idx = k
                    break
            if sl_bar_idx is None:
                break
            search_from = sl_bar_idx + 1
            if search_from >= len(bars):
                break
            continue
        else:
            day_had_non_sl = True
            break

    # 更新 consec_losses
    if day_sl_count >= len(reentry) and not day_had_non_sl:
        r.consec_to = consec_losses + 1
    elif r.last_reason == "TP":
        r.consec_to = 0
    else:
        r.consec_to = consec_losses

    return r


# ---------- N 对通用主循环 ----------

def joint_backtest_multi(
    dfs: dict,  # {pair_name: df1h}
    cfg: dict,
    initial: float = 280.0,
    start: pd.Timestamp = None,
    end: pd.Timestamp = None,
    verbose: bool = False,
    fee_rt: float = 0.0007,
    slippage_rt: float = 0.001,
    year1_end: pd.Timestamp = None,
) -> dict:
    """N 对联合回测（共享资金池）。dfs = {pair_name: df1h}"""
    dfs_local = {}
    for pair, df in dfs.items():
        d = df.copy()
        if start is not None:
            d = d[d["ts"] >= start]
        if end is not None:
            d = d[d["ts"] < end]
        d = d.reset_index(drop=True)
        d["date"] = d["ts"].dt.date
        dfs_local[pair] = d

    grouped = {pair: dict(list(d.groupby("date"))) for pair, d in dfs_local.items()}
    # 取所有 pair 都有的日期，并按序
    common_dates = set(next(iter(grouped.values())).keys())
    for g in grouped.values():
        common_dates &= set(g.keys())
    all_dates = sorted(common_dates)

    strat = HighLowStrategy(cfg)
    max_losses = int(cfg["strategy"]["max_consecutive_losses"])
    cooldown_hours = int(cfg["strategy"]["cooldown_hours"])

    balance = initial
    equity_curve = [(all_dates[0], balance)]
    trades_all = []
    total_fees_slip = 0.0
    year1_end_balance = None
    per_pair_stats = {p: {"pnl": 0.0, "n": 0, "w": 0, "l": 0, "cost": 0.0} for p in dfs}
    state = {p: {"consec": 0, "cooldown_until": None} for p in dfs}

    peak = balance
    max_dd = 0.0
    days_hold_count = {i: 0 for i in range(len(dfs) + 1)}  # 0=空仓, 1=1对..N=N对

    for i in range(len(all_dates) - 1):
        sig_date = all_dates[i]
        trade_date = all_dates[i + 1]
        day_start_balance = balance
        results = {}

        for pair, g in grouped.items():
            prev_df = g[sig_date]
            trade_df = g[trade_date]
            r = simulate_pair_day(
                strat, pair, cfg, prev_df, trade_df, sig_date, trade_date,
                day_start_balance,
                state[pair]["consec"], state[pair]["cooldown_until"],
                fee_rt=fee_rt, slippage_rt=slippage_rt,
            )
            results[pair] = r
            state[pair]["consec"] = r.consec_to
            if r.consec_to >= max_losses:
                base_ts = pd.Timestamp(trade_date, tz=UTC_LABEL) + pd.Timedelta(days=1)
                state[pair]["cooldown_until"] = base_ts + pd.Timedelta(hours=cooldown_hours)
                state[pair]["consec"] = 0

        day_pnl = sum(r.pnl for r in results.values())
        balance += day_pnl

        active_count = sum(1 for r in results.values() if r.n_trades > 0)
        days_hold_count[active_count] += 1

        equity_curve.append((trade_date, balance))
        peak = max(peak, balance)
        dd = (peak - balance) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

        for pair, r in results.items():
            per_pair_stats[pair]["pnl"] += r.pnl
            per_pair_stats[pair]["n"] += r.n_trades
            per_pair_stats[pair]["w"] += r.n_win
            per_pair_stats[pair]["l"] += r.n_loss
            ov = (cfg["strategy"].get("pair_overrides") or {}).get(pair, {})
            pair_lev = int(ov.get("leverage", cfg["strategy"]["leverage"]))
            for t in r.trades:
                notional = t["margin"] * pair_lev
                cost = notional * (fee_rt + slippage_rt)
                per_pair_stats[pair]["cost"] += cost
                total_fees_slip += cost
                trades_all.append(t)

        if year1_end is not None and year1_end_balance is None:
            if pd.Timestamp(trade_date, tz="UTC") >= year1_end:
                year1_end_balance = balance

    total_return = (balance - initial) / initial
    n_days = len(all_dates) - 1
    monthly = (1 + total_return) ** (30 / max(n_days, 1)) - 1 if total_return > -1 else -1

    return {
        "initial": initial, "final": balance, "total_return": total_return,
        "monthly": monthly, "max_dd": max_dd, "n_days": n_days,
        "per_pair": per_pair_stats, "trades": trades_all,
        "equity_curve": equity_curve,
        "days_hold_count": days_hold_count,
        "total_fees_slip": total_fees_slip,
        "year1_end_balance": year1_end_balance,
    }


# ---------- 双对旧版（保持向后兼容）----------

def joint_backtest(
    df_btc: pd.DataFrame,
    df_eth: pd.DataFrame,
    cfg: dict,
    initial: float = 280.0,
    start: pd.Timestamp = None,
    end: pd.Timestamp = None,
    verbose: bool = False,
    fee_rt: float = 0.0007,
    slippage_rt: float = 0.001,
    year1_end: pd.Timestamp = None,
) -> dict:
    if start is not None:
        df_btc = df_btc[df_btc["ts"] >= start].reset_index(drop=True)
        df_eth = df_eth[df_eth["ts"] >= start].reset_index(drop=True)
    if end is not None:
        df_btc = df_btc[df_btc["ts"] < end].reset_index(drop=True)
        df_eth = df_eth[df_eth["ts"] < end].reset_index(drop=True)
    df_btc["date"] = df_btc["ts"].dt.date
    df_eth["date"] = df_eth["ts"].dt.date

    grouped_btc = dict(list(df_btc.groupby("date")))
    grouped_eth = dict(list(df_eth.groupby("date")))
    # 取两个 pair 都有的日期，并按序
    all_dates = sorted(set(grouped_btc.keys()) & set(grouped_eth.keys()))

    strat = HighLowStrategy(cfg)
    max_losses = int(cfg["strategy"]["max_consecutive_losses"])
    cooldown_hours = int(cfg["strategy"]["cooldown_hours"])

    balance = initial
    equity_curve = [(all_dates[0], balance)]
    trades_all = []
    total_fees_slip = 0.0
    year1_end_balance = None
    per_pair_stats = {"BTC-USDT-SWAP": {"pnl": 0.0, "n": 0, "w": 0, "l": 0, "cost": 0.0},
                      "ETH-USDT-SWAP": {"pnl": 0.0, "n": 0, "w": 0, "l": 0, "cost": 0.0}}

    state = {
        "BTC-USDT-SWAP": {"consec": 0, "cooldown_until": None},
        "ETH-USDT-SWAP": {"consec": 0, "cooldown_until": None},
    }
    # 组合级 MDD
    peak = balance
    max_dd = 0.0
    days_both_flat = 0
    days_one_holding = 0
    days_both_holding = 0

    for i in range(len(all_dates) - 1):
        sig_date = all_dates[i]
        trade_date = all_dates[i + 1]

        day_start_balance = balance
        results = {}

        for pair, df_map in [("BTC-USDT-SWAP", grouped_btc), ("ETH-USDT-SWAP", grouped_eth)]:
            prev_df = df_map[sig_date]
            trade_df = df_map[trade_date]
            r = simulate_pair_day(
                strat, pair, cfg, prev_df, trade_df, sig_date, trade_date,
                day_start_balance,
                state[pair]["consec"], state[pair]["cooldown_until"],
                fee_rt=fee_rt, slippage_rt=slippage_rt,
            )
            results[pair] = r
            state[pair]["consec"] = r.consec_to
            if r.consec_to >= max_losses:
                base_ts = pd.Timestamp(trade_date, tz=UTC_LABEL) + pd.Timedelta(days=1)
                state[pair]["cooldown_until"] = base_ts + pd.Timedelta(hours=cooldown_hours)
                state[pair]["consec"] = 0

        # 结算：把当日两对 PnL 加进 balance
        day_pnl = results["BTC-USDT-SWAP"].pnl + results["ETH-USDT-SWAP"].pnl
        balance += day_pnl

        # 统计当日同时持仓情况（近似：n_trades > 0 视为"当日有活跃仓位"）
        b_active = results["BTC-USDT-SWAP"].n_trades > 0
        e_active = results["ETH-USDT-SWAP"].n_trades > 0
        if b_active and e_active:
            days_both_holding += 1
        elif b_active or e_active:
            days_one_holding += 1
        else:
            days_both_flat += 1

        # 组合级 equity 与 MDD
        equity_curve.append((trade_date, balance))
        peak = max(peak, balance)
        dd = (peak - balance) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

        for pair, r in results.items():
            per_pair_stats[pair]["pnl"] += r.pnl
            per_pair_stats[pair]["n"] += r.n_trades
            per_pair_stats[pair]["w"] += r.n_win
            per_pair_stats[pair]["l"] += r.n_loss
            # per-pair leverage 支持
            ov = (cfg["strategy"].get("pair_overrides") or {}).get(pair, {})
            pair_lev = int(ov.get("leverage", cfg["strategy"]["leverage"]))
            for t in r.trades:
                # 累计费用（用来汇总）
                notional = t["margin"] * pair_lev
                cost = notional * (fee_rt + slippage_rt)
                per_pair_stats[pair]["cost"] += cost
                total_fees_slip += cost
                trades_all.append(t)

        # 记录 Y1 收盘余额
        if year1_end is not None and year1_end_balance is None:
            if pd.Timestamp(trade_date, tz="UTC") >= year1_end:
                year1_end_balance = balance

        if verbose and day_pnl != 0:
            print(f"{trade_date}  BTC {results['BTC-USDT-SWAP'].pnl:+7.2f}U  "
                  f"ETH {results['ETH-USDT-SWAP'].pnl:+7.2f}U  "
                  f"bal={balance:.2f}  peak={peak:.2f}  dd={dd*100:.1f}%")

    total_return = (balance - initial) / initial
    n_days = len(all_dates) - 1
    monthly = (1 + total_return) ** (30 / max(n_days, 1)) - 1 if total_return > -1 else -1

    return {
        "initial": initial, "final": balance, "total_return": total_return,
        "monthly": monthly, "max_dd": max_dd, "n_days": n_days,
        "per_pair": per_pair_stats, "trades": trades_all,
        "equity_curve": equity_curve,
        "days_both_flat": days_both_flat,
        "days_one_holding": days_one_holding,
        "days_both_holding": days_both_holding,
        "total_fees_slip": total_fees_slip,
        "year1_end_balance": year1_end_balance,
    }


def load_cfg():
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--initial", type=float, default=280.0)
    ap.add_argument("--fee", type=float, default=0.0007)
    ap.add_argument("--slippage", type=float, default=0.001)
    args = ap.parse_args()

    cfg = load_cfg()
    print("使用 config.yaml 参数：")
    ov = cfg["strategy"]["pair_overrides"]
    print(f"  BTC: pos={ov['BTC-USDT-SWAP']['position_pct']*100:.0f}%  "
          f"tp={ov['BTC-USDT-SWAP']['tp_pct']*100:.2f}%  "
          f"sl={ov['BTC-USDT-SWAP']['sl_pct']*100:.2f}%  "
          f"max_amp={ov['BTC-USDT-SWAP']['max_prev_amp']*100:.2f}%")
    print(f"  ETH: pos={ov['ETH-USDT-SWAP']['position_pct']*100:.0f}%  "
          f"tp={ov['ETH-USDT-SWAP']['tp_pct']*100:.2f}%  "
          f"sl={ov['ETH-USDT-SWAP']['sl_pct']*100:.2f}%  "
          f"max_amp={ov['ETH-USDT-SWAP']['max_prev_amp']*100:.2f}%")
    print(f"成本模型：来回费率={args.fee*100:.3f}% + 滑点={args.slippage*100:.2f}%/笔 = {(args.fee+args.slippage)*100:.3f}% 总成本")
    print(f"起始资金：{args.initial:.0f} U（连续复利，不提现）")

    df_btc = load_csv(Path("csv_data/BTC_USDT_SWAP_1H_780d.csv"))
    df_eth = load_csv(Path("csv_data/ETH_USDT_SWAP_1H_780d.csv"))

    # 只跑连续 2 年（Y1 → Y2 无缝复利）
    y1_end = pd.Timestamp("2025-07-01", tz="UTC")
    for label, s, e in [
        ("2Y 连续复利 (2024-07-01 → 2026-07-01)",
         pd.Timestamp("2024-07-01", tz="UTC"), pd.Timestamp("2026-07-01", tz="UTC")),
    ]:
        res = joint_backtest(df_btc.copy(), df_eth.copy(), cfg,
                              initial=args.initial, start=s, end=e,
                              fee_rt=args.fee, slippage_rt=args.slippage,
                              year1_end=y1_end)
        pp = res["per_pair"]
        print()
        print(f"=== {label} ===")
        print(f"起始余额:       {res['initial']:.2f} U")
        if res["year1_end_balance"] is not None:
            y1_bal = res["year1_end_balance"]
            y1_ret = (y1_bal - res['initial']) / res['initial'] * 100
            y2_ret = (res['final'] - y1_bal) / y1_bal * 100
            print(f"Y1 结束余额:    {y1_bal:.2f} U  ({y1_ret:+.2f}%)  ← Y2 起始")
        print(f"Y2 结束余额:    {res['final']:.2f} U")
        if res["year1_end_balance"] is not None:
            print(f"  Y1 收益:      {y1_ret:+.2f}%")
            print(f"  Y2 收益:      {y2_ret:+.2f}%")
        print(f"2Y 总收益:      {res['total_return']*100:+.2f}%")
        print(f"月化(几何):     {res['monthly']*100:+.2f}%")
        print(f"组合最大回撤:   {res['max_dd']*100:.2f}%")
        print(f"总手续费+滑点:  {res['total_fees_slip']:.2f} U")
        print(f"交易日数:       {res['n_days']}")
        print(f"两对同持仓日:   {res['days_both_holding']}   ({res['days_both_holding']/res['n_days']*100:.1f}%)")
        print(f"仅一对持仓日:   {res['days_one_holding']}   ({res['days_one_holding']/res['n_days']*100:.1f}%)")
        print(f"两对都空仓日:   {res['days_both_flat']}   ({res['days_both_flat']/res['n_days']*100:.1f}%)")
        print(f"分币种贡献：")
        for pair, s2 in pp.items():
            wr = s2["w"] / s2["n"] * 100 if s2["n"] > 0 else 0
            print(f"  {pair}: 笔数={s2['n']:>3}  胜={s2['w']}  负={s2['l']}  胜率={wr:.1f}%  "
                  f"贡献 PnL={s2['pnl']:+.2f} U  费用={s2['cost']:.2f} U")

        # 月度分布（组合）
        eq = pd.DataFrame(res["equity_curve"], columns=["date", "equity"])
        eq["date"] = pd.to_datetime(eq["date"])
        eq["ym"] = eq["date"].dt.strftime("%Y-%m")
        m = eq.groupby("ym").agg(start_eq=("equity", "first"), end_eq=("equity", "last"))
        m["pnl"] = m["end_eq"] - m["start_eq"]
        m["ret%"] = m["pnl"] / m["start_eq"] * 100
        print("\n月度组合表现：")
        print(m[["pnl", "ret%"]].round(2).to_string())


if __name__ == "__main__":
    main()
