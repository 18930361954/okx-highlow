"""
布林带均值回归策略回测。

规则：
  - 布林带（默认 20 周期, 2σ）
  - "整根 K 超出布林带" 定义：
      · 做空信号：K.low > upper_band[K]  （整根在上轨之上）
      · 做多信号：K.high < lower_band[K] （整根在下轨之下）
  - 信号 K 收线后，挂反向限价单：
      · 做空：以 K.high 挂空
      · 做多：以 K.low 挂多
  - 挂单有效期：order_ttl 根（默认 16 根 = 15m×16 = 4h）
  - 触发后 SL=0.5%，TP=1.1%
  - 100x 逐仓，每单保证金 = 当前余额 × 20%
  - 初始 146U

用法：
  python scripts/backtest_bb.py --csv csv_data/BTC_USDT_SWAP_15m_400d.csv \
      --start 2025-07-01 --end 2026-07-01

假设：
  - 手续费 fee_rt（默认 0.001 = 10bp 来回）
  - 同根 K 内若 SL/TP 同时被触及 → SL 优先（保守）
  - 主单被 SL 或 TP 之前，同方向可能再次出现挂单信号 → 有持仓时忽略新信号
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------- 参数 ----------

@dataclass
class Params:
    bb_period: int = 20
    bb_std: float = 2.0
    sl_pct: float = 0.005          # 0.5%
    tp_pct: float = 0.011          # 1.1%
    leverage: int = 100
    position_pct: float = 0.20     # 保证金 = 余额 × 20%
    initial_capital: float = 146.0
    order_ttl_bars: int = 16       # 挂单有效期（K线根数）
    fee_rt: float = 0.0010         # 来回费用 10bp
    max_concurrent: int = 1        # 同时持仓上限


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


def add_bb(df: pd.DataFrame, period: int, std: float) -> pd.DataFrame:
    m = df["close"].rolling(period).mean()
    s = df["close"].rolling(period).std(ddof=0)
    df = df.copy()
    df["bb_mid"] = m
    df["bb_up"] = m + std * s
    df["bb_lo"] = m - std * s
    return df


# ---------- 回测 ----------

@dataclass
class Trade:
    ts_signal: pd.Timestamp
    ts_entry: pd.Timestamp
    ts_exit: pd.Timestamp
    direction: str
    entry: float
    exit: float
    sl: float
    tp: float
    reason: str
    margin: float
    pnl_u: float


def backtest(df: pd.DataFrame, p: Params, verbose: bool = False) -> tuple[list[Trade], float, list[tuple]]:
    df = add_bb(df, p.bb_period, p.bb_std)
    trades: list[Trade] = []
    capital = p.initial_capital
    equity_curve = [(df["ts"].iloc[p.bb_period], capital)]

    # 挂单队列（每个订单 = {direction, entry_px, sl, tp, expire_idx, margin, ts_signal}）
    pending: list[dict] = []
    # 当前持仓
    position: Optional[dict] = None

    n = len(df)
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    open_ = df["open"].to_numpy()
    close = df["close"].to_numpy()
    ts = df["ts"].to_numpy()
    up = df["bb_up"].to_numpy()
    lo = df["bb_lo"].to_numpy()

    for i in range(p.bb_period, n):
        # ---- 若有持仓：先在本根内判 SL/TP ----
        if position is not None:
            direction = position["direction"]
            sl = position["sl"]; tp = position["tp"]
            hi = high[i]; ll = low[i]
            hit_sl = (direction == "long" and ll <= sl) or (direction == "short" and hi >= sl)
            hit_tp = (direction == "long" and hi >= tp) or (direction == "short" and ll <= tp)
            exit_px = None; reason = None
            if hit_sl and hit_tp:
                # 同根双穿 → SL 优先
                exit_px, reason = sl, "SL"
            elif hit_sl:
                exit_px, reason = sl, "SL"
            elif hit_tp:
                exit_px, reason = tp, "TP"
            if exit_px is not None:
                notional = position["margin"] * p.leverage
                if direction == "long":
                    pnl_pct = (exit_px - position["entry"]) / position["entry"]
                else:
                    pnl_pct = (position["entry"] - exit_px) / position["entry"]
                pnl_u = notional * pnl_pct - notional * p.fee_rt
                capital += pnl_u
                trades.append(Trade(
                    ts_signal=position["ts_signal"], ts_entry=position["ts_entry"],
                    ts_exit=pd.Timestamp(ts[i]),
                    direction=direction, entry=position["entry"], exit=exit_px,
                    sl=sl, tp=tp, reason=reason,
                    margin=position["margin"], pnl_u=pnl_u,
                ))
                equity_curve.append((pd.Timestamp(ts[i]), capital))
                if verbose:
                    print(f"  [{ts[i]}] {direction} exit {reason} @ {exit_px:.2f} pnl={pnl_u:+.2f} cap={capital:.2f}")
                position = None

        # ---- 挂单：过期清理 ----
        pending = [o for o in pending if o["expire_idx"] > i]

        # ---- 挂单：检查是否触发（本根）----
        if position is None and pending:
            still = []
            for o in pending:
                hi = high[i]; ll = low[i]
                triggered = False
                fill_px = None
                if o["direction"] == "short" and hi >= o["entry_px"]:
                    triggered = True; fill_px = o["entry_px"]
                if o["direction"] == "long" and ll <= o["entry_px"]:
                    triggered = True; fill_px = o["entry_px"]
                if triggered and position is None:
                    # 建仓
                    if o["direction"] == "long":
                        sl = fill_px * (1 - p.sl_pct)
                        tp = fill_px * (1 + p.tp_pct)
                    else:
                        sl = fill_px * (1 + p.sl_pct)
                        tp = fill_px * (1 - p.tp_pct)
                    position = dict(
                        direction=o["direction"], entry=fill_px,
                        margin=o["margin"], sl=sl, tp=tp,
                        ts_signal=o["ts_signal"], ts_entry=pd.Timestamp(ts[i]),
                    )
                    # 触发这根 K 内也可能直接 SL/TP —— 但触发价即入场价，
                    # SL/TP 是相对 fill_px 的 ±，本根若极端波动可能击中。
                    hi = high[i]; ll = low[i]
                    hit_sl_now = (o["direction"] == "long" and ll <= sl) or \
                                 (o["direction"] == "short" and hi >= sl)
                    hit_tp_now = (o["direction"] == "long" and hi >= tp) or \
                                 (o["direction"] == "short" and ll <= tp)
                    if hit_sl_now or hit_tp_now:
                        if hit_sl_now:
                            exit_px, reason = sl, "SL"
                        else:
                            exit_px, reason = tp, "TP"
                        notional = position["margin"] * p.leverage
                        if o["direction"] == "long":
                            pnl_pct = (exit_px - fill_px) / fill_px
                        else:
                            pnl_pct = (fill_px - exit_px) / fill_px
                        pnl_u = notional * pnl_pct - notional * p.fee_rt
                        capital += pnl_u
                        trades.append(Trade(
                            ts_signal=o["ts_signal"], ts_entry=pd.Timestamp(ts[i]),
                            ts_exit=pd.Timestamp(ts[i]),
                            direction=o["direction"], entry=fill_px, exit=exit_px,
                            sl=sl, tp=tp, reason=reason,
                            margin=position["margin"], pnl_u=pnl_u,
                        ))
                        equity_curve.append((pd.Timestamp(ts[i]), capital))
                        position = None
                    # 已建仓/或已平仓，不保留该订单
                else:
                    still.append(o)
            pending = still

        # ---- 生成新信号（用第 i 根 K 判断，挂单从 i+1 起生效）----
        if np.isnan(up[i]) or np.isnan(lo[i]):
            continue
        signal_dir = None
        entry_px = None
        # 整根 K 在上轨之上 → 空信号
        if low[i] > up[i]:
            signal_dir = "short"; entry_px = high[i]
        elif high[i] < lo[i]:
            signal_dir = "long"; entry_px = low[i]

        if signal_dir is not None:
            # 只在无持仓且无同向未触发挂单时新挂
            if position is None:
                margin = round(capital * p.position_pct, 2)
                if margin >= 5:
                    pending.append(dict(
                        direction=signal_dir, entry_px=float(entry_px),
                        expire_idx=i + p.order_ttl_bars, margin=margin,
                        ts_signal=pd.Timestamp(ts[i]),
                    ))

    return trades, capital, equity_curve


# ---------- 汇报 ----------

def report(trades: list[Trade], final_cap: float, ec: list, p: Params,
           label: str) -> dict:
    n = len(trades)
    if n == 0:
        print(f"\n=== {label} ===\n无成交")
        return dict(label=label, n=0)
    wins = [t for t in trades if t.pnl_u > 0]
    losses = [t for t in trades if t.pnl_u <= 0]
    tp_ct = sum(1 for t in trades if t.reason == "TP")
    sl_ct = sum(1 for t in trades if t.reason == "SL")

    eq_vals = [e for _, e in ec]
    peak = eq_vals[0]; mdd = 0.0
    for e in eq_vals:
        peak = max(peak, e); mdd = max(mdd, (peak - e) / peak)

    total_pnl = sum(t.pnl_u for t in trades)
    ret = (final_cap - p.initial_capital) / p.initial_capital
    span_days = (trades[-1].ts_exit - trades[0].ts_entry).days
    span_days = max(span_days, 1)
    monthly = (1 + ret) ** (30 / span_days) - 1

    avg_win = sum(t.pnl_u for t in wins) / len(wins) if wins else 0
    avg_loss = -sum(t.pnl_u for t in losses) / len(losses) if losses else 0
    pf_num = sum(t.pnl_u for t in wins)
    pf_den = -sum(t.pnl_u for t in losses)
    pf = (pf_num / pf_den) if pf_den > 0 else 99.0

    print(f"\n=== {label} ===")
    print(f"周期:            {trades[0].ts_entry.date()} ~ {trades[-1].ts_exit.date()} ({span_days} 天)")
    print(f"成交笔数:        {n}   (TP={tp_ct} SL={sl_ct})")
    print(f"胜率:            {len(wins)/n*100:.1f}%")
    print(f"总收益:          {total_pnl:+.2f} U ({ret*100:+.1f}%)")
    print(f"月化(几何):      {monthly*100:+.2f}%")
    print(f"最大回撤:        {mdd*100:.1f}%")
    print(f"平均盈单:        {avg_win:+.2f} U")
    print(f"平均亏单:        -{avg_loss:.2f} U")
    print(f"盈亏比:          {pf:.2f}")
    print(f"结束余额:        {final_cap:.2f} U")

    return dict(
        label=label, n=n, wr=len(wins)/n, ret=ret, monthly=monthly,
        mdd=mdd, pf=pf, tp=tp_ct, sl=sl_ct,
        avg_win=avg_win, avg_loss=avg_loss, final=final_cap,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--bb-period", type=int, default=20)
    ap.add_argument("--bb-std", type=float, default=2.0)
    ap.add_argument("--sl", type=float, default=0.005)
    ap.add_argument("--tp", type=float, default=0.011)
    ap.add_argument("--leverage", type=int, default=100)
    ap.add_argument("--pos-pct", type=float, default=0.20)
    ap.add_argument("--capital", type=float, default=146.0)
    ap.add_argument("--ttl", type=int, default=16)
    ap.add_argument("--fee", type=float, default=0.0010)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    p = Params(
        bb_period=args.bb_period, bb_std=args.bb_std,
        sl_pct=args.sl, tp_pct=args.tp,
        leverage=args.leverage, position_pct=args.pos_pct,
        initial_capital=args.capital, order_ttl_bars=args.ttl,
        fee_rt=args.fee,
    )
    df = load_ohlcv(Path(args.csv), args.start, args.end)
    print(f"数据 {args.csv}: {len(df)} 行, {df.ts.min()} ~ {df.ts.max()}")
    trades, cap, ec = backtest(df, p, verbose=args.verbose)
    label = args.label or Path(args.csv).stem
    report(trades, cap, ec, p, label)


if __name__ == "__main__":
    main()
