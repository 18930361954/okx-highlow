# 验证记录：<变更主题>

- **日期**：YYYY-MM-DD
- **相关 commit**：`<sha>` `<sha>` ...
- **影响账户 / pair**：<例：模拟盘-A1455923264 (6H) · 模拟盘-bot14559 (12H)>
- **验证窗口**：YYYY-MM-DD ~ YYYY-MM-DD（UTC，共 N 天）

## 变更说明

一句话：改了什么，为什么可能让实盘和回测口径拉开距离。

例：OKX 默认 bar=6H/12H 用 HK 时区对齐（桶起点 UTC 04/10/16/22），
b989a8e 改为显式 6Hutc/12Hutc 强制 UTC 对齐 —— 与回测 pandas resample 默认
UTC 桶一致，但和历史模拟盘的错桶业绩不能直接类比。

## 验证方法

- 数据源：`data/trades.db`（快照文件 `trades_YYYY-MM-DD.db` 附在同目录）
- 过滤条件：`account = ? AND entry_time IS NOT NULL AND exit_time >= '<start>'`
- 对比基准：`docs/backtest_validated.md` 里对应参数的预期数字（或 walk-forward test 集）
- 取样命令（示例）：
  ```
  sqlite3 data/trades.db "SELECT pair, side, entry_time, exit_time, exit_reason,
    entry_price, exit_price, pnl, fee, funding
    FROM trades WHERE account='<name>' AND entry_time >= '<start-utc>'
    ORDER BY entry_time"
  ```

## 结果汇总

| 指标 | 实盘（本次验证窗口） | 回测预期 | 差异 |
|---|---|---|---|
| 成交笔数 | | | |
| 胜率 | | | |
| 平均每笔 净 PnL | | | |
| 累计净盈亏（%） | | | |
| 最大回撤（%） | | | |

## 分 pair 细节

| Pair | 笔数 | 胜/亏 | 净 PnL | 备注 |
|---|---|---|---|---|
| BTC-USDT-SWAP | | | | |
| ETH-USDT-SWAP | | | | |
| SOL-USDT-SWAP | | | | |

## 结论

选一个：

- [ ] **一致** —— 可以上实盘（enabled: true）
- [ ] **偏差在可解释范围** —— 说明来源（例如手续费差异、样本量小），可上实盘但要观察 N 天
- [ ] **不一致** —— 参数很可能在错配下调的，**不能上实盘**。后续动作：<例：用 UTC 桶重新 walk-forward → 更新 pair_overrides>

## 附件 / 引用

- db 快照：`trades_YYYY-MM-DD.db`
- 相关日志：`logs/bot.log.YYYY-MM-DD`（若有）
- 回测报告：`docs/backtest_validated.md#<section>`
