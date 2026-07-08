"""按「信号桶」缩小周期回测 HighLow 策略。

语义(策略保持不变,只把「1 桶」的定义从「1 日」推广到任意时间桶):
  1) 拿一份细粒度 CSV(比如 5m),按信号周期 resample 成「信号桶」序列
  2) 对每个桶取 open/close/high/low(桶内 K 聚合)
  3) 阳(close>open) → 下一桶只挂多,入场 = 上一桶 low * (1 - float_pct)
     阴(close<open) → 下一桶只挂空,入场 = 上一桶 high * (1 + float_pct)
  4) 下一桶内用细粒度 K 逐根判 TP/SL:同根 K 同时穿 → SL 优先(保守)
  5) 未触发 → 桶末 EOD 撤单;触发未结算 → 桶末以 close 平仓 (EOB)

TP/SL % 与 float_pct 都是相对入场价的百分比。杠杆固定 100x。

用法(单跑一组):
  python scripts/bucket_backtest.py --pair BTC-USDT-SWAP --base 5m --signal 15m \\
      --float 0.002 --tp 0.012 --sl 0.008 --balance 300 --days 730
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


BASE_BAR_SECS = {
    "5m": 300, "15m": 900, "30m": 1800, "1H": 3600,
    "2H": 7200, "4H": 14400, "6H": 21600, "12H": 43200, "1D": 86400,
}

# resample rule mapping (pandas)
RESAMPLE_RULE = {
    "5m": "5min", "15m": "15min", "30m": "30min",
    "1H": "1h", "2H": "2h", "4H": "4h", "6H": "6h",
    "12H": "12h", "1D": "1D",
}


@dataclass
class BacktestResult:
    pair: str
    base_bar: str
    signal_bar: str
    float_pct: float
    tp_pct: float
    sl_pct: float
    initial: float
    final: float
    total_return_pct: float
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    max_dd_pct: float
    profit_factor: float
    monthly_pct: float
    n_signals: int  # 生成过多少个信号桶

    def as_row(self) -> dict:
        d = self.__dict__.copy()
        if d["profit_factor"] == float("inf"):
            d["profit_factor"] = "inf"
        for k in ("final", "total_return_pct", "win_rate_pct", "max_dd_pct",
                 "profit_factor", "monthly_pct"):
            v = d[k]
            if isinstance(v, float):
                d[k] = round(v, 3)
        return d


def _load_csv(pair: str, base_bar: str, days: int) -> pd.DataFrame:
    coin = pair.split("-")[0]
    path = ROOT / "csv_data" / f"{coin}_USDT_SWAP_{base_bar}_{days}d.csv"
    if not path.exists():
        raise FileNotFoundError(str(path))
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    df = df.set_index("ts")
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    return df


def _resample(df: pd.DataFrame, signal_bar: str) -> pd.DataFrame:
    rule = RESAMPLE_RULE[signal_bar]
    agg = df.resample(rule, label="left", closed="left").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    ).dropna()
    return agg


def simulate(df_base: pd.DataFrame, df_sig: pd.DataFrame,
             pair: str, base_bar: str, signal_bar: str,
             float_pct: float, tp_pct: float, sl_pct: float,
             initial_balance: float = 300.0,
             position_pct: float = 0.10,
             leverage: int = 100,
             fixed_margin: bool = True,
             taker_fee: float = 0.0005) -> BacktestResult:
    """
    fixed_margin=True(默认): 每笔 margin = initial * position_pct(不复利)。
      好处:MDD 有真实意义,收益率线性可比,爆仓门槛真实。
    fixed_margin=False: 每笔 margin = balance * position_pct(全额复利)。
      问题:小周期高频交易下复利指数放大,MDD 99% + 收益百万倍并存,不真实。
    taker_fee: 每笔进出场共扣两次 fee (= 2 * taker_fee * notional * pct 近似)。
      OKX SWAP taker 5bp,进出场 → 每笔总费 ≈ 10bp of notional。
    """
    """按信号桶逐个跑。信号桶用 df_sig(聚合),入场后在 df_base 里逐根 K 判 TP/SL。
    向量化版:所有信号桶入场/退出用 numpy 一次算完。"""
    import numpy as np

    n_buckets = len(df_sig)
    if n_buckets < 2:
        return BacktestResult(
            pair=pair, base_bar=base_bar, signal_bar=signal_bar,
            float_pct=float_pct, tp_pct=tp_pct, sl_pct=sl_pct,
            initial=initial_balance, final=initial_balance,
            total_return_pct=0, trades=0, wins=0, losses=0,
            win_rate_pct=0, max_dd_pct=0, profit_factor=0, monthly_pct=0,
            n_signals=n_buckets,
        )

    sig_o = df_sig["open"].to_numpy()
    sig_c = df_sig["close"].to_numpy()
    sig_h = df_sig["high"].to_numpy()
    sig_l = df_sig["low"].to_numpy()
    # 用 int64 ns 表达时间;避开 tz-aware datetime object 与 timedelta64 相加的错
    sig_ts_ns = df_sig.index.view("int64")

    base_ts_ns = df_base.index.view("int64")
    base_h = df_base["high"].to_numpy()
    base_l = df_base["low"].to_numpy()
    base_c = df_base["close"].to_numpy()

    bucket_secs = BASE_BAR_SECS[signal_bar]
    bucket_ns = bucket_secs * 1_000_000_000

    # 每个信号桶(除最后一个):方向 / 入场价 / tp / sl
    dir_arr = np.zeros(n_buckets - 1, dtype=np.int8)  # 1 long, -1 short, 0 skip
    dir_arr[sig_c[:-1] > sig_o[:-1]] = 1
    dir_arr[sig_c[:-1] < sig_o[:-1]] = -1
    # 入场价
    entry_long = sig_l[:-1] * (1 - float_pct)
    entry_short = sig_h[:-1] * (1 + float_pct)

    # 下一桶时间范围 [start_ns[i], end_ns[i])
    start_ns = sig_ts_ns[1:]
    end_ns = start_ns + bucket_ns

    # 每桶在 base 里的 [lo, hi)
    lo_arr = np.searchsorted(base_ts_ns, start_ns, side="left")
    hi_arr = np.searchsorted(base_ts_ns, end_ns, side="left")

    balance = initial_balance
    peak = balance
    max_dd_pct = 0.0
    pnls: list[float] = []

    # 因 balance 依赖上一笔,需要按顺序 —— 但每笔内部完全用 numpy 切片判断
    for i in range(n_buckets - 1):
        d = dir_arr[i]
        if d == 0:
            continue
        lo = lo_arr[i]
        hi = hi_arr[i]
        if lo >= hi:
            continue

        if d == 1:
            entry = entry_long[i]
            tp = entry * (1 + tp_pct)
            sl = entry * (1 - sl_pct)
            # 触发入场:第一根 low <= entry
            hit_entry = base_l[lo:hi] <= entry
            if not hit_entry.any():
                continue
            entry_off = int(hit_entry.argmax())  # 第一个 True
            ek = lo + entry_off

            # entry 起判 TP/SL
            sub_h = base_h[ek:hi]
            sub_l = base_l[ek:hi]
            sl_mask = sub_l <= sl
            tp_mask = sub_h >= tp
            sl_first = int(sl_mask.argmax()) if sl_mask.any() else -1
            tp_first = int(tp_mask.argmax()) if tp_mask.any() else -1

            if sl_first == -1 and tp_first == -1:
                exit_price = float(base_c[hi - 1])
            elif sl_first == -1:
                exit_price = tp
            elif tp_first == -1:
                exit_price = sl
            elif sl_first <= tp_first:
                # 同根双穿 → SL 优先
                exit_price = sl
            else:
                exit_price = tp
            pct = (exit_price - entry) / entry
        else:
            entry = entry_short[i]
            tp = entry * (1 - tp_pct)
            sl = entry * (1 + sl_pct)
            hit_entry = base_h[lo:hi] >= entry
            if not hit_entry.any():
                continue
            entry_off = int(hit_entry.argmax())
            ek = lo + entry_off

            sub_h = base_h[ek:hi]
            sub_l = base_l[ek:hi]
            sl_mask = sub_h >= sl
            tp_mask = sub_l <= tp
            sl_first = int(sl_mask.argmax()) if sl_mask.any() else -1
            tp_first = int(tp_mask.argmax()) if tp_mask.any() else -1

            if sl_first == -1 and tp_first == -1:
                exit_price = float(base_c[hi - 1])
            elif sl_first == -1:
                exit_price = tp
            elif tp_first == -1:
                exit_price = sl
            elif sl_first <= tp_first:
                exit_price = sl
            else:
                exit_price = tp
            pct = (entry - exit_price) / entry

        margin = (initial_balance if fixed_margin else balance) * position_pct
        # 手续费:开+平各扣一次 taker_fee(相对 notional)
        notional = margin * leverage
        fee = 2 * taker_fee * notional
        pnl = notional * pct - fee
        balance += pnl
        peak = max(peak, balance)
        if peak > 0:
            dd = (peak - balance) / peak * 100
            max_dd_pct = max(max_dd_pct, dd)
        pnls.append(pnl)
        if balance <= 0:
            balance = 0
            break

    trades = [{"pnl": p} for p in pnls]

    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    total = len(trades)
    win_rate = wins / total * 100 if total else 0
    gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    total_return_pct = (balance - initial_balance) / initial_balance * 100 if initial_balance > 0 else 0
    n_days = (df_sig.index[-1] - df_sig.index[0]).days or 1
    monthly = total_return_pct / (n_days / 30) if n_days >= 30 else total_return_pct

    return BacktestResult(
        pair=pair, base_bar=base_bar, signal_bar=signal_bar,
        float_pct=float_pct, tp_pct=tp_pct, sl_pct=sl_pct,
        initial=initial_balance, final=balance,
        total_return_pct=total_return_pct,
        trades=total, wins=wins, losses=losses,
        win_rate_pct=win_rate, max_dd_pct=max_dd_pct,
        profit_factor=pf, monthly_pct=monthly,
        n_signals=n_buckets,
    )


def _pick_base_bar(signal_bar: str) -> str:
    """选一个足够细的底粒度 K,让桶内至少能有 >=6 根 K 供判 TP/SL。
    优先用最粗但仍满足"每桶 >= 6 根"的 K(节省 IO),不满足则用 5m。
    """
    sig_secs = BASE_BAR_SECS[signal_bar]
    target_max_secs = sig_secs / 6.0  # 至少 6 根桶内 K
    # 从细到粗,选最粗且满足的
    candidates = [("5m", 300), ("15m", 900), ("30m", 1800), ("1H", 3600)]
    best = "5m"
    for name, secs in candidates:
        if secs <= target_max_secs:
            best = name
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--base", default=None,
                    help="底粒度 K (5m/15m/30m/1H);缺省按 signal 推断")
    ap.add_argument("--signal", required=True,
                    help="信号周期 (5m/15m/30m/1H/2H/4H/6H/12H)")
    ap.add_argument("--float", dest="float_pct", type=float, default=0.002)
    ap.add_argument("--tp", dest="tp_pct", type=float, default=0.012)
    ap.add_argument("--sl", dest="sl_pct", type=float, default=0.008)
    ap.add_argument("--balance", type=float, default=300.0)
    ap.add_argument("--position-pct", type=float, default=0.10)
    ap.add_argument("--leverage", type=int, default=100)
    ap.add_argument("--days", type=int, default=730)
    args = ap.parse_args()

    base_bar = args.base or _pick_base_bar(args.signal)
    df_base = _load_csv(args.pair, base_bar, args.days)
    df_sig = _resample(df_base, args.signal)

    res = simulate(
        df_base, df_sig, args.pair, base_bar, args.signal,
        args.float_pct, args.tp_pct, args.sl_pct,
        initial_balance=args.balance,
        position_pct=args.position_pct, leverage=args.leverage,
    )

    print(f"=== {res.pair} base={res.base_bar} signal={res.signal_bar} ===")
    print(f"  float={res.float_pct} tp={res.tp_pct} sl={res.sl_pct}")
    print(f"  return={res.total_return_pct:+.2f}% monthly={res.monthly_pct:+.2f}%")
    print(f"  trades={res.trades} win={res.win_rate_pct:.1f}% mdd={res.max_dd_pct:.2f}%")
    print(f"  pf={res.profit_factor if res.profit_factor != float('inf') else 'inf'}")


if __name__ == "__main__":
    main()
