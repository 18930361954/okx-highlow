"""
140U 策略回测（docs/新项目方案/140U.docx）

规则（原文）：
  - 4H 图：前 3 根 K 线画箱体（高点=箱顶，低点=箱底）
  - 4H 放量突破/跌破箱体 → 切换 15M（本脚本用 1H 近似）
  - 15M 回踩箱顶±0.1~0.2%（做多）或反抽箱底（做空）
  - 15M 收阳/阴确认后入场
  - SL = -1.5%, TP = +3.75%（R:R = 2.5）
  - 20x 逐仓，单笔保证金 27.5U，初始 140U
  - 每周最多 3 单；连亏 2 单该周停手；日亏 >10% 该日停手
  - 浮盈 +15U 时同向加仓 7.5U 保证金，两单 SL 全上移至保本

用法：
  python scripts/backtest_140u.py --csv csv_data/BTC_USDT_SWAP_1H_12m.csv
  python scripts/backtest_140u.py --csv csv_data/ETH_USDT_SWAP_1H_12m.csv --vol-mult 1.5

假设（原方案未给量化定义处）：
  - "放量"：当前 4H vol >= 前 3 根 4H 均值 × VOL_MULT（默认 1.5）
  - "回踩窗口"：突破后 8 小时内（2 根 4H）必须触及 box_edge ± PULLBACK_TOL
  - "15M 确认"：用 1H 代替；触及箱边的那根 1H 若收线 close>=open（做多）/close<=open（做空）即视为确认
  - 手续费：单边 5bp（taker），总来回 10bp（可通过 --fee 调）
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------- 参数 ----------

@dataclass
class Params:
    box_lookback: int = 3           # 前 N 根 4H 画箱体
    vol_mult: float = 1.5           # "放量" 阈值：cur_vol / avg(box)
    pullback_tol: float = 0.002     # 回踩容差（±0.2%）
    pullback_win_4h: int = 2        # 突破后允许 N 根 4H 内出现回踩
    sl_pct: float = 0.015           # 止损 1.5%
    tp_pct: float = 0.0375          # 止盈 3.75%
    leverage: int = 20
    margin_per_trade: float = 27.5  # 单笔保证金 U
    initial_capital: float = 140.0
    max_trades_per_week: int = 3
    consec_loss_stop: int = 2       # 连亏 2 单该周停
    daily_loss_pct: float = 0.10    # 日亏 >10% 该日停手
    pyramid_trigger_u: float = 15.0 # 浮盈达到 +N U 触发加仓
    pyramid_margin_u: float = 7.5   # 加仓保证金
    withdraw_at: float = 300.0      # 余额到 300U "提现" 100U
    withdraw_amount: float = 100.0
    fee_rt: float = 0.0010          # 来回手续费（10bp）


# ---------- 数据 ----------

def load_1h(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return df[["ts", "open", "high", "low", "close", "volume"]]


def resample_4h(df1h: pd.DataFrame) -> pd.DataFrame:
    d = df1h.set_index("ts")
    o = d["open"].resample("4h", origin="epoch").first()
    h = d["high"].resample("4h", origin="epoch").max()
    l = d["low"].resample("4h", origin="epoch").min()
    c = d["close"].resample("4h", origin="epoch").last()
    v = d["volume"].resample("4h", origin="epoch").sum()
    out = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}).dropna().reset_index()
    return out


# ---------- 回测 ----------

@dataclass
class Trade:
    ts_signal: pd.Timestamp     # 4H 突破 K 的收盘时间
    ts_entry: pd.Timestamp      # 1H 确认入场时间
    ts_exit: pd.Timestamp
    direction: str              # 'long' / 'short'
    entry: float
    exit: float
    reason: str                 # 'TP' / 'SL' / 'BE' / 'EOD'
    margin: float
    pnl_u: float                # 净 U（含费）
    is_pyramid: bool = False
    week_key: str = ""


@dataclass
class State:
    capital: float = 140.0
    week_key: Optional[str] = None
    week_trades: int = 0
    week_consec_losses: int = 0
    week_halted: bool = False
    day_key: Optional[str] = None
    day_pnl: float = 0.0
    day_halted: bool = False
    withdrawn: float = 0.0

    def roll_week(self, wk: str):
        if self.week_key != wk:
            self.week_key = wk
            self.week_trades = 0
            self.week_consec_losses = 0
            self.week_halted = False

    def roll_day(self, dk: str):
        if self.day_key != dk:
            self.day_key = dk
            self.day_pnl = 0.0
            self.day_halted = False


def _find_pullback_entry(bars1h: pd.DataFrame, i0: int, i_end: int, direction: str,
                        box_edge: float, tol: float) -> Optional[tuple[int, float]]:
    """
    在 bars1h[i0:i_end] 内寻找第一根：
      - long: low 触及 box_edge*(1-tol) ~ box_edge*(1+tol) 且 close>=open
      - short: high 触及 box_edge*(1-tol) ~ box_edge*(1+tol) 且 close<=open
    返回 (idx, entry_price=收盘)；未找到 None
    """
    lo_edge = box_edge * (1 - tol)
    hi_edge = box_edge * (1 + tol)
    for i in range(i0, min(i_end, len(bars1h))):
        b = bars1h.iloc[i]
        if direction == "long":
            # 回踩到箱顶附近：low <= hi_edge（跌至或穿过区间）
            if b.low <= hi_edge and b.high >= lo_edge and b.close >= b.open:
                return i, float(b.close)
        else:
            if b.high >= lo_edge and b.low <= hi_edge and b.close <= b.open:
                return i, float(b.close)
    return None


def _simulate_exit(bars1h: pd.DataFrame, entry_idx: int, direction: str,
                   entry_price: float, tp_pct: float, sl_pct: float,
                   sl_override: Optional[float] = None) -> tuple[float, str, pd.Timestamp, int]:
    """从入场后一根 1H 开始逐根扫 TP/SL；同根双穿 → SL 优先。"""
    if direction == "long":
        tp = entry_price * (1 + tp_pct)
        sl = sl_override if sl_override is not None else entry_price * (1 - sl_pct)
    else:
        tp = entry_price * (1 - tp_pct)
        sl = sl_override if sl_override is not None else entry_price * (1 + sl_pct)

    for j in range(entry_idx + 1, len(bars1h)):
        b = bars1h.iloc[j]
        if direction == "long":
            if b.low <= sl:
                return sl, ("SL" if sl_override is None else "BE"), b.ts, j
            if b.high >= tp:
                return tp, "TP", b.ts, j
        else:
            if b.high >= sl:
                return sl, ("SL" if sl_override is None else "BE"), b.ts, j
            if b.low <= tp:
                return tp, "TP", b.ts, j
    # 数据结束
    last = bars1h.iloc[-1]
    return float(last.close), "EOD", last.ts, len(bars1h) - 1


def backtest(df1h: pd.DataFrame, p: Params, verbose: bool = False) -> tuple[list[Trade], State]:
    df4h = resample_4h(df1h)
    trades: list[Trade] = []
    state = State(capital=p.initial_capital)

    # 4H 索引 → 1H 起始索引（后一根 1H 开始判回踩）
    ts_to_1h_idx = pd.Series(df1h.index, index=df1h["ts"]).to_dict()

    for i in range(p.box_lookback, len(df4h) - 1):
        cur = df4h.iloc[i]
        box = df4h.iloc[i - p.box_lookback:i]
        box_top = float(box["high"].max())
        box_bot = float(box["low"].min())
        vol_avg = float(box["volume"].mean())
        cur_vol = float(cur["volume"])
        vol_ok = vol_avg > 0 and (cur_vol / vol_avg) >= p.vol_mult

        # 方向判定
        direction = None
        box_edge = None
        if cur["close"] > box_top and vol_ok:
            direction = "long"
            box_edge = box_top
        elif cur["close"] < box_bot and vol_ok:
            direction = "short"
            box_edge = box_bot
        if direction is None:
            continue

        # 4H 突破 K 的收盘时间
        ts_close_4h = cur["ts"]
        # 找到对应 1H 起点：≥ ts_close_4h 的第一根 1H
        i0 = df1h["ts"].searchsorted(ts_close_4h)
        if i0 >= len(df1h):
            continue
        i_end = i0 + p.pullback_win_4h * 4   # 4 根 1H = 4H

        # 若价格直接飞走没回踩 → 由 _find_pullback_entry 自然返回 None
        # 但方案里的"飞走"更严格：突破后马上拉飞。以回踩容差为准。
        found = _find_pullback_entry(df1h, i0, i_end, direction, box_edge, p.pullback_tol)
        if found is None:
            continue
        entry_idx, entry_price = found
        ts_entry = df1h.iloc[entry_idx]["ts"]

        # ---- 风控 gates ----
        wk = f"{ts_entry.isocalendar().year}-W{ts_entry.isocalendar().week:02d}"
        dk = ts_entry.strftime("%Y-%m-%d")
        state.roll_week(wk)
        state.roll_day(dk)

        if state.week_halted or state.day_halted:
            continue
        if state.week_trades >= p.max_trades_per_week:
            continue

        # ---- 主单模拟 ----
        exit_price, reason, ts_exit, exit_idx = _simulate_exit(
            df1h, entry_idx, direction, entry_price, p.tp_pct, p.sl_pct
        )
        notional = p.margin_per_trade * p.leverage
        if direction == "long":
            pnl_pct = (exit_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - exit_price) / entry_price
        pnl_u = notional * pnl_pct - notional * p.fee_rt

        # ---- 加仓判定：观察从入场后到主单退出前，是否浮盈曾达到 +15U ----
        pyramid_trade: Optional[Trade] = None
        pyramid_trigger_price = None
        if reason == "TP":
            # 检查是否在 TP 前先达到 +15U
            # +15U 需要 pnl_pct = 15 / notional
            trig_pct = p.pyramid_trigger_u / notional
            if trig_pct < p.tp_pct:
                if direction == "long":
                    pyramid_trigger_price = entry_price * (1 + trig_pct)
                else:
                    pyramid_trigger_price = entry_price * (1 - trig_pct)
                # 找到浮盈到达 +15U 的 1H
                for k in range(entry_idx + 1, exit_idx + 1):
                    b = df1h.iloc[k]
                    hit = (direction == "long" and b.high >= pyramid_trigger_price) or \
                          (direction == "short" and b.low <= pyramid_trigger_price)
                    if hit:
                        # 加仓：以 pyramid_trigger_price 入场，主单 SL 上移到 entry_price（保本）
                        # 加仓 SL 也是 entry_price（相对加仓价而言是亏 trig_pct）
                        py_notional = p.pyramid_margin_u * p.leverage
                        py_exit, py_reason, py_ts, py_idx = _simulate_exit(
                            df1h, k, direction, pyramid_trigger_price,
                            p.tp_pct, p.sl_pct,
                            sl_override=entry_price  # 保本线
                        )
                        if direction == "long":
                            py_pct = (py_exit - pyramid_trigger_price) / pyramid_trigger_price
                        else:
                            py_pct = (pyramid_trigger_price - py_exit) / pyramid_trigger_price
                        py_pnl = py_notional * py_pct - py_notional * p.fee_rt
                        pyramid_trade = Trade(
                            ts_signal=ts_close_4h, ts_entry=b.ts, ts_exit=py_ts,
                            direction=direction, entry=pyramid_trigger_price, exit=py_exit,
                            reason=py_reason, margin=p.pyramid_margin_u, pnl_u=py_pnl,
                            is_pyramid=True, week_key=wk,
                        )
                        # 主单 SL 也升到 entry_price；主单已按原 TP/SL 模拟，
                        # 需重跑：从 entry_idx 起，用 sl_override=entry_price（但只在 k 之后生效）
                        # 简化：主单在 k 之前不可能触 SL（因为已到 +15U）；k 之后 SL 若被触到，
                        # 会按保本触发（gain=0）。这里重跑主单从 k 起、sl_override=entry_price。
                        m_exit, m_reason, m_ts, _ = _simulate_exit(
                            df1h, k, direction, entry_price, p.tp_pct, p.sl_pct,
                            sl_override=entry_price
                        )
                        # 主单结算：从 entry 到 m_exit
                        if direction == "long":
                            new_pct = (m_exit - entry_price) / entry_price
                        else:
                            new_pct = (entry_price - m_exit) / entry_price
                        pnl_u = notional * new_pct - notional * p.fee_rt
                        reason = m_reason
                        exit_price, ts_exit = m_exit, m_ts
                        break

        state.capital += pnl_u
        state.day_pnl += pnl_u
        state.week_trades += 1
        if pnl_u < 0:
            state.week_consec_losses += 1
            if state.week_consec_losses >= p.consec_loss_stop:
                state.week_halted = True
        else:
            state.week_consec_losses = 0
        # 日风控
        if state.day_pnl <= -p.daily_loss_pct * (p.initial_capital + state.withdrawn):
            state.day_halted = True
        # 提现
        while state.capital + state.withdrawn - state.withdrawn >= p.withdraw_at:
            # 达到 300U 提 100U，剩余继续
            if state.capital >= p.withdraw_at:
                state.capital -= p.withdraw_amount
                state.withdrawn += p.withdraw_amount
            else:
                break

        trades.append(Trade(
            ts_signal=ts_close_4h, ts_entry=ts_entry, ts_exit=ts_exit,
            direction=direction, entry=entry_price, exit=exit_price,
            reason=reason, margin=p.margin_per_trade, pnl_u=pnl_u,
            is_pyramid=False, week_key=wk,
        ))
        if pyramid_trade is not None:
            state.capital += pyramid_trade.pnl_u
            state.day_pnl += pyramid_trade.pnl_u
            trades.append(pyramid_trade)

        if verbose:
            print(f"[{ts_entry}] {direction} @ {entry_price:.2f} -> {reason} @ {exit_price:.2f} "
                  f"pnl={pnl_u:+.2f}U cap={state.capital:.2f}")

    return trades, state


# ---------- 汇报 ----------

def report(trades: list[Trade], state: State, p: Params, label: str):
    main_trades = [t for t in trades if not t.is_pyramid]
    py_trades = [t for t in trades if t.is_pyramid]
    n = len(main_trades)
    if n == 0:
        print(f"\n=== {label} ===")
        print("无成交")
        return

    total_pnl = sum(t.pnl_u for t in trades)
    wins = [t for t in main_trades if t.pnl_u > 0]
    losses = [t for t in main_trades if t.pnl_u <= 0]
    tp_ct = sum(1 for t in main_trades if t.reason == "TP")
    sl_ct = sum(1 for t in main_trades if t.reason == "SL")
    be_ct = sum(1 for t in main_trades if t.reason == "BE")

    # 净值曲线 & 最大回撤（含起始 capital + 提现）
    equity = [p.initial_capital]
    for t in trades:
        equity.append(equity[-1] + t.pnl_u)
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        peak = max(peak, e)
        dd = (peak - e) / peak
        max_dd = max(max_dd, dd)

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

    print(f"\n=== {label} ===")
    print(f"周期: {trades[0].ts_entry.date()} ~ {trades[-1].ts_exit.date()} ({span_days} 天)")
    print(f"主单笔数:        {n}   (TP={tp_ct} SL={sl_ct} BE={be_ct})")
    print(f"加仓单笔数:      {len(py_trades)}")
    print(f"胜率:            {len(wins)/n*100:.1f}%")
    print(f"总收益:          {total_pnl:+.2f} U ({ret*100:+.1f}%)")
    print(f"月化收益:        {monthly*100:+.1f}%")
    print(f"最大回撤:        {max_dd*100:.1f}%")
    print(f"平均盈单:        {avg_win:+.2f} U")
    print(f"平均亏单:        {-avg_loss:+.2f} U")
    print(f"盈亏比:          {pf:.2f}")
    print(f"结束净值:        {state.capital:.2f} U（+已提现 {state.withdrawn:.0f}U）")
    # 月度分布
    df = pd.DataFrame([{"ts": t.ts_exit, "pnl": t.pnl_u} for t in trades])
    df["ym"] = df["ts"].dt.to_period("M")
    monthly_pnl = df.groupby("ym")["pnl"].agg(["sum", "count"])
    print("\n月度分布：")
    print(monthly_pnl.to_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
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
    df = load_1h(Path(args.csv))
    print(f"数据 {args.csv}: {len(df)} 行 1H, {df.ts.min()} ~ {df.ts.max()}")
    trades, state = backtest(df, p, verbose=args.verbose)
    label = args.label or Path(args.csv).stem
    report(trades, state, p, label)


if __name__ == "__main__":
    main()
