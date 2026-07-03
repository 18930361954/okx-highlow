"""稳健性检验 —— 检查档 A 参数是不是 in-sample 过拟合。

方法：
  1. 分别在 Y1 (2024-07~2025-07) 和 Y2 (2025-07~2026-07) 上跑档 A 参数
  2. 对比：分年度收益、胜率、MDD 是否稳定
  3. 若两年表现相似 → 参数稳健；若两年差异极大 → 过拟合警报
  4. 额外做一次：把 TP/SL 各自 ±10% ~ ±20% 扰动，看敏感度
"""
import sys
import copy
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest import load_csv  # noqa: E402
from scripts.backtest_joint import joint_backtest, load_cfg  # noqa: E402


FEE = 0.0004
SLIP = 0.0002

Y1_START = pd.Timestamp("2024-07-01", tz="UTC")
Y1_END = pd.Timestamp("2025-07-01", tz="UTC")
Y2_START = Y1_END
Y2_END = pd.Timestamp("2026-07-01", tz="UTC")


DF_BTC = None
DF_ETH = None


def load():
    global DF_BTC, DF_ETH
    if DF_BTC is None:
        DF_BTC = load_csv(Path("csv_data/BTC_USDT_SWAP_1H_780d.csv"))
        DF_ETH = load_csv(Path("csv_data/ETH_USDT_SWAP_1H_780d.csv"))
    return DF_BTC.copy(), DF_ETH.copy()


def run(cfg, start, end, initial=145.0):
    df_btc, df_eth = load()
    return joint_backtest(df_btc, df_eth, cfg,
                          initial=initial, start=start, end=end,
                          fee_rt=FEE, slippage_rt=SLIP)


def summ(r, initial):
    if r["n_days"] == 0:
        return None
    return {
        "ret": r["total_return"], "mdd": r["max_dd"],
        "final": r["final"], "monthly": r["monthly"],
        "btc_wr": r["per_pair"]["BTC-USDT-SWAP"]["w"] / max(r["per_pair"]["BTC-USDT-SWAP"]["n"], 1),
        "eth_wr": r["per_pair"]["ETH-USDT-SWAP"]["w"] / max(r["per_pair"]["ETH-USDT-SWAP"]["n"], 1),
        "btc_pnl": r["per_pair"]["BTC-USDT-SWAP"]["pnl"],
        "eth_pnl": r["per_pair"]["ETH-USDT-SWAP"]["pnl"],
        "btc_n": r["per_pair"]["BTC-USDT-SWAP"]["n"],
        "eth_n": r["per_pair"]["ETH-USDT-SWAP"]["n"],
    }


def check1_annual(cfg):
    """检验 1: 分年度独立跑（各自 145U 起）"""
    print("\n" + "=" * 100)
    print("[检验 1] 分年度独立回测（各自 145U 起始）")
    print("=" * 100)
    print(f"{'窗口':<25} {'收益%':>10} {'MDD%':>6} {'BTC 胜率':>9} {'ETH 胜率':>9} "
          f"{'BTC PnL':>9} {'ETH PnL':>9} {'月化':>7}")
    print("-" * 100)
    for label, s, e in [("Y1 (24-07~25-07)", Y1_START, Y1_END),
                         ("Y2 (25-07~26-07)", Y2_START, Y2_END)]:
        r = run(cfg, s, e, initial=145.0)
        sm = summ(r, 145.0)
        print(f"{label:<25} {sm['ret']*100:>+9.1f}% {sm['mdd']*100:>5.1f}% "
              f"{sm['btc_wr']*100:>8.1f}% {sm['eth_wr']*100:>8.1f}% "
              f"{sm['btc_pnl']:>+9.1f} {sm['eth_pnl']:>+9.1f} {sm['monthly']*100:>+6.2f}%")


def check2_walkforward(cfg):
    """检验 2: walk-forward —— Y1 结束余额作为 Y2 起始，2 年连续复利"""
    print("\n" + "=" * 100)
    print("[检验 2] Walk-forward 连续复利（Y1 结束 → Y2 起始，145U 出发）")
    print("=" * 100)
    r_full = joint_backtest(*load(), cfg,
                             initial=145.0, start=Y1_START, end=Y2_END,
                             fee_rt=FEE, slippage_rt=SLIP, year1_end=Y1_END)
    y1_end = r_full["year1_end_balance"]
    y1_ret = (y1_end - 145.0) / 145.0
    y2_ret = (r_full["final"] - y1_end) / y1_end
    print(f"起始:         145.00 U")
    print(f"Y1 结束:      {y1_end:.2f} U  ({y1_ret*100:+.2f}%)  ← Y2 起始")
    print(f"Y2 结束:      {r_full['final']:.2f} U  ({y2_ret*100:+.2f}%)")
    print(f"2Y 总收益:    {r_full['total_return']*100:+.2f}%")
    print(f"2Y MDD:       {r_full['max_dd']*100:.2f}%")


def check3_sensitivity(cfg):
    """检验 3: TP/SL 各自扰动 ±20% —— 参数敏感度"""
    print("\n" + "=" * 100)
    print("[检验 3] TP/SL 扰动敏感度（各自 -20%, -10%, 0, +10%, +20%）")
    print("=" * 100)

    base_btc_tp = cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["tp_pct"]
    base_btc_sl = cfg["strategy"]["pair_overrides"]["BTC-USDT-SWAP"]["sl_pct"]
    base_eth_tp = cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["tp_pct"]
    base_eth_sl = cfg["strategy"]["pair_overrides"]["ETH-USDT-SWAP"]["sl_pct"]

    def one(cfg, label):
        r = run(cfg, Y1_START, Y2_END, initial=145.0)
        return {"label": label,
                "ret": r["total_return"],
                "mdd": r["max_dd"],
                "final": r["final"]}

    print(f"\n{'扰动项':<20} {'扰动值':<12} {'2Y%':>10} {'MDD%':>6} {'Y2末余':>10}")
    print("-" * 65)
    rows = []
    rows.append(one(cfg, "基线（档A）"))
    for pct in [-0.20, -0.10, +0.10, +0.20]:
        for name, key_pair, key_field in [
            ("BTC TP", "BTC-USDT-SWAP", "tp_pct"),
            ("BTC SL", "BTC-USDT-SWAP", "sl_pct"),
            ("ETH TP", "ETH-USDT-SWAP", "tp_pct"),
            ("ETH SL", "ETH-USDT-SWAP", "sl_pct"),
        ]:
            c = copy.deepcopy(cfg)
            base_val = c["strategy"]["pair_overrides"][key_pair][key_field]
            c["strategy"]["pair_overrides"][key_pair][key_field] = base_val * (1 + pct)
            rows.append(one(c, f"{name} {pct*100:+.0f}%"))

    # 打印基线
    baseline = rows[0]
    print(f"{'基线（档A）':<20} {'—':<12} {baseline['ret']*100:>+9.1f}% "
          f"{baseline['mdd']*100:>5.1f}% {baseline['final']:>10.0f}")
    print()
    # 分组打印扰动
    for r in rows[1:]:
        delta_ret = (r['ret'] - baseline['ret']) * 100
        print(f"{'':<20} {r['label']:<12} {r['ret']*100:>+9.1f}% "
              f"{r['mdd']*100:>5.1f}% {r['final']:>10.0f}  Δ收益={delta_ret:+.0f}pt")


def check4_direction_sanity(cfg):
    """检验 4: 关闭 trend_filter 看方向过滤器的价值"""
    print("\n" + "=" * 100)
    print("[检验 4] trend_filter 开关对比")
    print("=" * 100)
    for tf, label in [(True, "开（现状）"), (False, "关（不做方向过滤）")]:
        c = copy.deepcopy(cfg)
        c["strategy"]["trend_filter"] = tf
        r = run(c, Y1_START, Y2_END, initial=145.0)
        sm = summ(r, 145.0)
        print(f"trend_filter={label:<20} 2Y={sm['ret']*100:>+8.1f}% MDD={sm['mdd']*100:>4.1f}% "
              f"末余={sm['final']:.0f} BTC胜率={sm['btc_wr']*100:.1f}% ETH胜率={sm['eth_wr']*100:.1f}%")


def main():
    cfg = load_cfg()
    print("=" * 100)
    print("稳健性检验 —— 档 A 参数（config.yaml 现值）")
    print(f"BTC: pos={cfg['strategy']['pair_overrides']['BTC-USDT-SWAP']['position_pct']*100:.1f}% "
          f"TP={cfg['strategy']['pair_overrides']['BTC-USDT-SWAP']['tp_pct']*100:.2f}% "
          f"SL={cfg['strategy']['pair_overrides']['BTC-USDT-SWAP']['sl_pct']*100:.2f}%")
    print(f"ETH: pos={cfg['strategy']['pair_overrides']['ETH-USDT-SWAP']['position_pct']*100:.1f}% "
          f"TP={cfg['strategy']['pair_overrides']['ETH-USDT-SWAP']['tp_pct']*100:.2f}% "
          f"SL={cfg['strategy']['pair_overrides']['ETH-USDT-SWAP']['sl_pct']*100:.2f}%")
    print(f"成本模型: 费{FEE*100:.3f}% + 滑{SLIP*100:.3f}% = {(FEE+SLIP)*100:.3f}%")

    check1_annual(cfg)
    check2_walkforward(cfg)
    check3_sensitivity(cfg)
    check4_direction_sanity(cfg)


if __name__ == "__main__":
    main()
