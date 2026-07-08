# 3 套账户级三币组合策略 (MDD ≤ 30% 精细回测)

- 数据:2 年 730 天,BTC/ETH/SOL 各底粒度 5m/15m/30m
- 初始 300 USDT,每笔 30 USDT 固定保证金,杠杆 100x,双向 taker 5bp × 2
- 每套组合 = 一个信号周期 + 三币各自最优的 float/tp/sl

| 组合 | 品种 | float | tp | sl | 收益率 | MDD | 胜率 | 笔数 |
|---|---|---|---|---|---|---|---|---|
| 4时三币 | BTC-USDT-SWAP | 0.005 | 0.006 | 0.015 | +4891.0% | 23.1% | 81.7% | 4229 |
| 4时三币 | ETH-USDT-SWAP | 0.004 | 0.008 | 0.02 | +9611.0% | 19.8% | 83.1% | 4253 |
| 4时三币 | SOL-USDT-SWAP | 0.004 | 0.008 | 0.02 | +10185.0% | 27.7% | 83.6% | 4227 |
| 6时三币 | BTC-USDT-SWAP | 0.005 | 0.006 | 0.02 | +5891.0% | 19.1% | 88.8% | 2811 |
| 6时三币 | ETH-USDT-SWAP | 0.005 | 0.01 | 0.02 | +8103.0% | 13.3% | 79.6% | 2827 |
| 6时三币 | SOL-USDT-SWAP | 0.0025 | 0.01 | 0.02 | +9570.0% | 17.8% | 81.3% | 2820 |
| 12时三币 | BTC-USDT-SWAP | 0.003 | 0.008 | 0.02 | +2870.0% | 24.2% | 82.4% | 1394 |
| 12时三币 | ETH-USDT-SWAP | 0.002 | 0.01 | 0.02 | +4737.0% | 17.6% | 81.3% | 1403 |
| 12时三币 | SOL-USDT-SWAP | 0.004 | 0.01 | 0.02 | +4455.0% | 12.9% | 80.7% | 1385 |

## 每套组合的 YAML 片段 (粘进 config.yaml 的 accounts[N].strategy)

### 4时三币组合 (signal_bar=4H)

```yaml
strategy:
  signal_bar: 4H
  pair_overrides:
    BTC-USDT-SWAP:
      float_pct: 0.005
      tp_pct: 0.006
      sl_pct: 0.015
    ETH-USDT-SWAP:
      float_pct: 0.004
      tp_pct: 0.008
      sl_pct: 0.02
    SOL-USDT-SWAP:
      float_pct: 0.004
      tp_pct: 0.008
      sl_pct: 0.02
```

### 6时三币组合 (signal_bar=6H)

```yaml
strategy:
  signal_bar: 6H
  pair_overrides:
    BTC-USDT-SWAP:
      float_pct: 0.005
      tp_pct: 0.006
      sl_pct: 0.02
    ETH-USDT-SWAP:
      float_pct: 0.005
      tp_pct: 0.01
      sl_pct: 0.02
    SOL-USDT-SWAP:
      float_pct: 0.0025
      tp_pct: 0.01
      sl_pct: 0.02
```

### 12时三币组合 (signal_bar=12H)

```yaml
strategy:
  signal_bar: 12H
  pair_overrides:
    BTC-USDT-SWAP:
      float_pct: 0.003
      tp_pct: 0.008
      sl_pct: 0.02
    ETH-USDT-SWAP:
      float_pct: 0.002
      tp_pct: 0.01
      sl_pct: 0.02
    SOL-USDT-SWAP:
      float_pct: 0.004
      tp_pct: 0.01
      sl_pct: 0.02
```
