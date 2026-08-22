from datetime import datetime, timedelta, timezone

import pytest

from core.account_state import AccountState
from core.okx_client import OKXError
from data.db import DB
from execution.reconciler import Reconciler


UTC = timezone.utc

CONFIG = {"strategy": {
    "pairs": ["BTC-USDT-SWAP"],
    "position_pct": 0.10,
    "max_consecutive_losses": 3,
    "cooldown_hours": 24,
    "fixed_mode_threshold": 800_000,
    "fixed_mode_margin": 1000,
    "leverage": 100,
    "float_pct": 0.0015,
    "tp_pct": 0.012,
    "sl_pct": 0.005,
}}


class FakeOKX:
    """按 pair 返回预设的 orders-history 与 pending algos。"""

    def __init__(self, history_by_pair: dict | None = None,
                 pending: list[dict] | None = None,
                 balance: float | None = None,
                 positions: list[dict] | None = None,
                 pending_orders: list[dict] | None = None,
                 algo_orders: dict | None = None):
        self.history_by_pair = history_by_pair or {}
        self.pending = list(pending or [])
        self.balance = balance  # None → get_balance 返 0,余额同步跳过
        self.positions = list(positions or [])
        self.pending_orders = list(pending_orders or [])  # 普通挂单 (algo 触发后的残单)
        self.algo_orders = dict(algo_orders or {})  # algoId → 终态单据 (get_algo_order)
        self.cancelled: list[tuple[str, str]] = []
        self.cancelled_orders: list[tuple[str, str]] = []  # (instId, ordId)
        self.placed_ocos: list[dict] = []  # place_oco_order 调用记录
        self.closed_positions: list[dict] = []  # close_position 调用记录
        self.oco_error: Exception | None = None  # 设了就让 place_oco_order 抛
        self.calls = 0

    def list_order_history(self, instId=None, state="filled", limit=100):
        self.calls += 1
        return list(self.history_by_pair.get(instId, []))

    def list_pending_algos(self, instType="SWAP", instId=None, ordType="trigger"):
        return [o for o in self.pending
                if (not instId or o.get("instId") == instId)
                and o.get("ordType", "trigger") == ordType]

    def cancel_algo_order(self, algoId, instId):
        self.cancelled.append((algoId, instId))
        self.pending = [o for o in self.pending if o.get("algoId") != algoId]
        return {"code": "0"}

    def get_positions(self, instId=None):
        return [p for p in self.positions if not instId or p.get("instId") == instId]

    def get_balance(self, ccy="USDT"):
        return float(self.balance) if self.balance is not None else 0.0

    def get_cash_balance(self, ccy="USDT"):
        # 测试里 balance 即现金余额 (fake 不区分 eq/cashBal)
        return float(self.balance) if self.balance is not None else 0.0

    def list_pending_orders(self, instType="SWAP", instId=None):
        return [o for o in self.pending_orders
                if not instId or o.get("instId") == instId]

    def cancel_order(self, instId, ordId):
        self.cancelled_orders.append((instId, ordId))
        self.pending_orders = [o for o in self.pending_orders
                               if o.get("ordId") != ordId]
        return {"code": "0"}

    def get_algo_order(self, algoClOrdId=None, algoId=None):
        return self.algo_orders.get(algoId)

    def place_oco_order(self, instId, tdMode, side, sz, posSide=None,
                        tpTriggerPx=None, tpOrdPx=None,
                        slTriggerPx=None, slOrdPx=None,
                        triggerPxType="last", reduceOnly=True, ccy="USDT"):
        if self.oco_error is not None:
            raise self.oco_error
        self.placed_ocos.append({
            "instId": instId, "side": side, "sz": sz, "posSide": posSide,
            "tpTriggerPx": tpTriggerPx, "slTriggerPx": slTriggerPx,
        })
        return {"code": "0", "data": [{"algoId": f"OCO{len(self.placed_ocos)}"}]}

    def close_position(self, instId, mgnMode, posSide=None, ccy="USDT",
                       autoCxl=True):
        self.closed_positions.append({"instId": instId, "posSide": posSide})
        return {"code": "0"}


def _fresh(tmp_path):
    db = DB(tmp_path / "recon.db")
    acc = AccountState(db, CONFIG)
    acc.set_balance(1000.0)
    return db, acc


def _mk_open_trade(db, algo_id="A1", pair="BTC-USDT-SWAP", side="short",
                    entry_price=60000.0, margin=100.0):
    return db.insert_trade(
        signal_date="2026-06-30", pair=pair, side=side,
        entry_price=entry_price, margin=margin, mode="PCT",
        okx_order_id=algo_id, entry_time=None,
    )


def test_no_open_trades_is_noop(tmp_path):
    db, acc = _fresh(tmp_path)
    r = Reconciler(FakeOKX({}), db, acc, CONFIG)
    assert r.run_once() == 0


def test_entry_only_backfills_entry_time(tmp_path):
    db, acc = _fresh(tmp_path)
    tid = _mk_open_trade(db)
    okx = FakeOKX({"BTC-USDT-SWAP": [
        {"algoId": "A1", "fillPx": "60050", "fillTime": "1751328000000",
         "reduceOnly": "false"},
    ]})
    r = Reconciler(okx, db, acc, CONFIG)
    assert r.run_once() == 1
    t = db.list_trades(limit=1)[0]
    assert t["entry_time"] and "2025" in t["entry_time"] or "2026" in t["entry_time"]
    assert t["entry_price"] == pytest.approx(60050.0)
    assert t["exit_price"] is None  # 未平仓


def test_full_exit_settles_and_updates_account(tmp_path):
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1", side="short", entry_price=60000.0, margin=100.0)
    # short：60000 入 → 59400 平，OKX exit order 携带 pnl=100 (直接用 OKX 值,不再本地估算)
    okx = FakeOKX({"BTC-USDT-SWAP": [
        {"algoId": "A1", "fillPx": "60000", "fillTime": "1751328000000",
         "reduceOnly": "false", "pnl": "0"},
        {"algoId": "A1", "fillPx": "59400", "fillTime": "1751331600000",
         "reduceOnly": "true", "category": "tp", "pnl": "100"},
    ]})
    r = Reconciler(okx, db, acc, CONFIG)
    assert r.run_once() == 2  # 一个 entry 回填 + 一个 exit 结算

    t = db.list_trades(limit=1)[0]
    assert t["exit_price"] == pytest.approx(59400.0)
    assert t["exit_reason"] == "TP"
    assert t["pnl"] == pytest.approx(100.0)
    assert acc.get_balance() == pytest.approx(1100.0)  # 1000 + 100
    assert acc.get_consecutive_losses() == 0


def test_loss_increments_streak(tmp_path):
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1", side="long", entry_price=60000.0, margin=100.0)
    # long：60000 入 → 59700 平,OKX exit order 携带 pnl=-50
    okx = FakeOKX({"BTC-USDT-SWAP": [
        {"algoId": "A1", "fillPx": "60000", "fillTime": "1751328000000",
         "reduceOnly": "false", "pnl": "0"},
        {"algoId": "A1", "fillPx": "59700", "fillTime": "1751331600000",
         "reduceOnly": "true", "category": "sl", "pnl": "-50"},
    ]})
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()

    t = db.list_trades(limit=1)[0]
    assert t["exit_reason"] == "SL"
    assert t["pnl"] == pytest.approx(-50.0)
    assert acc.get_balance() == pytest.approx(950.0)
    assert acc.get_consecutive_losses() == 1


def test_reconcile_is_idempotent(tmp_path):
    """跑两次不重复结算：db 里 exit_price 已存在的不再处理。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1", side="short", entry_price=60000.0, margin=100.0)
    okx = FakeOKX({"BTC-USDT-SWAP": [
        {"algoId": "A1", "fillPx": "60000", "fillTime": "1751328000000",
         "reduceOnly": "false"},
        {"algoId": "A1", "fillPx": "59400", "fillTime": "1751331600000",
         "reduceOnly": "true", "category": "tp"},
    ]})
    r = Reconciler(okx, db, acc, CONFIG)
    first = r.run_once()
    assert first == 2

    bal_after_first = acc.get_balance()
    second = r.run_once()
    assert second == 0  # 已闭合，不再出现在 list_open_trades
    assert acc.get_balance() == bal_after_first  # 余额没变


def test_uses_okx_pnl_when_provided(tmp_path):
    """OKX 提供了 pnl 字段（含手续费/资金费口径）就优先用它，不用估算。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1", side="short", entry_price=60000.0, margin=100.0)
    okx = FakeOKX({"BTC-USDT-SWAP": [
        {"algoId": "A1", "fillPx": "60000", "fillTime": "1751328000000",
         "reduceOnly": "false"},
        {"algoId": "A1", "fillPx": "59400", "fillTime": "1751331600000",
         "reduceOnly": "true", "category": "tp", "pnl": "97.35"},
    ]})
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()

    t = db.list_trades(limit=1)[0]
    assert t["pnl"] == pytest.approx(97.35)  # 用 OKX 给的，不是估算的 100


def test_exit_via_time_window_fallback_when_algo_id_differs(tmp_path):
    """OKX TP/SL 触发的平仓订单有独立 algoId，跟主 algo 不匹配。
    reconciler 必须用 pair+entry_time 时间窗口兜底匹配。"""
    db, acc = _fresh(tmp_path)
    tid = db.insert_trade(
        signal_date="2026-06-30", pair="BTC-USDT-SWAP", side="short",
        entry_price=60000.0, margin=100.0, mode="PCT",
        okx_order_id="MAIN_ALGO",
        entry_time="2026-07-01T14:00:00+00:00",
    )
    okx = FakeOKX({"BTC-USDT-SWAP": [
        {"algoId": "MAIN_ALGO", "ordId": "O1",
         "fillPx": "60000", "fillTime": "1782914400000",
         "reduceOnly": "false", "pnl": "0"},
        # 平仓：algoId 是独立的（模拟 OKX attach 触发生成的新 algoId）
        {"algoId": "DIFF_ALGO_XYZ", "ordId": "O2",
         "fillPx": "59400", "fillTime": "1782918000000",
         "reduceOnly": "true", "category": "tp", "pnl": "100"},
    ]})
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()

    t = db.list_trades(limit=1)[0]
    assert t["exit_price"] == pytest.approx(59400.0)
    assert t["exit_reason"] == "TP"
    assert t["pnl"] == pytest.approx(100.0)


def test_trade_without_algo_id_is_skipped(tmp_path):
    db, acc = _fresh(tmp_path)
    db.insert_trade(signal_date="2026-06-30", pair="BTC-USDT-SWAP", side="long",
                    entry_price=60000.0, margin=100.0, mode="PCT",
                    okx_order_id=None)
    r = Reconciler(FakeOKX(), db, acc, CONFIG)
    assert r.run_once() == 0


# ---------- 重复 pending 清理 ----------

def test_cleanup_single_pending_is_noop(tmp_path):
    """只有 1 张 pending → 不动。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1")
    okx = FakeOKX(pending=[{"algoId": "A1", "instId": "BTC-USDT-SWAP", "cTime": "1000"}])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert okx.cancelled == []


def test_cleanup_keeps_db_known_algo_id(tmp_path):
    """多张 pending，db 里记录的那张必须被保留，其他撤掉。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1")
    # A0 cTime 更早（正常应保留最早的），但 db 记录的是 A1 → 应保留 A1
    okx = FakeOKX(pending=[
        {"algoId": "A0", "instId": "BTC-USDT-SWAP", "cTime": "500"},
        {"algoId": "A1", "instId": "BTC-USDT-SWAP", "cTime": "1000"},
        {"algoId": "A2", "instId": "BTC-USDT-SWAP", "cTime": "1500"},
    ])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    cancelled_ids = {c[0] for c in okx.cancelled}
    assert cancelled_ids == {"A0", "A2"}
    assert [o["algoId"] for o in okx.pending] == ["A1"]

    # db 里 algoId 不变
    t = db.list_trades(limit=1)[0]
    assert t["okx_order_id"] == "A1"


def test_cleanup_orphan_rebinds_only_when_direction_and_prefix_match(tmp_path):
    """新版硬校验:orphan 必须同时满足 posSide 方向 + algoClOrdId 前缀含 signal_date 桶。
    命中 → 改绑,同时把无归属 survivor 撤单。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="GONE", side="short")  # 已丢
    okx = FakeOKX(pending=[
        # 方向对 + clOrdId 前缀对(BTC + 2026-06-30 alnum → hlBTC20260630)
        {"algoId": "OK_MATCH", "instId": "BTC-USDT-SWAP", "cTime": "1000",
         "posSide": "short", "algoClOrdId": "hlBTC20260630s1"},
        # 无归属 → 撤单
        {"algoId": "X_LATE", "instId": "BTC-USDT-SWAP", "cTime": "2000",
         "posSide": "short", "algoClOrdId": "hlBTC20260701s1"},
    ])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    cancelled_ids = {c[0] for c in okx.cancelled}
    assert cancelled_ids == {"X_LATE"}
    assert [o["algoId"] for o in okx.pending] == ["OK_MATCH"]
    t = db.list_trades(limit=1)[0]
    assert t["okx_order_id"] == "OK_MATCH"


def test_cleanup_rejects_wrong_direction_rebind(tmp_path):
    """事故复现:db trade 是 short,唯一 orphan 是 long → 拒绝改绑,orphan 撤,
    db trade 若桶已过则标 ORPHAN 平。"""
    db, acc = _fresh(tmp_path)
    tid = _mk_open_trade(db, algo_id="GONE", side="short")
    # 用一个远古 signal_date 保证 _is_past_bucket 为 True
    with db._conn() as c:
        c.execute("UPDATE trades SET signal_date='2020-01-01' WHERE id=?", (tid,))
    okx = FakeOKX(pending=[
        {"algoId": "WRONG_DIR", "instId": "BTC-USDT-SWAP", "cTime": "1000",
         "posSide": "long", "algoClOrdId": "hlBTC20200101l1"},
    ])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    # orphan 必须被撤(未被消化)
    assert {c[0] for c in okx.cancelled} == {"WRONG_DIR"}
    # db trade algoId 保持不变(未错绑),已过桶则被标 ORPHAN 平掉
    t = db.list_trades(limit=1)[0]
    assert t["okx_order_id"] == "GONE"  # 未被错误改绑
    assert t["exit_reason"] == "ORPHAN"
    assert t["exit_price"] == 0.0


def test_cleanup_rejects_wrong_signal_bucket(tmp_path):
    """方向对但 clOrdId 桶不对(跨天/跨桶) → 拒绝改绑。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="GONE", side="long")  # signal_date=2026-06-30
    okx = FakeOKX(pending=[
        # 方向对,但 clOrdId 前缀是别桶
        {"algoId": "WRONG_BKT", "instId": "BTC-USDT-SWAP", "cTime": "1000",
         "posSide": "long", "algoClOrdId": "hlBTC20260701l1"},
    ])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert {c[0] for c in okx.cancelled} == {"WRONG_BKT"}
    t = db.list_trades(limit=1)[0]
    assert t["okx_order_id"] == "GONE"  # db 未变(桶未过,不标 ORPHAN)


def test_startup_orphan_scan_cancels_duplicate_cl_ord_id(tmp_path):
    """startup_orphan_scan 独立于 open_trades:全 pair 扫,同 clOrdId 保留最早撤其余。"""
    db, acc = _fresh(tmp_path)
    okx = FakeOKX(pending=[
        {"algoId": "A_LATE", "instId": "BTC-USDT-SWAP", "cTime": "3000",
         "posSide": "long", "algoClOrdId": "hlBTC20260630l1"},
        {"algoId": "A_EARLY", "instId": "BTC-USDT-SWAP", "cTime": "1000",
         "posSide": "long", "algoClOrdId": "hlBTC20260630l1"},
        {"algoId": "B_ONLY", "instId": "ETH-USDT-SWAP", "cTime": "2000",
         "posSide": "short", "algoClOrdId": "hlETH20260630s1"},  # 不重复,不动
    ])
    r = Reconciler(okx, db, acc, CONFIG)
    n = r.startup_orphan_scan()
    assert n == 1
    assert {c[0] for c in okx.cancelled} == {"A_LATE"}
    assert {o["algoId"] for o in okx.pending} == {"A_EARLY", "B_ONLY"}


def test_startup_orphan_scan_no_pending(tmp_path):
    """无 pending → 0 cancels,不炸。"""
    db, acc = _fresh(tmp_path)
    okx = FakeOKX(pending=[])
    r = Reconciler(okx, db, acc, CONFIG)
    assert r.startup_orphan_scan() == 0


def test_cleanup_cancels_duplicate_cl_ord_id(tmp_path):
    """OKX 幂等键异常:同 algoClOrdId 出现 2 张 pending → 保留 cTime 最早,撤其余。"""
    db, acc = _fresh(tmp_path)
    # db 里没相关 trade → 走 "db 无 trade" 分支
    okx = FakeOKX(pending=[
        {"algoId": "DUP_LATE", "instId": "BTC-USDT-SWAP", "cTime": "2000",
         "posSide": "long", "algoClOrdId": "hlBTC20260630l1"},
        {"algoId": "DUP_EARLY", "instId": "BTC-USDT-SWAP", "cTime": "1000",
         "posSide": "long", "algoClOrdId": "hlBTC20260630l1"},
    ])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert {c[0] for c in okx.cancelled} == {"DUP_LATE"}
    assert [o["algoId"] for o in okx.pending] == ["DUP_EARLY"]


def test_cleanup_ignores_other_pairs(tmp_path):
    """非 strategy.pairs 里的 pair 不处理（避免动到用户自己下的单）。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1")
    okx = FakeOKX(pending=[
        {"algoId": "A1", "instId": "BTC-USDT-SWAP", "cTime": "1000"},
        {"algoId": "SOL1", "instId": "SOL-USDT-SWAP", "cTime": "1000"},
        {"algoId": "SOL2", "instId": "SOL-USDT-SWAP", "cTime": "2000"},
    ])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    # SOL 单一张不撤（非策略 pair，跟我们无关）
    assert okx.cancelled == []


def test_cleanup_keeps_two_legit_reentry_pendings(tmp_path):
    """日内重挂场景：同 pair 2 张 pending 都对应 db 里的 open trades → 都不撤。"""
    db, acc = _fresh(tmp_path)
    db.insert_trade(signal_date="2026-06-30", pair="BTC-USDT-SWAP", side="short",
                    entry_price=60000, margin=100, mode="PCT",
                    okx_order_id="A1", attempt=1)
    # A1 已成交进入建仓阶段，我们假设它 SL 了并进入 open_trades（exit_price None 表示未闭合）
    # 但 A1 是待触发挂单也可以。构造：两条 open trade 分别 attempt=1、attempt=2
    db.insert_trade(signal_date="2026-06-30", pair="BTC-USDT-SWAP", side="short",
                    entry_price=60300, margin=100, mode="PCT",
                    okx_order_id="A2", attempt=2)
    okx = FakeOKX(pending=[
        {"algoId": "A1", "instId": "BTC-USDT-SWAP", "cTime": "1000"},
        {"algoId": "A2", "instId": "BTC-USDT-SWAP", "cTime": "2000"},
    ])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    # 两张都合法（都在 db），不该撤
    assert okx.cancelled == []


def test_cleanup_ignores_fresh_orphan(tmp_path):
    """2026-07-12 race 事故防回归:cTime < 30s 的孤儿不当"过期孤儿"处理,防止
    order_manager.place_algo_order → insert_trade 之间的窗口被 reconciler 撞上。"""
    db, acc = _fresh(tmp_path)
    # db 里没相关 trade,pending 是"刚挂 3s"的新孤儿
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    okx = FakeOKX(pending=[
        {"algoId": "FRESH", "instId": "BTC-USDT-SWAP", "cTime": str(now_ms - 3000),
         "posSide": "long", "algoClOrdId": "hlBTC20260712l1"},
    ])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert okx.cancelled == []  # 不撤,给 insert_trade 缓冲窗口
    assert [o["algoId"] for o in okx.pending] == ["FRESH"]


def test_cleanup_still_cancels_stale_orphan(tmp_path):
    """cTime > 30s 的孤儿依然按原逻辑撤(保持既有行为)。
    构造:db 有一条 GONE 的 short trade, OKX pending 是老的孤儿且 clOrdId 前缀不匹配 → step 3 撤。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="GONE", side="short")  # signal_date=2026-06-30 (past)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    okx = FakeOKX(pending=[
        # 60s 前挂, clOrdId 前缀 hlBTC20260712 与 db 的 20260630 不匹配 → 改绑失败 → step 3 撤
        {"algoId": "STALE", "instId": "BTC-USDT-SWAP", "cTime": str(now_ms - 60_000),
         "posSide": "short", "algoClOrdId": "hlBTC20260712s1"},
    ])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert {c[0] for c in okx.cancelled} == {"STALE"}


def test_sweep_zombie_open_past_bucket(tmp_path):
    """db.open 但 algoId 不在 OKX pending 且信号桶已过 → 标 ORPHAN(僵尸兜底)。
    2026-07-12 daily_cancel 撤 OKX 后不同步 db 的僵尸堆积事故防回归。"""
    db, acc = _fresh(tmp_path)
    tid = _mk_open_trade(db, algo_id="DEAD")  # signal_date=2026-06-30, 早过
    okx = FakeOKX(pending=[])  # OKX 上啥都没
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    t = db.list_trades(limit=1)[0]
    assert t["exit_reason"] == "ORPHAN"
    assert t["exit_price"] == 0.0


def test_orphan_expire_cancels_residual_triggered_order(tmp_path):
    """2026-07-16 SOL 事故防回归: algo 触发后落地的限价单没成交 → algo 不在
    trigger pending, db 标 ORPHAN 前必须撤掉盘口上那张残留普通限价单,
    否则价格回来会以过期信号成交。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="TRIGGERED")  # signal_date=2026-06-30, 桶早过
    okx = FakeOKX(
        pending=[],  # algo 已触发 → 不在 trigger pending
        pending_orders=[
            # 触发后落地的限价单, 带 algoId 反查回主 algo
            {"instId": "BTC-USDT-SWAP", "ordId": "ORD_RESIDUAL",
             "algoId": "TRIGGERED", "state": "live"},
            # 无关残单 (别的 algoId), 不该被撤
            {"instId": "BTC-USDT-SWAP", "ordId": "ORD_OTHER",
             "algoId": "SOMEONE_ELSE", "state": "live"},
        ],
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    t = db.list_trades(limit=1)[0]
    assert t["exit_reason"] == "ORPHAN"
    # 残单被精确撤掉, 无关单不动
    assert okx.cancelled_orders == [("BTC-USDT-SWAP", "ORD_RESIDUAL")]


def test_orphan_expire_rescues_triggered_and_filled_entry(tmp_path):
    """2026-07-28 trade#537 防回归: algo state=effective 且入场单已成交 →
    绝不标 ORPHAN, 回填 entry_time/entry_price 交还正常对账流。"""
    db, acc = _fresh(tmp_path)
    tid = _mk_open_trade(db, algo_id="EFFECTIVE_FILLED")  # 桶早过
    okx = FakeOKX(
        history_by_pair={"BTC-USDT-SWAP": [
            {"algoId": "EFFECTIVE_FILLED", "fillPx": "60100",
             "fillTime": "1753714875704", "reduceOnly": "false"},
        ]},
        pending=[],  # 已触发 → 不在 trigger pending
        algo_orders={"EFFECTIVE_FILLED": {
            "state": "effective", "triggerTime": "1753714875706"}},
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    t = db.list_trades(limit=1)[0]
    assert t["exit_reason"] is None  # 不是 ORPHAN, 依然 open
    assert t["entry_time"]  # entry 已回填
    assert t["entry_price"] == pytest.approx(60100.0)


def test_orphan_expire_effective_but_unfilled_still_orphans(tmp_path):
    """algo=effective 但触发后的限价单没成交 (orders-history 无该 algoId 入场单)
    → 仍走 ORPHAN 清理 (2026-07-16 SOL 残单场景, 残单也要撤)。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="EFFECTIVE_UNFILLED")
    okx = FakeOKX(
        history_by_pair={"BTC-USDT-SWAP": []},  # 无成交
        pending=[],
        pending_orders=[
            {"instId": "BTC-USDT-SWAP", "ordId": "ORD_RESIDUAL",
             "algoId": "EFFECTIVE_UNFILLED", "state": "live"},
        ],
        algo_orders={"EFFECTIVE_UNFILLED": {
            "state": "effective", "triggerTime": "1753714875706"}},
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    t = db.list_trades(limit=1)[0]
    assert t["exit_reason"] == "ORPHAN"
    assert ("BTC-USDT-SWAP", "ORD_RESIDUAL") in okx.cancelled_orders


def test_orphan_expire_state_lookup_fails_skips_round(tmp_path):
    """标 ORPHAN 前 get_algo_order 抛异常 → 本轮不标, 保持 open 下轮重试
    (回查失败时贸然标 ORPHAN 可能误杀已触发活仓)。"""
    db, acc = _fresh(tmp_path)

    class BoomOKX(FakeOKX):
        def get_algo_order(self, algoClOrdId=None, algoId=None):
            raise RuntimeError("net down")

    _mk_open_trade(db, algo_id="DEAD")
    okx = BoomOKX(pending=[])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    t = db.list_trades(limit=1)[0]
    assert t["exit_reason"] is None  # 保持 open
    assert t["exit_price"] is None


def _mk_entered_trade(db, algo_id="A1", pair="BTC-USDT-SWAP", side="short",
                       entry_price=60000.0):
    tid = _mk_open_trade(db, algo_id=algo_id, pair=pair, side=side,
                          entry_price=entry_price)
    with db._conn() as c:
        c.execute("UPDATE trades SET entry_time=? WHERE id=?",
                  ("2026-06-30T12:00:00+00:00", tid))
    return tid


_ATTACH_TP_SL = {"attachAlgoOrds": [{
    "tpTriggerPx": "59400", "tpOrdPx": "59406",
    "slTriggerPx": "60300", "slOrdPx": "60306",
}]}


def test_unprotected_position_rearms_oco(tmp_path):
    """2026-07-30 ETH V 反事故防回归: 活仓 + OCO 触发未成交(无 pending 保护单,
    盘口剩 reduceOnly 残单) → 撤残单, 按主 algo attachAlgoOrds 参数重挂 OCO。"""
    db, acc = _fresh(tmp_path)
    _mk_entered_trade(db, algo_id="MAIN1")
    okx = FakeOKX(
        positions=[{"instId": "BTC-USDT-SWAP", "posSide": "short",
                    "pos": "5.29", "mgnMode": "cross"}],
        pending=[],  # 无任何 pending algo (oco 已消耗)
        pending_orders=[  # TP 触发后落地未成交的平仓限价残单
            {"instId": "BTC-USDT-SWAP", "ordId": "RESID_TP",
             "posSide": "short", "reduceOnly": "true", "state": "live"},
        ],
        algo_orders={"MAIN1": {"state": "effective", **_ATTACH_TP_SL}},
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r._sweep_unprotected_positions()
    assert okx.cancelled_orders == [("BTC-USDT-SWAP", "RESID_TP")]
    assert len(okx.placed_ocos) == 1
    oco = okx.placed_ocos[0]
    assert oco["posSide"] == "short" and oco["side"] == "buy"
    assert oco["sz"] == "5.29"
    assert oco["tpTriggerPx"] == "59400" and oco["slTriggerPx"] == "60300"


def test_protected_position_not_rearmed(tmp_path):
    """已有 pending OCO 保护单 → 不动。"""
    db, acc = _fresh(tmp_path)
    _mk_entered_trade(db, algo_id="MAIN1")
    okx = FakeOKX(
        positions=[{"instId": "BTC-USDT-SWAP", "posSide": "short",
                    "pos": "5.29", "mgnMode": "cross"}],
        pending=[{"instId": "BTC-USDT-SWAP", "algoId": "OCO_LIVE",
                  "ordType": "oco", "posSide": "short"}],
        algo_orders={"MAIN1": {"state": "effective", **_ATTACH_TP_SL}},
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r._sweep_unprotected_positions()
    assert okx.placed_ocos == []
    assert okx.cancelled_orders == []


def test_no_position_skips_rearm(tmp_path):
    """OKX 无持仓(已平/未入场) → 交给平仓匹配流程, 不重挂。"""
    db, acc = _fresh(tmp_path)
    _mk_entered_trade(db, algo_id="MAIN1")
    okx = FakeOKX(
        positions=[],
        algo_orders={"MAIN1": {"state": "effective", **_ATTACH_TP_SL}},
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r._sweep_unprotected_positions()
    assert okx.placed_ocos == []


def test_rearm_cooldown_prevents_duplicate(tmp_path):
    """5 分钟冷却: 同一 trade 连续两轮 sweep 只挂一次 (pending 索引延迟防重复)。"""
    db, acc = _fresh(tmp_path)
    _mk_entered_trade(db, algo_id="MAIN1")
    okx = FakeOKX(
        positions=[{"instId": "BTC-USDT-SWAP", "posSide": "short",
                    "pos": "5.29", "mgnMode": "cross"}],
        algo_orders={"MAIN1": {"state": "effective", **_ATTACH_TP_SL}},
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r._sweep_unprotected_positions()
    r._sweep_unprotected_positions()  # 第二轮: pending 索引未更新, 仍查不到保护单
    assert len(okx.placed_ocos) == 1


def test_no_attach_params_skips_rearm(tmp_path):
    """主 algo 没带 attachAlgoOrds (无 TP/SL 设计的单) → 不属于兜底范围。"""
    db, acc = _fresh(tmp_path)
    _mk_entered_trade(db, algo_id="MAIN1")
    okx = FakeOKX(
        positions=[{"instId": "BTC-USDT-SWAP", "posSide": "short",
                    "pos": "5.29", "mgnMode": "cross"}],
        algo_orders={"MAIN1": {"state": "effective", "attachAlgoOrds": []}},
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r._sweep_unprotected_positions()
    assert okx.placed_ocos == []


def test_rearm_converges_breached_sl_to_last_price(tmp_path):
    """2026-08-06 BTC 事故防回归: 现价已越过原 SL (short, last 60500 > SL 60300)
    → SL 收敛到现价 +0.2% 重挂, 而不是原价被 51278 拒到死循环。TP 未越线保持原价。"""
    db, acc = _fresh(tmp_path)
    _mk_entered_trade(db, algo_id="MAIN1")
    okx = FakeOKX(
        positions=[{"instId": "BTC-USDT-SWAP", "posSide": "short",
                    "pos": "1.87", "mgnMode": "cross", "last": "60500"}],
        algo_orders={"MAIN1": {"state": "effective", **_ATTACH_TP_SL}},
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r._sweep_unprotected_positions()
    assert len(okx.placed_ocos) == 1
    oco = okx.placed_ocos[0]
    assert oco["tpTriggerPx"] == "59400"          # 未越线, 原价保留
    assert float(oco["slTriggerPx"]) == pytest.approx(60500 * 1.002)
    assert okx.closed_positions == []


def test_rearm_rejected_price_breach_closes_position(tmp_path):
    """收敛后仍被 51278 拒 (极速行情竞态) → 市价平仓兜底, 绝不留裸仓。"""
    db, acc = _fresh(tmp_path)
    _mk_entered_trade(db, algo_id="MAIN1")
    okx = FakeOKX(
        positions=[{"instId": "BTC-USDT-SWAP", "posSide": "short",
                    "pos": "1.87", "mgnMode": "cross", "last": "60500"}],
        algo_orders={"MAIN1": {"state": "effective", **_ATTACH_TP_SL}},
    )
    okx.oco_error = OKXError(
        "OKX error code=1 msg= sCode=51278 sMsg=SL trigger price cannot be "
        "lower than the last price  endpoint=/api/v5/trade/order-algo",
        code="1")
    r = Reconciler(okx, db, acc, CONFIG)
    r._sweep_unprotected_positions()
    assert okx.placed_ocos == []
    assert okx.closed_positions == [
        {"instId": "BTC-USDT-SWAP", "posSide": "short"}]


def test_rearm_other_okx_error_no_market_close(tmp_path):
    """非触发价越线类错误 (如余额/参数) → 不市价平仓, 走下轮重试。"""
    db, acc = _fresh(tmp_path)
    _mk_entered_trade(db, algo_id="MAIN1")
    okx = FakeOKX(
        positions=[{"instId": "BTC-USDT-SWAP", "posSide": "short",
                    "pos": "1.87", "mgnMode": "cross"}],
        algo_orders={"MAIN1": {"state": "effective", **_ATTACH_TP_SL}},
    )
    okx.oco_error = OKXError("OKX error code=1 msg= sCode=51119 sMsg=whatever",
                             code="1")
    r = Reconciler(okx, db, acc, CONFIG)
    r._sweep_unprotected_positions()   # 不抛出 (外层 except 吃掉记 warning)
    assert okx.closed_positions == []


def test_sweep_zombie_open_current_bucket_skips(tmp_path):
    """当前桶(未过窗口)不应被 sweep 误伤。"""
    db, acc = _fresh(tmp_path)
    # 用今天的 signal_date, _is_past_bucket 判 False
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    db.insert_trade(
        signal_date=today, pair="BTC-USDT-SWAP", side="short",
        entry_price=60000.0, margin=100.0, mode="PCT",
        okx_order_id="LIVE_TODAY", entry_time=None,
    )
    okx = FakeOKX(pending=[])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    t = db.list_trades(limit=1)[0]
    assert t["exit_reason"] is None  # 依然 open
    assert t["exit_price"] is None


def test_sweep_zombie_open_entry_filled_skips(tmp_path):
    """已 entry_time (活持仓) 不该被 sweep 误伤 (test_entry_only_backfills_entry_time 场景)。"""
    db, acc = _fresh(tmp_path)
    tid = _mk_open_trade(db, algo_id="FILLED_ALGO")
    # 手动设 entry_time (模拟已入场)
    with db._conn() as c:
        c.execute("UPDATE trades SET entry_time=? WHERE id=?",
                  ("2026-06-30T12:00:00+00:00", tid))
    okx = FakeOKX(pending=[])  # 已 triggered, 不在 pending
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    t = db.list_trades(limit=1)[0]
    assert t["exit_reason"] is None  # 持仓, 由 exit 匹配处理, 不该 ORPHAN
    assert t["entry_time"]


# ---------- 日内重挂 ----------

class FakeStrategyReentry:
    """最小 stub。只提供 reentry_floats_for + compute_reentry_signal。"""

    def __init__(self, floats: list[float]):
        self._floats = floats

    def reentry_floats_for(self, pair):
        return list(self._floats)

    def compute_reentry_signal(self, pair, direction, day_candles_so_far,
                                attempt, signal_date=None):
        # 固定返回一个可预测的 signal
        return {
            "pair": pair, "direction": direction,
            "entry_price": 1650.0, "tp_price": 1617.0, "sl_price": 1666.5,
            "signal_date": signal_date, "reason": "test reentry", "attempt": attempt,
        }


class FakeOrderMgrReentry:
    def __init__(self):
        self.calls: list[dict] = []
        self.next_algo_id = 9000

    def place_algo_orders(self, signal, margin, leverage, attempt=1, **kwargs):
        aid = str(self.next_algo_id)
        self.next_algo_id += 1
        self.calls.append({"signal": signal, "margin": margin, "attempt": attempt,
                            "algo_id": aid, **kwargs})
        return aid


def test_reentry_fires_after_sl(tmp_path):
    """完整流程：db 里有 attempt=1 open trade，OKX 显示已 SL →
    reconciler 结算 → 检测 pair 启用重挂 → 拉今日 K → 调用 strategy 算 attempt=2 入场价 →
    调用 order_mgr 挂 attempt=2。"""
    from datetime import datetime, timezone
    UTC = timezone.utc

    db, acc = _fresh(tmp_path)
    # 今天 = 2026-06-30，signal_date = 2026-06-29
    sig_date = "2026-06-29"
    tid = db.insert_trade(
        signal_date=sig_date, pair="ETH-USDT-SWAP", side="short",
        entry_price=1604.17, margin=100.0, mode="PCT",
        okx_order_id="A1", attempt=1,
    )
    # 让今天 UTC 在挂单日内
    today_ms = int(datetime(2026, 6, 30, 5, 0, tzinfo=UTC).timestamp() * 1000)

    class TimeAwareOKX(FakeOKX):
        def get_candles(self, instId, bar="1H", limit=24):
            # 返回一段"今日 UTC 已发生"的 K 线
            return [
                {"ts": today_ms - 3600_000, "open": 1600, "high": 1650,
                 "low": 1598, "close": 1610},
                {"ts": today_ms, "open": 1610, "high": 1620,
                 "low": 1600, "close": 1605},
            ]

    okx = TimeAwareOKX(
        history_by_pair={
            "ETH-USDT-SWAP": [
                # entry 成交
                {"algoId": "A1", "fillPx": "1604.17",
                 "fillTime": str(today_ms - 3600_000), "reduceOnly": "false"},
                # SL 平仓
                {"algoId": "A1", "fillPx": "1620.21",
                 "fillTime": str(today_ms - 1800_000),
                 "reduceOnly": "true", "category": "sl", "pnl": "-16"},
            ]
        }
    )

    strat_stub = FakeStrategyReentry(floats=[0.0015, 0.006])
    om_stub = FakeOrderMgrReentry()

    # 冻结 now 到 2026-06-30 05:00 UTC
    import execution.reconciler as recon_mod
    real_datetime = recon_mod.datetime

    class FrozenDT(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 6, 30, 5, 0, tzinfo=UTC)

    recon_mod.datetime = FrozenDT
    try:
        r = Reconciler(okx, db, acc, CONFIG, strategy=strat_stub,
                       order_manager=om_stub)
        r.run_once()
    finally:
        recon_mod.datetime = real_datetime

    # 断言：om_stub 收到 attempt=2 的挂单请求
    assert len(om_stub.calls) == 1
    call = om_stub.calls[0]
    assert call["attempt"] == 2
    assert call["signal"]["direction"] == "short"
    assert call["signal"]["entry_price"] == 1650.0


def test_reentry_skipped_after_tp(tmp_path):
    """TP 平仓不应触发重挂。"""
    from datetime import datetime, timezone
    UTC = timezone.utc
    db, acc = _fresh(tmp_path)
    sig_date = "2026-06-29"
    db.insert_trade(signal_date=sig_date, pair="ETH-USDT-SWAP", side="short",
                    entry_price=1604.17, margin=100.0, mode="PCT",
                    okx_order_id="A1", attempt=1)
    today_ms = int(datetime(2026, 6, 30, 5, 0, tzinfo=UTC).timestamp() * 1000)

    okx = FakeOKX(history_by_pair={
        "ETH-USDT-SWAP": [
            {"algoId": "A1", "fillPx": "1604.17", "fillTime": str(today_ms - 3600_000),
             "reduceOnly": "false"},
            {"algoId": "A1", "fillPx": "1572.09", "fillTime": str(today_ms - 1800_000),
             "reduceOnly": "true", "category": "tp", "pnl": "32"},
        ]
    })

    om_stub = FakeOrderMgrReentry()
    strat_stub = FakeStrategyReentry(floats=[0.0015, 0.006])
    r = Reconciler(okx, db, acc, CONFIG, strategy=strat_stub, order_manager=om_stub)
    r.run_once()
    assert om_stub.calls == []


def test_reentry_skipped_when_reached_max(tmp_path):
    """attempt=2 已在 db → 不再第 3 次重挂。"""
    from datetime import datetime, timezone
    UTC = timezone.utc
    db, acc = _fresh(tmp_path)
    sig_date = "2026-06-29"
    # 已经有 attempt=1 和 attempt=2 各一条（attempt=1 已闭合，attempt=2 SL）
    tid1 = db.insert_trade(signal_date=sig_date, pair="ETH-USDT-SWAP", side="short",
                           entry_price=1604, margin=100, mode="PCT",
                           okx_order_id="A1", attempt=1)
    db.update_trade_exit(trade_id=tid1, exit_price=1620, exit_reason="SL",
                          pnl=-16, exit_time="2026-06-30T03:00:00+00:00")
    tid2 = db.insert_trade(signal_date=sig_date, pair="ETH-USDT-SWAP", side="short",
                           entry_price=1650, margin=100, mode="PCT",
                           okx_order_id="A2", attempt=2)
    today_ms = int(datetime(2026, 6, 30, 5, 0, tzinfo=UTC).timestamp() * 1000)

    okx = FakeOKX(history_by_pair={
        "ETH-USDT-SWAP": [
            {"algoId": "A2", "fillPx": "1650", "fillTime": str(today_ms - 3600_000),
             "reduceOnly": "false"},
            {"algoId": "A2", "fillPx": "1666.5", "fillTime": str(today_ms - 1800_000),
             "reduceOnly": "true", "category": "sl", "pnl": "-16.5"},
        ]
    })

    om_stub = FakeOrderMgrReentry()
    strat_stub = FakeStrategyReentry(floats=[0.0015, 0.006])
    r = Reconciler(okx, db, acc, CONFIG, strategy=strat_stub, order_manager=om_stub)
    r.run_once()
    # attempt=2 已存在，不再挂 attempt=3
    assert om_stub.calls == []


# ---------------- P3: 网络失败信号 ----------------

class RaisingOKX:
    """按调用序列抛出不同错误的 OKX,用于验证 last_run_had_net_error 标记。"""

    def __init__(self, exc_seq):
        self._exc_seq = list(exc_seq)
        self._i = 0

    def _next_exc(self):
        exc = self._exc_seq[self._i]
        self._i = min(self._i + 1, len(self._exc_seq) - 1)
        return exc

    def list_order_history(self, instId=None, state="filled", limit=100):
        raise self._next_exc()

    def list_pending_algos(self, instType="SWAP", instId=None, ordType="trigger"):
        raise self._next_exc()

    def list_positions_history(self, instType="SWAP", instId=None, limit=100):
        raise self._next_exc()

    def cancel_algo_order(self, algoId, instId):  # pragma: no cover
        return {"code": "0"}


def test_run_once_marks_net_error_on_requests_exception(tmp_path):
    import requests
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1")
    # list_pending_algos(cleanup 里第一个调) → 网络异常
    okx = RaisingOKX([requests.ConnectionError("dns failed")])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert r.last_run_had_net_error is True


def test_run_once_okx_business_error_not_flagged_as_net(tmp_path):
    from core.okx_client import OKXError
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1")
    okx = RaisingOKX([OKXError("code=51000 msg=param err", code="51000")])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert r.last_run_had_net_error is False


def test_run_once_net_error_flag_resets_each_call(tmp_path):
    """先失败一轮标记为 True,下一轮全成功应重置为 False。"""
    import requests
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1")
    okx = FakeOKX({"BTC-USDT-SWAP": [
        {"algoId": "A1", "fillPx": "60050", "fillTime": "1751328000000",
         "reduceOnly": "false"},
    ]})
    r = Reconciler(okx, db, acc, CONFIG)
    r.last_run_had_net_error = True  # 假装上轮有网络失败
    r.run_once()
    assert r.last_run_had_net_error is False


# ---------------- 平仓后余额同步 (吸收充值/提现) ----------------

def _tp_history():
    return {"BTC-USDT-SWAP": [
        {"algoId": "A1", "fillPx": "60000", "fillTime": "1751328000000",
         "reduceOnly": "false", "pnl": "0"},
        {"algoId": "A1", "fillPx": "59400", "fillTime": "1751331600000",
         "reduceOnly": "true", "category": "tp", "pnl": "100"},
    ]}


def test_exit_syncs_balance_from_okx_when_flat(tmp_path):
    """平仓结算后无持仓 → 用 OKX 余额覆盖本地(中途充值 500 被吸收)。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1", side="short", entry_price=60000.0, margin=100.0)
    # 本地口径 1000+100=1100,但 OKX 上用户充了 500 → 1600
    okx = FakeOKX(_tp_history(), balance=1600.0)
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert acc.get_balance() == pytest.approx(1600.0)


def test_exit_syncs_balance_even_when_position_held(tmp_path):
    """其它 pair 有持仓也同步: cashBal 不含未实现盈亏, 持仓不再阻塞余额更新
    (2026-07-20 反馈: 账户长期持仓导致余额一直不同步, 看着有歧义)。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1", side="short", entry_price=60000.0, margin=100.0)
    okx = FakeOKX(_tp_history(), balance=1600.0,
                  positions=[{"instId": "ETH-USDT-SWAP", "pos": "3"}])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert acc.get_balance() == pytest.approx(1600.0)  # cashBal 直接对齐


def test_exit_keeps_local_balance_when_okx_fetch_fails(tmp_path):
    """get_balance 抛异常 → 沿用本地累加值,不影响结算。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1", side="short", entry_price=60000.0, margin=100.0)

    class BalanceFailOKX(FakeOKX):
        def get_balance(self, ccy="USDT"):
            import requests
            raise requests.ConnectionError("dns failed")

        def get_cash_balance(self, ccy="USDT"):
            import requests
            raise requests.ConnectionError("dns failed")

    okx = BalanceFailOKX(_tp_history())
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert acc.get_balance() == pytest.approx(1100.0)


# ---------- fade OCO: 一腿成交撤另一腿 (2026-07 新结构) ----------

def _mk_fade_pair(db, lg="fSOL20260728", pair="SOL-USDT-SWAP",
                  algo_long="AL", algo_short="AS"):
    """同桶 fade 双腿: long + short 共享 leg_group。返回 (tid_long, tid_short)。"""
    tid_l = db.insert_trade(
        signal_date="2026-06-30", pair=pair, side="long",
        entry_price=74.0, margin=10.0, mode="PCT",
        okx_order_id=algo_long, leg_group=lg)
    tid_s = db.insert_trade(
        signal_date="2026-06-30", pair=pair, side="short",
        entry_price=77.0, margin=10.0, mode="PCT",
        okx_order_id=algo_short, leg_group=lg)
    return tid_l, tid_s


def test_fade_long_fill_cancels_short_sibling(tmp_path):
    """多腿 entry 成交 → 空腿 OKX 撤单 + db 标 CANCELLED(pnl=0)。"""
    db, acc = _fresh(tmp_path)
    tid_l, tid_s = _mk_fade_pair(db)
    okx = FakeOKX(
        history_by_pair={"SOL-USDT-SWAP": [
            {"algoId": "AL", "fillPx": "74.0", "fillTime": "1751328000000",
             "reduceOnly": "false"},
        ]},
        pending=[{"algoId": "AS", "instId": "SOL-USDT-SWAP", "cTime": "1000"}],
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()

    # 空腿被撤: OKX cancel 被调用 + db 标 CANCELLED
    assert ("AS", "SOL-USDT-SWAP") in okx.cancelled
    rows = {t["id"]: t for t in db.list_trades(limit=10)}
    assert rows[tid_s]["exit_reason"] == "CANCELLED"
    assert rows[tid_s]["pnl"] == 0
    # 多腿正常入场, 不受影响
    assert rows[tid_l]["entry_time"] is not None
    assert rows[tid_l]["exit_price"] is None
    # 余额/连亏不受 CANCELLED 影响
    assert acc.get_balance() == pytest.approx(1000.0)
    assert acc.get_consecutive_losses() == 0


def test_fade_short_fill_cancels_long_sibling(tmp_path):
    """镜像: 空腿成交 → 多腿撤。"""
    db, acc = _fresh(tmp_path)
    tid_l, tid_s = _mk_fade_pair(db)
    okx = FakeOKX(
        history_by_pair={"SOL-USDT-SWAP": [
            {"algoId": "AS", "fillPx": "77.0", "fillTime": "1751328000000",
             "reduceOnly": "false"},
        ]},
        pending=[{"algoId": "AL", "instId": "SOL-USDT-SWAP", "cTime": "1000"}],
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert ("AL", "SOL-USDT-SWAP") in okx.cancelled
    rows = {t["id"]: t for t in db.list_trades(limit=10)}
    assert rows[tid_l]["exit_reason"] == "CANCELLED"
    assert rows[tid_s]["entry_time"] is not None


def test_fade_both_filled_no_cancel_and_both_settle(tmp_path):
    """both-fill 边界(20s 竞态窗口内行情双向扫过): 两腿都已入场 → 谁也不撤,
    各自走 TP/SL 结算。不能误撤活仓(会裸奔)。"""
    db, acc = _fresh(tmp_path)
    tid_l, tid_s = _mk_fade_pair(db)
    okx = FakeOKX(
        history_by_pair={"SOL-USDT-SWAP": [
            {"algoId": "AL", "fillPx": "74.0", "fillTime": "1751328000000",
             "reduceOnly": "false"},
            {"algoId": "AS", "fillPx": "77.0", "fillTime": "1751328005000",
             "reduceOnly": "false"},
        ]},
        pending=[],
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    # 谁也没被撤
    assert okx.cancelled == []
    rows = {t["id"]: t for t in db.list_trades(limit=10)}
    assert rows[tid_l]["exit_reason"] is None
    assert rows[tid_s]["exit_reason"] is None
    assert rows[tid_l]["entry_time"] is not None
    assert rows[tid_s]["entry_time"] is not None


def test_fade_sibling_cancel_also_clears_residual_order(tmp_path):
    """撤 sibling 时连已触发未成交的限价残单一起撤(2026-07-16 SOL 残单裸奔事故防线)。"""
    db, acc = _fresh(tmp_path)
    tid_l, tid_s = _mk_fade_pair(db)
    okx = FakeOKX(
        history_by_pair={"SOL-USDT-SWAP": [
            {"algoId": "AL", "fillPx": "74.0", "fillTime": "1751328000000",
             "reduceOnly": "false"},
        ]},
        pending=[{"algoId": "AS", "instId": "SOL-USDT-SWAP", "cTime": "1000"}],
        # 空腿 algo 已触发落地一张限价残单 (未成交)
        pending_orders=[{"algoId": "AS", "instId": "SOL-USDT-SWAP",
                         "ordId": "RESID1", "reduceOnly": "false"}],
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert ("SOL-USDT-SWAP", "RESID1") in okx.cancelled_orders
    rows = {t["id"]: t for t in db.list_trades(limit=10)}
    assert rows[tid_s]["exit_reason"] == "CANCELLED"


def test_trend_trade_without_leg_group_skips_oco(tmp_path):
    """单向 trend trade (leg_group=None) 不触发 OCO 路径 —— 回归保护。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="A1", side="long")
    # 同 pair 另一笔无关 open trade (无 leg_group), 不能被误撤
    other = db.insert_trade(
        signal_date="2026-06-30", pair="BTC-USDT-SWAP", side="short",
        entry_price=61000.0, margin=100.0, mode="PCT", okx_order_id="B1")
    okx = FakeOKX(
        history_by_pair={"BTC-USDT-SWAP": [
            {"algoId": "A1", "fillPx": "60000", "fillTime": "1751328000000",
             "reduceOnly": "false"},
        ]},
        pending=[{"algoId": "B1", "instId": "BTC-USDT-SWAP", "cTime": "1000"}],
    )
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    assert okx.cancelled == []  # B1 不能被 OCO 误撤
    rows = {t["id"]: t for t in db.list_trades(limit=10)}
    assert rows[other]["exit_reason"] is None


# ---------- Step-2 平仓匹配的方向校验 (fade both-fill 后绑对腿) ----------

def test_step2_exit_matches_correct_leg_by_pos_side(tmp_path):
    """两腿都入场后, 平仓单必须按 posSide 绑对腿:
    short 腿的平仓单(posSide=short)不能绑到 long 腿上。"""
    db, acc = _fresh(tmp_path)
    lg = "fSOL20260728"
    tid_l = db.insert_trade(
        signal_date="2026-06-30", pair="SOL-USDT-SWAP", side="long",
        entry_price=74.0, margin=10.0, mode="PCT",
        okx_order_id="AL", leg_group=lg,
        entry_time="2026-07-01T14:00:00+00:00")
    tid_s = db.insert_trade(
        signal_date="2026-06-30", pair="SOL-USDT-SWAP", side="short",
        entry_price=77.0, margin=10.0, mode="PCT",
        okx_order_id="AS", leg_group=lg,
        entry_time="2026-07-01T14:00:05+00:00")
    # 只有 short 腿的平仓单出现 (posSide=short, 独立 algoId)
    okx = FakeOKX(history_by_pair={"SOL-USDT-SWAP": [
        {"algoId": "TPALGO", "ordId": "O9", "fillPx": "76.0",
         "fillTime": "1782918000000", "reduceOnly": "true",
         "posSide": "short", "category": "tp", "pnl": "13.0"},
    ]})
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    rows = {t["id"]: t for t in db.list_trades(limit=10)}
    # short 腿结算, long 腿仍 open
    assert rows[tid_s]["exit_price"] == pytest.approx(76.0)
    assert rows[tid_s]["pnl"] == pytest.approx(13.0)
    assert rows[tid_l]["exit_price"] is None


# ---------- per-pair signal_bar (混周期) ----------

def test_is_past_bucket_uses_pair_signal_bar(tmp_path):
    """混周期账户: _is_past_bucket 按 pair 自己的周期判窗。
    6H pair 的桶 13 小时前已过窗(6h*2=12h), 1D pair 的同刻桶(1d*2=48h)未过。"""
    from strategy.high_low import HighLowStrategy

    db, acc = _fresh(tmp_path)
    cfg_mixed = {"strategy": {
        **CONFIG["strategy"],
        "signal_bar": "6H",
        "pair_overrides": {"BTC-USDT-SWAP": {"signal_bar": "1D"}},
    }}
    strat = HighLowStrategy(cfg_mixed)
    r = Reconciler(FakeOKX(), db, acc, CONFIG, strategy=strat)

    sig_13h_ago = (datetime.now(UTC) - timedelta(hours=13)).strftime("%Y-%m-%dT%H:00Z")
    # SOL 走账户级 6H: 13h > 12h 窗 → 已过
    assert r._is_past_bucket(sig_13h_ago, "SOL-USDT-SWAP") is True
    # BTC override 1D: 13h < 48h 窗 → 未过
    assert r._is_past_bucket(sig_13h_ago, "BTC-USDT-SWAP") is False


def test_reentry_count_ignores_opposite_leg_and_cancelled(tmp_path):
    """fade 双腿桶里 SL 重挂计数: 对向腿和 CANCELLED 腿不计入 already,
    防止翻倍误触 reentry_floats 上限。"""
    from strategy.high_low import HighLowStrategy

    db, acc = _fresh(tmp_path)
    cfg_re = {"strategy": {
        **CONFIG["strategy"],
        "pair_overrides": {"SOL-USDT-SWAP": {"reentry_floats": [0.005, 0.01]}},
    }}
    strat = HighLowStrategy(cfg_re)

    calls = []

    class SpyOM:
        def place_algo_orders(self, sig, **kw):
            calls.append((sig, kw))
            return "NEW_ALGO"

    lg = "fSOL20260728"
    # signal_date 必须是"上一个 1D 桶"(昨日 00:00), now 才落在挂单窗口 [sig+1d, sig+2d) 内
    sig_date = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT00:00Z")
    # long 腿 SL 平仓; short 腿被 OCO 撤 (CANCELLED)
    tid_l = db.insert_trade(signal_date=sig_date, pair="SOL-USDT-SWAP", side="long",
                            entry_price=74.0, margin=10.0, mode="PCT",
                            okx_order_id="AL", leg_group=lg,
                            entry_time="2026-07-01T14:00:00+00:00")
    tid_s = db.insert_trade(signal_date=sig_date, pair="SOL-USDT-SWAP", side="short",
                            entry_price=77.0, margin=10.0, mode="PCT",
                            okx_order_id="AS", leg_group=lg)
    db.update_trade_exit(trade_id=tid_s, exit_price=0, exit_reason="CANCELLED",
                          pnl=0, exit_time="2026-07-01T14:00:10+00:00")
    db.update_trade_exit(trade_id=tid_l, exit_price=71.8, exit_reason="SL",
                          pnl=-3.0, exit_time="2026-07-01T14:30:00+00:00")

    r = Reconciler(FakeOKXWithCandles(), db, acc, CONFIG,
                   strategy=strat, order_manager=SpyOM())
    sl_trade = db.list_trades(limit=10)
    sl_row = [t for t in sl_trade if t["id"] == tid_l][0]
    r._try_reentry(sl_row, datetime.now(UTC))

    # 老逻辑: same_day=2(两腿) >= len(floats)=2 → 被误跳过。
    # 新逻辑: 只数同向非 CANCELLED = 1 < 2 → 正常重挂 attempt=2
    assert len(calls) == 1
    assert calls[0][1].get("attempt") == 2


class FakeOKXWithCandles(FakeOKX):
    """_try_reentry 需要 get_candles 返回当前桶 K。"""
    def get_candles(self, instId, bar="1H", limit=24):
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        return [[str(now_ms), "74", "75", "71", "72", "0", "0", "0", "1"]]


# ---------------- 漏单检测 (验收硬性第三关, 2026-07-28 新增) ----------------

def test_expire_orphan_logs_order_failed_as_missed_fill(tmp_path, caplog):
    """ORPHAN 标记前回查 OKX 终态: order_failed(触发后下单被拒=漏单)必须 ERROR
    记「漏单」+ failCode, 与普通孤儿区分; 其它终态照旧只记 ORPHAN。"""
    import logging
    db, acc = _fresh(tmp_path)
    tid = _mk_open_trade(db, algo_id="MISS1")
    okx = FakeOKX(algo_orders={
        "MISS1": {"algoId": "MISS1", "state": "order_failed", "failCode": "51008"},
    })
    logger = logging.getLogger("t_missfill")
    r = Reconciler(okx, db, acc, CONFIG, logger=logger)
    row = db.list_trades(limit=1)[0]
    with caplog.at_level(logging.ERROR, logger="t_missfill"):
        r._expire_as_orphan(row)

    t = db.list_trades(limit=1)[0]
    assert t["exit_reason"] == "ORPHAN"
    text = caplog.text
    assert "漏单" in text
    assert "51008" in text
    assert "order_failed" in text  # ORPHAN 行附带终态


def test_expire_orphan_normal_state_no_missed_fill_log(tmp_path, caplog):
    """终态 canceled(daily_cancel 撤的)不产生「漏单」告警; 回查失败保持 open 下轮重试。"""
    import logging
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="C1")
    okx = FakeOKX(algo_orders={"C1": {"algoId": "C1", "state": "canceled"}})
    logger = logging.getLogger("t_missfill2")
    r = Reconciler(okx, db, acc, CONFIG, logger=logger)
    row = db.list_trades(limit=1)[0]
    with caplog.at_level(logging.ERROR, logger="t_missfill2"):
        r._expire_as_orphan(row)
    assert db.list_trades(limit=1)[0]["exit_reason"] == "ORPHAN"
    assert "漏单" not in caplog.text

    # get_algo_order 抛异常 → 不标 ORPHAN, 保持 open 下轮重试
    # (回查失败时贸然标可能误杀已触发活仓, 2026-07-28 trade#537 教训)
    _mk_open_trade(db, algo_id="E1", pair="BTC-USDT-SWAP")

    class BoomOKX(FakeOKX):
        def get_algo_order(self, algoClOrdId=None, algoId=None):
            raise RuntimeError("api down")

    r2 = Reconciler(BoomOKX(), db, acc, CONFIG, logger=logger)
    row2 = [t for t in db.list_trades(limit=5) if t["okx_order_id"] == "E1"][0]
    r2._expire_as_orphan(row2)
    assert [t for t in db.list_trades(limit=5)
            if t["okx_order_id"] == "E1"][0]["exit_reason"] is None


# ==================== 持仓超时强平 (_sweep_hold_timeout) ====================

class FakeStrategyTimeout:
    """最小 stub: 提供 max_hold_bars_for + signal_bar_for + tp_sl_for。"""

    def __init__(self, max_hold_bars=0.5, signal_bar="6H"):
        self._mult = max_hold_bars
        self._bar = signal_bar

    def max_hold_bars_for(self, pair):
        return self._mult

    def signal_bar_for(self, pair=None):
        return self._bar

    def tp_sl_for(self, pair):
        return (0.008, 0.030)


def _mk_held_trade(db, held_hours, algo_id="T1", pair="SOL-USDT-SWAP",
                   side="short"):
    """插一条已入场、持仓 held_hours 小时的 open trade。"""
    entry_dt = datetime.now(UTC) - timedelta(hours=held_hours)
    return db.insert_trade(
        signal_date="2026-08-18T06:00Z", pair=pair, side=side,
        entry_price=76.8, margin=10.0, mode="PCT", okx_order_id=algo_id,
        entry_time=entry_dt.isoformat(), signal_bar="6H",
    )


def _timeout_pos(pair="SOL-USDT-SWAP", side="short", pos="10"):
    return {"instId": pair, "posSide": side, "pos": pos, "mgnMode": "cross"}


def test_timeout_closes_position_past_limit(tmp_path):
    """6H 桶 × 0.5 = 3h 上限; 持仓 17h → 市价平仓。"""
    db, acc = _fresh(tmp_path)
    _mk_held_trade(db, held_hours=17)
    okx = FakeOKX(positions=[_timeout_pos()])
    r = Reconciler(okx, db, acc, CONFIG, strategy=FakeStrategyTimeout(0.5, "6H"))
    r._sweep_hold_timeout()
    assert len(okx.closed_positions) == 1
    assert okx.closed_positions[0]["instId"] == "SOL-USDT-SWAP"
    assert okx.closed_positions[0]["posSide"] == "short"


def test_timeout_holds_within_limit(tmp_path):
    """持仓 2h < 3h 上限 → 不平。"""
    db, acc = _fresh(tmp_path)
    _mk_held_trade(db, held_hours=2)
    okx = FakeOKX(positions=[_timeout_pos()])
    r = Reconciler(okx, db, acc, CONFIG, strategy=FakeStrategyTimeout(0.5, "6H"))
    r._sweep_hold_timeout()
    assert okx.closed_positions == []


def test_timeout_disabled_when_mult_zero(tmp_path):
    """max_hold_bars=0 → 功能关闭, 无论持多久都不平 (向后兼容默认)。"""
    db, acc = _fresh(tmp_path)
    _mk_held_trade(db, held_hours=99)
    okx = FakeOKX(positions=[_timeout_pos()])
    r = Reconciler(okx, db, acc, CONFIG, strategy=FakeStrategyTimeout(0, "6H"))
    r._sweep_hold_timeout()
    assert okx.closed_positions == []


def test_timeout_noop_without_strategy(tmp_path):
    """未注入 strategy → 静默跳过, 不抛。"""
    db, acc = _fresh(tmp_path)
    _mk_held_trade(db, held_hours=99)
    okx = FakeOKX(positions=[_timeout_pos()])
    r = Reconciler(okx, db, acc, CONFIG, strategy=None)
    r._sweep_hold_timeout()
    assert okx.closed_positions == []


def test_timeout_exit_labeled_TIME_not_SL(tmp_path):
    """超时平仓是市价单(无 TP/SL 字段), 结算时必须标 TIME ——
    否则会被 _infer_exit_reason_by_price 按价格就近误判成 SL 并触发 reentry。"""
    db, acc = _fresh(tmp_path)
    tid = _mk_held_trade(db, held_hours=17, algo_id="T9")
    okx = FakeOKX(positions=[_timeout_pos()])
    strat = FakeStrategyTimeout(0.5, "6H")
    r = Reconciler(okx, db, acc, CONFIG, strategy=strat)

    r._sweep_hold_timeout()
    assert len(okx.closed_positions) == 1
    assert tid in r._timeout_closed

    # 下一轮: 平仓单进 orders-history, 走主匹配结算。市价单无 category 字段,
    # 平仓价 79.3 贴近 short SL(76.8×1.03=79.1) → 不标 TIME 就会被判成 SL。
    # fillTime 必须晚于 entry_time(动态算的 now-17h), 否则 Step2 时间窗口不认。
    entry_ms = int((datetime.now(UTC) - timedelta(hours=17)).timestamp() * 1000)
    okx.positions = []
    okx.history_by_pair = {"SOL-USDT-SWAP": [
        {"algoId": "T9", "fillPx": "76.8", "fillTime": str(entry_ms),
         "reduceOnly": "false", "pnl": "0"},
        {"ordId": "X9", "algoId": "", "fillPx": "79.3",
         "fillTime": str(entry_ms + 3600_000), "reduceOnly": "true",
         "posSide": "short", "pnl": "-18"},
    ]}
    r.run_once()
    t = [x for x in db.list_trades(limit=10) if x["id"] == tid][0]
    assert t["exit_reason"] == "TIME"
    assert tid not in r._timeout_closed  # 标记已消费


def test_timeout_cooldown_prevents_duplicate_close(tmp_path):
    """平仓请求已发出但 OKX 持仓未同步 → 下一轮不重复发平仓。"""
    db, acc = _fresh(tmp_path)
    _mk_held_trade(db, held_hours=17)
    okx = FakeOKX(positions=[_timeout_pos()])
    r = Reconciler(okx, db, acc, CONFIG, strategy=FakeStrategyTimeout(0.5, "6H"))
    r._sweep_hold_timeout()
    r._sweep_hold_timeout()  # 持仓仍在(OKX 未同步), 冷却内不重复
    assert len(okx.closed_positions) == 1


def test_timeout_skips_trade_without_entry_time(tmp_path):
    """未入场(挂单中)的 trade 不参与超时判定。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="P1", pair="SOL-USDT-SWAP")  # entry_time=None
    okx = FakeOKX(positions=[_timeout_pos()])
    r = Reconciler(okx, db, acc, CONFIG, strategy=FakeStrategyTimeout(0.5, "6H"))
    r._sweep_hold_timeout()
    assert okx.closed_positions == []


def test_timeout_bucket_length_scales_limit(tmp_path):
    """1D 桶 × 0.5 = 12h 上限: 持仓 10h 不平, 13h 平。"""
    db, acc = _fresh(tmp_path)
    _mk_held_trade(db, held_hours=10, algo_id="D1")
    okx = FakeOKX(positions=[_timeout_pos()])
    r = Reconciler(okx, db, acc, CONFIG, strategy=FakeStrategyTimeout(0.5, "1D"))
    r._sweep_hold_timeout()
    assert okx.closed_positions == []

    db2, acc2 = _fresh(tmp_path / "b")
    _mk_held_trade(db2, held_hours=13, algo_id="D2")
    okx2 = FakeOKX(positions=[_timeout_pos()])
    r2 = Reconciler(okx2, db2, acc2, CONFIG, strategy=FakeStrategyTimeout(0.5, "1D"))
    r2._sweep_hold_timeout()
    assert len(okx2.closed_positions) == 1
