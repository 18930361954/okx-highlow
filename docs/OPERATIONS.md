# 运维手册 (固定命令集)

> 本文档列出 okx-highlow 的固定运维命令。全部命令在项目根目录 `C:\Users\14559\PycharmProjects\okx-highlow\` 下执行。

---

## 1. 启动机器人

### 前提检查

```powershell
# 确认在项目根目录
cd C:\Users\14559\PycharmProjects\okx-highlow

# 确认依赖装齐(首次或换环境时跑一次)
python -m pip install -r requirements.txt

# 确认 .env 里 OKX_API_KEY 有(单账户回退需要;多账户段配了 API 就不用)
# 确认 config.yaml 里 accounts 段账户 enabled 值符合预期
```

### 启动命令

```powershell
python main.py
```

启动后会看到:

- `[boot] 启用 N 个账户: [...]` — 确认启用的账户列表
- 每账户 `[<账户名>] OKX connected (env=demo)` — 连通
- 每账户 `[<账户名>] signal_bar=XH, 每天 N 次挂单` — cron 就绪
- `[ready] HighLow Bot 系统就绪,等待下一次信号桶触发` — 已就位
- 上部 rich 面板显示第一个账户的实时状态

### 现在的启动状态(2026-07-08 起固定)

配置文件里:
- **3 个实盘账户** `enabled: false`(暂不启动,先跑模拟盘)
- **3 个模拟盘账户** 全部启用

启动即跑 3 个模拟盘:
- 模拟盘-主账户 (4H,每天 6 次)
- 模拟盘-A1455923264 (6H,每天 4 次)
- 模拟盘-bot14559 (12H,每天 2 次)

---

## 2. 停止机器人

### 优雅停止

在运行终端按 `Ctrl + C`。

- scheduler 立即停止,不再触发新 cron
- monitor 停止刷新面板
- **已挂的 OKX algo 单在服务端继续生效**(不会被撤,除非到 daily_cancel 时刻)
- 已入场的持仓 TP/SL 由 OKX 服务端管理,程序断线不影响

### 强制停止(极端场景)

关闭整个终端窗口即可。同上,OKX 服务端订单不受影响。

### 停止后手动撤 OKX 挂单

若不想让挂单继续等待触发,登录 OKX 网页 → 交易 → 策略委托 → 手动撤单。

---

## 3. 切换实盘/模拟盘

### 启用某个实盘账户

编辑 `config.yaml`,找到对应实盘账户,把 `enabled: false` 改为 `enabled: true`(或直接删掉这行,默认就是 true):

```yaml
- account_name: 实盘-bot14559
  enabled: true          # ← 改这里
  ...
```

保存后 **重启 `python main.py`** 生效。

### 禁用某个账户

同理,把 `enabled` 改成 `false`。

### 查看当前生效的账户

```powershell
python -c "import yaml; from data.db import DB; from core.multi_account import load_accounts; from utils.logger import get_logger; cfg = yaml.safe_load(open('config.yaml', encoding='utf-8')); rts = load_accounts(cfg, DB('data/trades.db'), get_logger('t', level='INFO')); print(f'启用 {len(rts)} 个:'); [print(f'  {rt.name} env={rt.cfg.env} signal_bar={rt.strategy.signal_bar}') for rt in rts]"
```

---

## 4. 日常查看

### 看实时日志(另开一个终端)

```powershell
Get-Content -Wait logs/bot.log
```

### 看某账户成交明细(最近 20 笔)

```powershell
python -c "from data.db import DB; db = DB('data/trades.db'); [print(r) for r in db.list_trades(limit=20, account='模拟盘-bot14559')]"
```

替换 `模拟盘-bot14559` 为其他账户名可查其他账户。

### 看某账户当前余额

```powershell
python -c "from data.db import DB; db = DB('data/trades.db'); print('余额:', db.get_state('current_balance', account='模拟盘-bot14559'))"
```

### 手动生成某天日报

```powershell
python scripts/daily_report.py --date 2026-07-08
```

生成到 `docs/daily_reports/report_2026-07-08.md`。

### 看已有的日报

```powershell
ls docs/daily_reports/
```

---

## 5. 故障处理

### 场景 1:某账户 3 连亏被熔断

日志会看到 `[熔断] 连亏 3 次,暂停至 2026-07-09T08:00:00+00:00`。

想立即解除熔断(重启后仍生效):

```powershell
python scripts/reset_cooldown.py
```

**注意**:这个脚本默认清 `default` 账户。多账户下需要指定账户:

```powershell
python -c "from data.db import DB; from core.account_state import AccountState; import yaml; cfg=yaml.safe_load(open('config.yaml',encoding='utf-8')); db=DB('data/trades.db'); acc=AccountState(db, {'strategy':cfg['strategy']}, account='模拟盘-bot14559'); acc.reset_cooldown(); print('done')"
```

### 场景 2:发现同 pair 有多张 pending 挂单

不用管 — reconciler 每 20 秒自动扫一次 `_cleanup_duplicate_pending`,会保留 db 已知那张,撤其他。

日志里会看到 `[reconcile] cleanup ETH-USDT-SWAP: 发现重复 pending algo algoId=XXX,撤单`。

### 场景 3:程序崩溃重启后要恢复未闭合交易

不用管 — 启动时会:
1. 立即跑一次 reconcile,把未闭合 trade 的 entry/exit 从 OKX orders-history 回填
2. 跑 `startup_catchup_if_needed`,判断当前信号桶该 pair 是否已挂单,未挂则补挂

### 场景 4:OKX 连接失败

日志会看到 `OKX connection failed — exiting`。

**排查顺序**:
1. `.env` 里 `OKX_API_KEY` 是不是空 / 拼错
2. `config.yaml` 里 accounts 的 API 三件套是不是对
3. 是不是网络问题 → 打开代理:`config.yaml` 里 `network.proxy_enabled: true`
4. OKX 服务是否正常(登录网页看)

### 场景 5:某账户的 OKX 余额没同步过来

首次启动 `init_balance_if_needed` 会调 `okx.get_balance` 拉一次。如果拉失败,db 里 `current_balance` 会保持 0,导致 `can_trade` 返回 `False` 一直不下单。

手动同步:

```powershell
python -c "import os, yaml; from dotenv import load_dotenv; load_dotenv(); from core.okx_client import OKXClient; from data.db import DB; from core.account_state import AccountState; cfg=yaml.safe_load(open('config.yaml',encoding='utf-8')); db=DB('data/trades.db'); # 修改下面账户名和 API for account_raw in cfg['accounts']:
    if account_raw.get('account_name') != '模拟盘-bot14559': continue
    okx = OKXClient(account_raw['api_key'], account_raw['secret_key'], account_raw['passphrase'], env='demo' if account_raw.get('env_adapt')=='demo' else 'live')
    bal = okx.get_balance('USDT')
    acc = AccountState(db, {'strategy':cfg['strategy']}, account=account_raw['account_name'])
    acc.set_balance(bal)
    print(f'{account_raw[\"account_name\"]} 余额同步到: {bal}')
"
```

---

## 6. 代理开关

如果需要走本地代理(比如国内网络):

编辑 `config.yaml`:

```yaml
network:
  proxy_enabled: true                # ← 改成 true
  proxy_url: "http://127.0.0.1:18081"
```

保存后 **重启** 生效。所有账户的 OKX REST 都会走这个代理。

---

## 7. 挂单信号触发时刻(UTC 时间)

| 账户 | signal_bar | UTC 触发时刻 | 每天次数 |
|---|---|---|---|
| 模拟盘-主账户 / 实盘-主账户 | 4H | 00:00 / 04:00 / 08:00 / 12:00 / 16:00 / 20:00 | 6 |
| 模拟盘-A1455923264 / 实盘-A1455923264 | 6H | 00:00 / 06:00 / 12:00 / 18:00 | 4 |
| 模拟盘-bot14559 / 实盘-bot14559 | 12H | 00:00 / 12:00 | 2 |

其他固定 cron:
- **每 20 秒**每账户各自 reconcile 一次
- **每日 23:55 UTC** 生成汇总日报到 `docs/daily_reports/report_YYYY-MM-DD.md`
- **每桶结束前 1 分钟** 撤销该账户未触发的挂单(例:4H 账户在 23:59/03:59/07:59/... 撤单)

**北京时间转换**:UTC + 8 = 北京时间。例如 UTC 00:00 = 北京 08:00。

---

## 8. 上线路径建议(顺序执行)

### 第一阶段:模拟盘观察(3-5 天)

现在的状态就是。启动后:

```powershell
python main.py
```

**目标观察**:
1. 每 4/6/12 小时挂单是否符合预期(看日志 `[scheduler] bucket_signal fired`)
2. 每笔实际成交价 vs 触发价的滑点(看 db.trades 里 entry_price vs 挂单时的 signal.entry_price)
3. 日报生成是否正常
4. 三个模拟盘的余额曲线走势

### 第二阶段:小额实盘试水(3-5 天)

若模拟盘滑点均值 < 15bp 且没异常:

1. 编辑 `config.yaml`,把 **实盘-bot14559** 的 `enabled: false` 改成 `true`
   - 12H 组合 MDD 34% 最保守,先跑它
2. 在 OKX 该账户存入 100-300 USDT
3. 重启 `python main.py`
4. 每日看 `docs/daily_reports/report_YYYY-MM-DD.md` 汇总

### 第三阶段:扩展实盘(逐步加账户)

12H 实盘稳定后,依次开启:

1. **实盘-主账户 (4H)** — MDD 31%
2. **实盘-A1455923264 (6H)** — MDD 84%,**最后再开**,资金规模上要能扛回撤

---

## 9. 关键回测参考数字(2 年复利,140U 起,含滑点/fee/funding/张数封顶)

| 账户 | 2 年终值 | 收益率 | MDD | 胜率 |
|---|---|---|---|---|
| 4H 组合 | 140 → 2.26M | +1611181% | 30.7% | 79.3% |
| 6H 组合 | 140 → 3.10M | +2214314% | **84.1%** | 66.1% |
| 12H 组合 | 140 → 2.11M | +1508864% | 33.9% | 80.3% |

**注意事项**:
- 数字是回测理论上限,含复利指数放大 + 张数封顶前期未触发
- 真实实盘按经验保守打对折,仍是可观收益
- **MDD 是心理承受关键**:6H 组合可能余额跌到峰值 16%,需要不动手能扛住

回测详情见 `docs/backtest_validated.md`。

---

## 10. 关闭机器人前的检查清单

关闭前如果不想留隐患:

- [ ] 看一眼终端面板,确认无异常持仓(意外方向 / 意外张数)
- [ ] 看一眼日志末尾,确认没有 ERROR / 51xxx 错误码堆积
- [ ] 若有未触发挂单不想留,`python -c "from execution.order_manager import OrderManager; ..."` 手动撤(或到 OKX 网页手动)
- [ ] `Ctrl + C` 优雅停止

---

## 11. 联系与备份

- 项目仓库(私有):https://github.com/18930361954/okx-highlow
- 日报归档:`docs/daily_reports/`
- 数据库:`data/trades.db`(SQLite 单文件,备份直接复制即可)
- 日志:`logs/bot.log*`(自动 rotate,默认保留 30 天)

**建议每周备份一次 `data/trades.db`** 到本地其他位置或 U 盘,防止误删。
