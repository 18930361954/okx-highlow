"""策略实验室:在 bucket 框架上探索 HighLow 之外的新策略结构。

复用 bucket_backtest 的数据加载/重采样/成本口径(taker 5bp×2 + 滑点 + funding),
只改「方向 + 入场价」的生成规则。

模式 (--mode):
  trend      现行 HighLow:阳→下桶挂多@prev_low*(1-f);阴→挂空@prev_high*(1+f)  [基线]
  reversal   反转:阳→下桶挂空@prev_high*(1+f);阴→挂多@prev_low*(1-f)
  breakout   突破追势:阳→挂多突破单@prev_high*(1+f) (涨破入场);
             阴→挂空突破单@prev_low*(1-f) (跌破入场)
  fade       双向网:同时挂 trend 多单与 reversal 空单,先触发者成交 (每桶最多 1 笔,
             简化为价格先到哪个触发价算哪单)

过滤器 (可与任意 mode 组合):
  --confirm N    连续 N 桶同向才挂单 (N=1 等价无过滤)
  --min-amp/--max-amp   前桶振幅 (high-low)/open 边界过滤

口径与 bucket_grid 完全一致:同根 K 先 SL 保守判定、EOB 收盘平、复利可选。
"""
from __future__ import annotations

import argparse
import csv
import itertools
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bucket_backtest import (  # noqa: E402
    BASE_BAR_SECS, CT_VAL, _load_csv, _pick_base_bar, _resample,
)


def simulate_mode(df_base, df_sig, pair: str, base_bar: str, signal_bar: str,
                  mode: str, float_pct: float, tp_pct: float, sl_pct: float,
                  initial_balance: float = 148.0, position_pct: float = 0.10,
                  leverage: int = 100, fixed_margin: bool = False,
                  taker_fee: float = 0.0005, slippage_bps: float = 0.0,
                  funding_bps_per_8h: float = 0.0,
                  max_margin: float | None = None,
                  confirm: int = 1,
                  min_amp: float = 0.0, max_amp: float = 10.0) -> dict:
    n_buckets = len(df_sig)
    empty = {
        "pair": pair, "mode": mode, "signal_bar": signal_bar,
        "float_pct": float_pct, "tp_pct": tp_pct, "sl_pct": sl_pct,
        "confirm": confirm, "min_amp": min_amp, "max_amp": max_amp,
        "initial": initial_balance, "final": initial_balance,
        "total_return_pct": 0.0, "trades": 0, "wins": 0, "losses": 0,
        "win_rate_pct": 0.0, "max_dd_pct": 0.0, "profit_factor": 0.0,
        "monthly_pct": 0.0, "n_signals": n_buckets,
    }
    if n_buckets < max(2, confirm + 1):
        return empty

    sig_o = df_sig["open"].to_numpy()
    sig_c = df_sig["close"].to_numpy()
    sig_h = df_sig["high"].to_numpy()
    sig_l = df_sig["low"].to_numpy()
    sig_ts_ms = df_sig.index.tz_convert("UTC").tz_localize(None).astype("datetime64[ms]").view("int64")
    base_ts_ms = df_base.index.tz_convert("UTC").tz_localize(None).astype("datetime64[ms]").view("int64")
    base_h = df_base["high"].to_numpy()
    base_l = df_base["low"].to_numpy()
    base_c = df_base["close"].to_numpy()

    bucket_ms = BASE_BAR_SECS[signal_bar] * 1000

    # 前桶阴阳: +1 阳 / -1 阴 / 0 平
    bar_dir = np.zeros(n_buckets, dtype=np.int8)
    bar_dir[sig_c > sig_o] = 1
    bar_dir[sig_c < sig_o] = -1

    # 振幅过滤 (前桶)
    with np.errstate(divide="ignore", invalid="ignore"):
        amp = np.where(sig_o > 0, (sig_h - sig_l) / sig_o, 0.0)
    amp_ok = (amp >= min_amp) & (amp <= max_amp)

    # confirm: 位置 i 需要 [i-confirm+1, i] 全部同向
    conf_ok = np.ones(n_buckets, dtype=bool)
    if confirm > 1:
        for k in range(1, confirm):
            shifted = np.roll(bar_dir, k)
            shifted[:k] = 0
            conf_ok &= (shifted == bar_dir)
    conf_ok &= bar_dir != 0

    balance = initial_balance
    peak = balance
    max_dd_pct = 0.0
    pnls: list[float] = []
    exit_years: list[int] = []
    yearly_end: dict[int, float] = {}

    slippage = slippage_bps * 1e-4
    funding = funding_bps_per_8h * 1e-4
    base_ts_secs = base_ts_ms // 1000

    start_ms = sig_ts_ms[1:]
    end_ms = start_ms + bucket_ms
    lo_arr = np.searchsorted(base_ts_ms, start_ms, side="left")
    hi_arr = np.searchsorted(base_ts_ms, end_ms, side="left")

    trades = wins = losses = 0
    sum_win = sum_loss = 0.0

    for i in range(n_buckets - 1):
        if not (conf_ok[i] and amp_ok[i]):
            continue
        d_prev = int(bar_dir[i])
        lo, hi = int(lo_arr[i]), int(hi_arr[i])
        if lo >= hi:
            continue

        # ---- 按 mode 决定 (方向, 入场价, 触发方式) ----
        # trigger='dip'  : 挂低吸/高抛限价,价格「回落到/反弹到」入场
        # trigger='break': 突破单,价格「涨破/跌破」入场
        if mode == "trend":
            d = d_prev
            entry = sig_l[i] * (1 - float_pct) if d == 1 else sig_h[i] * (1 + float_pct)
            trigger = "dip"
        elif mode == "reversal":
            d = -d_prev
            entry = sig_h[i] * (1 + float_pct) if d == -1 else sig_l[i] * (1 - float_pct)
            trigger = "dip"
        elif mode == "breakout":
            d = d_prev
            entry = sig_h[i] * (1 + float_pct) if d == 1 else sig_l[i] * (1 - float_pct)
            trigger = "break"
        elif mode == "fade":
            # 双向: trend 腿 (低吸多) vs reversal 腿 (高空)。看桶内价格先到哪个。
            e_long = sig_l[i] * (1 - float_pct)
            e_short = sig_h[i] * (1 + float_pct)
            hit_l = base_l[lo:hi] <= e_long
            hit_s = base_h[lo:hi] >= e_short
            fl = int(hit_l.argmax()) if hit_l.any() else -1
            fs = int(hit_s.argmax()) if hit_s.any() else -1
            if fl == -1 and fs == -1:
                continue
            if fs == -1 or (fl != -1 and fl <= fs):
                d, entry, trigger = 1, e_long, "dip"
            else:
                d, entry, trigger = -1, e_short, "dip"
        else:
            raise ValueError(f"unknown mode {mode}")

        # ---- 触发判定 ----
        if d == 1:
            hit_arr = (base_l[lo:hi] <= entry) if trigger == "dip" else (base_h[lo:hi] >= entry)
        else:
            hit_arr = (base_h[lo:hi] >= entry) if trigger == "dip" else (base_l[lo:hi] <= entry)
        if not hit_arr.any():
            continue
        ek = lo + int(hit_arr.argmax())

        # ---- TP/SL (同根 K 先 SL 保守) ----
        if d == 1:
            tp = entry * (1 + tp_pct)
            sl = entry * (1 - sl_pct)
            sub_h, sub_l = base_h[ek:hi], base_l[ek:hi]
            sl_mask, tp_mask = sub_l <= sl, sub_h >= tp
        else:
            tp = entry * (1 - tp_pct)
            sl = entry * (1 + sl_pct)
            sub_h, sub_l = base_h[ek:hi], base_l[ek:hi]
            sl_mask, tp_mask = sub_h >= sl, sub_l <= tp
        sl_first = int(sl_mask.argmax()) if sl_mask.any() else -1
        tp_first = int(tp_mask.argmax()) if tp_mask.any() else -1
        if sl_first == -1 and tp_first == -1:
            exit_off, exit_price = (hi - ek) - 1, float(base_c[hi - 1])
        elif sl_first == -1:
            exit_off, exit_price = tp_first, tp
        elif tp_first == -1:
            exit_off, exit_price = sl_first, sl
        elif sl_first <= tp_first:
            exit_off, exit_price = sl_first, sl
        else:
            exit_off, exit_price = tp_first, tp

        pct = (exit_price - entry) / entry if d == 1 else (entry - exit_price) / entry
        pct -= slippage

        margin = (initial_balance if fixed_margin else balance) * position_pct
        if max_margin is not None and margin > max_margin:
            margin = max_margin
        notional = margin * leverage

        hold_secs = max(0, int(base_ts_secs[ek + exit_off] - base_ts_secs[ek]))
        funding_periods = hold_secs // (8 * 3600) + (1 if hold_secs % (8 * 3600) > 0 and hold_secs > 0 else 0)
        funding_cost = funding_periods * funding * notional
        fee = 2 * taker_fee * notional
        pnl = notional * pct - fee - funding_cost

        balance += pnl
        trades += 1
        if pnl > 0:
            wins += 1
            sum_win += pnl
        else:
            losses += 1
            sum_loss += -pnl
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak * 100 if peak > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd)
        yr = pd.Timestamp(base_ts_ms[ek + exit_off], unit="ms").year
        yearly_end[yr] = balance
        if balance <= 0:
            balance = 0.0
            break

    total_ret = (balance - initial_balance) / initial_balance * 100
    months = max((df_sig.index[-1] - df_sig.index[0]).days / 30.44, 1e-9)
    pf = (sum_win / sum_loss) if sum_loss > 0 else (float("inf") if sum_win > 0 else 0.0)
    out = dict(empty)
    out.update({
        "final": round(balance, 2), "total_return_pct": round(total_ret, 2),
        "trades": trades, "wins": wins, "losses": losses,
        "win_rate_pct": round(wins / trades * 100, 2) if trades else 0.0,
        "max_dd_pct": round(max_dd_pct, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "monthly_pct": round(total_ret / max((df_sig.index[-1] - df_sig.index[0]).days / 30, 1), 3),
    })
    # 年度分解
    prev = initial_balance
    for yr in sorted(yearly_end):
        end = yearly_end[yr]
        out[f"y{yr}_end"] = round(end, 2)
        out[f"y{yr}_pnl_pct"] = round((end - prev) / prev * 100 if prev > 0 else 0.0, 2)
        prev = end
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP")
    ap.add_argument("--signals", default="4H,6H,12H,1D")
    ap.add_argument("--modes", default="reversal,breakout,fade")
    ap.add_argument("--floats", default="0.001,0.002,0.003,0.005,0.007,0.010")
    ap.add_argument("--tps", default="0.006,0.008,0.010,0.015,0.020,0.030")
    ap.add_argument("--sls", default="0.010,0.015,0.020,0.025,0.030")
    ap.add_argument("--confirms", default="1")
    ap.add_argument("--min-amps", default="0")
    ap.add_argument("--max-amps", default="10")
    ap.add_argument("--balance", type=float, default=148.0)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--position-pct", type=float, default=0.10)
    ap.add_argument("--leverage", type=int, default=100)
    ap.add_argument("--compound", action="store_true")
    ap.add_argument("--slippage-bps", type=float, default=10.0)
    ap.add_argument("--funding-bps", type=float, default=3.0)
    ap.add_argument("--max-margin", type=float, default=1500.0)
    ap.add_argument("--date-from", default=None)
    ap.add_argument("--date-to", default=None)
    ap.add_argument("--out", default=str(ROOT / "reports" / "strategy_lab.csv"))
    args = ap.parse_args()

    pairs = [x.strip() for x in args.pairs.split(",") if x.strip()]
    signals = [x.strip() for x in args.signals.split(",") if x.strip()]
    modes = [x.strip() for x in args.modes.split(",") if x.strip()]
    floats = [float(x) for x in args.floats.split(",")]
    tps = [float(x) for x in args.tps.split(",")]
    sls = [float(x) for x in args.sls.split(",")]
    confirms = [int(x) for x in args.confirms.split(",")]
    min_amps = [float(x) for x in args.min_amps.split(",")]
    max_amps = [float(x) for x in args.max_amps.split(",")]

    total = (len(pairs) * len(signals) * len(modes) * len(floats) * len(tps)
             * len(sls) * len(confirms) * len(min_amps) * len(max_amps))
    print(f"[lab] total cases: {total}")
    t0 = time.time()
    done = 0
    results: list[dict] = []

    for pair in pairs:
        for sb in signals:
            base_bar = _pick_base_bar(sb)
            try:
                df_base = _load_csv(pair, base_bar, args.days)
            except FileNotFoundError:
                done += total // (len(pairs) * len(signals))
                continue
            df_sig = _resample(df_base, sb)
            if args.date_from:
                ts = pd.Timestamp(args.date_from, tz="UTC")
                df_base = df_base[df_base.index >= ts]
                df_sig = df_sig[df_sig.index >= ts]
            if args.date_to:
                ts = pd.Timestamp(args.date_to, tz="UTC")
                df_base = df_base[df_base.index < ts]
                df_sig = df_sig[df_sig.index < ts]

            for mode, fp, tp, sl, cf, mna, mxa in itertools.product(
                    modes, floats, tps, sls, confirms, min_amps, max_amps):
                r = simulate_mode(
                    df_base, df_sig, pair, base_bar, sb, mode, fp, tp, sl,
                    initial_balance=args.balance, position_pct=args.position_pct,
                    leverage=args.leverage, fixed_margin=not args.compound,
                    slippage_bps=args.slippage_bps,
                    funding_bps_per_8h=args.funding_bps,
                    max_margin=args.max_margin,
                    confirm=cf, min_amp=mna, max_amp=mxa,
                )
                results.append(r)
                done += 1
            el = time.time() - t0
            eta = el / done * (total - done) if done else 0
            print(f"  [{done}/{total}] {pair} {sb} done (elapsed={el:.0f}s eta={eta:.0f}s)")

    keys: list[str] = []
    for r in results:
        for k in r:
            if k not in keys:
                keys.append(k)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, restval="")
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"\nsaved {len(results)} rows → {out}")


if __name__ == "__main__":
    main()
