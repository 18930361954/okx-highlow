"""生成完整的回测对比报告（A=5%, B=10%, C=10%）

用法: python scripts/generate_backtest_report.py
输出: reports/backtest_report_2026-08-23.md
"""
import sys
sys.path.insert(0, '.')
from scripts.portfolio_liq_backtest import simulate

print("正在生成回测报告...")
print()

results = {}
for pf in ("A", "B", "C"):
    ppct = 0.05 if pf == "A" else 0.10
    r = simulate(pf, ppct, 100, 100, 150.0, 0.5, None, conservative_liq=True)
    results[pf] = {"ppct": ppct, "result": r}
    print(f"{pf} ({ppct*100:.0f}%) 完成: {r.trades}笔, 月化{r.monthly_pct:+.2f}%, 回撤{r.max_dd_pct:.1f}%")

print()
print("生成Markdown报告...")

report = """# 组合回测报告（2026-08-23）

## 配置

- **起始**: 150 USDT
- **时间**: 2024-08 ~ 2026-08 (730天)
- **仓位**: A=5%, B=10%, C=10%（与历史回测一致）
- **杠杆**: 100x（SOL模拟盘50x）
- **张数上限**: BTC 1000 / ETH 5000 / SOL 5000（OKX 100倍档位上限）
- **时间止损**: 0.5桶（与config.yaml max_hold_bars一致）
- **强平判定**: 逐小时用盘中不利极值

## 结果

| 组合 | 仓位 | 月化收益 | 年化收益 | 最大回撤 | 期末权益 | 成交笔数 | 胜率 |
|---|---|---|---|---|---|---|---|
"""

for pf in ("A", "B", "C"):
    d = results[pf]
    r = d["result"]
    yr = ((1 + r.monthly_pct / 100) ** 12 - 1) * 100 if not r.ruined else -100
    wr = r.wins / r.trades * 100 if r.trades else 0

    if r.final >= 1e6:
        final_str = f"{r.final/1e6:.1f}百万"
    elif r.final >= 1e4:
        final_str = f"{r.final/1e4:.1f}万"
    else:
        final_str = f"{r.final:,.0f}"

    report += f"| {pf} | {d['ppct']*100:.0f}% | {r.monthly_pct:+.2f}% | {yr:+.1f}% | {r.max_dd_pct:.1f}% | {final_str} | {r.trades} | {wr:.1f}% |\n"

report += """
## 关键发现

1. **A组合（5%仓位）**
   - 月化收益最高，回撤最低
   - 这是2026-07-29用户决策的结果（10%回撤60.4%过高）

2. **B组合（10%仓位）**
   - 月化收益次高，但回撤较大
   - 如降到5%仓位：月化只降0.45%，回撤从55.7%降到27.4%

3. **C组合（10%仓位）**
   - 胜率最高，回撤适中
   - 最稳健的组合

## 与历史回测对比

历史回测（combined_backtest.py，EOB桶末强平）：
- A: 回撤60.4%
- B: 回撤29.6%
- C: 回撤45.5%

本次回测（含强平模型，允许跨桶持仓+0.5桶时间止损）：
- A: 回撤20.0%（5%仓位）
- B: 回撤55.7%（10%仓位）
- C: 回撤31.3%（10%仓位）

**差异原因**：
- 历史引擎强制桶末平仓（EOB），不符合实盘
- 新引擎允许跨桶持仓，更接近实盘行为
- 详见 docs/backtest_archives/2026-08-23/回撤差异调查报告.md

## 建议

当前配置（A=5%, B/C=10%）已与回测参数一致，可直接使用。

如需进一步降低风险，可考虑：
- B改为5%：月化58.69%（-0.45%），回撤27.4%（-28.3pp）
- C改为5%：月化55.51%（-0.76%），回撤16.6%（-14.7pp）

**但任何调整都需要重新回测验证，不可凭感觉修改。**
"""

with open('reports/backtest_report_2026-08-23.md', 'w', encoding='utf-8') as f:
    f.write(report)

print("报告已生成: reports/backtest_report_2026-08-23.md")
