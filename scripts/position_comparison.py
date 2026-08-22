"""生成完整的仓位对比报告（5% vs 10%）

A组合: 5% (已定)
B组合: 5% vs 10%
C组合: 5% vs 10%
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')
from scripts.portfolio_liq_backtest import simulate

print("=" * 80)
print("仓位对比回测（5% vs 10%）")
print("=" * 80)
print()
print("配置:")
print("  起始: 150 USDT")
print("  时间: 2024-08 ~ 2026-08 (730天)")
print("  杠杆: 100x")
print("  张数上限: BTC 1000 / ETH 5000 / SOL 5000")
print("  时间止损: 0.5桶")
print()

results = {}

# A组合固定5%
print("A组合 (5%仓位，已定)...")
r = simulate("A", 0.05, 100, 100, 150.0, 0.5, None, conservative_liq=True)
results["A-5%"] = r
print(f"  完成: 月化{r.monthly_pct:+.2f}%, 回撤{r.max_dd_pct:.1f}%, {r.trades}笔")
print()

# B组合对比
print("B组合 (10%仓位)...")
r = simulate("B", 0.10, 100, 100, 150.0, 0.5, None, conservative_liq=True)
results["B-10%"] = r
print(f"  完成: 月化{r.monthly_pct:+.2f}%, 回撤{r.max_dd_pct:.1f}%, {r.trades}笔")

print("B组合 (5%仓位)...")
r = simulate("B", 0.05, 100, 100, 150.0, 0.5, None, conservative_liq=True)
results["B-5%"] = r
print(f"  完成: 月化{r.monthly_pct:+.2f}%, 回撤{r.max_dd_pct:.1f}%, {r.trades}笔")
print()

# C组合对比
print("C组合 (10%仓位)...")
r = simulate("C", 0.10, 100, 100, 150.0, 0.5, None, conservative_liq=True)
results["C-10%"] = r
print(f"  完成: 月化{r.monthly_pct:+.2f}%, 回撤{r.max_dd_pct:.1f}%, {r.trades}笔")

print("C组合 (5%仓位)...")
r = simulate("C", 0.05, 100, 100, 150.0, 0.5, None, conservative_liq=True)
results["C-5%"] = r
print(f"  完成: 月化{r.monthly_pct:+.2f}%, 回撤{r.max_dd_pct:.1f}%, {r.trades}笔")
print()

# 生成报告
print("=" * 80)
print("生成报告...")

def fmt_money(v):
    if v >= 1e6:
        return f"{v/1e6:.1f}百万"
    elif v >= 1e4:
        return f"{v/1e4:.1f}万"
    else:
        return f"{v:,.0f}"

report = """# 仓位对比回测报告（2026-08-23）

## 完整数据对比

| 组合 | 仓位 | 月化收益 | 年化收益 | 最大回撤 | 期末权益 | 成交笔数 | 胜率 | 爆仓 |
|---|---|---|---|---|---|---|---|---|
"""

for key in ("A-5%", "B-10%", "B-5%", "C-10%", "C-5%"):
    r = results[key]
    pf, pct = key.split("-")
    yr = ((1 + r.monthly_pct / 100) ** 12 - 1) * 100 if not r.ruined else -100
    wr = r.wins / r.trades * 100 if r.trades else 0
    fate = "是" if r.ruined else "否"

    report += f"| {pf} | {pct} | {r.monthly_pct:+.2f}% | {yr:+.1f}% | {r.max_dd_pct:.1f}% | {fmt_money(r.final)} | {r.trades} | {wr:.1f}% | {fate} |\n"

report += """
## 关键对比

### B组合（5% vs 10%）
"""

b10 = results["B-10%"]
b5 = results["B-5%"]
yr10 = ((1 + b10.monthly_pct / 100) ** 12 - 1) * 100
yr5 = ((1 + b5.monthly_pct / 100) ** 12 - 1) * 100

report += f"""
- **10%仓位**: 月化{b10.monthly_pct:+.2f}%, 年化{yr10:+.1f}%, 回撤{b10.max_dd_pct:.1f}%, 期末{fmt_money(b10.final)}
- **5%仓位**: 月化{b5.monthly_pct:+.2f}%, 年化{yr5:+.1f}%, 回撤{b5.max_dd_pct:.1f}%, 期末{fmt_money(b5.final)}

**收益差异**: 月化相差{abs(b10.monthly_pct - b5.monthly_pct):.2f}个百分点
**回撤差异**: 回撤相差{abs(b10.max_dd_pct - b5.max_dd_pct):.1f}个百分点

### C组合（5% vs 10%）
"""

c10 = results["C-10%"]
c5 = results["C-5%"]
yr10 = ((1 + c10.monthly_pct / 100) ** 12 - 1) * 100
yr5 = ((1 + c5.monthly_pct / 100) ** 12 - 1) * 100

report += f"""
- **10%仓位**: 月化{c10.monthly_pct:+.2f}%, 年化{yr10:+.1f}%, 回撤{c10.max_dd_pct:.1f}%, 期末{fmt_money(c10.final)}
- **5%仓位**: 月化{c5.monthly_pct:+.2f}%, 年化{yr5:+.1f}%, 回撤{c5.max_dd_pct:.1f}%, 期末{fmt_money(c5.final)}

**收益差异**: 月化相差{abs(c10.monthly_pct - c5.monthly_pct):.2f}个百分点
**回撤差异**: 回撤相差{abs(c10.max_dd_pct - c5.max_dd_pct):.1f}个百分点

## 决策依据

### 如果追求最高收益
- 选择 **10%仓位**（B/C都用10%）
- 代价：回撤更高

### 如果追求收益与风险平衡
- 选择 **5%仓位**（B/C都用5%）
- 收益只略降，但回撤显著降低

**用户自行决定**，两种配置都有数据支撑。

---

**回测参数**:
- 起始: 150 USDT
- 时间: 2024-08 ~ 2026-08 (730天)
- 杠杆: 100x
- 张数上限: BTC 1000 / ETH 5000 / SOL 5000
- 时间止损: 0.5桶（与config.yaml一致）
- 强平判定: 逐小时用盘中不利极值
"""

with open('reports/position_comparison_2026-08-23.md', 'w', encoding='utf-8') as f:
    f.write(report)

print("报告已生成: reports/position_comparison_2026-08-23.md")
print()
print("=" * 80)
print("请查看报告，根据数据自行决定使用哪个仓位配置")
print("=" * 80)
