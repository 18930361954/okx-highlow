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


# ---------------- leg_group (fade OCO, 2026-07 新增) ----------------

def test_leg_group_migration_idempotent(tmp_path):
    """leg_group 列迁移幂等: 二次 init 不炸, 列存在。"""
    import sqlite3
    p = tmp_path / "lg.db"
    DB(p)
    DB(p)  # 二次 init
    con = sqlite3.connect(str(p))
    cols = {r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()}
    con.close()
    assert "leg_group" in cols


def test_get_open_sibling_leg(tmp_path):
    db = DB(tmp_path / "lg2.db")
    lg = "fSOL20260728T06"
    tid_l = db.insert_trade(signal_date="2026-07-28T06:00Z", pair="SOL-USDT-SWAP",
                            side="long", entry_price=74.0, okx_order_id="AL",
                            account="acc", leg_group=lg)
    tid_s = db.insert_trade(signal_date="2026-07-28T06:00Z", pair="SOL-USDT-SWAP",
                            side="short", entry_price=77.0, okx_order_id="AS",
                            account="acc", leg_group=lg)
    # 互为 sibling
    assert db.get_open_sibling_leg("acc", lg, tid_l)["id"] == tid_s
    assert db.get_open_sibling_leg("acc", lg, tid_s)["id"] == tid_l
    # 账户隔离
    assert db.get_open_sibling_leg("other", lg, tid_l) is None
    # 空 leg_group → None
    assert db.get_open_sibling_leg("acc", "", tid_l) is None


def test_get_open_sibling_leg_excludes_closed(tmp_path):
    """已平/已撤的腿不再是 open sibling。"""
    db = DB(tmp_path / "lg3.db")
    lg = "fETH20260728"
    tid_l = db.insert_trade(signal_date="2026-07-28", pair="ETH-USDT-SWAP",
                            side="long", entry_price=1900.0, okx_order_id="EL",
                            account="acc", leg_group=lg)
    tid_s = db.insert_trade(signal_date="2026-07-28", pair="ETH-USDT-SWAP",
                            side="short", entry_price=2000.0, okx_order_id="ES",
                            account="acc", leg_group=lg)
    db.update_trade_exit(trade_id=tid_s, exit_price=0, exit_reason="CANCELLED",
                          pnl=0, exit_time="2026-07-28T07:00:00+00:00")
    assert db.get_open_sibling_leg("acc", lg, tid_l) is None


def test_insert_trade_leg_group_default_none(tmp_path):
    """不传 leg_group 默认 NULL —— 单向策略不受影响。"""
    db = DB(tmp_path / "lg4.db")
    db.insert_trade(signal_date="2026-07-28", pair="BTC-USDT-SWAP",
                    side="long", okx_order_id="B1", account="acc")
    t = db.list_trades(limit=1, account="acc")[0]
    assert t["leg_group"] is None


# ---------------- signal_bar (混周期行级周期, 2026-07-28 新增) ----------------

def test_signal_bar_migration_idempotent(tmp_path):
    """signal_bar 列迁移幂等: 二次 init 不炸, 列存在。"""
    import sqlite3
    p = tmp_path / "sb.db"
    DB(p)
    DB(p)
    con = sqlite3.connect(str(p))
    cols = {r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()}
    con.close()
    assert "signal_bar" in cols


def test_insert_trade_signal_bar_roundtrip(tmp_path):
    """signal_bar 写入后可读回; 不传默认 NULL(旧数据/旧调用兼容)。"""
    db = DB(tmp_path / "sb2.db")
    db.insert_trade(signal_date="2026-07-28T06:00Z", pair="SOL-USDT-SWAP",
                    side="long", okx_order_id="S1", account="acc", signal_bar="6H")
    db.insert_trade(signal_date="2026-07-28", pair="BTC-USDT-SWAP",
                    side="long", okx_order_id="B1", account="acc")
    rows = {r["pair"]: r for r in db.list_trades(limit=10, account="acc")}
    assert rows["SOL-USDT-SWAP"]["signal_bar"] == "6H"
    assert rows["BTC-USDT-SWAP"]["signal_bar"] is None
