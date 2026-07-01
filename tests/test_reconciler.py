from datetime import datetime, timezone

import pytest

from core.account_state import AccountState
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
                 pending: list[dict] | None = None):
        self.history_by_pair = history_by_pair or {}
        self.pending = list(pending or [])
        self.cancelled: list[tuple[str, str]] = []
        self.calls = 0

    def list_order_history(self, instId=None, state="filled", limit=100):
        self.calls += 1
        return list(self.history_by_pair.get(instId, []))

    def list_pending_algos(self, instType="SWAP", instId=None, ordType="trigger"):
        return [o for o in self.pending if not instId or o.get("instId") == instId]

    def cancel_algo_order(self, algoId, instId):
        self.cancelled.append((algoId, instId))
        self.pending = [o for o in self.pending if o.get("algoId") != algoId]
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
    # short：60000 入 → 59400 平（-1%），margin=100, lev=100 → pnl = 100*100*0.01 = +100
    okx = FakeOKX({"BTC-USDT-SWAP": [
        {"algoId": "A1", "fillPx": "60000", "fillTime": "1751328000000",
         "reduceOnly": "false"},
        {"algoId": "A1", "fillPx": "59400", "fillTime": "1751331600000",
         "reduceOnly": "true", "category": "tp"},
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
    # long：60000 入 → 59700 平（-0.5%），pnl = 100*100*(-0.005) = -50
    okx = FakeOKX({"BTC-USDT-SWAP": [
        {"algoId": "A1", "fillPx": "60000", "fillTime": "1751328000000",
         "reduceOnly": "false"},
        {"algoId": "A1", "fillPx": "59700", "fillTime": "1751331600000",
         "reduceOnly": "true", "category": "sl"},
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


def test_cleanup_orphan_rebinds_db(tmp_path):
    """db 记录的 algoId 已不在 OKX（可能因外部撤单），同 pair 有其它 pending →
    保留 cTime 最早的，并把 db 的 algoId 改绑到它。"""
    db, acc = _fresh(tmp_path)
    _mk_open_trade(db, algo_id="GONE")  # OKX 上找不到
    okx = FakeOKX(pending=[
        {"algoId": "X_LATE", "instId": "BTC-USDT-SWAP", "cTime": "2000"},
        {"algoId": "X_EARLY", "instId": "BTC-USDT-SWAP", "cTime": "1000"},
    ])
    r = Reconciler(okx, db, acc, CONFIG)
    r.run_once()
    cancelled_ids = {c[0] for c in okx.cancelled}
    assert cancelled_ids == {"X_LATE"}
    assert [o["algoId"] for o in okx.pending] == ["X_EARLY"]

    # db 改绑到 X_EARLY
    t = db.list_trades(limit=1)[0]
    assert t["okx_order_id"] == "X_EARLY"


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
