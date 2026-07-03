"""
140U 策略回测 v2：真实 4H + 真实 15M K 线，复利、只提现 1 次本金 140U。

用法：
  python scripts/backtest_140u_v2.py \
      --bar4h csv_data/BTC_USDT_SWAP_4H_400d.csv \
      --bar15m csv_data/BTC_USDT_SWAP_15m_400d.csv \
      --start 2025-07-01 --end 2026-07-01

策略：
  - 4H 图上，前 3 根 K 线画箱体（高点 max / 低点 min）
  - 当前 4H 放量突破/跌破箱体（vol_mult 阈值）
  - 15M 图上，突破后 pullback_win_4h 根 4H 内，等待价格回踩 box_edge±pullback_tol
  - 15M 收线阳/阴确认后按 close 入场
  - SL = -1.5%, TP = +3.75%（R:R = 2.5）
  - 20x 逐仓
  - 复利仓位：保证金 = 当前余额 × 20%（初期落在 27~30U，符合原方案）
  - 只提现 1 次本金 140U：余额首次 ≥ 280U（本金翻倍）时提出 140U，之后不再提现，其余复利
  - 每周最多 3 单；连亏 2 单该周停手；日亏 >10% 该日停手
  - 浮盈 +N U（默认 主单浮盈达到 保证金×0.55，即约+15U）触发同向加仓
    · 加仓保证金 = 主单保证金 / 4（≈7.5U）
    · 两单 SL 全上移至保本线（entry_price）
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------- 参数 ----------

@dataclass
class Params:
    box_lookback: int = 3
    vol_mult: float = 1.5              # 放量阈值：cur_vol / avg(前3根) 达到
    pullback_tol: float = 0.002        # ±0.2%
    pullback_win_4h: int = 2           # 兼容旧调用（等价 pullback_hours=8）
    pullback_hours: float = 0          # 若>0 则覆盖 pullback_win_4h（更通用）
    sl_pct: float = 0.015
    tp_pct: float = 0.0375
    leverage: int = 20
    position_pct: float = 0.20         # 保证金 = 余额 × 20%
    initial_capital: float = 140.0
    max_trades_per_week: int = 3
    consec_loss_stop: int = 2          # 连亏 2 单该周停
    daily_loss_pct: float = 0.10       # 日亏 >10% 当日停
    pyramid_ratio: float = 0.55        # 主单浮盈达 保证金×N 触发（27.5×0.55≈15U）
    pyramid_frac: float = 0.25         # 加仓保证金 = 主单保证金 × 0.25（≈7.5U）
    withdraw_once_at: float = 280.0    # 首次到达 280U 提出 140U，之后不再提现
    withdraw_amount: float = 140.0
    fee_rt: float = 0.0010             # 来回费用（10bp）


# ---------- 数据 ----------

def load_ohlcv(path: Path, start: str | None, end: str | None) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    if start:
        df = df[df["ts"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df["ts"] < pd.Timestamp(end, tz="UTC")]
    df = df[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    return df


# ---------- 回测状态 ----------

@dataclass
class Trade:
    ts_signal: pd.Timestamp
    ts_entry: pd.Timestamp
    ts_exit: pd.Timestamp
    direction: str
    entry: float
    exit: float
    reason: str
    margin: float
    pnl_u: float
    is_pyramid: bool = False
    week_key: str = ""


@dataclass
class State:
    capital: float
    initial: float
    withdrawn: float = 0.0
    withdraw_done: bool = False
    week_key: Optional[str] = None
    week_trades: int = 0
    week_consec_losses: int = 0
    week_halted: bool = False
    day_key: Optional[str] = None
    day_pnl: float = 0.0
    day_halted: bool = False

    def roll_week(self, wk):
        if self.week_key != wk:
            self.week_key = wk
            self.week_trades = 0
            self.week_consec_losses = 0
            self.week_halted = False

    def roll_day(self, dk):
        if self.day_key != dk:
            self.day_key = dk
            self.day_pnl = 0.0
            self.day_halted = False


# ---------- 15M 入场搜索 ----------

def _find_pullback_entry(bars15m: pd.DataFrame, i0: int, i_end: int, direction: str,
                        box_edge: float, tol: float) -> Optional[tuple[int, float]]:
    lo_edge = box_edge * (1 - tol)
    hi_edge = box_edge * (1 + tol)
    n = len(bars15m)
    for i in range(i0, min(i_end, n)):
        b = bars15m.iloc[i]
        if direction == "long":
            # 回踩到箱顶附近：low 触及区间下沿且 high 未离开区间上沿；15M 收阳
            if b.low <= hi_edge and b.high >= lo_edge and b.close >= b.open:
                return i, float(b.close)
        else:
            if b.high >= lo_edge and b.low <= hi_edge and b.close <= b.open:
                return i, float(b.close)
    return None


def _simulate_exit(bars15m: pd.DataFrame, entry_idx: int, direction: str,
                   entry_price: float, tp_pct: float, sl_pct: float,
                   sl_override: Optional[float] = None) -> tuple[float, str, pd.Timestamp, int]:
    if direction == "long":
        tp = entry_price * (1 + tp_pct)
        sl = sl_override if sl_override is not None else entry_price * (1 - sl_pct)
    else:
        tp = entry_price * (1 - tp_pct)
        sl = sl_override if sl_override is not None else entry_price * (1 + sl_pct)

    reason_sl = "BE" if sl_override is not None else "SL"

    for j in range(entry_idx + 1, len(bars15m)):
        b = bars15m.iloc[j]
        if direction == "long":
            if b.low <= sl:
                return sl, reason_sl, b.ts, j
            if b.high >= tp:
                return tp, "TP", b.ts, j
        else:
            if b.high >= sl:
                return sl, reason_sl, b.ts, j
            if b.low <= tp:
                return tp, "TP", b.ts, j
    last = bars15m.iloc[-1]
    return float(last.close), "EOD", last.ts, len(bars15m) - 1


# ---------- 回测主体 ----------

def backtest(df4h: pd.DataFrame, df15m: pd.DataFrame, p: Params,
             verbose: bool = False) -> tuple[list[Trade], State, list[tuple]]:
    trades: list[Trade] = []
    state = State(capital=p.initial_capital, initial=p.initial_capital)
    equity_curve: list[tuple[pd.Timestamp, float]] = [(df4h.iloc[0]["ts"], p.initial_capital)]

    # 用于快速定位 entry 起点
    ts15m = df15m["ts"]
    # 主时框每根多少秒
    if len(df4h) >= 2:
        main_bar_sec = (df4h["ts"].iloc[1] - df4h["ts"].iloc[0]).total_seconds()
    else:
        main_bar_sec = 4 * 3600.0
    main_bar_td = pd.Timedelta(seconds=main_bar_sec)
    # entry K 每根多少秒（用来把 pullback_hours 转成根数）
    if len(df15m) >= 2:
        entry_bar_sec = (df15m["ts"].iloc[1] - df15m["ts"].iloc[0]).total_seconds()
    else:
        entry_bar_sec = 900.0
    # 优先用 pullback_hours；否则用 pullback_win_4h × 4h
    pullback_hours = p.pullback_hours if p.pullback_hours > 0 else p.pullback_win_4h * 4
    pullback_bars = max(1, int(round(pullback_hours * 3600 / entry_bar_sec)))

    for i in range(p.box_lookback, len(df4h) - 1):
        cur = df4h.iloc[i]
        box = df4h.iloc[i - p.box_lookback:i]
        box_top = float(box["high"].max())
        box_bot = float(box["low"].min())
        vol_avg = float(box["volume"].mean())
        cur_vol = float(cur["volume"])
        if vol_avg <= 0:
            continue
        vol_ratio = cur_vol / vol_avg

        direction = None
        box_edge = None
        if cur["close"] > box_top and vol_ratio >= p.vol_mult:
            direction = "long"
            box_edge = box_top
        elif cur["close"] < box_bot and vol_ratio >= p.vol_mult:
            direction = "short"
            box_edge = box_bot
        if direction is None:
            continue

        # 4H 突破 K 的收盘时间 = 下一根 4H 的开盘时间
        # 15M 搜索窗口从紧邻的下一根 15M 开始
        ts_break_close = cur["ts"] + main_bar_td
        i0 = int(ts15m.searchsorted(ts_break_close))
        if i0 >= len(df15m):
            continue
        # 回踩窗口：按 pullback_hours 折算成 entry K 根数
        i_end = i0 + pullback_bars

        found = _find_pullback_entry(df15m, i0, i_end, direction, box_edge, p.pullback_tol)
        if found is None:
            continue
        entry_idx, entry_price = found
        ts_entry = df15m.iloc[entry_idx]["ts"]

        # ---- 风控 gates ----
        wk = f"{ts_entry.isocalendar().year}-W{ts_entry.isocalendar().week:02d}"
        dk = ts_entry.strftime("%Y-%m-%d")
        state.roll_week(wk)
        state.roll_day(dk)

        if state.week_halted or state.day_halted:
            continue
        if state.week_trades >= p.max_trades_per_week:
            continue

        # ---- 复利仓位 ----
        margin = round(state.capital * p.position_pct, 2)
        if margin < 5:  # 极端情况保护
            continue
        notional = margin * p.leverage

        # ---- 主单模拟 ----
        exit_price, reason, ts_exit, exit_idx = _simulate_exit(
            df15m, entry_idx, direction, entry_price, p.tp_pct, p.sl_pct
        )
        if direction == "long":
            pnl_pct = (exit_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - exit_price) / entry_price
        pnl_u = notional * pnl_pct - notional * p.fee_rt

        # ---- 浮盈加仓 ----
        pyramid_trade: Optional[Trade] = None
        pyramid_margin = round(margin * p.pyramid_frac, 2)
        pyramid_notional = pyramid_margin * p.leverage
        # 主单浮盈达到 margin × pyramid_ratio（约 27.5×0.55≈15U）
        target_pnl_u = margin * p.pyramid_ratio
        trig_pct = target_pnl_u / notional  # = pyramid_ratio / leverage = 0.55/20 = 0.0275
        # 只有主单最终是 TP 才可能触发（否则主单先 SL，加仓不会发生）
        if reason == "TP" and trig_pct < p.tp_pct:
            if direction == "long":
                py_trigger_px = entry_price * (1 + trig_pct)
            else:
                py_trigger_px = entry_price * (1 - trig_pct)
            for k in range(entry_idx + 1, exit_idx + 1):
                b = df15m.iloc[k]
                hit = (direction == "long" and b.high >= py_trigger_px) or \
                      (direction == "short" and b.low <= py_trigger_px)
                if not hit:
                    continue
                # 加仓：k 根内以 py_trigger_px 入场，主单和加仓单 SL 都上移到 entry_price
                py_exit, py_reason, py_ts, _ = _simulate_exit(
                    df15m, k, direction, py_trigger_px,
                    p.tp_pct, p.sl_pct,
                    sl_override=entry_price
                )
                if direction == "long":
                    py_pct = (py_exit - py_trigger_px) / py_trigger_px
                else:
                    py_pct = (py_trigger_px - py_exit) / py_trigger_px
                py_pnl = pyramid_notional * py_pct - pyramid_notional * p.fee_rt
                pyramid_trade = Trade(
                    ts_signal=cur["ts"] + main_bar_td, ts_entry=b.ts, ts_exit=py_ts,
                    direction=direction, entry=py_trigger_px, exit=py_exit,
                    reason=py_reason, margin=pyramid_margin, pnl_u=py_pnl,
                    is_pyramid=True, week_key=wk,
                )
                # 主单从 k 起改用保本 SL 继续跑
                m_exit, m_reason, m_ts, _ = _simulate_exit(
                    df15m, k, direction, entry_price, p.tp_pct, p.sl_pct,
                    sl_override=entry_price
                )
                if direction == "long":
                    new_pct = (m_exit - entry_price) / entry_price
                else:
                    new_pct = (entry_price - m_exit) / entry_price
                pnl_u = notional * new_pct - notional * p.fee_rt
                reason, exit_price, ts_exit = m_reason, m_exit, m_ts
                break

        # ---- 结算 ----
        state.capital += pnl_u
        state.day_pnl += pnl_u
        state.week_trades += 1
        if pnl_u < 0:
            state.week_consec_losses += 1
            if state.week_consec_losses >= p.consec_loss_stop:
                state.week_halted = True
        else:
            state.week_consec_losses = 0
        if state.day_pnl <= -p.daily_loss_pct * state.capital:
            state.day_halted = True

        trades.append(Trade(
            ts_signal=cur["ts"] + main_bar_td, ts_entry=ts_entry, ts_exit=ts_exit,
            direction=direction, entry=entry_price, exit=exit_price,
            reason=reason, margin=margin, pnl_u=pnl_u, is_pyramid=False, week_key=wk,
        ))
        if pyramid_trade is not None:
            state.capital += pyramid_trade.pnl_u
            state.day_pnl += pyramid_trade.pnl_u
            trades.append(pyramid_trade)

        # ---- 提现（只 1 次）----
        if not state.withdraw_done and state.capital >= p.withdraw_once_at:
            state.capital -= p.withdraw_amount
            state.withdrawn = p.withdraw_amount
            state.withdraw_done = True
            if verbose:
                print(f"  [{ts_exit}] 首次达到 {p.withdraw_once_at}U → 提现 {p.withdraw_amount}U")

        equity_curve.append((ts_exit, state.capital + state.withdrawn))

        if verbose:
            tag = "PY" if False else ""
            print(f"[{ts_entry}] {direction:5s} M={margin:5.1f}U @ {entry_price:.2f} "
                  f"-> {reason:3s} @ {exit_price:.2f}  pnl={pnl_u:+7.2f}U  "
                  f"cap={state.capital:7.2f}+{state.withdrawn:.0f}")

    return trades, state, equity_curve


# ---------- 汇报 ----------

def report(trades: list[Trade], state: State, equity_curve: list[tuple], p: Params,
           label: str, start: str, end: str) -> dict:
    main_trades = [t for t in trades if not t.is_pyramid]
    py_trades = [t for t in trades if t.is_pyramid]
    n = len(main_trades)
    if n == 0:
        print(f"\n=== {label} ===\n无成交")
        return {}

    total_pnl = sum(t.pnl_u for t in trades)
    wins = [t for t in main_trades if t.pnl_u > 0]
    losses = [t for t in main_trades if t.pnl_u <= 0]
    tp_ct = sum(1 for t in main_trades if t.reason == "TP")
    sl_ct = sum(1 for t in main_trades if t.reason == "SL")
    be_ct = sum(1 for t in main_trades if t.reason == "BE")

    # 净值曲线 & 最大回撤（含已提现）
    eq_vals = [e for _, e in equity_curve]
    peak = eq_vals[0]
    max_dd = 0.0
    peak_ts = equity_curve[0][0]
    dd_bottom_ts = equity_curve[0][0]
    for ts, e in equity_curve:
        if e > peak:
            peak = e
            peak_ts = ts
        dd = (peak - e) / peak
        if dd > max_dd:
            max_dd = dd
            dd_bottom_ts = ts

    final_equity = state.capital + state.withdrawn
    ret = (final_equity - p.initial_capital) / p.initial_capital

    avg_win = sum(t.pnl_u for t in wins) / len(wins) if wins else 0.0
    avg_loss = -sum(t.pnl_u for t in losses) / len(losses) if losses else 0.0
    pf_num = sum(t.pnl_u for t in wins)
    pf_den = -sum(t.pnl_u for t in losses)
    pf = (pf_num / pf_den) if pf_den > 0 else float("inf")

    span_days = (trades[-1].ts_exit - trades[0].ts_entry).days
    span_days = max(span_days, 1)
    monthly = (1 + ret) ** (30 / span_days) - 1

    print(f"\n=== {label} ({start} ~ {end}) ===")
    print(f"数据段:          {trades[0].ts_entry.date()} ~ {trades[-1].ts_exit.date()} ({span_days} 天)")
    print(f"主单笔数:        {n}   (TP={tp_ct} SL={sl_ct} BE={be_ct})")
    print(f"加仓单笔数:      {len(py_trades)}")
    print(f"胜率:            {len(wins)/n*100:.1f}%")
    print(f"总收益:          {total_pnl:+.2f} U ({ret*100:+.1f}%)")
    print(f"月化(几何):      {monthly*100:+.2f}%")
    print(f"最大回撤:        {max_dd*100:.1f}%")
    print(f"平均盈单:        {avg_win:+.2f} U")
    print(f"平均亏单:        -{avg_loss:.2f} U")
    print(f"盈亏比:          {pf:.2f}")
    print(f"结束状态:        余额 {state.capital:.2f}U + 已提现 {state.withdrawn:.0f}U = {final_equity:.2f}U")
    if state.withdraw_done:
        # 找出提现时间点
        wt = None
        for t in trades:
            # 简化：第一个使 capital 累加过阈值的 trade
            pass
        print(f"提现动作:        余额首次达 {p.withdraw_once_at}U 时提出 {p.withdraw_amount}U（只 1 次）")
    df_m = pd.DataFrame([{"ts": t.ts_exit, "pnl": t.pnl_u} for t in trades])
    df_m["ym"] = df_m["ts"].dt.tz_convert("UTC").dt.strftime("%Y-%m")
    monthly_pnl = df_m.groupby("ym")["pnl"].agg(["sum", "count"]).round(2)
    print("\n月度分布 (U | 单数)：")
    print(monthly_pnl.to_string())

    return {
        "label": label, "n_main": n, "n_pyramid": len(py_trades),
        "win_rate": len(wins) / n, "total_pnl_u": total_pnl,
        "total_return": ret, "monthly_return": monthly, "max_dd": max_dd,
        "profit_factor": pf, "final_capital": state.capital,
        "withdrawn": state.withdrawn, "final_equity": final_equity,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "tp": tp_ct, "sl": sl_ct, "be": be_ct,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar4h", required=True)
    ap.add_argument("--bar15m", required=True)
    ap.add_argument("--start", default=None, help="e.g. 2025-07-01")
    ap.add_argument("--end", default=None, help="e.g. 2026-07-01")
    ap.add_argument("--vol-mult", type=float, default=1.5)
    ap.add_argument("--pullback-tol", type=float, default=0.002)
    ap.add_argument("--pullback-win-4h", type=int, default=2)
    ap.add_argument("--fee", type=float, default=0.0010)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    p = Params(
        vol_mult=args.vol_mult,
        pullback_tol=args.pullback_tol,
        pullback_win_4h=args.pullback_win_4h,
        fee_rt=args.fee,
    )
    df4h = load_ohlcv(Path(args.bar4h), args.start, args.end)
    df15m = load_ohlcv(Path(args.bar15m), args.start, args.end)
    print(f"4H  {args.bar4h}: {len(df4h)} 行, {df4h.ts.min()} ~ {df4h.ts.max()}")
    print(f"15m {args.bar15m}: {len(df15m)} 行, {df15m.ts.min()} ~ {df15m.ts.max()}")
    trades, state, ec = backtest(df4h, df15m, p, verbose=args.verbose)
    label = args.label or Path(args.bar4h).stem.split("_")[0]
    report(trades, state, ec, p, label, args.start or "开头", args.end or "结尾")


if __name__ == "__main__":
    main()
