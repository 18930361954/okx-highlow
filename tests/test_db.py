from data.db import DB


def test_state_roundtrip(tmp_path):
    db = DB(tmp_path / "t.db")
    assert db.get_state("nope") is None
    db.set_state("x", "1")
    assert db.get_state("x") == "1"
    db.set_state("x", "42")
    assert db.get_state("x") == "42"


def test_insert_and_list_trades(tmp_path):
    db = DB(tmp_path / "t.db")
    tid = db.insert_trade(
        signal_date="2026-06-29",
        pair="BTC-USDT-SWAP",
        side="long",
        entry_price=105341.0,
        margin=7.5, mode="PCT",
    )
    assert tid > 0
    db.update_trade_exit(tid, exit_price=106605.0, exit_reason="TP",
                         pnl=4.05, exit_time="2026-06-29T12:34:00Z")
    rows = db.list_trades_by_date("2026-06-29")
    assert len(rows) == 1
    r = rows[0]
    assert r["pair"] == "BTC-USDT-SWAP"
    assert r["exit_reason"] == "TP"
    assert r["pnl"] == 4.05
