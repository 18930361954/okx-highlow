"""端到端生命周期测试（FakeOKX，无真实 API）：
  daily_signal_and_place → OKX 触发建仓 → OKX TP 平仓 → reconciler 结算
  → account 更新 → daily_report 出报告
断言：db.trades 完整闭合、account 余额/连亏正确、报告文本能看到成交。"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core.account_state import AccountState
from data.db import DB
from execution.order_manager import OrderManager
from execution.reconciler import Reconciler
from scripts.daily_report import generate_report
from strategy.high_low import HighLowStrategy


UTC = timezone.utc

CONFIG = {
    "strategy": {
        "pairs": ["BTC-USDT-SWAP"],
        "position_pct": 0.10,
        "float_pct": 0.0015,
        "tp_pct": 0.012,
        "sl_pct": 0.005,
        "leverage": 100,
        "trend_filter": True,
        "max_consecutive_losses": 3,
        "cooldown_hours": 24,
        "fixed_mode_threshold": 800_000,
        "fixed_mode_margin": 1000,
        "signal_time_utc": "00:00",
    },
    "system": {"log_level": "INFO", "log_keep_days": 30, "db_path": "data/trades.db",
               "daily_report_time_utc": "23:55"},
    "account": {"env": "demo", "td_mode": "cross"},
}


class FakeOKX:
    """一个可脚本化的 fake：控制 place → pending → filled 的状态转换。"""

    def __init__(self):
        self.pending: list[dict] = []
        self.history: list[dict] = []
        self.positions: list[dict] = []
        self.next_algo_id = 1000

    # --- account/leverage ---
    def set_leverage(self, *a, **kw):
        return {"code": "0"}

    def get_balance(self, ccy="USDT"):
        return 1000.0

    def get_positions(self, instId=None):
        return [p for p in self.positions if not instId or p.get("instId") == instId]

    # --- algo ---
    def place_algo_order(self, **body):
        aid = str(self.next_algo_id)
        self.next_algo_id += 1
        self.pending.append({
            "algoId": aid, "instId": body["instId"], "side": body["side"],
            "triggerPx": body.get("triggerPx"), "state": "live",
        })
        return {"code": "0", "data": [{"algoId": aid}]}

    def list_pending_algos(self, instType="SWAP", instId=None, ordType="trigger"):
        return [p for p in self.pending if not instId or p["instId"] == instId]

    def cancel_algo_order(self, algoId, instId):
        self.pending = [p for p in self.pending if p["algoId"] != algoId]
        return {"code": "0"}

    def list_order_history(self, instId=None, state="filled", limit=100):
        return [o for o in self.history if not instId or o["instId"] == instId]

    # --- scripted transitions used by the test ---
    def simulate_entry_fill(self, algo_id: str, fill_px: float, fill_time_ms: int):
        """服务端触发 entry 单：pending -> history(entry) + 建立持仓。"""
        pending = next((p for p in self.pending if p["algoId"] == algo_id), None)
        if not pending:
            return
        # 保留 algo（服务端仍持有 TP/SL 挂载）；写一条 entry 成交记录
        self.history.append({
            "algoId": algo_id, "instId": pending["instId"],
            "side": pending["side"], "fillPx": str(fill_px),
            "fillTime": str(fill_time_ms), "reduceOnly": "false",
        })
        self.positions.append({
            "instId": pending["instId"], "posSide": "short" if pending["side"] == "sell" else "long",
            "pos": "1", "avgPx": str(fill_px), "upl": "0",
        })

    def simulate_tp_fill(self, algo_id: str, fill_px: float, fill_time_ms: int, pnl: float):
        """服务端 TP 触发：history 增一条平仓单 + 撤持仓 + 撤 pending。"""
        entry = next((o for o in self.history if o["algoId"] == algo_id and o["reduceOnly"] == "false"), None)
        if not entry:
            return
        self.history.append({
            "algoId": algo_id, "instId": entry["instId"],
            "side": "buy" if entry["side"] == "sell" else "sell",
            "fillPx": str(fill_px), "fillTime": str(fill_time_ms),
            "reduceOnly": "true", "category": "tp", "pnl": str(pnl),
        })
        self.positions = [p for p in self.positions if p["instId"] != entry["instId"]]
        self.pending = [p for p in self.pending if p["algoId"] != algo_id]


def _mk_candles_bearish_day(base_ts_ms: int):
    """构造 24 根 1H 阴线：day_open=100, day_close=90, high=105, low=88 → 挂空。"""
    rows = []
    price = 100.0
    for i in range(24):
        o = price
        h = price + 1
        l = price - 1
        c = price - (10.0 / 24)  # 缓慢下跌
        rows.append({
            "timestamp": base_ts_ms + i * 3600_000,
            "open": o, "high": h, "low": l, "close": c,
        })
        price = c
    # 强制 high/low 覆盖以匹配预期
    rows[5]["high"] = 105.0
    rows[10]["low"] = 88.0
    return rows


def test_full_lifecycle_place_fill_settle_report(tmp_path, monkeypatch):
    db = DB(tmp_path / "e2e.db")
    account = AccountState(db, CONFIG)
    account.set_balance(1000.0)
    okx = FakeOKX()
    strat = HighLowStrategy(CONFIG)
    om = OrderManager(okx, db, td_mode="cross")

    # --- 阶段1：daily_signal → 下算法单 ---
    sig_date = "2024-06-29"
    candles = _mk_candles_bearish_day(1719619200000)  # 2024-06-29 00:00 UTC 起
    raw = [{"ts": c["timestamp"], "open": c["open"], "high": c["high"],
            "low": c["low"], "close": c["close"]} for c in candles]

    sig = strat.compute_signal("BTC-USDT-SWAP", raw, signal_date=sig_date)
    assert sig is not None
    assert sig["direction"] == "short"

    algo_id = om.place_algo_orders(sig, margin=100.0, leverage=100)
    assert algo_id is not None

    # db 里应有 1 条未闭合 trade
    trades = db.list_trades(limit=10)
    assert len(trades) == 1
    assert trades[0]["exit_price"] is None
    assert trades[0]["okx_order_id"] == algo_id

    # --- 阶段2：OKX 触发 entry ---
    entry_time_ms = 1719705600000  # 2024-06-30 00:00 UTC
    okx.simulate_entry_fill(algo_id, fill_px=sig["entry_price"], fill_time_ms=entry_time_ms)

    reconciler = Reconciler(okx, db, account, CONFIG)
    n = reconciler.run_once()
    assert n == 1  # 仅 entry 回填
    t = db.list_trades(limit=1)[0]
    assert t["entry_time"] and "2024-06-30" in t["entry_time"]
    assert t["exit_price"] is None

    # --- 阶段3：OKX TP 平仓（盈利 +100 USDT） ---
    tp_time_ms = entry_time_ms + 4 * 3600_000  # 4h 后
    # short：从 100 跌到 98.8 是 1.2%，pnl 由 fake 直接给
    tp_price = round(sig["entry_price"] * (1 - CONFIG["strategy"]["tp_pct"]), 6)
    okx.simulate_tp_fill(algo_id, fill_px=tp_price, fill_time_ms=tp_time_ms, pnl=100.0)

    n = reconciler.run_once()
    assert n == 1  # exit 结算
    t = db.list_trades(limit=1)[0]
    assert t["exit_price"] == pytest.approx(tp_price)
    assert t["exit_reason"] == "TP"
    assert t["pnl"] == pytest.approx(100.0)
    assert account.get_balance() == pytest.approx(1100.0)
    assert account.get_consecutive_losses() == 0

    # 幂等：再跑一次不重复结算
    assert reconciler.run_once() == 0
    assert account.get_balance() == pytest.approx(1100.0)

    # --- 阶段4：daily_report 能看到今日成交（signal_date=昨日） ---
    monkeypatch.setattr("scripts.daily_report.ROOT", tmp_path)
    # 今日 = 2024-06-30，signal_date = 2024-06-29
    out = generate_report(db, account, CONFIG, target_date="2024-06-30")
    text = out.read_text(encoding="utf-8")
    assert "BTC-USDT-SWAP" in text
    assert "TP" in text
    assert "+100.00" in text
    assert "1 笔" in text
    assert "盈 1 / 亏 0" in text


def test_three_consecutive_sl_triggers_cooldown_end_to_end(tmp_path):
    """跑 3 笔 SL：验证连亏累计 → 熔断触发 → can_trade 拒绝。"""
    db = DB(tmp_path / "e2e2.db")
    account = AccountState(db, CONFIG)
    account.set_balance(1000.0)
    okx = FakeOKX()
    om = OrderManager(okx, db, td_mode="cross")

    # 用"当前时间前 1 小时"作为 base，最后一笔 exit_time 就在 now 附近，
    # cooldown_until 落到 24h 后 → is_in_cooldown 判 True
    base_ms = int(datetime.now(UTC).timestamp() * 1000) - 3600_000
    for i in range(3):
        sig = {
            "pair": "BTC-USDT-SWAP", "direction": "long",
            "entry_price": 100.0, "tp_price": 101.2, "sl_price": 99.5,
            "signal_date": f"2026-06-{27 + i:02d}", "reason": "test",
        }
        algo_id = om.place_algo_orders(sig, margin=100.0, leverage=100)
        # 3 笔都在最近 1 小时内完成，最后一笔 exit_time ≈ now
        entry_ms = base_ms + i * 60_000
        okx.simulate_entry_fill(algo_id, fill_px=100.0, fill_time_ms=entry_ms)
        # SL：long 从 100 跌到 99.5 → -0.5%，pnl = 100*100*(-0.005) = -50
        okx.history.append({
            "algoId": algo_id, "instId": "BTC-USDT-SWAP", "side": "sell",
            "fillPx": "99.5", "fillTime": str(entry_ms + 3600_000),
            "reduceOnly": "true", "category": "sl", "pnl": "-50",
        })
        okx.positions = [p for p in okx.positions if p["instId"] != "BTC-USDT-SWAP"]
        okx.pending = [p for p in okx.pending if p["algoId"] != algo_id]

        Reconciler(okx, db, account, CONFIG).run_once()

    # 3 连亏 → 熔断
    assert account.is_in_cooldown()
    ok, why = account.can_trade()
    assert not ok
    assert "cooldown" in why
    # 余额从 1000 → 850
    assert account.get_balance() == pytest.approx(850.0)
