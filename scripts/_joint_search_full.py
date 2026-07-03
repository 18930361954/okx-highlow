"""联合回测全参数寻优（145U 起，2 年，全 maker 0.06% 成本）。

策略：逐阶段协调下降（避免全笛卡尔积爆炸）
  阶段 1（已跑）: 仓位百分比 → 起点 BTC 5% / ETH 12%
  阶段 2: 固定其他，扫 ETH TP × SL
  阶段 3: 固定其他，扫 BTC TP × SL
  阶段 4: 扫 leverage × 位置微调
  阶段 5: 扫 reentry_floats
  阶段 6: 最终微调 + 三档结果（激进/均衡/保守）
"""
import sys
import copy
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest import load_csv  # noqa: E402
from scripts.backtest_joint import joint_backtest, load_cfg  # noqa: E402


INITIAL = 145.0
FEE = 0.0004
SLIP = 0.0002
Y_START = pd.Timestamp("2024-07-01", tz="UTC")
Y_MID = pd.Timestamp("2025-07-01", tz="UTC")
Y_END = pd.Timestamp("2026-07-01", tz="UTC")

# 全局缓存 CSV 避免反复读盘
DF_BTC = None
DF_ETH = None


def load():
    global DF_BTC, DF_ETH
    if DF_BTC is None:
        DF_BTC = load_csv(Path("csv_data/BTC_USDT_SWAP_1H_780d.csv"))
        DF_ETH = load_csv(Path("csv_data/ETH_USDT_SWAP_1H_780d.csv"))
    return DF_BTC.copy(), DF_ETH.copy()


def run(cfg):
    df_btc, df_eth = load()
    return joint_backtest(df_btc, df_eth, cfg,
                          initial=INITIAL, start=Y_START, end=Y_END,
                          fee_rt=FEE, slippage_rt=SLIP, year1_end=Y_MID)


def summarize(r):
    y1 = r["year1_end_balance"]
    y1_ret = (y1 - INITIAL) / INITIAL if y1 else 0
    y2_ret = (r["final"] - y1) / y1 if y1 else 0
    return {
        "ret": r["total_return"], "mdd": r["max_dd"],
        "y1": y1_ret, "y2": y2_ret,
        "final": r["final"], "monthly": r["monthly"],
        "btc_pnl": r["per_pair"]["BTC-USDT-SWAP"]["pnl"],
        "eth_pnl": r["per_pair"]["ETH-USDT-SWAP"]["pnl"],
        "btc_n": r["per_pair"]["BTC-USDT-SWAP"]["n"],
        "eth_n": r["per_pair"]["ETH-USDT-SWAP"]["n"],
        "btc_w": r["per_pair"]["BTC-USDT-SWAP"]["w"],
        "eth_w": r["per_pair"]["ETH-USDT-SWAP"]["w"],
    }


def stage2_eth_tpsl(base):
    """扫 ETH TP × SL（BTC 保留 config，其他不变）"""
    print("\n" + "=" * 100)
    print("[阶段 2] ETH TP × SL 网格 (BTC pos=5%, ETH pos=12%, leverage=100x)")
    print("=" * 100)
    tp_grid = [0.015, 0.0175, 0.020, 0.0225, 0.025, 0.0275, 0.030]
    sl_grid = [0.004, 0.005, 0.006, 0.007, 0.008, 0.010]
    print(f"{'ETH TP':>7} {'ETH SL':>7} {'R:R':>5} | {'2Y%':>8} {'Y1%':>6} {'Y2%':>7} "
          f"{'MDD%':>5} {'ETH 胜率':>8} {'ETH PnL':>10}")
    print("-" * 90)
    all_res = []
    for tp in tp_grid:
        for sl in sl_grid:
            cfg = copy.deepcopy(base)
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["position_pct"] = 0.05
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["position_pct"] = 0.12
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = tp
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = sl
            r = run(cfg)
            s = summarize(r)
            wr = s["eth_w"] / s["eth_n"] * 100 if s["eth_n"] > 0 else 0
            print(f"{tp*100:>6.2f}% {sl*100:>6.2f}% {tp/sl:>4.2f} | "
                  f"{s['ret']*100:>+7.1f}% {s['y1']*100:>+5.1f}% {s['y2']*100:>+6.1f}% "
                  f"{s['mdd']*100:>4.1f}% {wr:>7.1f}% {s['eth_pnl']:>+10.0f}")
            all_res.append({"tp": tp, "sl": sl, **s})
    all_res.sort(key=lambda x: x["ret"], reverse=True)
    print(f"\n[Top 5 by 收益]")
    for x in all_res[:5]:
        print(f"  TP={x['tp']*100:.2f}% SL={x['sl']*100:.2f}% "
              f"收益={x['ret']*100:+.1f}% MDD={x['mdd']*100:.1f}%")
    return all_res[0]  # 返回最优


def stage3_btc_tpsl(base, best_eth):
    print("\n" + "=" * 100)
    print(f"[阶段 3] BTC TP × SL 网格 (ETH 用阶段 2 最优 TP={best_eth['tp']*100:.2f}%/SL={best_eth['sl']*100:.2f}%)")
    print("=" * 100)
    tp_grid = [0.010, 0.0125, 0.015, 0.0175, 0.020, 0.025]
    sl_grid = [0.003, 0.004, 0.005, 0.006, 0.008]
    print(f"{'BTC TP':>7} {'BTC SL':>7} {'R:R':>5} | {'2Y%':>8} {'Y1%':>6} {'Y2%':>7} "
          f"{'MDD%':>5} {'BTC 胜率':>8} {'BTC PnL':>10}")
    print("-" * 90)
    all_res = []
    for tp in tp_grid:
        for sl in sl_grid:
            cfg = copy.deepcopy(base)
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["position_pct"] = 0.05
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["position_pct"] = 0.12
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = best_eth["tp"]
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = best_eth["sl"]
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"] = tp
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"] = sl
            r = run(cfg)
            s = summarize(r)
            wr = s["btc_w"] / s["btc_n"] * 100 if s["btc_n"] > 0 else 0
            print(f"{tp*100:>6.2f}% {sl*100:>6.2f}% {tp/sl:>4.2f} | "
                  f"{s['ret']*100:>+7.1f}% {s['y1']*100:>+5.1f}% {s['y2']*100:>+6.1f}% "
                  f"{s['mdd']*100:>4.1f}% {wr:>7.1f}% {s['btc_pnl']:>+10.0f}")
            all_res.append({"tp": tp, "sl": sl, **s})
    all_res.sort(key=lambda x: x["ret"], reverse=True)
    print(f"\n[Top 5 by 收益]")
    for x in all_res[:5]:
        print(f"  TP={x['tp']*100:.2f}% SL={x['sl']*100:.2f}% "
              f"收益={x['ret']*100:+.1f}% MDD={x['mdd']*100:.1f}%")
    return all_res[0]


def stage4_leverage(base, best_eth, best_btc):
    print("\n" + "=" * 100)
    print("[阶段 4] Leverage 扫描（同时按比例微调 pos 保持 notional 大致不变）")
    print("=" * 100)
    combos = [
        (50,  0.10, 0.24),
        (75,  0.067, 0.16),
        (100, 0.05, 0.12),   # 基准
        (125, 0.04, 0.096),
        (150, 0.033, 0.08),
    ]
    print(f"{'lev':>4} {'BTC pos':>8} {'ETH pos':>8} | {'2Y%':>9} {'Y1%':>6} {'Y2%':>7} {'MDD%':>5}")
    print("-" * 75)
    all_res = []
    for lev, bp, ep in combos:
        cfg = copy.deepcopy(base)
        cfg["strategy"]["leverage"] = lev
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["position_pct"] = bp
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["position_pct"] = ep
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = best_eth["tp"]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = best_eth["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"] = best_btc["tp"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"] = best_btc["sl"]
        r = run(cfg)
        s = summarize(r)
        print(f"{lev:>3}x {bp*100:>6.1f}% {ep*100:>6.1f}% | "
              f"{s['ret']*100:>+8.1f}% {s['y1']*100:>+5.1f}% {s['y2']*100:>+6.1f}% "
              f"{s['mdd']*100:>4.1f}%")
        all_res.append({"lev": lev, "bp": bp, "ep": ep, **s})
    return all_res


def stage5_reentry(base, best_eth, best_btc):
    print("\n" + "=" * 100)
    print("[阶段 5] reentry_floats 扫描（重挂参数）")
    print("=" * 100)
    combos = [
        # (BTC 尝试列表, ETH 尝试列表)
        ("baseline",       [0.002, 0.004],           [0.0015, 0.006]),
        ("single_only",    [0.002],                  [0.0015]),
        ("btc_add_3rd",    [0.002, 0.004, 0.006],    [0.0015, 0.006]),
        ("eth_add_3rd",    [0.002, 0.004],           [0.0015, 0.003, 0.006]),
        ("both_3rd",       [0.002, 0.004, 0.006],    [0.0015, 0.003, 0.006]),
        ("btc_tight",      [0.0015, 0.003],          [0.0015, 0.006]),
        ("btc_wide",       [0.003, 0.005],           [0.0015, 0.006]),
        ("eth_tight",      [0.002, 0.004],           [0.001, 0.003]),
        ("eth_wide",       [0.002, 0.004],           [0.002, 0.008]),
    ]
    print(f"{'name':<15} {'BTC re':<22} {'ETH re':<22} | {'2Y%':>9} {'MDD%':>5} {'BTC笔':>5} {'ETH笔':>5}")
    print("-" * 108)
    all_res = []
    for name, btc_re, eth_re in combos:
        cfg = copy.deepcopy(base)
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["position_pct"] = 0.05
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["position_pct"] = 0.12
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = best_eth["tp"]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = best_eth["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"] = best_btc["tp"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"] = best_btc["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["reentry_floats"] = btc_re
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["reentry_floats"] = eth_re
        r = run(cfg)
        s = summarize(r)
        print(f"{name:<15} {str(btc_re):<22} {str(eth_re):<22} | "
              f"{s['ret']*100:>+8.1f}% {s['mdd']*100:>4.1f}% "
              f"{s['btc_n']:>5} {s['eth_n']:>5}")
        all_res.append({"name": name, "btc_re": btc_re, "eth_re": eth_re, **s})
    all_res.sort(key=lambda x: x["ret"], reverse=True)
    print(f"\n[Top 3]")
    for x in all_res[:3]:
        print(f"  {x['name']}: 收益={x['ret']*100:+.1f}% MDD={x['mdd']*100:.1f}%")
    return all_res[0]


def stage6_final(base, best_eth, best_btc, best_reentry):
    """最终三档：稳健 / 均衡 / 激进"""
    print("\n" + "=" * 100)
    print("[阶段 6] 最终推荐三档")
    print("=" * 100)
    final = []
    for name, bp, ep in [("稳健 (BTC 5% / ETH 10%)", 0.05, 0.10),
                          ("均衡 (BTC 5% / ETH 12%)", 0.05, 0.12),
                          ("激进 (BTC 5% / ETH 15%)", 0.05, 0.15)]:
        cfg = copy.deepcopy(base)
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["position_pct"] = bp
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["position_pct"] = ep
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = best_eth["tp"]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = best_eth["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"] = best_btc["tp"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"] = best_btc["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["reentry_floats"] = best_reentry["btc_re"]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["reentry_floats"] = best_reentry["eth_re"]
        r = run(cfg)
        s = summarize(r)
        print(f"\n{name}")
        print(f"  参数: BTC TP/SL = {best_btc['tp']*100:.2f}%/{best_btc['sl']*100:.2f}%   "
              f"ETH TP/SL = {best_eth['tp']*100:.2f}%/{best_eth['sl']*100:.2f}%   "
              f"reentry = {best_reentry['name']}")
        print(f"  2Y 收益: {s['ret']*100:+.1f}%   Y1 {s['y1']*100:+.1f}%   Y2 {s['y2']*100:+.1f}%")
        print(f"  最大回撤: {s['mdd']*100:.1f}%   月化: {s['monthly']*100:+.2f}%")
        print(f"  Y2 末余额: {s['final']:.2f} U (从 145U 起)")
        print(f"  BTC 贡献: {s['btc_pnl']:+.1f}U ({s['btc_n']} 笔, 胜率 {s['btc_w']/max(s['btc_n'],1)*100:.1f}%)")
        print(f"  ETH 贡献: {s['eth_pnl']:+.1f}U ({s['eth_n']} 笔, 胜率 {s['eth_w']/max(s['eth_n'],1)*100:.1f}%)")
        final.append({"name": name, "bp": bp, "ep": ep, **s})
    return final


def main():
    base = load_cfg()
    print("=" * 100)
    print(f"起始 145U，2 年连续复利，全 maker 成本 0.06%（费{FEE*100:.3f}%+滑{SLIP*100:.3f}%）")
    print("=" * 100)

    best_eth = stage2_eth_tpsl(base)
    print(f"\n>>> 阶段 2 最优 ETH TP/SL: {best_eth['tp']*100:.2f}% / {best_eth['sl']*100:.2f}%")

    best_btc = stage3_btc_tpsl(base, best_eth)
    print(f"\n>>> 阶段 3 最优 BTC TP/SL: {best_btc['tp']*100:.2f}% / {best_btc['sl']*100:.2f}%")

    stage4_leverage(base, best_eth, best_btc)

    best_reentry = stage5_reentry(base, best_eth, best_btc)
    print(f"\n>>> 阶段 5 最优 reentry: {best_reentry['name']}")

    stage6_final(base, best_eth, best_btc, best_reentry)


if __name__ == "__main__":
    main()
