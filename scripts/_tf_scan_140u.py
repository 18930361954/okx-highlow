"""扫描"3根K线箱体+放量突破+回踩"策略在不同时间框架组合下的表现。"""
import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_140u_v2 import Params, backtest, load_ohlcv  # noqa: E402

START, END = "2025-07-01", "2026-07-01"


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    d = df.set_index("ts")
    o = d["open"].resample(rule, origin="epoch").first()
    h = d["high"].resample(rule, origin="epoch").max()
    l = d["low"].resample(rule, origin="epoch").min()
    c = d["close"].resample(rule, origin="epoch").last()
    v = d["volume"].resample(rule, origin="epoch").sum()
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}).dropna().reset_index()


def load_base(pair):
    return load_ohlcv(Path(f"csv_data/{pair}_USDT_SWAP_15m_400d.csv"), START, END)


def rule_minutes(rule: str) -> int:
    m = {"15min": 15, "30min": 30, "1h": 60, "2h": 120, "4h": 240,
         "8h": 480, "12h": 720, "1D": 1440}
    return m[rule]


def summarize(trades, state, ec, p):
    main = [t for t in trades if not t.is_pyramid]
    n = len(main)
    if n == 0:
        return dict(n=0)
    wins = [t for t in main if t.pnl_u > 0]
    pf_num = sum(t.pnl_u for t in trades if t.pnl_u > 0)
    pf_den = -sum(t.pnl_u for t in trades if t.pnl_u <= 0)
    eq_vals = [e for _, e in ec]
    peak = eq_vals[0]; mdd = 0.0
    for e in eq_vals:
        peak = max(peak, e); mdd = max(mdd, (peak - e) / peak)
    ret = (state.capital + state.withdrawn - p.initial_capital) / p.initial_capital
    return dict(n=n, wr=len(wins)/n, ret=ret, mdd=mdd,
                pf=(pf_num/pf_den) if pf_den > 0 else 99.0)


COMBOS = [
    ("4H+15m", "4h", "15min"),
    ("4H+1h",  "4h", "1h"),
    ("8H+30m", "8h", "30min"),
    ("8H+1h",  "8h", "1h"),
    ("1D+1h",  "1D", "1h"),
    ("1D+4h",  "1D", "4h"),
    ("2H+15m", "2h", "15min"),
    ("2H+30m", "2h", "30min"),
]


def main():
    print(f"{'pair':<4} {'combo':<9} {'vm':>4} | {'n':>4} {'wr':>6} {'pf':>5} {'ret':>7} {'mdd':>6}")
    for pair in ["BTC", "ETH"]:
        df15 = load_base(pair)
        for lab, mrule, erule in COMBOS:
            df_main = resample(df15, mrule)
            df_entry = df15 if erule == "15min" else resample(df15, erule)
            # 突破后 2 根主时框窗口
            pullback_hours = rule_minutes(mrule) * 2 / 60
            for vm in [1.5, 2.0, 2.5]:
                p = Params(vol_mult=vm, pullback_tol=0.002, pullback_hours=pullback_hours)
                trades, state, ec = backtest(df_main, df_entry, p)
                r = summarize(trades, state, ec, p)
                if r["n"] == 0:
                    print(f"{pair:<4} {lab:<9} {vm:>4} | (no trades)")
                    continue
                print(f"{pair:<4} {lab:<9} {vm:>4} | "
                      f"{r['n']:>4} {r['wr']*100:>5.1f}% {r['pf']:>5.2f} "
                      f"{r['ret']*100:>+6.1f}% {r['mdd']*100:>5.1f}%")
        print()


if __name__ == "__main__":
    main()
