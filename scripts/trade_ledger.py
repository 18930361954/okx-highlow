"""逐笔流水导出: 在 strategy_lab 的 simulate_mode 口径上,把每笔交易连同
「开仓时的行情状态」一起吐出来,用于归因分析(哪种行情结构在亏钱)。

与 strategy_lab.simulate_mode 逐位对齐: 同根 K 先 SL 保守判定、EOB 收盘平、
成本 = taker 5bp×2 + 滑点 + funding。差异仅在于额外记录每笔的上下文列。

行情状态标签 (开仓桶前视, 无未来信息):
  ret_n     前 N 桶累计涨跌幅 (方向性强度)
  adx_like  前 N 桶「净位移/路径长度」比 = 单边度 (0=纯震荡, 1=纯单边)
  atr_pct   前 N 桶平均真实波幅 / 收盘价 (波动水平)
  regime    单边上涨 / 单边下跌 / 震荡 (由 adx_like + ret_n 联合判定)

用法:
  python scripts/trade_ledger.py --pair ETH-USDT-SWAP --signal 6H --mode trend \
      --float 0.003 --tp 0.008 --sl 0.030 --out reports/ledger_A_ETH.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bucket_backtest import (  # noqa: E402
    BASE_BAR_SECS, RESAMPLE_RULE, _pick_base_bar,
)

# 单边度阈值: 净位移/路径长度。实测 6H 桶上 0.35 能把单边段和震荡段分开。
TREND_THRESHOLD = 0.35
# 方向性阈值: 前 N 桶累计涨跌幅超过这个绝对值才算「有方向」
RET_THRESHOLD = 0.02
# 回看桶数 (计算 regime 用)
LOOKBACK = 6


def load_csv(pair: str, base_bar: str, days: int, datadir: str) -> pd.DataFrame:
    coin = pair.split("-")[0]
    path = ROOT / datadir / f"{coin}_USDT_SWAP_{base_bar}_{days}d.csv"
    if not path.exists():
        raise FileNotFoundError(str(path))
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True).set_index("ts")
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df


def resample(df: pd.DataFrame, signal_bar: str) -> pd.DataFrame:
    return df.resample(RESAMPLE_RULE[signal_bar], label="left", closed="left").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    ).dropna()


def compute_regime(sig_o, sig_h, sig_l, sig_c, i: int) -> dict:
    """位置 i (信号桶) 的行情状态, 只用 [i-LOOKBACK+1, i] 的已收盘数据。"""
    lo = max(0, i - LOOKBACK + 1)
    seg_c = sig_c[lo:i + 1]
    seg_h = sig_h[lo:i + 1]
    seg_l = sig_l[lo:i + 1]
    seg_o = sig_o[lo:i + 1]
    if len(seg_c) < 2:
        return {"ret_n": 0.0, "adx_like": 0.0, "atr_pct": 0.0, "regime": "unknown"}

    net = seg_c[-1] - seg_o[0]
    ret_n = net / seg_o[0] if seg_o[0] > 0 else 0.0
    # 路径长度 = 逐桶收盘绝对变动之和, 净位移/路径 = 单边度
    path = float(np.abs(np.diff(seg_c)).sum()) + abs(seg_c[0] - seg_o[0])
    adx_like = abs(net) / path if path > 0 else 0.0
    atr_pct = float(np.mean(seg_h - seg_l) / seg_c[-1]) if seg_c[-1] > 0 else 0.0

    if adx_like >= TREND_THRESHOLD and ret_n >= RET_THRESHOLD:
        regime = "单边上涨"
    elif adx_like >= TREND_THRESHOLD and ret_n <= -RET_THRESHOLD:
        regime = "单边下跌"
    else:
        regime = "震荡"
    return {"ret_n": round(ret_n, 5), "adx_like": round(adx_like, 4),
            "atr_pct": round(atr_pct, 5), "regime": regime}


def run(df_base, df_sig, pair, signal_bar, mode, float_pct, tp_pct, sl_pct,
        initial_balance=148.0, position_pct=0.10, leverage=100,
        fixed_margin=True, taker_fee=0.0005, slippage_bps=10.0,
        funding_bps_per_8h=3.0, max_margin=1500.0,
        stop_on_ruin=True, hold_beyond_bucket=False,
        skip_while_open=False, max_hold_hours=None,
        block_counter_trend=False, trend_gate=TREND_THRESHOLD,
        ret_gate=RET_THRESHOLD) -> list[dict]:
    """返回逐笔流水。口径与 strategy_lab.simulate_mode 对齐。

    stop_on_ruin=True 复现 strategy_lab 的爆仓即停(用于对齐校验);
    归因分析用 False + 看 ret_pct/r_mult 列(与仓位大小无关)。

    hold_beyond_bucket: 回测默认桶末收盘平仓(EOB); 实盘 daily_cancel 只撤未成交挂单,
      已成交持仓不平, 一直持到 TP/SL (order_manager.cancel_all_pending 注释明确)。
      置 True 复现实盘行为 —— 单边行情下这是回测与实盘的核心分歧点。
    skip_while_open: 实盘同 pair 有持仓时跳过新桶挂单 (`[skip] 当前有持仓,暂不挂单`)。

    修复候选:
    max_hold_hours: 超时强平 (给定小时数后按现价市价平)。把回测的 EOB 语义
      以「时间止损」形式搬到实盘, 不依赖桶边界。
    block_counter_trend: 单边行情中禁止逆势腿 (上涨段不开空/下跌段不开多),
      顺势腿照常。trend_gate/ret_gate 为判定阈值。"""
    n = len(df_sig)
    if n < 2:
        return []

    sig_o = df_sig["open"].to_numpy()
    sig_c = df_sig["close"].to_numpy()
    sig_h = df_sig["high"].to_numpy()
    sig_l = df_sig["low"].to_numpy()
    sig_ts_ms = df_sig.index.tz_convert("UTC").tz_localize(None) \
        .astype("datetime64[ms]").view("int64")
    base_ts_ms = df_base.index.tz_convert("UTC").tz_localize(None) \
        .astype("datetime64[ms]").view("int64")
    base_h = df_base["high"].to_numpy()
    base_l = df_base["low"].to_numpy()
    base_c = df_base["close"].to_numpy()
    base_ts_secs = base_ts_ms // 1000
    bucket_ms = BASE_BAR_SECS[signal_bar] * 1000

    bar_dir = np.zeros(n, dtype=np.int8)
    bar_dir[sig_c > sig_o] = 1
    bar_dir[sig_c < sig_o] = -1

    start_ms = sig_ts_ms[1:]
    lo_arr = np.searchsorted(base_ts_ms, start_ms, side="left")
    hi_arr = np.searchsorted(base_ts_ms, start_ms + bucket_ms, side="left")

    slippage = slippage_bps * 1e-4
    funding = funding_bps_per_8h * 1e-4
    balance = initial_balance
    rows: list[dict] = []
    n_base = len(base_c)
    busy_until = -1  # 持仓占用到的 base K 索引 (skip_while_open 用)

    for i in range(n - 1):
        if bar_dir[i] == 0:
            continue
        d_prev = int(bar_dir[i])
        lo, hi = int(lo_arr[i]), int(hi_arr[i])
        if lo >= hi:
            continue
        # 实盘: 同 pair 有持仓时该桶不挂单 (`[skip] 当前有持仓,暂不挂单`)
        if skip_while_open and lo < busy_until:
            continue

        # regime 只用 [i-LOOKBACK+1, i] 已收盘数据, 可在入场决策前算 (无未来信息)
        reg = compute_regime(sig_o, sig_h, sig_l, sig_c, i)

        # 单边行情中被禁的方向: 上涨段禁空(-1), 下跌段禁多(+1), 0=不禁
        banned = 0
        if block_counter_trend and reg["adx_like"] >= trend_gate:
            if reg["ret_n"] >= ret_gate:
                banned = -1
            elif reg["ret_n"] <= -ret_gate:
                banned = 1

        if mode == "trend":
            d = d_prev
            entry = sig_l[i] * (1 - float_pct) if d == 1 else sig_h[i] * (1 + float_pct)
        elif mode == "reversal":
            d = -d_prev
            entry = sig_h[i] * (1 + float_pct) if d == -1 else sig_l[i] * (1 - float_pct)
        elif mode == "fade":
            e_long = sig_l[i] * (1 - float_pct)
            e_short = sig_h[i] * (1 + float_pct)
            hit_l = base_l[lo:hi] <= e_long
            hit_s = base_h[lo:hi] >= e_short
            fl = int(hit_l.argmax()) if hit_l.any() else -1
            fs = int(hit_s.argmax()) if hit_s.any() else -1
            if banned == 1:
                fl = -1
            elif banned == -1:
                fs = -1
            if fl == -1 and fs == -1:
                continue
            if fs == -1 or (fl != -1 and fl <= fs):
                d, entry = 1, e_long
            else:
                d, entry = -1, e_short
        else:
            raise ValueError(f"unknown mode {mode}")

        if d == banned:
            continue

        hit_arr = (base_l[lo:hi] <= entry) if d == 1 else (base_h[lo:hi] >= entry)
        if not hit_arr.any():
            continue
        ek = lo + int(hit_arr.argmax())

        # 平仓搜索窗口: 回测到桶末(hi)截止; 实盘持仓不受桶末影响, 一直持到 TP/SL
        xh = n_base if hold_beyond_bucket else hi
        # 超时强平: 把搜索窗口压到 max_hold_hours 内, 窗口内无 TP/SL 则按现价平
        if max_hold_hours is not None:
            deadline = base_ts_secs[ek] + int(max_hold_hours * 3600)
            cap = int(np.searchsorted(base_ts_secs, deadline, side="right"))
            xh = max(ek + 1, min(xh, cap))
        if d == 1:
            tp, sl = entry * (1 + tp_pct), entry * (1 - sl_pct)
            sl_mask, tp_mask = base_l[ek:xh] <= sl, base_h[ek:xh] >= tp
        else:
            tp, sl = entry * (1 - tp_pct), entry * (1 + sl_pct)
            sl_mask, tp_mask = base_h[ek:xh] >= sl, base_l[ek:xh] <= tp
        sl_first = int(sl_mask.argmax()) if sl_mask.any() else -1
        tp_first = int(tp_mask.argmax()) if tp_mask.any() else -1
        if sl_first == -1 and tp_first == -1:
            exit_off, exit_price = (xh - ek) - 1, float(base_c[xh - 1])
            reason = "TIME" if max_hold_hours is not None else "EOB"
        elif sl_first == -1:
            exit_off, exit_price, reason = tp_first, tp, "TP"
        elif tp_first == -1:
            exit_off, exit_price, reason = sl_first, sl, "SL"
        elif sl_first <= tp_first:
            exit_off, exit_price, reason = sl_first, sl, "SL"
        else:
            exit_off, exit_price, reason = tp_first, tp, "TP"

        pct = (exit_price - entry) / entry if d == 1 else (entry - exit_price) / entry
        pct -= slippage

        margin = (initial_balance if fixed_margin else balance) * position_pct
        if max_margin is not None and margin > max_margin:
            margin = max_margin
        notional = margin * leverage
        hold_secs = max(0, int(base_ts_secs[ek + exit_off] - base_ts_secs[ek]))
        fp = hold_secs // (8 * 3600) + (1 if hold_secs % (8 * 3600) > 0 and hold_secs > 0 else 0)
        pnl = notional * pct - 2 * taker_fee * notional - fp * funding * notional
        balance += pnl
        busy_until = ek + exit_off

        rows.append({
            "pair": pair, "signal_bar": signal_bar, "mode": mode,
            "signal_ts": pd.Timestamp(sig_ts_ms[i], unit="ms").isoformat(),
            "entry_ts": pd.Timestamp(base_ts_ms[ek], unit="ms").isoformat(),
            "exit_ts": pd.Timestamp(base_ts_ms[ek + exit_off], unit="ms").isoformat(),
            "side": "long" if d == 1 else "short",
            "prev_bar": "阳" if d_prev == 1 else "阴",
            "entry": round(entry, 6), "exit": round(exit_price, 6),
            "exit_reason": reason, "hold_hours": round(hold_secs / 3600, 2),
            # ret_pct/r_mult 与仓位无关: 归因用这两列, pnl 只在对齐校验时看
            "ret_pct": round(pct * 100, 4),
            "r_mult": round(pct / sl_pct, 3) if sl_pct else 0.0,
            "pnl": round(pnl, 4), "balance": round(balance, 2),
            **reg,
        })
        if stop_on_ruin and balance <= 0:
            break
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--signal", required=True)
    ap.add_argument("--mode", required=True)
    ap.add_argument("--float", dest="float_pct", type=float, required=True)
    ap.add_argument("--tp", type=float, required=True)
    ap.add_argument("--sl", type=float, required=True)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--datadir", default="csv_data")
    ap.add_argument("--balance", type=float, default=148.0)
    ap.add_argument("--position-pct", type=float, default=0.10)
    ap.add_argument("--leverage", type=int, default=100)
    ap.add_argument("--slippage-bps", type=float, default=10.0)
    ap.add_argument("--funding-bps", type=float, default=3.0)
    ap.add_argument("--max-margin", type=float, default=1500.0)
    ap.add_argument("--date-from", default=None)
    ap.add_argument("--date-to", default=None)
    ap.add_argument("--no-stop-on-ruin", action="store_true",
                    help="爆仓后继续记账 (归因分析用; 默认与 strategy_lab 一致爆仓即停)")
    ap.add_argument("--hold-beyond-bucket", action="store_true",
                    help="复现实盘: 持仓不在桶末平, 一直持到 TP/SL")
    ap.add_argument("--skip-while-open", action="store_true",
                    help="复现实盘: 同 pair 有持仓时跳过新桶挂单")
    ap.add_argument("--max-hold-hours", type=float, default=None,
                    help="超时强平: 持仓超过该小时数按现价平 (时间止损)")
    ap.add_argument("--block-counter-trend", action="store_true",
                    help="单边行情中禁逆势腿 (上涨段不开空/下跌段不开多)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    base_bar = _pick_base_bar(args.signal)
    df_base = load_csv(args.pair, base_bar, args.days, args.datadir)
    df_sig = resample(df_base, args.signal)
    if args.date_from:
        ts = pd.Timestamp(args.date_from, tz="UTC")
        df_base, df_sig = df_base[df_base.index >= ts], df_sig[df_sig.index >= ts]
    if args.date_to:
        ts = pd.Timestamp(args.date_to, tz="UTC")
        df_base, df_sig = df_base[df_base.index < ts], df_sig[df_sig.index < ts]

    rows = run(df_base, df_sig, args.pair, args.signal, args.mode,
               args.float_pct, args.tp, args.sl,
               initial_balance=args.balance, position_pct=args.position_pct,
               leverage=args.leverage, slippage_bps=args.slippage_bps,
               funding_bps_per_8h=args.funding_bps, max_margin=args.max_margin,
               stop_on_ruin=not args.no_stop_on_ruin,
               hold_beyond_bucket=args.hold_beyond_bucket,
               skip_while_open=args.skip_while_open,
               max_hold_hours=args.max_hold_hours,
               block_counter_trend=args.block_counter_trend)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
            w.writeheader()
            w.writerows(rows)
        print(f"saved {len(rows)} trades -> {out}")

    if not rows:
        print("no trades")
        return
    wins = [r for r in rows if r["pnl"] > 0]
    print(f"{args.pair} {args.signal} {args.mode}: {len(rows)} trades, "
          f"WR={len(wins)/len(rows)*100:.1f}%, net={sum(r['pnl'] for r in rows):+.2f}")
    print(f"{'regime':10s} {'n':>4s} {'WR':>7s} {'net':>10s} {'avg':>8s}")
    for reg in ("单边上涨", "单边下跌", "震荡", "unknown"):
        s = [r for r in rows if r["regime"] == reg]
        if not s:
            continue
        w = [r for r in s if r["pnl"] > 0]
        net = sum(r["pnl"] for r in s)
        print(f"{reg:10s} {len(s):4d} {len(w)/len(s)*100:6.1f}% {net:+10.2f} {net/len(s):+8.2f}")


if __name__ == "__main__":
    main()
