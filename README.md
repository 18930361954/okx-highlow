# HighLow Limit Order Bot (okx-highlow)

按前日 high/low 浮动挂限价单 + 服务端 TP/SL 的 OKX SWAP 策略机器人。
设计目标：**精简**、**单机可跑**、**重启可恢复**、**REST 对账自愈**。

策略细节见 `docs/新项目方案/01_策略与架构方案.md`。

---

## 快速启动

```bash
# 1) 安装依赖
python -m pip install -r requirements.txt

# 2) 配置 API
cp .env.example .env
#   填入 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE
#   demo 子账户 key 权限只勾「交易」+「读取」，不要「提币」

# 3) 检查 config.yaml
#   account.env = demo（先 demo 跑通再切 live）
#   strategy.pair_overrides 里给 BTC/ETH 分别配 sl/tp

# 4) 跑测试
python -m pytest tests/

# 5) 启动
python main.py
```

启动后：
1. 先跑一次 `startup_catchup_if_needed`：若已过 00:00 UTC 且今日没挂过单，立刻补挂
2. 挂上 20s 一轮的 `Reconciler`：REST 轮询回填 entry/exit、触发 `on_trade_filled` 结算
3. 显示 rich 终端面板 + `[ready] HighLow Bot 系统就绪`

---

## 目录结构

```
okx-highlow/
├── main.py                      # 入口（catchup + scheduler + monitor + reconciler）
├── config.yaml                  # 唯一配置
├── .env                         # API 密钥（gitignore）
├── core/
│   ├── okx_client.py            # OKX REST 封装（含 orders-history / algo-history）
│   ├── scheduler.py             # APScheduler：3 daily cron + 1 interval reconcile
│   └── account_state.py         # 余额/连亏/熔断/切档 持久化
├── strategy/
│   └── high_low.py              # 阴阳判定 + 浮动挂单价 + pair 级 TP/SL
├── execution/
│   ├── order_manager.py         # algo 单（trigger 限价 + attach TP/SL + algoClOrdId 幂等）
│   ├── position_monitor.py      # rich 终端面板（余额/pending/持仓/成交）
│   └── reconciler.py            # 每 20s：回填 entry_time/exit，清理重复 pending
├── data/
│   └── db.py                    # SQLite（trades + state）
├── utils/
│   ├── logger.py                # 文件 rotate + 控制台
│   └── time_helper.py
├── scripts/
│   ├── backtest.py              # 单品种回测
│   ├── diagnose.py              # 方向 / 退出原因诊断
│   ├── rolling_window.py        # 30 天滚动窗口
│   ├── fetch_history.py         # 从 OKX 拉历史 CSV
│   ├── daily_report.py          # 每日 Markdown 报告
│   └── reset_cooldown.py        # 紧急清熔断
└── tests/                       # pytest 单测（42 项）
```

---

## 配置（config.yaml）

```yaml
strategy:
  pairs: [BTC-USDT-SWAP, ETH-USDT-SWAP]
  position_pct: 0.10          # 余额 × 10% 为单笔保证金
  float_pct: 0.0015           # high/low × ±0.15% 浮动
  tp_pct: 0.012               # 默认止盈（未在 pair_overrides 里的 pair 使用）
  sl_pct: 0.005               # 默认止损
  pair_overrides:             # 每个 pair 单独调 sl/tp
    BTC-USDT-SWAP: { sl_pct: 0.005, tp_pct: 0.012 }
    ETH-USDT-SWAP: { sl_pct: 0.010, tp_pct: 0.020 }
  leverage: 100
  trend_filter: true          # 阳线只多 / 阴线只空
  max_consecutive_losses: 3   # 连亏几次触发熔断
  cooldown_hours: 24
  fixed_mode_threshold: 800000  # 净值首次过此值切固定保证金
  fixed_mode_margin: 1000
  signal_time_utc: "00:00"

system:
  log_level: INFO
  log_keep_days: 30
  db_path: data/trades.db
  daily_report_time_utc: "23:55"

account:
  env: demo                   # demo / live
  td_mode: cross              # cross（推荐）/ isolated
```

---

## 关键运行时行为

### 1. 每日 cron

| 时刻 (UTC) | Job | 行为 |
|---|---|---|
| 00:00 | `daily_signal` | 拉前日 24 根 1H → 算信号 → 下 algo 单（若不熔断） |
| 23:55 | `daily_report` | 生成当日 Markdown 报告 → `docs/daily_reports/` |
| 23:59 | `daily_cancel` | 撤所有未触发挂单 |

### 2. 20s 对账（`Reconciler`）

每 20 秒跑一轮：
1. **重复 pending 清理**：每 pair 若 pending algo > 1 张 → 保留 db 已记录那张（否则保留 cTime 最早的），其余撤掉，日志 WARNING
2. **entry 回填**：OKX orders-history 里找到该 algoId 的建仓单 → 写 `entry_time / entry_price` 到 db
3. **exit 结算**：找到 tp/sl 平仓单 → 写 `exit_price / exit_reason / pnl / exit_time`，同时调 `account.on_trade_filled` 更新余额、连亏、熔断、切档

**幂等**：db 里 `exit_price` 已存在的不再处理。

### 3. 启动补挂（`startup_catchup_if_needed`）

启动时若已过 `signal_time_utc`，判断今日是否需补挂：
- 查 db 今日已有记录的 pair → 跳过
- 查 OKX 已有 pending / 持仓的 pair → 跳过
- 否则补跑一次 `daily_signal_and_place`

### 4. 重复挂单防线

| 层 | 机制 | 覆盖场景 |
|---|---|---|
| L1 源头 | `algoClOrdId=hlBTC20260630s` 幂等键 | 同一请求被 HTTP/TCP 层重传 → OKX 服务端直接拒 |
| L2 对账 | `_cleanup_duplicate_pending` 每 20s 扫 | 兜底：任何原因产生的同 pair 多张 pending，最迟 20s 内自动撤到剩 1 张 |

---

## 回测

数据准备：把 1H K 线 CSV 放到 `csv_data/`：

```
csv_data/
├── BTC_USDT_SWAP_1H_12m.csv
└── ETH_USDT_SWAP_1H_12m.csv
```

CSV 列：`timestamp(ms),open,high,low,close,volume,volume_ccy,volume_ccy_quote,confirm`

```bash
# 180 天回测
python scripts/backtest.py --pair BTC-USDT-SWAP --days 180

# 诊断：方向分布 / 退出原因
python scripts/diagnose.py --pair BTC-USDT-SWAP --days 180

# 30 天滚动窗口（监控边际衰减）
python scripts/rolling_window.py --pair BTC-USDT-SWAP --window 30 --step 7

# 从 OKX 现拉一份 CSV
python scripts/fetch_history.py --pair BTC-USDT-SWAP --days 180
```

**当前 pair_overrides 下 180 天保守口径实测**：

| Pair | sl/tp | 收益 | 胜率 | PF | Max DD |
|---|---|---|---|---|---|
| BTC | 0.5%/1.2% | +50.09% | 36.84% | 1.44 | 37.21% |
| ETH | 1.0%/2.0% | +57.79% | 46.00% | 1.19 | 40.59% |

回测口径：同根 K 同时穿 TP+SL → SL 优先（保守）。

---

## 日常运维

**应急停止**：`Ctrl+C` 优雅退出（已挂的 algo 单在 OKX 服务端继续生效）。需要时登录 OKX 网页手动撤算法单。

**手动清熔断**：`python scripts/reset_cooldown.py`

**手动生成报告**：`python scripts/daily_report.py --date 2026-06-30`

**看实时日志**：`PositionMonitor` 面板会覆盖终端 stderr 日志，日志一直在 `logs/bot.log`。想同时看两个：另开窗口 `Get-Content -Wait logs/bot.log`（Windows）或 `tail -f logs/bot.log`（Linux/Mac）。

---

## 验收

- `pytest tests/` 全部通过（42 项：账户/db/策略/order_manager/reconciler/daily_report/e2e/time_helper）
- e2e 用例覆盖完整生命周期：挂单→建仓→TP 平仓→结算→报告，以及 3 连亏→熔断→拒交易
- demo 环境运行 ≥7 天无 crash

---

## 已知风险

- 1H K 粒度存在"同根 K 同时穿 TP+SL"歧义；本项目采用 SL 优先的保守口径，与方案 1.3 节的乐观口径数字不可比
- 5 月后两品种胜率掉到 33–43%，**每周复跑 `rolling_window.py` 监控**
- 连续两周窗口胜率 < 45% → 暂停策略调研

---

## 不做的事

- WebSocket 实时行情 —— HighLow 是日频策略，REST 轮询 20s 完全够
- 复杂订单簿分析、tick 级模型 —— 与"精简"目标冲突
- 详见 `docs/新项目方案/01_策略与架构方案.md` 第八节
