"""强平感知联合回测:单账户内多 pair 同时持仓 + 全仓保证金逐时盯市。

与 combined_backtest 的区别:
  - 逐 1H 盯市:equity = balance + Σ 未实现盈亏(用 1H bar 对持仓的最不利极值,保守)
  - equity < Σ(MMR_i × notional_i) → 全账户强平,balance = 0,回测终止
  - MMR(OKX USDT 本位永续 tier1 近似): BTC 0.4% / ETH 0.5% / SOL 1.0%
  - 支持 trend / reversal / fade 三种入场模式(与 strategy_lab 口径一致)

保守性说明:
  - 同一 1H bar 内所有持仓同时取各自最不利极值(实际未必同时发生)
  - 持仓中间 bar 的极值天然 < SL 距离(事件生成按首根穿越即出场),口径自洽
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bucket_backtest import (  # noqa: E402
    BASE_BAR_SECS, CT_VAL, _load_csv, _pick_base_bar, _resample,
)

MMR = {"BTC-USDT-SWAP": 0.004, "ETH-USDT-SWAP": 0.005, "SOL-USDT-SWAP": 0.01}


@dataclass
class Ev:
    pair: str
    mode: str
    entry_ts: int
    exit_ts: int
    d: int              # 1 long / -1 short
    entry_px: float
    exit_px: float
    pct: float          # 已含滑点
    hold_secs: int
    sl_pct: float


def gen_events(pair: str, signal_bar: str, mode: str,
               float_pct: float, tp_pct: float, sl_pct: float,
               slippage_bps: float, days: int = 730) -> list[Ev]:
    base_bar = _pick_base_bar(signal_bar)
    df_base = _load_csv(pair, base_bar, days)
    df_sig = _resample(df_base, signal_bar)

    sig_o = df_sig["open"].to_numpy(); sig_c = df_sig["close"].to_numpy()
    sig_h = df_sig["high"].to_numpy(); sig_l = df_sig["low"].to_numpy()
    sig_ts = df_sig.index.tz_convert("UTC").tz_localize(None).astype("datetime64[ms]").view("int64")
    base_ts = df_base.index.tz_convert("UTC").tz_localize(None).astype("datetime64[ms]").view("int64")
    bh = df_base["high"].to_numpy(); bl = df_base["low"].to_numpy(); bc = df_base["close"].to_numpy()
    bucket_ms = BASE_BAR_SECS[signal_bar] * 1000
    base_secs = base_ts // 1000
    slip = slippage_bps * 1e-4

    bar_dir = np.zeros(len(df_sig), dtype=np.int8)
    bar_dir[sig_c > sig_o] = 1
    bar_dir[sig_c < sig_o] = -1

    evs: list[Ev] = []
    for i in range(len(df_sig) - 1):
        dp = int(bar_dir[i])
        if dp == 0:
            continue
        lo = int(np.searchsorted(base_ts, sig_ts[i + 1]))
        hi = int(np.searchsorted(base_ts, sig_ts[i + 1] + bucket_ms))
        if lo >= hi:
            continue

        if mode == "trend":
            d = dp
            entry = sig_l[i] * (1 - float_pct) if d == 1 else sig_h[i] * (1 + float_pct)
        elif mode == "reversal":
            d = -dp
            entry = sig_h[i] * (1 + float_pct) if d == -1 else sig_l[i] * (1 - float_pct)
        elif mode == "fade":
            e_long = sig_l[i] * (1 - float_pct)
            e_short = sig_h[i] * (1 + float_pct)
            hl = bl[lo:hi] <= e_long
            hs = bh[lo:hi] >= e_short
            fl = int(hl.argmax()) if hl.any() else -1
            fs = int(hs.argmax()) if hs.any() else -1
            if fl == -1 and fs == -1:
                continue
            if fs == -1 or (fl != -1 and fl <= fs):
                d, entry = 1, e_long
            else:
                d, entry = -1, e_short
        else:
            raise ValueError(mode)

        if d == 1:
            hit = bl[lo:hi] <= entry
        else:
            hit = bh[lo:hi] >= entry
        if not hit.any():
            continue
        ek = lo + int(hit.argmax())

        if d == 1:
            tp = entry * (1 + tp_pct); sl = entry * (1 - sl_pct)
            sl_m = bl[ek:hi] <= sl; tp_m = bh[ek:hi] >= tp
        else:
            tp = entry * (1 - tp_pct); sl = entry * (1 + sl_pct)
            sl_m = bh[ek:hi] >= sl; tp_m = bl[ek:hi] <= tp
        sf = int(sl_m.argmax()) if sl_m.any() else -1
        tf = int(tp_m.argmax()) if tp_m.any() else -1
        if sf == -1 and tf == -1:
            xo, xp = (hi - ek) - 1, float(bc[hi - 1])
        elif sf == -1:
            xo, xp = tf, tp
        elif tf == -1:
            xo, xp = sf, sl
        elif sf <= tf:
            xo, xp = sf, sl
        else:
            xo, xp = tf, tp

        pct = ((xp - entry) / entry if d == 1 else (entry - xp) / entry) - slip
        evs.append(Ev(pair, mode, int(base_ts[ek]), int(base_ts[ek + xo]), d,
                      float(entry), float(xp), float(pct),
                      max(0, int(base_secs[ek + xo] - base_secs[ek])), sl_pct))
    return evs


def run_portfolio(legs: list[dict], initial: float = 148.0,
                  position_pct: float = 0.10, leverage: int = 100,
                  taker_fee: float = 0.0005, slippage_bps: float = 10.0,
                  funding_bps: float = 3.0, max_margin: float = 1500.0,
                  days: int = 730, check_liq: bool = True) -> dict:
    """legs: [{pair, signal_bar, mode, float_pct, tp_pct, sl_pct}, ...] 同一账户内。"""
    all_evs: list[Ev] = []
    for leg in legs:
        all_evs.extend(gen_events(leg["pair"], leg["signal_bar"], leg.get("mode", "trend"),
                                  leg["float_pct"], leg["tp_pct"], leg["sl_pct"],
                                  slippage_bps, days))

    # 1H 盯市数据
    marks: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    pairs = {leg["pair"] for leg in legs}
    for p in pairs:
        df = _load_csv(p, "1H", days)
        ts = df.index.tz_convert("UTC").tz_localize(None).astype("datetime64[ms]").view("int64")
        marks[p] = (ts, df["high"].to_numpy(), df["low"].to_numpy())

    OPEN, CLOSE, MARK = 0, 1, 2
    timeline: list[tuple[int, int, object]] = []
    for ev in all_evs:
        timeline.append((ev.entry_ts, OPEN, ev))
        timeline.append((ev.exit_ts, CLOSE, ev))
    # 1H 检查点(全 pair 共用第一个 pair 的 1H 时间轴即可,均为整点)
    any_ts = next(iter(marks.values()))[0]
    for t in any_ts:
        timeline.append((int(t), MARK, None))
    timeline.sort(key=lambda x: (x[0], x[1]))

    balance = initial
    peak = initial
    max_dd = 0.0
    open_pos: dict[int, tuple[Ev, float, float]] = {}
    trades = wins = losses = 0
    sum_win = sum_loss = 0.0
    yearly_end: dict[int, float] = {}
    per_pair: dict[str, dict] = {}
    liq_ts = None
    min_eq_ratio = float("inf")  # 权益/维持保证金 最小比值 (风险余量)
    funding = funding_bps * 1e-4

    mark_idx = {p: 0 for p in pairs}

    for ts, kind, ev in timeline:
        if kind == OPEN:
            if any(o.pair == ev.pair for (o, _, _) in open_pos.values()):
                continue
            margin = balance * position_pct
            if max_margin is not None:
                margin = min(margin, max_margin)
            occupied = sum(m for (_, m, _) in open_pos.values())
            if occupied + margin > balance or margin <= 0:
                continue
            open_pos[id(ev)] = (ev, margin, margin * leverage)
        elif kind == CLOSE:
            got = open_pos.pop(id(ev), None)
            if got is None:
                continue
            _, margin, notional = got
            fee = 2 * taker_fee * notional
            fp = ev.hold_secs // 28800 + (1 if ev.hold_secs % 28800 > 0 and ev.hold_secs > 0 else 0)
            pnl = notional * ev.pct - fee - fp * funding * notional
            balance += pnl
            trades += 1
            pp = per_pair.setdefault(ev.pair, {"n": 0, "pnl": 0.0, "wins": 0, "fee": 0.0})
            pp["n"] += 1
            pp["pnl"] += pnl
            pp["fee"] += fee
            if pnl > 0:
                pp["wins"] += 1
                wins += 1; sum_win += pnl
            else:
                losses += 1; sum_loss += -pnl
            peak = max(peak, balance)
            if peak > 0:
                max_dd = max(max_dd, (peak - balance) / peak * 100)
            yearly_end[pd.Timestamp(ts, unit="ms").year] = balance
            if balance <= 0:
                balance = 0.0
                liq_ts = ts
                break
        else:  # MARK
            if not check_liq or not open_pos:
                continue
            unreal = 0.0
            mmr_total = 0.0
            for (o, margin, notional) in open_pos.values():
                mts, mh, ml = marks[o.pair]
                j = int(np.searchsorted(mts, ts, side="right")) - 1
                if j < 0:
                    continue
                adverse = float(ml[j]) if o.d == 1 else float(mh[j])
                u = (adverse - o.entry_px) / o.entry_px if o.d == 1 else (o.entry_px - adverse) / o.entry_px
                unreal += notional * min(u, 0.0)  # 只计浮亏(保守)
                mmr_total += notional * MMR.get(o.pair, 0.01)
            equity = balance + unreal
            if mmr_total > 0:
                min_eq_ratio = min(min_eq_ratio, equity / mmr_total)
            if equity < mmr_total:
                balance = 0.0
                liq_ts = ts
                break

    pf = (sum_win / sum_loss) if sum_loss > 0 else (float("inf") if sum_win > 0 else 0.0)
    out = {
        "initial": initial,
        "final": round(balance, 2),
        "net_profit": round(balance - initial, 2),
        "total_return_pct": round((balance - initial) / initial * 100, 1),
        "trades": trades,
        "win_rate_pct": round(wins / trades * 100, 1) if trades else 0.0,
        "max_dd_pct": round(max_dd, 1),
        "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
        "liquidated": liq_ts is not None,
        "liq_time": str(pd.Timestamp(liq_ts, unit="ms")) if liq_ts else "",
        "min_equity_mmr_ratio": round(min_eq_ratio, 2) if min_eq_ratio != float("inf") else "",
        "per_pair": {p: {"n": d["n"], "pnl": round(d["pnl"], 2),
                          "win_rate": round(d["wins"] / d["n"] * 100, 1) if d["n"] else 0,
                          "fee": round(d["fee"], 2)}
                     for p, d in per_pair.items()},
    }
    prev = initial
    for yr in sorted(yearly_end):
        out[f"y{yr}_end"] = round(yearly_end[yr], 2)
        out[f"y{yr}_pnl_usdt"] = round(yearly_end[yr] - prev, 2)
        out[f"y{yr}_pnl_pct"] = round((yearly_end[yr] - prev) / prev * 100 if prev > 0 else 0, 1)
        prev = yearly_end[yr]
    return out


if __name__ == "__main__":
    print("用法: 从其它脚本 import run_portfolio,或直接改 main 里的场景列表")
