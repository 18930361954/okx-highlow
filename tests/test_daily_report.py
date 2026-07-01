"""daily_report 关键回归：今日报告能看到"signal_date=昨日"的已成交 trade。
不测 markdown 格式化细节，只保证内容出现。"""
from datetime import date, timedelta

from core.account_state import AccountState
from data.db import DB
from scripts.daily_report import generate_report


CONFIG = {
    "strategy": {
        "position_pct": 0.10,
        "max_consecutive_losses": 3,
        "cooldown_hours": 24,
        "fixed_mode_threshold": 800_000,
        "fixed_mode_margin": 1000,
    },
    "system": {"log_level": "INFO", "log_keep_days": 30, "db_path": "data/trades.db",
               "daily_report_time_utc": "23:55"},
}


def test_report_finds_yesterdays_signal_date(tmp_path, monkeypatch):
    # 让 report 输出到 tmp
    import scripts.daily_report as dr
    monkeypatch.setattr(dr, "ROOT", tmp_path)

    db = DB(tmp_path / "t.db")
    acc = AccountState(db, CONFIG)
    acc.set_balance(1000.0)

    today = date(2026, 6, 30)
    yesterday = today - timedelta(days=1)

    # 今日执行的一笔已闭合 trade（signal_date=昨日）
    tid = db.insert_trade(
        signal_date=yesterday.isoformat(),
        pair="BTC-USDT-SWAP", side="short",
        entry_price=60000.0, margin=100.0, mode="PCT",
        okx_order_id="A1",
    )
    db.update_trade_exit(trade_id=tid, exit_price=59400.0, exit_reason="TP",
                          pnl=100.0, exit_time="2026-06-30T05:00:00+00:00")

    out = generate_report(db, acc, CONFIG, target_date=today.isoformat())
    content = out.read_text(encoding="utf-8")

    assert "BTC-USDT-SWAP" in content
    assert "TP" in content
    assert "+100.00" in content
    assert "当日交易: 1 笔" in content


def test_report_empty_when_no_trades(tmp_path, monkeypatch):
    import scripts.daily_report as dr
    monkeypatch.setattr(dr, "ROOT", tmp_path)

    db = DB(tmp_path / "t.db")
    acc = AccountState(db, CONFIG)
    acc.set_balance(1000.0)

    out = generate_report(db, acc, CONFIG, target_date="2026-06-30")
    content = out.read_text(encoding="utf-8")
    assert "（无成交）" in content
