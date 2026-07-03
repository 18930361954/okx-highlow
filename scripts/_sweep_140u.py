"""参数扫描一年 12 个月，输出汇总表。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_140u_v2 import Params, load_ohlcv, backtest  # noqa: E402


CFG = [
    ("BTC", "csv_data/BTC_USDT_SWAP_4H_400d.csv", "csv_data/BTC_USDT_SWAP_15m_400d.csv"),
    ("ETH", "csv_data/ETH_USDT_SWAP_4H_400d.csv", "csv_data/ETH_USDT_SWAP_15m_400d.csv"),
]
START = "2025-07-01"
END = "2026-07-01"


def one(label, f4, f15, vm, tol, win):
    p = Params(vol_mult=vm, pullback_tol=tol, pullback_win_4h=win)
    df4 = load_ohlcv(Path(f4), START, END)
    df15 = load_ohlcv(Path(f15), START, END)
    trades, state, ec = backtest(df4, df15, p)
    main = [t for t in trades if not t.is_pyramid]
    n = len(main)
    if n == 0:
        return dict(n=0)
    wins = [t for t in main if t.pnl_u > 0]
    tp = sum(1 for t in main if t.reason == "TP")
    sl = sum(1 for t in main if t.reason == "SL")
    be = sum(1 for t in main if t.reason == "BE")
    pf_num = sum(t.pnl_u for t in main if t.pnl_u > 0) + sum(t.pnl_u for t in trades if t.is_pyramid and t.pnl_u > 0)
    pf_den = -sum(t.pnl_u for t in main if t.pnl_u <= 0) - sum(t.pnl_u for t in trades if t.is_pyramid and t.pnl_u <= 0)
    eq_vals = [e for _, e in ec]
    peak = eq_vals[0]; mdd = 0.0
    for e in eq_vals:
        peak = max(peak, e); mdd = max(mdd, (peak - e) / peak)
    total_pnl = sum(t.pnl_u for t in trades)
    ret = (state.capital + state.withdrawn - p.initial_capital) / p.initial_capital
    return dict(
        n=n, wr=len(wins)/n, tp=tp, sl=sl, be=be,
        ret=ret, pnl=total_pnl, mdd=mdd,
        pf=(pf_num/pf_den) if pf_den > 0 else 99.0,
        cap=state.capital, wd=state.withdrawn,
        n_py=sum(1 for t in trades if t.is_pyramid),
    )


def main():
    print(f"{'label':<3} {'vm':>4} {'tol':>6} {'win':>4} | {'n':>4} {'wr':>6} {'pf':>5} {'ret':>7} "
          f"{'pnl':>8} {'mdd':>6} {'cap':>7} {'wd':>4} {'TP/SL/BE':>10}")
    for lab, f4, f15 in CFG:
        for vm in [1.5, 2.0, 2.5, 3.0]:
            for tol in [0.001, 0.002, 0.003]:
                for win in [1, 2, 3]:
                    r = one(lab, f4, f15, vm, tol, win)
                    if r["n"] == 0:
                        print(f"{lab:<3} {vm:>4} {tol:>6} {win:>4} | (no trades)")
                        continue
                    print(f"{lab:<3} {vm:>4} {tol:>6} {win:>4} | "
                          f"{r['n']:>4} {r['wr']*100:>5.1f}% {r['pf']:>5.2f} "
                          f"{r['ret']*100:>+6.1f}% {r['pnl']:>+8.1f} {r['mdd']*100:>5.1f}% "
                          f"{r['cap']:>7.1f} {r['wd']:>4.0f} "
                          f"{r['tp']:>3}/{r['sl']:>3}/{r['be']:>2}")


if __name__ == "__main__":
    main()
