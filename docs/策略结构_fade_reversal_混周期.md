# fade / reversal / 混周期 使用说明（2026-07-28 开发）

> 背景:2026-07 全策略研究(`策略研究报告_2026-07.md`)的三个推荐组合 A/B/C 都依赖
> fade(SOL 唯一幸存结构)、reversal(ETH 12H 唯一幸存结构)与每账户混周期。
> 本文档说明配置方法、执行机制与**已知限制**。上线前必读第三节。

## 一、配置方法

三个新配置键,全部走 `pair_overrides`(账户级 `strategy.mode` / `strategy.signal_bar` 是默认值):

```yaml
# 推荐组合 C「防御高胜率」完整示例 (1D BTC trend + 12H ETH reversal + 6H SOL fade)
- account_name: 某账户
  strategy_name: v3-mixed          # db strategy 列,换策略必改
  pairs: [BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP]
  strategy:
    signal_bar: 6H                 # 默认周期(未覆盖的 pair 用)
    mode: trend                    # 默认模式
    pair_overrides:
      BTC-USDT-SWAP: { signal_bar: 1D,  mode: trend,    float_pct: 0.010, tp_pct: 0.008, sl_pct: 0.030 }
      ETH-USDT-SWAP: { signal_bar: 12H, mode: reversal, float_pct: 0.010, tp_pct: 0.008, sl_pct: 0.025 }
      SOL-USDT-SWAP: { signal_bar: 6H,  mode: fade,     float_pct: 0.005, tp_pct: 0.008, sl_pct: 0.030 }
```

### 三种 mode(与回测 `scripts/strategy_lab.py` simulate_mode 逐位一致,有 parity 测试)

| mode | 前桶阳 | 前桶阴 | 单桶挂单数 |
|---|---|---|---|
| trend(现行) | 挂多 @ low×(1-f) | 挂空 @ high×(1+f) | 1 |
| reversal | 挂空 @ high×(1+f) | 挂多 @ low×(1-f) | 1 |
| fade | 双向:多 @ low×(1-f) + 空 @ high×(1+f) | 同左(不看方向) | **2** |

### 混周期调度机制

- scheduler 对账户内所有 pair 的 cron hours 取**并集**注册 job;每个 job 触发时带 `fire_hour`,
  main 按 `signal_bar_for(pair)` 过滤「这个 hour 轮到哪些 pair」。
- 桶末撤单(daily_cancel)同样按 fire_hour 过滤:12H 桶末只撤 12H/6H pair,**不会误撤 1D pair 未到期的挂单**。
- 启动补挂(startup_catchup)逐 pair 按自己的周期判「当前桶是否过半」。

## 二、fade 执行机制(OCO 模拟)

OKX **没有**「两张反向入场触发单二选一」的原生 OCO。bot 的模拟方式:

1. 信号桶起始:同 pair 同时挂两张 trigger algo(多腿+空腿),各自带服务端 TP/SL,
   db 两行共享 `leg_group`(格式 `f{coin}{sig_id}`)。
2. reconciler 每 20s 对账;发现一腿 entry 成交 → `_sweep_fade_oco` 立即撤另一腿
   (OKX trigger algo + 已触发未成交残单),db 标 `CANCELLED`(pnl=0,不影响余额/连亏)。
3. 撤单失败自动下轮重试(幂等扫描,不依赖单次成功)。

## 三、已知限制(上线前必读)

### ⚠️ 20s both-fill 窗口 —— fade 最大的实盘/回测偏差源

一腿成交到 reconciler 下一轮发现之间最长 **20 秒**。此窗口内若行情双向扫过两个触发价,
**两腿都会成交 = 同时持有多空仓**(正是 07-23~26 whipsaw 的执行版)。此时:
- bot **不会**撤任何一腿(撤已入场的腿=活仓裸奔),各自由服务端 TP/SL 结算,并打 ERROR 日志告警;
- 最坏情形 = 两腿都 SL,单桶亏 2×sl_pct(sl=3% 时 ≈ 6% 仓位保证金×杠杆放大);
- 回测假设「只有先触发的腿成交」——**both-fill 是回测里不存在的成本**,fade 实盘表现
  预期略差于回测,差值取决于 20s 内双向扫过 float 区间的频率(float 越窄越危险,
  幸存参数 f=0.5~1.0% 属于较宽档)。
- 模拟盘验证期要专门盯 `[reconcile][fade] 两腿都已成交` 日志的出现频率。

### 其它限制

- **保证金占用(2026-07-28 核实 OKX 规则后修正)**:入场用计划委托(trigger algo),OKX 触发前
  **不冻结保证金**——fade 双腿 pending 占用为 0,一腿成交后占 10%(与 trend 相同),另一腿 20s 内撤销。
  both-fill 时同 pair 多空双持占 20%,但两腿对冲净敞口≈0,不推高强平风险;真实风险是两腿先后都 SL
  的已实现亏损(见上节)。代价:触发瞬间可用保证金不足时 OKX 直接**触发失败=漏单**(不是爆仓),
  多 pair 同时持仓时后触发的腿有漏单可能,模拟盘验证期同样要盯。
- `_try_reentry`(SL 后日内重挂)对 fade 腿按「同方向非 CANCELLED」计数,行为与单向一致。
- 混周期下**不同 pair 的 sig_id 长度不同**(1D=日期,12H/6H=带小时),报表按 pair 分组时正常,
  跨 pair 按 signal_date join 时注意。

## 四、历史 bug 防线检查清单(fade 相关回归测试覆盖)

| 事故 | 防线 | 回归测试 |
|---|---|---|
| 07-09 错绑方向 | 改绑需 posSide+前缀硬校验(原有);Step-2 平仓匹配新增 posSide 校验 | `test_step2_exit_matches_correct_leg_by_pos_side` |
| 07-12 新单误撤 | `_FRESH_PENDING_GRACE_MS` 宽限(原有,fade 两腿同享) | 原有 cleanup 测试 |
| 07-16 残单裸奔 | 撤 sibling 时连残单一起撤 | `test_fade_sibling_cancel_also_clears_residual_order` |
| 07-20 双录 | clOrdId 含方向字符,两腿天然不同键;db 唯一索引不变 | `test_insert_trade_idempotent_by_account_algo_id` |
| 07-20 TP 触发未成交 | SLIP_PCT 穿价偏移(原有,每腿独立生效) | 原有 order_manager 测试 |
| whipsaw 双持 | both-fill 不误撤 + ERROR 告警 | `test_fade_both_filled_no_cancel_and_both_settle` |

## 五、上线路径(2026-07-29 更新:已启用)

1. ~~本次开发只合入代码,config 未启用~~ → **2026-07-28 午后模拟盘已全量启用**(v3-mixed A/B/C,
   commit ac5cf00),fade/reversal/混周期首次真实运行,实盘全停等验收。
2. 验证期半个月(至 08-11~15),重点观察 both-fill 频率(硬性门槛 ≤5%)、OCO 撤单延迟、漏单、分腿胜率。
   验收标准与资金安排见 `资金与仓位规划.md`。
3. 观测配套已补齐(07-28~29):db `signal_bar` 行级周期列、`trigger_price` 触发价列(滑点=entry_price-trigger_price)、
   reconciler ORPHAN 前回查 OKX 终态(order_failed 记「漏单」ERROR)。
