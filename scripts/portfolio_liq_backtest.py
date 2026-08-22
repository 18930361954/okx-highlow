"""组合回测（含强制平仓模型）—— 决定实盘杠杆/仓位的依据。

为什么要新写一个: 现有回测(strategy_lab / combined_backtest)都**没有爆仓模型**,
假设「价格碰到止损价就按止损价平掉」。但全仓模式下三个币共用保证金,
10% 仓位 × 100 倍 × 3 币 = 总名义 30 倍权益 → 强平距离仅 2.93%,
比 3% 的止损还小。这种配置下实盘会先被强平, 旧回测的结论完全失效。

本脚本做三件旧回测没做的事:
  1. 逐小时跟踪权益 = 已实现余额 + 所有持仓浮动盈亏
  2. 按 OKX 全仓规则判强平: 权益 <= 总名义 × 维持保证金率 → 账户归零
  3. 复利下注(每笔 margin = 当前余额 × 仓位%), 与实盘 compute_margin 一致

价格决策(入场价/离场价/离场原因/时点)只取决于价格, 与下注多少无关,
所以先用 trade_ledger 生成各腿成交流水, 再在组合层重新按共享余额定仓位。

用法:
  python scripts/portfolio_liq_backtest.py --position-pct 0.10 --sol-lev 100
  python scripts/portfolio_liq_backtest.py --sweep      # 扫仓位×杠杆全组合
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from scripts.bucket_backtest import _pick_base_bar
from scripts import trade_ledger as TL

# OKX 档1 维持保证金率(名义 <=5000 张)。实测 2026-08-22:
# BTC/ETH/SOL-USDT-SWAP 均为 0.004, 档1 最大杠杆 100x。
MMR = 0.004
# OKX 合约面值
CT_VAL = {"BTC-USDT-SWAP": 0.01, "ETH-USDT-SWAP": 0.1, "SOL-USDT-SWAP": 1.0}
# 单笔张数封顶(与 config.yaml strategy.max_contracts 一致)。
# 不封顶的话复利会算出「权益涨到几千亿」这种数学爆炸 —— OKX 盘口根本吃不下,
# 那种数字当收益预期是自欺欺人。
#
# OKX 实测 2026-08-23 (position-tiers API):
#   100倍杠杆档位上限: BTC 1000 / ETH 5000 / SOL 5000 张
#   超过该张数会自动降杠杆(档2=66.66x, 档3=50x, ...)
# 这才是实盘 100 倍下的真实约束，而非市价单上限(那个是单笔订单限制,
# 与杠杆档位无关，会更大)。
MAX_CONTRACTS = {"BTC-USDT-SWAP": 1000, "ETH-USDT-SWAP": 5000,
                 "SOL-USDT-SWAP": 5000}
# 单笔保证金封顶(USDT)。与历史回测口径一致(参数选拔时用的 1500)。
DEFAULT_MAX_MARGIN = 1500.0
DATADIR = "csv_data_0822"
BARH = {"4H": 4, "6H": 6, "12H": 12, "1D": 24}

# 三个组合的腿定义 (账户, 币, 信号周期, 玩法, 浮动, 止盈, 止损)
PORTFOLIOS = {
    "A": [
        ("BTC-USDT-SWAP", "6H", "trend", 0.007, 0.008, 0.030),
        ("ETH-USDT-SWAP", "6H", "trend", 0.003, 0.008, 0.030),
        ("SOL-USDT-SWAP", "6H", "fade", 0.010, 0.010, 0.030),
    ],
    "B": [
        ("BTC-USDT-SWAP", "1D", "trend", 0.010, 0.008, 0.025),
        ("ETH-USDT-SWAP", "4H", "fade", 0.010, 0.010, 0.025),
        ("SOL-USDT-SWAP", "6H", "fade", 0.005, 0.008, 0.030),
    ],
    "C": [
        ("BTC-USDT-SWAP", "1D", "trend", 0.010, 0.008, 0.030),
        ("ETH-USDT-SWAP", "12H", "reversal", 0.010, 0.008, 0.025),
        ("SOL-USDT-SWAP", "6H", "fade", 0.005, 0.006, 0.030),
    ],
}

_LEG_CACHE: dict[tuple, list[dict]] = {}
_PX_CACHE: dict[str, pd.DataFrame] = {}


def price_frame(pair: str) -> pd.DataFrame:
    """1H 高开低收, 用于逐小时估值与强平判定。"""
    if pair not in _PX_CACHE:
        _PX_CACHE[pair] = TL.load_csv(pair, "1H", 730, DATADIR)
    return _PX_CACHE[pair]


def leg_trades(pair: str, sb: str, mode: str, fp: float, tp: float, sl: float,
               timestop: float | None) -> list[dict]:
    """生成该腿的成交流水(价格决策层, 与下注多少无关)。"""
    key = (pair, sb, mode, fp, tp, sl, timestop)
    if key in _LEG_CACHE:
        return _LEG_CACHE[key]
    base = TL.load_csv(pair, _pick_base_bar(sb), 730, DATADIR)
    sig = TL.resample(base, sb)
    kw = dict(initial_balance=1000.0, position_pct=0.10, leverage=100,
              fixed_margin=True, slippage_bps=10.0, funding_bps_per_8h=3.0,
              max_margin=None, stop_on_ruin=False,
              hold_beyond_bucket=True, skip_while_open=True)
    if timestop:
        kw["max_hold_hours"] = BARH[sb] * timestop
    rows = TL.run(base, sig, pair, sb, mode, fp, tp, sl, **kw)
    for r in rows:
        r["pair"] = pair
        r["sl_pct_cfg"] = sl
    _LEG_CACHE[key] = rows
    return rows


@dataclass
class OpenPos:
    pair: str
    side: str          # long / short
    entry: float
    entry_ts: pd.Timestamp  # 开仓时间(用于判断本小时新开还是之前开的)
    coin_qty: float    # 币数量
    margin: float
    exit_ts: pd.Timestamp
    exit_px: float
    reason: str
    fee_open: float


@dataclass
class Result:
    initial: float
    final: float
    ruined: bool
    ruin_ts: str | None
    trades: int
    wins: int
    max_dd_pct: float
    monthly_pct: float
    yearly: dict = field(default_factory=dict)
    liq_events: int = 0
    skipped_margin: int = 0
    equity_curve: list = field(default_factory=list)


def simulate(portfolio: str, position_pct: float, sol_lev: int,
             other_lev: int = 100, initial: float = 150.0,
             timestop: float | None = 0.5, max_margin: float | None = None,
             taker_fee: float = 0.0005, funding_bps: float = 3.0,
             conservative_liq: bool = True) -> Result:
    """逐小时推进的组合回测, 含全仓强平判定。

    max_margin: 单笔保证金封顶(USDT)。None=不封顶，0 或负数也视为不封顶。
    conservative_liq=True: 强平检查用每根 1H 的不利极值(多单看最低价/空单看最高价),
      即「盘中最坏时刻」。False 则用收盘价。真实强平按标记价盘中触发,
      所以 True 更接近现实, False 会低估爆仓频率。
    """
    # max_margin 的 0 或负数视为不封顶
    if max_margin is not None and max_margin <= 0:
        max_margin = None
    legs = PORTFOLIOS[portfolio]
    lev_of = {p: (sol_lev if p.startswith("SOL") else other_lev)
              for p, *_ in legs}

    # 1) 各腿成交流水 → 按入场时间排成待处理队列
    pending: list[dict] = []
    for pair, sb, mode, fp, tp, sl in legs:
        pending.extend(leg_trades(pair, sb, mode, fp, tp, sl, timestop))
    for r in pending:
        r["_in"] = pd.Timestamp(r["entry_ts"], tz="UTC")
        r["_out"] = pd.Timestamp(r["exit_ts"], tz="UTC")
    pending.sort(key=lambda r: r["_in"])

    # 2) 统一 1H 时间轴(三币交集)
    frames = {p: price_frame(p) for p, *_ in legs}
    idx = None
    for f in frames.values():
        idx = f.index if idx is None else idx.intersection(f.index)
    idx = idx.sort_values()
    hi = {p: f["high"].reindex(idx) for p, f in frames.items()}
    lo = {p: f["low"].reindex(idx) for p, f in frames.items()}
    cl = {p: f["close"].reindex(idx) for p, f in frames.items()}

    balance = initial
    peak = initial
    max_dd = 0.0
    open_pos: list[OpenPos] = []
    trades = wins = 0
    liq_events = 0
    skipped_margin = 0
    ruined = False
    ruin_ts = None
    yearly: dict[int, float] = {}
    curve: list[tuple[str, float]] = []
    pi = 0
    n = len(pending)

    for ts in idx:
        # ---- a) 本小时到期的持仓先结算(离场) ----
        still: list[OpenPos] = []
        for p in open_pos:
            if p.exit_ts <= ts:
                px = p.exit_px
                if p.side == "long":
                    pnl = p.coin_qty * (px - p.entry)
                else:
                    pnl = p.coin_qty * (p.entry - px)
                notional_out = p.coin_qty * px
                fee = taker_fee * notional_out
                hold_h = max(0.0, (p.exit_ts - ts).total_seconds() / 3600 + 1)
                periods = int(hold_h // 8) + (1 if hold_h % 8 else 0)
                fund = periods * (funding_bps * 1e-4) * (p.coin_qty * p.entry)
                net = pnl - p.fee_open - fee - fund
                balance += net
                trades += 1
                if net > 0:
                    wins += 1
                yearly[ts.year] = balance
            else:
                still.append(p)
        open_pos = still

        # ---- b) 本小时新开仓 ----
        while pi < n and pending[pi]["_in"] <= ts:
            r = pending[pi]
            pi += 1
            if balance <= 0:
                continue
            # 同 pair 已有持仓 → 跳过(与实盘 held_pairs 一致)
            if any(op.pair == r["pair"] for op in open_pos):
                continue
            lev = lev_of[r["pair"]]
            margin = balance * position_pct
            if max_margin is not None:
                margin = min(margin, max_margin)
            notional = margin * lev
            entry = r["entry"]
            if entry <= 0:
                continue
            # 张数取整(OKX 最小 1 张) + 张数封顶 → 名义按整张重算
            ct = CT_VAL[r["pair"]]
            contracts = int(notional / (ct * entry))
            cap = MAX_CONTRACTS.get(r["pair"])
            if cap is not None and contracts > cap:
                contracts = cap
            if contracts < 1:
                skipped_margin += 1
                continue
            coin_qty = contracts * ct
            notional = coin_qty * entry
            open_pos.append(OpenPos(
                pair=r["pair"], side=r["side"], entry=entry, entry_ts=r["_in"],
                coin_qty=coin_qty, margin=notional / lev, exit_ts=r["_out"],
                exit_px=r["exit"], reason=r["exit_reason"],
                fee_open=taker_fee * notional,
            ))

        # ---- c) 逐小时估值 + 全仓强平判定 ----
        if open_pos:
            upl = 0.0
            maint = 0.0
            for p in open_pos:
                # 本小时新开的仓用入场价估值(浮盈=0), 之前开的才用本小时极值
                if p.entry_ts >= ts:
                    px = p.entry
                else:
                    if conservative_liq:
                        px = lo[p.pair].get(ts) if p.side == "long" else hi[p.pair].get(ts)
                    else:
                        px = cl[p.pair].get(ts)
                    if px is None or px != px:      # NaN
                        px = p.entry
                if p.side == "long":
                    upl += p.coin_qty * (px - p.entry)
                else:
                    upl += p.coin_qty * (p.entry - px)
                maint += p.coin_qty * px * MMR
            equity = balance + upl
            if equity <= maint:
                # 强平: 全部持仓按当时不利价平掉, 权益归零
                liq_events += 1
                balance = 0.0
                open_pos = []
                ruined = True
                ruin_ts = ts.isoformat()
                curve.append((ts.isoformat(), 0.0))
                break
            eq_for_dd = equity
        else:
            eq_for_dd = balance

        peak = max(peak, eq_for_dd)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq_for_dd) / peak * 100)
        if ts.hour == 0:
            curve.append((ts.isoformat(), eq_for_dd))

    days = (idx[-1] - idx[0]).days or 1
    months = days / 30.44
    total_ret = (balance - initial) / initial * 100 if initial > 0 else 0.0
    # 月化用复利开方(不是总收益÷月数) —— 复利下注就必须用复利口径
    if balance > 0 and initial > 0:
        monthly = ((balance / initial) ** (1 / months) - 1) * 100
    else:
        monthly = -100.0

    return Result(
        initial=initial, final=balance, ruined=ruined, ruin_ts=ruin_ts,
        trades=trades, wins=wins, max_dd_pct=max_dd, monthly_pct=monthly,
        yearly=yearly, liq_events=liq_events, skipped_margin=skipped_margin,
        equity_curve=curve,
    )


def _fmt_money(v: float) -> str:
    if v >= 1e9:
        return f"{v/1e9:,.1f}十亿"
    if v >= 1e6:
        return f"{v/1e6:,.1f}百万"
    if v >= 1e4:
        return f"{v/1e4:,.1f}万"
    return f"{v:,.0f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="组合回测(含强平模型)")
    ap.add_argument("--portfolios", default="A,B,C")
    ap.add_argument("--position-pct", type=float, default=0.10)
    ap.add_argument("--sol-lev", type=int, default=100)
    ap.add_argument("--other-lev", type=int, default=100)
    ap.add_argument("--initial", type=float, default=150.0)
    ap.add_argument("--timestop", type=float, default=0.5,
                    help="超时强平倍数(信号周期的倍数), 0=关闭")
    ap.add_argument("--max-margin", type=float, default=DEFAULT_MAX_MARGIN,
                    help=f"单笔保证金封顶 USDT (默认 {DEFAULT_MAX_MARGIN:.0f}, "
                         f"传 0 = 不封顶)")
    ap.add_argument("--close-liq", action="store_true",
                    help="强平判定用收盘价(默认用盘中不利极值, 更接近现实)")
    ap.add_argument("--sweep", action="store_true", help="扫仓位×杠杆")
    args = ap.parse_args()

    pfs = [x.strip() for x in args.portfolios.split(",") if x.strip()]
    ts = args.timestop if args.timestop > 0 else None
    cons = not args.close_liq
    # --max-margin 0 → 不封顶
    mm = args.max_margin if args.max_margin and args.max_margin > 0 else None

    if args.sweep:
        print("=" * 92)
        print(f"仓位 × SOL杠杆 全扫描  (起始 {args.initial:.0f} USDT, "
              f"BTC/ETH 固定 {args.other_lev}x, 强平判定="
              f"{'盘中最坏' if cons else '收盘价'})")
        print("=" * 92)
        print(f"{'组合':4s} {'仓位':>5s} {'SOL杠杆':>7s} {'总名义/权益':>11s} "
              f"{'强平距离':>8s} {'结局':>8s} {'期末':>10s} {'月化':>8s} "
              f"{'年化':>9s} {'最大回撤':>8s} {'笔数':>5s} {'胜率':>6s}")
        print("-" * 92)
        for pf in pfs:
            for ppct in (0.10, 0.075, 0.05, 0.03, 0.02):
                for slev in (100, 50):
                    r = simulate(pf, ppct, slev, args.other_lev, args.initial,
                                 ts, mm, conservative_liq=cons)
                    mult = ppct * (args.other_lev * 2 + slev)
                    liqd = (1 - mult * MMR) / mult * 100 if mult > 0 else 0
                    fate = "爆仓" if r.ruined else "存活"
                    yr = (((1 + r.monthly_pct / 100) ** 12 - 1) * 100
                          if not r.ruined else -100.0)
                    wr = r.wins / r.trades * 100 if r.trades else 0
                    print(f"{pf:4s} {ppct*100:4.1f}% {slev:6d}x {mult:10.1f}× "
                          f"{liqd:7.2f}% {fate:>8s} "
                          f"{_fmt_money(r.final):>10s} "
                          f"{r.monthly_pct:+7.2f}% "
                          f"{yr:+8.1f}% {r.max_dd_pct:7.1f}% "
                          f"{r.trades:5d} {wr:5.1f}%")
            print()
        return 0

    for pf in pfs:
        r = simulate(pf, args.position_pct, args.sol_lev, args.other_lev,
                     args.initial, ts, mm, conservative_liq=cons)
        mult = args.position_pct * (args.other_lev * 2 + args.sol_lev)
        liqd = (1 - mult * MMR) / mult * 100 if mult > 0 else 0
        print("=" * 76)
        print(f"组合 {pf}  仓位 {args.position_pct*100:.1f}%  "
              f"SOL {args.sol_lev}x / BTC·ETH {args.other_lev}x")
        print("=" * 76)
        print(f"  总名义 = 权益的 {mult:.1f} 倍 → 强平距离 {liqd:.2f}%")
        print(f"  起始 {r.initial:,.0f} → 期末 {_fmt_money(r.final)} USDT"
              f"{'  【爆仓】' + (r.ruin_ts or '')[:16] if r.ruined else ''}")
        print(f"  成交 {r.trades} 笔, 胜率 "
              f"{r.wins/r.trades*100 if r.trades else 0:.2f}%")
        print(f"  月化(复利) {r.monthly_pct:+.2f}%   "
              f"年化 {((1+r.monthly_pct/100)**12-1)*100 if not r.ruined else -100:+.1f}%")
        print(f"  最大回撤 {r.max_dd_pct:.2f}%   强平次数 {r.liq_events}")
        if r.yearly:
            print("  年末权益:", {k: _fmt_money(v) for k, v in sorted(r.yearly.items())})
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
