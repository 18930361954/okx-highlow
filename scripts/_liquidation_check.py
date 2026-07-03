"""三币联合的强平风险量化分析。

模型：cross margin 全仓模式（config.yaml 现值 td_mode: cross）
情形：假设某日 3 对同时开仓，且价格同向反向击穿各自 SL

计算：
  1. 单日最大理论亏损（所有 SL 都触发）
  2. 累计名义敞口（notional exposure）
  3. OKX cross 强平边界估算
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 常数
BALANCE = 280.0  # 初始余额
OKX_MAINT_RATIO = 0.005  # OKX cross 维持保证金率约 0.5%（BTC/ETH/SOL 主流合约）


def analyze(name, btc_pos, eth_pos, sol_pos, btc_lev=100, eth_lev=100, sol_lev=50,
            btc_sl=0.010, eth_sl=0.008, sol_sl=0.020,
            btc_tp=0.010, eth_tp=0.014, sol_tp=0.010,
            fee=0.0006):  # 全 maker 0.06%
    print(f"\n{'=' * 90}")
    print(f"配置：{name}")
    print(f"{'=' * 90}")
    print(f"BTC: pos={btc_pos*100:.0f}% × {btc_lev}x  SL={btc_sl*100:.2f}%  TP={btc_tp*100:.2f}%")
    print(f"ETH: pos={eth_pos*100:.0f}% × {eth_lev}x  SL={eth_sl*100:.2f}%  TP={eth_tp*100:.2f}%")
    print(f"SOL: pos={sol_pos*100:.0f}% × {sol_lev}x  SL={sol_sl*100:.2f}%  TP={sol_tp*100:.2f}%")

    # 名义敞口
    btc_notional = btc_pos * btc_lev
    eth_notional = eth_pos * eth_lev
    sol_notional = sol_pos * sol_lev
    total_notional_pct = btc_notional + eth_notional + sol_notional
    total_margin_pct = btc_pos + eth_pos + sol_pos

    print(f"\n【仓位与敞口】")
    print(f"  总保证金占用: {total_margin_pct*100:.1f}% (剩余 {(1-total_margin_pct)*100:.1f}% 作为缓冲)")
    print(f"  BTC 名义敞口: {btc_notional*100:.0f}% of balance ({btc_notional*BALANCE:.0f}U)")
    print(f"  ETH 名义敞口: {eth_notional*100:.0f}% of balance ({eth_notional*BALANCE:.0f}U)")
    print(f"  SOL 名义敞口: {sol_notional*100:.0f}% of balance ({sol_notional*BALANCE:.0f}U)")
    print(f"  总名义敞口: {total_notional_pct*100:.0f}% of balance ({total_notional_pct*BALANCE:.0f}U)")

    # 情形1：所有 3 对同一天全部触发 SL
    btc_sl_loss = btc_notional * btc_sl
    eth_sl_loss = eth_notional * eth_sl
    sol_sl_loss = sol_notional * sol_sl
    total_sl_loss_pct = btc_sl_loss + eth_sl_loss + sol_sl_loss

    # 情形2：加上手续费
    total_fees = (btc_notional + eth_notional + sol_notional) * fee
    total_worst = total_sl_loss_pct + total_fees

    print(f"\n【情形 A：三对同日同时触发 SL】")
    print(f"  BTC SL 亏损: {btc_sl_loss*100:.2f}% ({btc_sl_loss*BALANCE:.2f}U)")
    print(f"  ETH SL 亏损: {eth_sl_loss*100:.2f}% ({eth_sl_loss*BALANCE:.2f}U)")
    print(f"  SOL SL 亏损: {sol_sl_loss*100:.2f}% ({sol_sl_loss*BALANCE:.2f}U)")
    print(f"  手续费+滑点: {total_fees*100:.2f}% ({total_fees*BALANCE:.2f}U)")
    print(f"  合计单日最大亏损: {total_worst*100:.2f}% ({total_worst*BALANCE:.2f}U)")
    if total_worst < 0.5:
        print(f"  评估: ✅ 安全（<50%）")
    elif total_worst < 0.7:
        print(f"  评估: ⚠️ 警戒（50-70%）")
    else:
        print(f"  评估: ❌ 危险（≥70%）")

    # 情形3：SL 失效（价格瞬间穿透 SL）—— 计算强平边界
    # cross 模式：equity = balance - unrealized_loss
    # 强平：equity < maintenance_margin
    # maintenance = 总名义 × 0.5% (approx)
    # 假设 3 对同方向反向移动 X%，等效于名义总敞口 × X%
    maint_margin_pct = total_notional_pct * OKX_MAINT_RATIO
    liquidation_move = (1 - maint_margin_pct) / total_notional_pct  # 全部同向亏损多少 % 就爆
    print(f"\n【情形 B：SL 失效 / 快市穿透 —— 强平边界估算】")
    print(f"  维持保证金 (cross ~0.5%): {maint_margin_pct*100:.2f}% of balance")
    print(f"  若三对同向反向移动 X%（无 SL）→ 需 X ≥ {liquidation_move*100:.2f}% 才强平")
    if liquidation_move > 0.05:
        print(f"  评估: ✅ 强平门槛高（>5%），需 3 对同时暴跌 {liquidation_move*100:.1f}%+")
    elif liquidation_move > 0.03:
        print(f"  评估: ⚠️ 强平门槛中等（3-5%）")
    else:
        print(f"  评估: ❌ 强平门槛低（<3%），极端行情高风险")

    # 情形4：单对 SL 未触发（BTC/ETH/SOL 中最危险的一个）
    print(f"\n【情形 C：某对 SL 失效】")
    for name_p, pos, lev, sl in [("BTC", btc_pos, btc_lev, btc_sl),
                                    ("ETH", eth_pos, eth_lev, eth_sl),
                                    ("SOL", sol_pos, sol_lev, sol_sl)]:
        notional = pos * lev
        # 若 SL 失效，该单需要多大反向才让整个账户强平
        # 已被 SL 消耗的余额 = 别的 SL 亏损（假设别的都触发了）
        # 简化：只算这一对 SL 失效，其他 SL 都正常触发
        others_loss = total_sl_loss_pct - notional * sl
        # 剩余余额可承受损失 = 1 - others_loss - maint
        available = 1 - others_loss - maint_margin_pct
        move_to_liq = available / notional
        print(f"  仅 {name_p} SL 失效: 需该对反向移动 {move_to_liq*100:.2f}% 才强平"
              f" (其他对 SL 正常，余下 {available*100:.1f}% 缓冲)")

    return {
        "name": name,
        "worst_sl": total_worst,
        "liq_move": liquidation_move,
        "total_notional_pct": total_notional_pct,
    }


CONFIGS = [
    ("稳健档", 0.05, 0.10, 0.15),
    ("均衡档", 0.05, 0.12, 0.15),
    ("激进档", 0.05, 0.12, 0.20),
    ("极限档 SOL 20%+", 0.05, 0.10, 0.20),
    ("保守档 SOL 10%", 0.03, 0.10, 0.10),
]


def main():
    print("=" * 90)
    print("三币联合强平风险量化 (cross margin 全仓模式)")
    print("=" * 90)
    print(f"起始余额: {BALANCE}U，OKX cross 维持保证金约 {OKX_MAINT_RATIO*100:.1f}%")
    print(f"BTC 100x, ETH 100x, SOL 50x")

    results = []
    for name, bp, ep, sp in CONFIGS:
        r = analyze(name, bp, ep, sp)
        results.append(r)

    # 汇总
    print(f"\n\n{'=' * 90}")
    print("汇总对比")
    print(f"{'=' * 90}")
    print(f"{'档':<20} {'总敞口':>8} {'三对同SL亏损':>13} {'强平门槛':>10} {'评估'}")
    print("-" * 80)
    for r in results:
        if r["worst_sl"] < 0.5 and r["liq_move"] > 0.05:
            grade = "✅ 安全"
        elif r["worst_sl"] < 0.7 and r["liq_move"] > 0.04:
            grade = "⚠️ 警戒"
        else:
            grade = "❌ 危险"
        print(f"{r['name']:<20} {r['total_notional_pct']*100:>7.0f}% "
              f"{r['worst_sl']*100:>12.1f}% {r['liq_move']*100:>9.2f}% {grade}")


if __name__ == "__main__":
    main()
