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


def test_insert_trade_idempotent_by_account_algo_id(tmp_path):
    """2026-07-20 ETH 双录事故防回归: 同 (account, okx_order_id) 二次 insert
    返回已存在的 id, 不建新行 —— catchup 补挂与整点 cron 竞态时 OKX 幂等键
    返回同一 algoId, db 层必须兜底。"""
    db = DB(tmp_path / "t.db")
    kw = dict(signal_date="2026-07-20T04:00Z", pair="ETH-USDT-SWAP", side="short",
              entry_price=1886.7, margin=6.7, mode="PCT",
              okx_order_id="ALGO_DUP", account="acc1")
    id1 = db.insert_trade(**kw)
    id2 = db.insert_trade(**kw)
    assert id1 == id2
    assert len(db.list_trades(limit=10, account="acc1")) == 1
    # 不同账户同 algoId 互不冲突
    id3 = db.insert_trade(**{**kw, "account": "acc2"})
    assert id3 != id1
    # okx_order_id=None 不受唯一索引约束 (挂单失败路径可多条)
    n1 = db.insert_trade(signal_date="s", pair="p", side="long", account="acc1")
    n2 = db.insert_trade(signal_date="s", pair="p", side="long", account="acc1")
    assert n1 != n2
