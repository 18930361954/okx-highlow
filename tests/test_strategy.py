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


def test_pair_override_float_pct():
    """pair_overrides 里的 float_pct 应覆盖默认值。"""
    cfg = {"strategy": {
        "float_pct": 0.0015,   # 默认
        "tp_pct": 0.012,
        "sl_pct": 0.005,
        "trend_filter": True,
        "pair_overrides": {
            "BTC-USDT-SWAP": {"float_pct": 0.002},
        },
    }}
    c = _mk_candles(
        opens=[100, 102, 105, 108],
        highs=[103, 106, 109, 112],
        lows=[99, 100, 103, 107],
        closes=[102, 105, 108, 110],
    )
    s = HighLowStrategy(cfg)

    # BTC 用 override 0.2%
    sig_btc = s.compute_signal("BTC-USDT-SWAP", c)
    assert sig_btc["entry_price"] == round(99 * (1 - 0.002), 6)

    # ETH 无 override，走默认 0.15%
    sig_eth = s.compute_signal("ETH-USDT-SWAP", c)
    assert sig_eth["entry_price"] == round(99 * (1 - 0.0015), 6)


def test_reentry_short_uses_intraday_high():
    """重挂：short 方向 → 用当日 K 段的 max(high) × (1 + fp)。"""
    cfg = {"strategy": {
        "float_pct": 0.0015, "tp_pct": 0.02, "sl_pct": 0.01,
        "trend_filter": True,
        "pair_overrides": {
            "ETH-USDT-SWAP": {
                "sl_pct": 0.010, "tp_pct": 0.020,
                "reentry_floats": [0.0015, 0.006],
            },
        },
    }}
    s = HighLowStrategy(cfg)
    # 假设日内 K 段：high 最高到 1650，low 最低 1580
    today_bars = _mk_candles(
        opens=[1600, 1620, 1610],
        highs=[1610, 1650, 1620],
        lows=[1595, 1600, 1580],
        closes=[1608, 1611, 1590],
    )
    sig = s.compute_reentry_signal("ETH-USDT-SWAP", "short", today_bars, attempt=2)
    assert sig is not None
    # 第 2 次 fp=0.006，high_so_far=1650 → entry = 1650×1.006
    assert sig["entry_price"] == round(1650 * 1.006, 6)
    assert sig["attempt"] == 2
    assert sig["direction"] == "short"
    # sl/tp 沿用 override：sl 1%、tp 2%
    assert sig["tp_price"] == round(sig["entry_price"] * (1 - 0.020), 6)
    assert sig["sl_price"] == round(sig["entry_price"] * (1 + 0.010), 6)


def test_reentry_disabled_without_config():
    """没配 reentry_floats 的 pair 返回 None。"""
    cfg = {"strategy": {
        "float_pct": 0.0015, "tp_pct": 0.012, "sl_pct": 0.005,
        "trend_filter": True,
        "pair_overrides": {"BTC-USDT-SWAP": {"sl_pct": 0.005, "tp_pct": 0.012}},
    }}
    s = HighLowStrategy(cfg)
    today_bars = _mk_candles([100],[102],[99],[101])
    assert s.compute_reentry_signal("BTC-USDT-SWAP", "long", today_bars, attempt=2) is None


def test_reentry_attempt_out_of_range():
    cfg = {"strategy": {
        "float_pct": 0.0015, "tp_pct": 0.02, "sl_pct": 0.01,
        "trend_filter": True,
        "pair_overrides": {
            "ETH-USDT-SWAP": {"sl_pct": 0.010, "tp_pct": 0.020,
                              "reentry_floats": [0.0015, 0.006]},
        },
    }}
    s = HighLowStrategy(cfg)
    today_bars = _mk_candles([100],[102],[99],[101])
    # 只配了 2 次，attempt=3 应返回 None
    assert s.compute_reentry_signal("ETH-USDT-SWAP", "long", today_bars, attempt=3) is None


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
