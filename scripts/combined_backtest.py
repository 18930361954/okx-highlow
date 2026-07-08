"""三币联合回测:一个共享 balance 同时跑 BTC/ETH/SOL。

跟 bucket_backtest 单币独立跑的区别:
  - 三币共享同一 balance
  - 每笔入场 margin = 当前 balance × pair.position_pct(默认 10%)
  - 三币可同时持仓(cross margin 共享保证金池)
  - 三币累计浮亏 > balance 时全部强平,回测结束
  - 3 连亏熔断(共享)暂停所有品种 24h

实现:
  1. 对每 pair 用现有 bucket_backtest 逻辑生成 (entry_ts, entry_price, direction, exit_ts, exit_price, exit_reason) 事件
  2. 按时间顺序处理事件流:
     - entry_ts: 若 balance 足够 + 未熔断 → 记为持仓中,扣除 margin 到"占用"池
     - exit_ts: 结算 pnl,归还 margin + pnl 到 balance,更新熔断计数
  3. 输出年度余额,最终 return,最大 MDD
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bucket_backtest import (  # noqa: E402
    CT_VAL, BASE_BAR_SECS, _load_csv, _resample, _pick_base_bar,
)


@dataclass
class TradeEvent:
    """(pair, entry_ts, exit_ts, direction, entry_px, exit_px, exit_reason, pct)
    - pct: 出场 pnl 百分比 (long: (exit-entry)/entry, short: (entry-exit)/entry, 已含滑点扣减)
    """
    pair: str
    entry_ts: int  # ms
    exit_ts: int   # ms
    direction: str
    entry_px: float
    exit_px: float
    exit_reason: str
    pct: float
    hold_secs: int  # 用于 funding
    leverage: int
    max_contracts: int


def _gen_events_for_pair(
    pair: str, signal_bar: str, float_pct: float, tp_pct: float, sl_pct: float,
    leverage: int, max_contracts: int, slippage_bps: float,
    days: int = 730,
) -> list[TradeEvent]:
    """对一个 pair 生成 (entry_ts, exit_ts, pct) 事件列表 — 不含 balance/margin,只标结构。
    balance 相关的复利在主循环处理。"""
    base_bar = _pick_base_bar(signal_bar)
    df_base = _load_csv(pair, base_bar, days)
    df_sig = _resample(df_base, signal_bar)

    sig_ts_ms = df_sig.index.tz_convert("UTC").tz_localize(None).astype("datetime64[ms]").view("int64")
    base_ts_ms = df_base.index.tz_convert("UTC").tz_localize(None).astype("datetime64[ms]").view("int64")
    sig_o = df_sig["open"].to_numpy()
    sig_c = df_sig["close"].to_numpy()
    sig_h = df_sig["high"].to_numpy()
    sig_l = df_sig["low"].to_numpy()
    base_h = df_base["high"].to_numpy()
    base_l = df_base["low"].to_numpy()
    base_c = df_base["close"].to_numpy()

    bucket_secs = BASE_BAR_SECS[signal_bar]
    bucket_ms = bucket_secs * 1000

    n = len(df_sig)
    events: list[TradeEvent] = []
    slip = slippage_bps * 1e-4

    for i in range(n - 1):
        if sig_c[i] == sig_o[i]:
            continue
        direction = "long" if sig_c[i] > sig_o[i] else "short"
        if direction == "long":
            entry = sig_l[i] * (1 - float_pct)
        else:
            entry = sig_h[i] * (1 + float_pct)

        start_ms = sig_ts_ms[i + 1]
        end_ms = start_ms + bucket_ms
        lo = int(np.searchsorted(base_ts_ms, start_ms, side="left"))
        hi = int(np.searchsorted(base_ts_ms, end_ms, side="left"))
        if lo >= hi:
            continue

        if direction == "long":
            hit_entry = base_l[lo:hi] <= entry
            if not hit_entry.any():
                continue
            entry_off = int(hit_entry.argmax())
            ek = lo + entry_off
            tp = entry * (1 + tp_pct)
            sl = entry * (1 - sl_pct)
            sub_h = base_h[ek:hi]
            sub_l = base_l[ek:hi]
            sl_mask = sub_l <= sl
            tp_mask = sub_h >= tp
            sl_first = int(sl_mask.argmax()) if sl_mask.any() else -1
            tp_first = int(tp_mask.argmax()) if tp_mask.any() else -1
            if sl_first == -1 and tp_first == -1:
                exit_off = (hi - ek) - 1
                exit_price = float(base_c[hi - 1])
                exit_reason = "EOB"
            elif sl_first == -1:
                exit_off = tp_first; exit_price = tp; exit_reason = "TP"
            elif tp_first == -1:
                exit_off = sl_first; exit_price = sl; exit_reason = "SL"
            elif sl_first <= tp_first:
                exit_off = sl_first; exit_price = sl; exit_reason = "SL"
            else:
                exit_off = tp_first; exit_price = tp; exit_reason = "TP"
            pct = (exit_price - entry) / entry - slip
        else:
            hit_entry = base_h[lo:hi] >= entry
            if not hit_entry.any():
                continue
            entry_off = int(hit_entry.argmax())
            ek = lo + entry_off
            tp = entry * (1 - tp_pct)
            sl = entry * (1 + sl_pct)
            sub_h = base_h[ek:hi]
            sub_l = base_l[ek:hi]
            sl_mask = sub_h >= sl
            tp_mask = sub_l <= tp
            sl_first = int(sl_mask.argmax()) if sl_mask.any() else -1
            tp_first = int(tp_mask.argmax()) if tp_mask.any() else -1
            if sl_first == -1 and tp_first == -1:
                exit_off = (hi - ek) - 1
                exit_price = float(base_c[hi - 1])
                exit_reason = "EOB"
            elif sl_first == -1:
                exit_off = tp_first; exit_price = tp; exit_reason = "TP"
            elif tp_first == -1:
                exit_off = sl_first; exit_price = sl; exit_reason = "SL"
            elif sl_first <= tp_first:
                exit_off = sl_first; exit_price = sl; exit_reason = "SL"
            else:
                exit_off = tp_first; exit_price = tp; exit_reason = "TP"
            pct = (entry - exit_price) / entry - slip

        exit_k = ek + exit_off
        hold_secs = int((base_ts_ms[exit_k] - base_ts_ms[ek]) // 1000)
        events.append(TradeEvent(
            pair=pair, entry_ts=int(base_ts_ms[ek]), exit_ts=int(base_ts_ms[exit_k]),
            direction=direction, entry_px=float(entry), exit_px=float(exit_price),
            exit_reason=exit_reason, pct=float(pct), hold_secs=hold_secs,
            leverage=leverage, max_contracts=max_contracts,
        ))
    return events


def combined_simulate(
    per_pair_params: list[dict],  # [{pair, signal_bar, float_pct, tp_pct, sl_pct, leverage, max_contracts}]
    initial_balance: float = 140.0,
    position_pct: float = 0.10,
    taker_fee: float = 0.0005,
    slippage_bps: float = 10.0,
    funding_bps_per_8h: float = 3.0,
    max_consecutive_losses: int = 3,
    cooldown_hours: int = 24,
    days: int = 730,
) -> dict:
    """三币联合回测:一个共享 balance,三币事件流按时间顺序处理。"""
    # 1) 为每 pair 生成事件
    all_events: list[TradeEvent] = []
    for pp in per_pair_params:
        evs = _gen_events_for_pair(
            pp["pair"], pp["signal_bar"], pp["float_pct"], pp["tp_pct"], pp["sl_pct"],
            pp["leverage"], pp["max_contracts"], slippage_bps, days,
        )
        all_events.extend(evs)

    # 2) 构造"时间点"事件:每笔 = 一个 open 一个 close
    # 用 (ts, type, event_ref) 排序, open 在 close 前面(同一 ts)
    tp_open = 0
    tp_close = 1
    timeline: list[tuple[int, int, TradeEvent]] = []
    for ev in all_events:
        timeline.append((ev.entry_ts, tp_open, ev))
        timeline.append((ev.exit_ts, tp_close, ev))
    timeline.sort(key=lambda x: (x[0], x[1]))

    # 3) 主循环
    balance = initial_balance
    open_positions: dict[int, tuple[TradeEvent, float, float]] = {}  # id(ev) → (ev, margin, notional)
    consec_losses = 0
    cooldown_until_ts = 0
    peak = balance
    max_dd = 0.0
    yearly_end: dict[int, float] = {}
    trades_log: list[dict] = []

    for ts, tp, ev in timeline:
        if tp == tp_open:
            # 熔断中不开
            if ts < cooldown_until_ts:
                continue
            # 持仓保护(与实盘 main.daily_signal_and_place 里的 held_pairs 逻辑对齐):
            # 同 pair 若还有 open position,跳过新单
            if any(p.pair == ev.pair for (p, _, _) in open_positions.values()):
                continue
            # 计算 margin
            margin = balance * position_pct
            notional = margin * ev.leverage
            # 张数封顶
            cv = CT_VAL.get(ev.pair, 0.01)
            if ev.max_contracts and ev.entry_px > 0:
                ct_wanted = notional / (cv * ev.entry_px)
                if ct_wanted > ev.max_contracts:
                    notional = ev.max_contracts * cv * ev.entry_px
                    margin = notional / ev.leverage
            # 检查是否有足够 balance:如果 margin > balance(考虑已占用),跳过
            occupied = sum(m for (_, m, _) in open_positions.values())
            if occupied + margin > balance:
                continue  # 资金不足
            open_positions[id(ev)] = (ev, margin, notional)
        else:
            # 关闭
            if id(ev) not in open_positions:
                continue
            _, margin, notional = open_positions.pop(id(ev))
            # pnl
            fee = 2 * taker_fee * notional
            funding_periods = ev.hold_secs // (8 * 3600) + (1 if ev.hold_secs % (8 * 3600) > 0 and ev.hold_secs > 0 else 0)
            funding_cost = funding_periods * (funding_bps_per_8h * 1e-4) * notional
            pnl = notional * ev.pct - fee - funding_cost
            balance += pnl
            trades_log.append({"ts": ts, "pair": ev.pair, "pnl": pnl,
                                "reason": ev.exit_reason, "balance": balance})
            peak = max(peak, balance)
            if peak > 0:
                dd = (peak - balance) / peak * 100
                max_dd = max(max_dd, dd)
            # 熔断
            if pnl < 0:
                consec_losses += 1
                if consec_losses >= max_consecutive_losses:
                    cooldown_until_ts = ts + cooldown_hours * 3600 * 1000
                    consec_losses = 0
            else:
                consec_losses = 0
            # 年度
            yr = int(pd.Timestamp(ts, unit="ms", tz="UTC").year)
            yearly_end[yr] = balance
            # 爆仓
            if balance <= 0:
                balance = 0
                break

    wins = sum(1 for t in trades_log if t["pnl"] > 0)
    losses = sum(1 for t in trades_log if t["pnl"] < 0)
    total = len(trades_log)
    win_rate = wins / total * 100 if total else 0
    gross_win = sum(t["pnl"] for t in trades_log if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades_log if t["pnl"] < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    # per-pair 汇总
    per_pair: dict[str, dict] = {}
    for t in trades_log:
        d = per_pair.setdefault(t["pair"], {"n": 0, "pnl": 0.0, "wins": 0})
        d["n"] += 1
        d["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            d["wins"] += 1

    yearly = {}
    prev = initial_balance
    for yr in sorted(yearly_end.keys()):
        end_bal = yearly_end[yr]
        yearly[str(yr)] = {
            "end_balance": end_bal,
            "pnl_pct": (end_bal - prev) / prev * 100 if prev > 0 else 0,
        }
        prev = end_bal

    return {
        "initial": initial_balance, "final": balance,
        "total_return_pct": (balance - initial_balance) / initial_balance * 100 if initial_balance > 0 else 0,
        "trades": total, "wins": wins, "losses": losses,
        "win_rate_pct": win_rate, "max_dd_pct": max_dd, "profit_factor": pf,
        "per_pair": per_pair, "yearly": yearly,
    }


def _print_result(name: str, per_pair_params: list[dict], r: dict) -> None:
    print(f"\n{'='*72}\n{name}\n{'='*72}")
    print(f"参数:")
    for pp in per_pair_params:
        print(f"  {pp['pair']} {pp['signal_bar']}: "
              f"f={pp['float_pct']} tp={pp['tp_pct']} sl={pp['sl_pct']} "
              f"lev={pp['leverage']}x cap={pp['max_contracts']}")
    print(f"\n结果: {r['initial']:.0f} → {r['final']:.0f} USDT "
          f"({r['total_return_pct']:+.0f}%) MDD {r['max_dd_pct']:.1f}%")
    print(f"总笔数 {r['trades']} · 胜率 {r['win_rate_pct']:.1f}% · PF {r['profit_factor']:.2f}")
    print(f"\n分币:")
    for pair, d in r["per_pair"].items():
        wr = d["wins"] / d["n"] * 100 if d["n"] else 0
        print(f"  {pair}: {d['n']} 笔, PnL {d['pnl']:+.0f}, 胜率 {wr:.1f}%")
    print(f"\n年度余额曲线:")
    for yr, y in r["yearly"].items():
        print(f"  {yr} 末: {y['end_balance']:.0f} USDT  ({y['pnl_pct']:+.0f}%)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=140.0)
    ap.add_argument("--position-pct", type=float, default=0.10)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--slippage-bps", type=float, default=10.0)
    args = ap.parse_args()

    # 三个账户对应的三币参数(来自 walk-forward v3 通过组合)
    configs = {
        "实盘-主账户 (4H)": [
            {"pair": "BTC-USDT-SWAP", "signal_bar": "4H", "float_pct": 0.005, "tp_pct": 0.006, "sl_pct": 0.020, "leverage": 100, "max_contracts": 1000},
            {"pair": "ETH-USDT-SWAP", "signal_bar": "4H", "float_pct": 0.004, "tp_pct": 0.006, "sl_pct": 0.020, "leverage": 100, "max_contracts": 5000},
            {"pair": "SOL-USDT-SWAP", "signal_bar": "4H", "float_pct": 0.005, "tp_pct": 0.006, "sl_pct": 0.020, "leverage": 100, "max_contracts": 5000},
        ],
        "实盘-A1455923264 (6H)": [
            {"pair": "BTC-USDT-SWAP", "signal_bar": "6H", "float_pct": 0.005, "tp_pct": 0.010, "sl_pct": 0.020, "leverage": 100, "max_contracts": 1000},
            {"pair": "ETH-USDT-SWAP", "signal_bar": "6H", "float_pct": 0.005, "tp_pct": 0.010, "sl_pct": 0.015, "leverage": 100, "max_contracts": 5000},
            {"pair": "SOL-USDT-SWAP", "signal_bar": "6H", "float_pct": 0.004, "tp_pct": 0.015, "sl_pct": 0.020, "leverage": 100, "max_contracts": 5000},
        ],
        "实盘-bot14559 (12H)": [
            {"pair": "BTC-USDT-SWAP", "signal_bar": "12H", "float_pct": 0.003, "tp_pct": 0.008, "sl_pct": 0.020, "leverage": 100, "max_contracts": 1000},
            {"pair": "ETH-USDT-SWAP", "signal_bar": "12H", "float_pct": 0.004, "tp_pct": 0.008, "sl_pct": 0.020, "leverage": 100, "max_contracts": 5000},
            {"pair": "SOL-USDT-SWAP", "signal_bar": "12H", "float_pct": 0.004, "tp_pct": 0.010, "sl_pct": 0.020, "leverage": 100, "max_contracts": 5000},
        ],
    }

    for name, ppp in configs.items():
        r = combined_simulate(ppp, initial_balance=args.balance,
                              position_pct=args.position_pct,
                              slippage_bps=args.slippage_bps, days=args.days)
        _print_result(name, ppp, r)


if __name__ == "__main__":
    main()
