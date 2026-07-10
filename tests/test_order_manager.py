from unittest.mock import MagicMock

from data.db import DB
from execution.order_manager import OrderManager, SLIP_PCT


def _mk_signal():
    return {
        "pair": "BTC-USDT-SWAP",
        "direction": "long",
        "entry_price": 105341.0,
        "tp_price": 106605.0,
        "sl_price": 104814.0,
        "signal_date": "2026-06-29",
        "reason": "test",
    }


def test_place_uses_deterministic_algo_cl_ord_id(tmp_path):
    """clOrdId 是防重的关键：同 pair+signal_date+direction 必须产出同一个值。"""
    db = DB(tmp_path / "t.db")
    okx = MagicMock()
    okx.set_leverage.return_value = {"code": "0"}
    okx.place_algo_order.return_value = {"code": "0", "data": [{"algoId": "A"}]}

    om = OrderManager(okx, db)
    om.place_algo_orders(_mk_signal(), margin=7.5, leverage=100)
    _, kw1 = okx.place_algo_order.call_args
    assert "algoClOrdId" in kw1
    assert kw1["algoClOrdId"] == "hlBTC20260629l1"  # 末尾是 attempt 编号
    assert len(kw1["algoClOrdId"]) <= 32

    # 同参数第二次调用 → 同 clOrdId（OKX 服务端会拒第二次）
    om.place_algo_orders(_mk_signal(), margin=7.5, leverage=100)
    _, kw2 = okx.place_algo_order.call_args
    assert kw2["algoClOrdId"] == kw1["algoClOrdId"]


def test_place_algo_orders_uses_limit_not_market(tmp_path):
    """orderPx/tpOrdPx/slOrdPx 必须是具体价格，不能是 '-1'（市价）。"""
    db = DB(tmp_path / "t.db")
    okx = MagicMock()
    okx.set_leverage.return_value = {"code": "0"}
    okx.place_algo_order.return_value = {"code": "0", "data": [{"algoId": "ALGO123"}]}

    om = OrderManager(okx, db)
    algo_id = om.place_algo_orders(_mk_signal(), margin=7.5, leverage=100)

    assert algo_id == "ALGO123"
    args, kwargs = okx.place_algo_order.call_args

    # 触发限价：orderPx 必须是数字字符串，不能是 -1
    assert kwargs["orderPx"] != "-1"
    assert float(kwargs["orderPx"]) > 0
    # long 方向：orderPx 应 ≈ entry × (1 + SLIP_PCT)
    expected = round(105341.0 * (1 + SLIP_PCT), 6)
    assert abs(float(kwargs["orderPx"]) - expected) < 1e-3

    # TP/SL 都是限价，价格 = trigger 价
    assert kwargs["tpOrdPx"] != "-1"
    assert kwargs["slOrdPx"] != "-1"
    assert kwargs["tpOrdPx"] == "106605.0"
    assert kwargs["tpTriggerPx"] == "106605.0"
    assert kwargs["slOrdPx"] == "104814.0"
    assert kwargs["slTriggerPx"] == "104814.0"


def test_short_orderPx_slips_below_entry(tmp_path):
    """空头的限价价格应低于触发价（向下放宽更易成交）。"""
    db = DB(tmp_path / "t.db")
    okx = MagicMock()
    okx.set_leverage.return_value = {"code": "0"}
    okx.place_algo_order.return_value = {"code": "0", "data": [{"algoId": "X"}]}

    sig = _mk_signal()
    sig.update({"direction": "short", "entry_price": 3500.0,
                "tp_price": 3458.0, "sl_price": 3517.5})
    om = OrderManager(okx, db)
    om.place_algo_orders(sig, margin=10, leverage=100)
    _, kwargs = okx.place_algo_order.call_args
    assert kwargs["side"] == "sell"
    assert kwargs["posSide"] == "short"
    assert float(kwargs["orderPx"]) < 3500.0  # short 是下方限价
    expected = round(3500.0 * (1 - SLIP_PCT), 6)
    assert abs(float(kwargs["orderPx"]) - expected) < 1e-3


def test_set_leverage_called_with_cross_mode_no_posSide(tmp_path):
    """cross 模式 set_leverage 不传 posSide，避免被 OKX 默认 3x 覆盖。"""
    db = DB(tmp_path / "t.db")
    okx = MagicMock()
    okx.set_leverage.return_value = {"code": "0"}
    okx.place_algo_order.return_value = {"code": "0", "data": [{"algoId": "X"}]}

    om = OrderManager(okx, db, td_mode="cross")
    om.place_algo_orders(_mk_signal(), margin=7.5, leverage=100)

    okx.set_leverage.assert_called_once_with("BTC-USDT-SWAP", 100, mgnMode="cross")


def test_eth_leverage_100_passes_through(tmp_path):
    """ETH 100x 必须真的传 100，不能默默被截。"""
    db = DB(tmp_path / "t.db")
    okx = MagicMock()
    okx.set_leverage.return_value = {"code": "0"}
    okx.place_algo_order.return_value = {"code": "0", "data": [{"algoId": "X"}]}

    sig = _mk_signal()
    sig["pair"] = "ETH-USDT-SWAP"
    sig["entry_price"] = 3500.0
    sig["tp_price"] = 3542.0
    sig["sl_price"] = 3482.5
    om = OrderManager(okx, db, td_mode="cross")
    om.place_algo_orders(sig, margin=7.5, leverage=100)

    okx.set_leverage.assert_called_once_with("ETH-USDT-SWAP", 100, mgnMode="cross")
    _, kwargs = okx.place_algo_order.call_args
    assert kwargs["tdMode"] == "cross"


def test_cancel_all_pending(tmp_path):
    db = DB(tmp_path / "t.db")
    okx = MagicMock()
    okx.list_pending_algos.return_value = [
        {"algoId": "A1", "instId": "BTC-USDT-SWAP"},
        {"algoId": "A2", "instId": "ETH-USDT-SWAP"},
    ]
    om = OrderManager(okx, db)
    n = om.cancel_all_pending()
    assert n == 2
    assert okx.cancel_algo_order.call_count == 2


def test_place_algo_handles_failure(tmp_path, monkeypatch):
    """挂单本身抛出且回查也 miss → 返回 None,且不写 db(避免脏数据)。"""
    import execution.order_manager as _om
    monkeypatch.setattr(_om, "_POLL_BACKOFFS", (0.0, 0.0))  # 加速测试
    db = DB(tmp_path / "t.db")
    okx = MagicMock()
    okx.set_leverage.return_value = {"code": "0"}
    okx.place_algo_order.side_effect = RuntimeError("api down")
    okx.get_algo_order.return_value = None
    okx.list_pending_algos.return_value = []
    om = OrderManager(okx, db)
    algo_id = om.place_algo_orders(_mk_signal(), margin=7.5, leverage=100)
    assert algo_id is None
    # 关键回归:不再插脏 trade 到 db
    assert db.list_trades_by_date("2026-06-29") == []


def test_place_recovers_via_cl_ord_id_poll_after_51149(tmp_path, monkeypatch):
    """51149 timeout → get_algo_order 第 2 轮拿到 algoId → 正常 insert_trade。"""
    import execution.order_manager as _om
    from core.okx_client import OKXError
    monkeypatch.setattr(_om, "_POLL_BACKOFFS", (0.0, 0.0, 0.0))
    db = DB(tmp_path / "t.db")
    okx = MagicMock()
    okx.set_leverage.return_value = {"code": "0"}
    okx.place_algo_order.side_effect = OKXError("Order timed out", code="51149")
    # 第 1 轮返回 None,第 2 轮返回单据
    okx.get_algo_order.side_effect = [
        None,
        {"algoId": "RECOVERED_A", "algoClOrdId": "hlBTC20260629l1", "state": "live"},
    ]
    okx.list_pending_algos.return_value = []

    om = OrderManager(okx, db)
    aid = om.place_algo_orders(_mk_signal(), margin=7.5, leverage=100)
    assert aid == "RECOVERED_A"
    rows = db.list_trades_by_date("2026-06-29")
    assert len(rows) == 1
    assert rows[0]["okx_order_id"] == "RECOVERED_A"


def test_place_falls_back_to_pending_scan_when_get_algo_fails(tmp_path, monkeypatch):
    """get_algo_order 抛异常时,同轮 list_pending_algos 兜底能拿到 algoId。"""
    import execution.order_manager as _om
    from core.okx_client import OKXError
    monkeypatch.setattr(_om, "_POLL_BACKOFFS", (0.0,))
    db = DB(tmp_path / "t.db")
    okx = MagicMock()
    okx.set_leverage.return_value = {"code": "0"}
    okx.place_algo_order.side_effect = OKXError("timeout", code="51149")
    okx.get_algo_order.side_effect = RuntimeError("network")
    okx.list_pending_algos.return_value = [
        {"algoId": "PB1", "algoClOrdId": "hlBTC20260629l1", "instId": "BTC-USDT-SWAP"},
    ]
    om = OrderManager(okx, db)
    aid = om.place_algo_orders(_mk_signal(), margin=7.5, leverage=100)
    assert aid == "PB1"


def test_place_all_poll_rounds_miss_returns_none_no_db(tmp_path, monkeypatch):
    """全轮 miss:返回 None + 不 insert_trade,让下轮 scheduler/catchup 用同 clOrdId 重试。"""
    import execution.order_manager as _om
    from core.okx_client import OKXError
    monkeypatch.setattr(_om, "_POLL_BACKOFFS", (0.0, 0.0, 0.0, 0.0))
    db = DB(tmp_path / "t.db")
    okx = MagicMock()
    okx.set_leverage.return_value = {"code": "0"}
    okx.place_algo_order.side_effect = OKXError("timeout", code="51149")
    okx.get_algo_order.return_value = None
    okx.list_pending_algos.return_value = []
    om = OrderManager(okx, db)
    aid = om.place_algo_orders(_mk_signal(), margin=7.5, leverage=100)
    assert aid is None
    assert db.list_trades_by_date("2026-06-29") == []


def test_okx_client_builds_attach_algo_ords(tmp_path):
    """直接验证 OKXClient.place_algo_order 把 TP/SL 装到 attachAlgoOrds，
    而不是顶层 tpTriggerPx（顶层在 trigger 单上会被服务端丢弃）。"""
    from core.okx_client import OKXClient
    cli = OKXClient("k", "s", "p", env="demo")
    captured: dict = {}

    def fake_request(method, endpoint, params=None, body=None, **kw):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["body"] = body
        return {"code": "0", "data": [{"algoId": "X"}]}

    cli._request = fake_request
    cli.place_algo_order(
        instId="BTC-USDT-SWAP", tdMode="cross", side="buy", ordType="trigger",
        sz="1", triggerPx="54000", orderPx="54027", posSide="long",
        tpTriggerPx="54648", tpOrdPx="54648",
        slTriggerPx="53730", slOrdPx="53730",
    )
    body = captured["body"]
    assert "tpTriggerPx" not in body  # 顶层没有
    assert "slTriggerPx" not in body
    assert "attachAlgoOrds" in body
    assert len(body["attachAlgoOrds"]) == 1
    a = body["attachAlgoOrds"][0]
    assert a["tpTriggerPx"] == "54648"
    assert a["tpOrdPx"] == "54648"
    assert a["slTriggerPx"] == "53730"
    assert a["slOrdPx"] == "53730"
    assert a["tpTriggerPxType"] == "last"
    assert a["slTriggerPxType"] == "last"


def test_get_algo_order_returns_row_when_found():
    """OKXClient.get_algo_order 命中时返回单条 dict。"""
    from core.okx_client import OKXClient
    cli = OKXClient("k", "s", "p", env="demo")

    def fake_request(method, endpoint, params=None, body=None, **kw):
        assert method == "GET"
        assert endpoint == "/api/v5/trade/order-algo"
        assert params == {"algoClOrdId": "hlBTC20260629l1"}
        return {"code": "0", "data": [{"algoId": "A9", "algoClOrdId": "hlBTC20260629l1",
                                        "state": "live"}]}

    cli._request = fake_request
    row = cli.get_algo_order(algoClOrdId="hlBTC20260629l1")
    assert row is not None
    assert row["algoId"] == "A9"


def test_get_algo_order_returns_none_when_not_found():
    """空 data → None,不抛异常。"""
    from core.okx_client import OKXClient
    cli = OKXClient("k", "s", "p", env="demo")
    cli._request = lambda *a, **kw: {"code": "0", "data": []}
    assert cli.get_algo_order(algoClOrdId="does-not-exist") is None


def test_get_algo_order_swallows_not_found_codes():
    """OKX 侧 51000/51603 表示未找到,应返回 None 而不是抛。"""
    from core.okx_client import OKXClient, OKXError
    cli = OKXClient("k", "s", "p", env="demo")

    def fake_request(*a, **kw):
        raise OKXError("not exist", code="51603")

    cli._request = fake_request
    assert cli.get_algo_order(algoClOrdId="x") is None


def test_get_algo_order_reraises_other_okx_errors():
    """其它 OKX 错误码应向上抛,不能吞。"""
    from core.okx_client import OKXClient, OKXError
    import pytest as _pytest
    cli = OKXClient("k", "s", "p", env="demo")
    cli._request = lambda *a, **kw: (_ for _ in ()).throw(OKXError("boom", code="50011"))
    with _pytest.raises(OKXError):
        cli.get_algo_order(algoClOrdId="x")


def test_get_algo_order_requires_key():
    """两个 key 都不传应抛 ValueError。"""
    from core.okx_client import OKXClient
    import pytest as _pytest
    cli = OKXClient("k", "s", "p", env="demo")
    with _pytest.raises(ValueError):
        cli.get_algo_order()


def test_persisted_trade_records_algoId_and_margin(tmp_path):
    db = DB(tmp_path / "t.db")
    okx = MagicMock()
    okx.set_leverage.return_value = {"code": "0"}
    okx.place_algo_order.return_value = {"code": "0", "data": [{"algoId": "ALGO_ID_PERS"}]}
    om = OrderManager(okx, db)
    om.place_algo_orders(_mk_signal(), margin=7.5, leverage=100)
    rows = db.list_trades_by_date("2026-06-29")
    assert len(rows) == 1
    assert rows[0]["okx_order_id"] == "ALGO_ID_PERS"
    assert rows[0]["pair"] == "BTC-USDT-SWAP"
    assert rows[0]["side"] == "long"
