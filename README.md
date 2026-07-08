# HighLow Bot · OKX SWAP 多账户挂单机器人

前 K 阴阳判定 → 下一桶浮动挂 trigger 限价 + 服务端 TP/SL。**多账户 · 多信号周期 · 共享 db**。

- **信号周期**:支持 1D / 12H / 6H / 4H / 2H / 1H,每账户独立
- **多币种**:BTC / ETH / SOL,每 pair 独立 float/tp/sl/leverage
- **多账户**:同一进程并行跑,共享 `data/trades.db` 用 `account` 列区分
- **无状态启动**:catchup 补挂 + reconciler REST 对账,重启自愈

---

## 目录

```
main.py                # 入口(多账户 scheduler + monitor + reconciler)
config.yaml            # 唯一配置(accounts 段 + 顶层默认)
core/
  okx_client.py        # OKX REST 封装 + 代理支持
  scheduler.py         # 按 signal_bar 生成 per-account cron
  account_state.py     # 每账户余额/连亏/熔断持久化
  multi_account.py     # 多账户加载器 + AccountRuntime
strategy/high_low.py   # 阴阳判定 + 浮动价 + pair 级 tp/sl/signal_bar
execution/
  order_manager.py     # algo 单挂载(trigger + attach TP/SL + 幂等)
  position_monitor.py  # rich 多账户面板(汇总 + 挂单/持仓/成交)
  reconciler.py        # 20s 一轮:回填 entry/exit + 桶级重挂/补挂
data/db.py             # SQLite:trades / state 表带 account 列
utils/                 # logger + time_helper

scripts/               # 只留活的
  bucket_backtest.py      # 桶回测器(向量化,支持复利/滑点/funding/张数封顶)
  bucket_grid.py          # pair × signal × 参数网格搜索
  bucket_top_v2.py        # 从网格选 Top + 年度余额分解
  combined_backtest.py    # 三币联合共享余额回测
  walk_forward.py         # Walk-Forward 反过拟合
  slippage_sweep.py       # 滑点敏感性分析
  fetch_multi.py          # 批量拉多周期 K 到 csv_data/
  daily_report.py         # 每日汇总报告
  reset_cooldown.py       # 应急清熔断

tests/                 # pytest(52 项)
docs/
  OPERATIONS.md        # 运维手册(启动/停止/故障/日常查看)
  backtest_validated.md# 三层验证报告(walk-forward + 滑点 + 张数封顶)
  daily_reports/       # 自动生成的每日 md 报告
csv_data/              # 730 天历史 K(3 pair × 9 周期 = 27 份)
reports/               # 回测结果 CSV(grid/walk_forward/slippage_sweep)
```

---

## 快速启动

```powershell
# 1) 安装依赖
python -m pip install -r requirements.txt

# 2) 跑测试(52 项)
python -m pytest tests/

# 3) 启动机器人
python main.py
```

**日常运维**:详见 `docs/OPERATIONS.md`(启动/停止/切换实盘/日常查看/故障处理)。

---

## 回测方法论

三层交叉验证,详见 `docs/backtest_validated.md`:

1. **Walk-Forward**:18 月 train 选参 + 6 月 test 验证,反过拟合
2. **滑点敏感性**:0/10/30/50bp 扫描,判断稳健性
3. **张数封顶**:BTC 1000 张 / ETH SOL 5000 张,避免复利数学放大到 OKX 流动性不可承接

回测口径(悲观):taker 5bp × 2 + 滑点 10bp + funding 3bp/8h,复利模式。

---

## 关键约束

- **杠杆**:BTC/ETH 100x(实盘 & 模拟盘),SOL 实盘 100x 但**模拟盘上限 50x**(config 已按环境覆盖)
- **仓位**:每笔余额 × 10%,三币满仓总保证金 30%
- **熔断**:每账户独立,3 连亏后暂停 24h

---

## 已知不做

- WebSocket 实时行情(REST 20s 对账够用)
- 复杂订单簿分析、tick 级模型
- 跨账户资金再平衡

---

**License** · 私人使用,勿公开传播 API 密钥。
