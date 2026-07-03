"""二轮寻优：在均衡档基础上继续深挖剩余参数。

基线：BTC pos=5%, TP=1.25%, SL=0.80%, ETH pos=12%, TP=1.5%, SL=0.80%
       reentry baseline, leverage=100x, max_prev_amp BTC 4.75%/ETH 8%, min_prev_amp 1%
基线收益：+22,978%, MDD 66.5%

阶段 7：TP/SL 精细化（±0.05%~0.25%）
阶段 8：float_pct（首次浮动）
阶段 9：熔断阈值 max_consecutive_losses × cooldown_hours
阶段 10：min_prev_amp（下界）
阶段 11：reentry_floats 精细化
阶段 12：BTC pos 精细化 3-8%
阶段 13：最终整合
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


# 均衡档基础配置
def base_cfg():
    b = load_cfg()
    b["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["position_pct"] = 0.05
    b["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"] = 0.0125
    b["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"] = 0.008
    b["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["position_pct"] = 0.12
    b["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = 0.015
    b["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = 0.008
    return b


def stage7_tpsl_fine():
    print("\n" + "=" * 100)
    print("[阶段 7] BTC × ETH TP/SL 精细化（在均衡档附近 ±0.05%~0.25%）")
    print("=" * 100)
    # ETH 精细
    print("\n>> ETH TP × SL 精细（BTC 保持 1.25/0.80）")
    eth_tps = [0.0125, 0.014, 0.015, 0.016, 0.0175]
    eth_sls = [0.006, 0.007, 0.008, 0.009, 0.010]
    print(f"{'ETH TP':>7} {'ETH SL':>7} {'R:R':>5} | {'2Y%':>9} {'Y1%':>6} {'Y2%':>7} {'MDD%':>5} {'ETH胜率':>7}")
    print("-" * 80)
    best_eth = None
    for tp in eth_tps:
        for sl in eth_sls:
            cfg = base_cfg()
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = tp
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = sl
            r = run(cfg); s = summarize(r)
            wr = s["eth_w"]/max(s["eth_n"],1)*100
            print(f"{tp*100:>6.2f}% {sl*100:>6.2f}% {tp/sl:>4.2f} | "
                  f"{s['ret']*100:>+8.1f}% {s['y1']*100:>+5.1f}% {s['y2']*100:>+6.1f}% "
                  f"{s['mdd']*100:>4.1f}% {wr:>6.1f}%")
            if best_eth is None or s["ret"] > best_eth["ret"]:
                best_eth = {"tp": tp, "sl": sl, **s}
    print(f"\n>>> ETH 精细最优: TP={best_eth['tp']*100:.2f}% SL={best_eth['sl']*100:.2f}% 收益={best_eth['ret']*100:+.1f}%")

    # BTC 精细（用刚找到的最优 ETH）
    print("\n>> BTC TP × SL 精细")
    btc_tps = [0.010, 0.0115, 0.0125, 0.014, 0.015]
    btc_sls = [0.006, 0.007, 0.008, 0.009, 0.010]
    print(f"{'BTC TP':>7} {'BTC SL':>7} {'R:R':>5} | {'2Y%':>9} {'Y1%':>6} {'Y2%':>7} {'MDD%':>5} {'BTC胜率':>7}")
    print("-" * 80)
    best_btc = None
    for tp in btc_tps:
        for sl in btc_sls:
            cfg = base_cfg()
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = best_eth["tp"]
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = best_eth["sl"]
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"] = tp
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"] = sl
            r = run(cfg); s = summarize(r)
            wr = s["btc_w"]/max(s["btc_n"],1)*100
            print(f"{tp*100:>6.2f}% {sl*100:>6.2f}% {tp/sl:>4.2f} | "
                  f"{s['ret']*100:>+8.1f}% {s['y1']*100:>+5.1f}% {s['y2']*100:>+6.1f}% "
                  f"{s['mdd']*100:>4.1f}% {wr:>6.1f}%")
            if best_btc is None or s["ret"] > best_btc["ret"]:
                best_btc = {"tp": tp, "sl": sl, **s}
    print(f"\n>>> BTC 精细最优: TP={best_btc['tp']*100:.2f}% SL={best_btc['sl']*100:.2f}% 收益={best_btc['ret']*100:+.1f}%")
    return best_btc, best_eth


def stage8_float_pct(best_btc, best_eth):
    print("\n" + "=" * 100)
    print("[阶段 8] float_pct（首次入场浮动）")
    print("=" * 100)
    combos = [
        # (BTC float, ETH float)
        (0.001, 0.001), (0.001, 0.0015), (0.001, 0.002),
        (0.0015, 0.001), (0.0015, 0.0015), (0.0015, 0.002),
        (0.002, 0.001), (0.002, 0.0015), (0.002, 0.002),
        (0.0025, 0.0015), (0.0025, 0.002),
        (0.003, 0.0015), (0.003, 0.002),
    ]
    print(f"{'BTC float':>9} {'ETH float':>9} | {'2Y%':>9} {'MDD%':>5} {'BTC笔':>5} {'ETH笔':>5}")
    print("-" * 65)
    best = None
    for bf, ef in combos:
        cfg = base_cfg()
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = best_eth["tp"]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = best_eth["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"] = best_btc["tp"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"] = best_btc["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["float_pct"] = bf
        # ETH 用 float_pct 但代码里 ETH 主要靠 reentry_floats 第一个
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["reentry_floats"] = [ef, 0.006]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["reentry_floats"] = [bf, 0.004]
        r = run(cfg); s = summarize(r)
        print(f"{bf*100:>7.3f}% {ef*100:>8.3f}% | "
              f"{s['ret']*100:>+8.1f}% {s['mdd']*100:>4.1f}% "
              f"{s['btc_n']:>5} {s['eth_n']:>5}")
        if best is None or s["ret"] > best["ret"]:
            best = {"bf": bf, "ef": ef, **s}
    print(f"\n>>> 最优 float: BTC={best['bf']*100:.3f}% ETH={best['ef']*100:.3f}% 收益={best['ret']*100:+.1f}%")
    return best


def stage9_circuit_breaker(best_btc, best_eth, best_float):
    print("\n" + "=" * 100)
    print("[阶段 9] 熔断阈值")
    print("=" * 100)
    combos = [
        (2, 12), (2, 24), (2, 48),
        (3, 12), (3, 24), (3, 48), (3, 72),
        (4, 24), (4, 48), (4, 72),
        (5, 24), (5, 48),
        (10, 24),  # 相当于关闭熔断
    ]
    print(f"{'max_loss':>8} {'cooldown_h':>10} | {'2Y%':>9} {'MDD%':>5}")
    print("-" * 55)
    best = None
    for ml, ch in combos:
        cfg = base_cfg()
        cfg["strategy"]["max_consecutive_losses"] = ml
        cfg["strategy"]["cooldown_hours"] = ch
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = best_eth["tp"]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = best_eth["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"] = best_btc["tp"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"] = best_btc["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["float_pct"] = best_float["bf"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["reentry_floats"] = [best_float["bf"], 0.004]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["reentry_floats"] = [best_float["ef"], 0.006]
        r = run(cfg); s = summarize(r)
        print(f"{ml:>7} {ch:>8}h | {s['ret']*100:>+8.1f}% {s['mdd']*100:>4.1f}%")
        if best is None or s["ret"] > best["ret"]:
            best = {"ml": ml, "ch": ch, **s}
    print(f"\n>>> 最优熔断: max_loss={best['ml']} cooldown={best['ch']}h 收益={best['ret']*100:+.1f}%")
    return best


def stage10_min_amp(best_btc, best_eth, best_float, best_cb):
    print("\n" + "=" * 100)
    print("[阶段 10] min_prev_amp（下界）")
    print("=" * 100)
    combos = [
        (0.000, 0.000),  # 关闭下界
        (0.005, 0.005),
        (0.010, 0.010),  # 现状
        (0.015, 0.015),
        (0.020, 0.020),
        (0.010, 0.005),
        (0.005, 0.010),
        (0.010, 0.015),
        (0.015, 0.010),
    ]
    print(f"{'BTC min':>8} {'ETH min':>8} | {'2Y%':>9} {'MDD%':>5} {'BTC笔':>5} {'ETH笔':>5}")
    print("-" * 70)
    best = None
    for bm, em in combos:
        cfg = base_cfg()
        cfg["strategy"]["max_consecutive_losses"] = best_cb["ml"]
        cfg["strategy"]["cooldown_hours"] = best_cb["ch"]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = best_eth["tp"]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = best_eth["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"] = best_btc["tp"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"] = best_btc["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["float_pct"] = best_float["bf"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["reentry_floats"] = [best_float["bf"], 0.004]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["reentry_floats"] = [best_float["ef"], 0.006]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["min_prev_amp"] = bm
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["min_prev_amp"] = em
        r = run(cfg); s = summarize(r)
        print(f"{bm*100:>6.1f}% {em*100:>7.1f}% | "
              f"{s['ret']*100:>+8.1f}% {s['mdd']*100:>4.1f}% "
              f"{s['btc_n']:>5} {s['eth_n']:>5}")
        if best is None or s["ret"] > best["ret"]:
            best = {"bm": bm, "em": em, **s}
    print(f"\n>>> 最优 min_prev_amp: BTC={best['bm']*100:.2f}% ETH={best['em']*100:.2f}% 收益={best['ret']*100:+.1f}%")
    return best


def stage11_reentry_fine(best_btc, best_eth, best_float, best_cb, best_min):
    print("\n" + "=" * 100)
    print("[阶段 11] reentry_floats 第二次浮动精细化 + 是否加第 3 次")
    print("=" * 100)
    combos = [
        ("baseline", [best_float["bf"], 0.004], [best_float["ef"], 0.006]),
        ("BTC 2nd=0.003", [best_float["bf"], 0.003], [best_float["ef"], 0.006]),
        ("BTC 2nd=0.005", [best_float["bf"], 0.005], [best_float["ef"], 0.006]),
        ("BTC 2nd=0.006", [best_float["bf"], 0.006], [best_float["ef"], 0.006]),
        ("ETH 2nd=0.004", [best_float["bf"], 0.004], [best_float["ef"], 0.004]),
        ("ETH 2nd=0.005", [best_float["bf"], 0.004], [best_float["ef"], 0.005]),
        ("ETH 2nd=0.008", [best_float["bf"], 0.004], [best_float["ef"], 0.008]),
        ("BTC 3-attempt(0.006)", [best_float["bf"], 0.004, 0.006], [best_float["ef"], 0.006]),
        ("BTC 3-attempt(0.008)", [best_float["bf"], 0.004, 0.008], [best_float["ef"], 0.006]),
        ("both 3-attempt", [best_float["bf"], 0.004, 0.008], [best_float["ef"], 0.006, 0.010]),
    ]
    print(f"{'name':<22} {'BTC re':<25} {'ETH re':<20} | {'2Y%':>9} {'MDD%':>5}")
    print("-" * 100)
    best = None
    for name, br, er in combos:
        cfg = base_cfg()
        cfg["strategy"]["max_consecutive_losses"] = best_cb["ml"]
        cfg["strategy"]["cooldown_hours"] = best_cb["ch"]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = best_eth["tp"]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = best_eth["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"] = best_btc["tp"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"] = best_btc["sl"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["float_pct"] = best_float["bf"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["min_prev_amp"] = best_min["bm"]
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["min_prev_amp"] = best_min["em"]
        cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["reentry_floats"] = br
        cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["reentry_floats"] = er
        r = run(cfg); s = summarize(r)
        print(f"{name:<22} {str(br):<25} {str(er):<20} | "
              f"{s['ret']*100:>+8.1f}% {s['mdd']*100:>4.1f}%")
        if best is None or s["ret"] > best["ret"]:
            best = {"name": name, "br": br, "er": er, **s}
    print(f"\n>>> 最优 reentry: {best['name']} 收益={best['ret']*100:+.1f}%")
    return best


def stage12_btc_pos_fine(best_btc, best_eth, best_float, best_cb, best_min, best_re):
    print("\n" + "=" * 100)
    print("[阶段 12] BTC pos 精细化 3-8%（ETH 固定 12%）")
    print("=" * 100)
    print(f"{'BTC pos':>8} {'ETH pos':>8} | {'2Y%':>9} {'MDD%':>5}")
    print("-" * 55)
    best = None
    for bp in [0.03, 0.04, 0.05, 0.06, 0.07, 0.08]:
        for ep in [0.10, 0.12, 0.14]:
            cfg = base_cfg()
            cfg["strategy"]["max_consecutive_losses"] = best_cb["ml"]
            cfg["strategy"]["cooldown_hours"] = best_cb["ch"]
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["position_pct"] = bp
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["position_pct"] = ep
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = best_eth["tp"]
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = best_eth["sl"]
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"] = best_btc["tp"]
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"] = best_btc["sl"]
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["float_pct"] = best_float["bf"]
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["min_prev_amp"] = best_min["bm"]
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["min_prev_amp"] = best_min["em"]
            cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["reentry_floats"] = best_re["br"]
            cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["reentry_floats"] = best_re["er"]
            r = run(cfg); s = summarize(r)
            print(f"{bp*100:>6.1f}% {ep*100:>7.1f}% | "
                  f"{s['ret']*100:>+8.1f}% {s['mdd']*100:>4.1f}%")
            if best is None or s["ret"] > best["ret"]:
                best = {"bp": bp, "ep": ep, **s}
    print(f"\n>>> 最优仓位: BTC={best['bp']*100:.1f}% ETH={best['ep']*100:.1f}% 收益={best['ret']*100:+.1f}%")
    return best


def stage13_final(best_btc, best_eth, best_float, best_cb, best_min, best_re, best_pos):
    print("\n" + "=" * 100)
    print("[阶段 13] 最终整合结果 vs 均衡档基线")
    print("=" * 100)

    # 均衡档基线
    r0 = run(base_cfg())
    s0 = summarize(r0)

    # 最优整合
    cfg = base_cfg()
    cfg["strategy"]["max_consecutive_losses"] = best_cb["ml"]
    cfg["strategy"]["cooldown_hours"] = best_cb["ch"]
    cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["position_pct"] = best_pos["bp"]
    cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["position_pct"] = best_pos["ep"]
    cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"] = best_eth["tp"]
    cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"] = best_eth["sl"]
    cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"] = best_btc["tp"]
    cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"] = best_btc["sl"]
    cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["float_pct"] = best_float["bf"]
    cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["min_prev_amp"] = best_min["bm"]
    cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["min_prev_amp"] = best_min["em"]
    cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["reentry_floats"] = best_re["br"]
    cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["reentry_floats"] = best_re["er"]
    r = run(cfg); s = summarize(r)

    print()
    print(f"{'指标':<15} {'均衡基线':>12} {'新整合':>12} {'改进':>10}")
    print("-" * 60)
    print(f"{'2Y 收益':<15} {s0['ret']*100:>+11.1f}% {s['ret']*100:>+11.1f}% {(s['ret']-s0['ret'])*100:>+9.1f}pt")
    print(f"{'Y1 收益':<15} {s0['y1']*100:>+11.1f}% {s['y1']*100:>+11.1f}% {(s['y1']-s0['y1'])*100:>+9.1f}pt")
    print(f"{'Y2 收益':<15} {s0['y2']*100:>+11.1f}% {s['y2']*100:>+11.1f}% {(s['y2']-s0['y2'])*100:>+9.1f}pt")
    print(f"{'MDD':<15} {s0['mdd']*100:>11.1f}% {s['mdd']*100:>11.1f}% {(s['mdd']-s0['mdd'])*100:>+9.1f}pt")
    print(f"{'月化':<15} {s0['monthly']*100:>+11.2f}% {s['monthly']*100:>+11.2f}%")
    print(f"{'Y2 末余额':<15} {s0['final']:>11.0f}U {s['final']:>11.0f}U {(s['final']-s0['final']):>+9.0f}U")

    print("\n[新整合最优参数]")
    print(f"  BTC: pos={best_pos['bp']*100:.1f}% tp={best_btc['tp']*100:.2f}% sl={best_btc['sl']*100:.2f}% "
          f"float={best_float['bf']*100:.3f}% reentry={best_re['br']} min_amp={best_min['bm']*100:.2f}%")
    print(f"  ETH: pos={best_pos['ep']*100:.1f}% tp={best_eth['tp']*100:.2f}% sl={best_eth['sl']*100:.2f}% "
          f"reentry={best_re['er']} min_amp={best_min['em']*100:.2f}%")
    print(f"  熔断: max_loss={best_cb['ml']} cooldown={best_cb['ch']}h")


def main():
    print("=" * 100)
    print(f"起始 145U，2 年连续复利，全 maker 成本 0.06%")
    print(f"基线（均衡档）：BTC pos=5% TP=1.25% SL=0.80% / ETH pos=12% TP=1.50% SL=0.80%")
    print(f"基线收益 +22,978% / MDD 66.5%")
    print("=" * 100)

    best_btc, best_eth = stage7_tpsl_fine()
    best_float = stage8_float_pct(best_btc, best_eth)
    best_cb = stage9_circuit_breaker(best_btc, best_eth, best_float)
    best_min = stage10_min_amp(best_btc, best_eth, best_float, best_cb)
    best_re = stage11_reentry_fine(best_btc, best_eth, best_float, best_cb, best_min)
    best_pos = stage12_btc_pos_fine(best_btc, best_eth, best_float, best_cb, best_min, best_re)
    stage13_final(best_btc, best_eth, best_float, best_cb, best_min, best_re, best_pos)


if __name__ == "__main__":
    main()
