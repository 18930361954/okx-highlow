from strategy.high_low import HighLowStrategy


CONFIG = {"strategy": {
    "float_pct": 0.0015,
    "tp_pct": 0.012,
    "sl_pct": 0.005,
    "trend_filter": True,
}}


def _mk_candles(opens, highs, lows, closes):
    out = []
    for i, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes)):
        out.append({"ts": 1700000000000 + i * 3600_000,
                    "open": o, "high": h, "low": l, "close": c})
    return out


def test_bullish_day_emits_long_at_low_minus_float():
    # day_open=100, day_close=110, low=99, high=112 → 阳线 → 挂多
    c = _mk_candles(
        opens=[100, 102, 105, 108],
        highs=[103, 106, 109, 112],
        lows=[99, 100, 103, 107],
        closes=[102, 105, 108, 110],
    )
    s = HighLowStrategy(CONFIG)
    sig = s.compute_signal("BTC-USDT-SWAP", c)
    assert sig is not None
    assert sig["direction"] == "long"
    assert abs(sig["entry_price"] - 99 * (1 - 0.0015)) < 1e-4
    assert sig["tp_price"] > sig["entry_price"]
    assert sig["sl_price"] < sig["entry_price"]


def test_bearish_day_emits_short_at_high_plus_float():
    # 阴线 close < open
    c = _mk_candles(
        opens=[110, 108, 105, 102],
        highs=[112, 110, 107, 104],
        lows=[107, 104, 101, 98],
        closes=[108, 105, 102, 100],
    )
    s = HighLowStrategy(CONFIG)
    sig = s.compute_signal("BTC-USDT-SWAP", c)
    assert sig is not None
    assert sig["direction"] == "short"
    assert abs(sig["entry_price"] - 112 * (1 + 0.0015)) < 1e-3
    assert sig["tp_price"] < sig["entry_price"]
    assert sig["sl_price"] > sig["entry_price"]


def test_flat_day_returns_none():
    # close == open
    c = _mk_candles(opens=[100, 101, 99], highs=[102, 102, 100],
                    lows=[99, 99, 98], closes=[101, 99, 100])
    s = HighLowStrategy(CONFIG)
    sig = s.compute_signal("BTC-USDT-SWAP", c)
    assert sig is None


def test_insufficient_data_returns_none():
    s = HighLowStrategy(CONFIG)
    assert s.compute_signal("BTC-USDT-SWAP", []) is None
    assert s.compute_signal("BTC-USDT-SWAP", [_mk_candles([1],[2],[0],[1])[0]]) is None


def test_okx_list_format_accepted():
    # OKX 原生返回是 list[list[str]]，按时间倒序
    raw = [
        ["1700010800000", "108", "112", "107", "110", "0", "0", "0", "1"],
        ["1700007200000", "105", "109", "103", "108", "0", "0", "0", "1"],
        ["1700003600000", "102", "106", "100", "105", "0", "0", "0", "1"],
        ["1700000000000", "100", "103", "99", "102", "0", "0", "0", "1"],
    ]
    s = HighLowStrategy(CONFIG)
    sig = s.compute_signal("BTC-USDT-SWAP", raw)
    assert sig is not None
    # 升序后 first.open=100 last.close=110 → 阳
    assert sig["direction"] == "long"
    assert sig["day_low"] == 99
