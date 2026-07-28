"""回测 parity: live HighLowStrategy 的入场/TP/SL 公式必须与回测引擎
scripts/strategy_lab.py simulate_mode 的公式逐位一致 (≤1e-6, live 有 round(...,6))。

回测公式 (strategy_lab.py:123-148):
  trend:    d=d_prev;  entry = sig_l*(1-f) if d==1 else sig_h*(1+f)
  reversal: d=-d_prev; entry = sig_h*(1+f) if d==-1 else sig_l*(1-f)
  fade:     e_long = sig_l*(1-f); e_short = sig_h*(1+f)  (双腿)
  TP/SL (strategy_lab.py:162-171):
    d==1: tp=entry*(1+tp_pct) sl=entry*(1-sl_pct)
    d==-1: tp=entry*(1-tp_pct) sl=entry*(1+sl_pct)
"""
import itertools

from strategy.high_low import HighLowStrategy


TOL = 1e-6

# 多组真实量级的 OHLC (BTC/ETH/SOL 价位) × 阳/阴
CASES = [
    # (open, high, low, close)
    (64000.0, 66384.775, 63588.096, 65950.5),      # BTC 阳
    (66384.775, 66500.0, 63500.25, 64000.0),       # BTC 阴
    (1928.45722, 1992.59992, 1901.68777, 1980.0),  # ETH 阳
    (1992.59992, 2001.3, 1854.0819, 1900.0),       # ETH 阴
    (74.42112, 76.5, 71.30001, 75.9),              # SOL 阳
    (76.5, 77.123456, 70.000001, 71.5),            # SOL 阴
]
PARAMS = [
    # (float_pct, tp_pct, sl_pct) — 取自 34 幸存策略的真实参数族
    (0.007, 0.008, 0.030),
    (0.010, 0.008, 0.025),
    (0.010, 0.006, 0.030),
    (0.005, 0.008, 0.030),
    (0.003, 0.008, 0.030),
]


def _lab_trend(o, h, l, c, f):
    """strategy_lab.py:123-125 的 trend 公式。d_prev: 阳=1 阴=-1。"""
    d = 1 if c > o else -1
    entry = l * (1 - f) if d == 1 else h * (1 + f)
    return d, entry


def _lab_reversal(o, h, l, c, f):
    """strategy_lab.py:127-129: d=-d_prev; entry = h*(1+f) if d==-1 else l*(1-f)。"""
    d = -(1 if c > o else -1)
    entry = h * (1 + f) if d == -1 else l * (1 - f)
    return d, entry


def _lab_fade(o, h, l, c, f):
    """strategy_lab.py:136-138: 双腿。"""
    return l * (1 - f), h * (1 + f)


def _lab_tp_sl(d, entry, tp_pct, sl_pct):
    """strategy_lab.py:162-171。"""
    if d == 1:
        return entry * (1 + tp_pct), entry * (1 - sl_pct)
    return entry * (1 - tp_pct), entry * (1 + sl_pct)


def _mk_strategy(mode, f, tp, sl):
    return HighLowStrategy({"strategy": {
        "float_pct": f, "tp_pct": tp, "sl_pct": sl,
        "trend_filter": True, "mode": mode,
    }})


def _candle(o, h, l, c):
    return [{"ts": 1700000000000, "open": o, "high": h, "low": l, "close": c}]


def test_trend_parity():
    for (o, h, l, c), (f, tp, sl) in itertools.product(CASES, PARAMS):
        d, lab_entry = _lab_trend(o, h, l, c, f)
        lab_tp, lab_sl = _lab_tp_sl(d, lab_entry, tp, sl)
        sig = _mk_strategy("trend", f, tp, sl).compute_signal("BTC-USDT-SWAP", _candle(o, h, l, c))
        assert sig["direction"] == ("long" if d == 1 else "short")
        assert abs(sig["entry_price"] - lab_entry) <= TOL, (o, h, l, c, f)
        # live tp/sl 基于 round 后的 entry, 容差放大到 round 误差传播上界 (1e-6 × (1+pct))
        assert abs(sig["tp_price"] - lab_tp) <= 1e-5
        assert abs(sig["sl_price"] - lab_sl) <= 1e-5


def test_reversal_parity():
    for (o, h, l, c), (f, tp, sl) in itertools.product(CASES, PARAMS):
        d, lab_entry = _lab_reversal(o, h, l, c, f)
        lab_tp, lab_sl = _lab_tp_sl(d, lab_entry, tp, sl)
        sig = _mk_strategy("reversal", f, tp, sl).compute_signal("ETH-USDT-SWAP", _candle(o, h, l, c))
        assert sig["direction"] == ("long" if d == 1 else "short")
        assert abs(sig["entry_price"] - lab_entry) <= TOL, (o, h, l, c, f)
        assert abs(sig["tp_price"] - lab_tp) <= 1e-5
        assert abs(sig["sl_price"] - lab_sl) <= 1e-5


def test_fade_parity():
    for (o, h, l, c), (f, tp, sl) in itertools.product(CASES, PARAMS):
        lab_long, lab_short = _lab_fade(o, h, l, c, f)
        sig = _mk_strategy("fade", f, tp, sl).compute_signal("SOL-USDT-SWAP", _candle(o, h, l, c))
        by_dir = {leg["direction"]: leg for leg in sig["legs"]}
        assert abs(by_dir["long"]["entry_price"] - lab_long) <= TOL
        assert abs(by_dir["short"]["entry_price"] - lab_short) <= TOL
        # 每腿 TP/SL 按各自方向的公式
        tp_l, sl_l = _lab_tp_sl(1, lab_long, tp, sl)
        tp_s, sl_s = _lab_tp_sl(-1, lab_short, tp, sl)
        assert abs(by_dir["long"]["tp_price"] - tp_l) <= 1e-5
        assert abs(by_dir["long"]["sl_price"] - sl_l) <= 1e-5
        assert abs(by_dir["short"]["tp_price"] - tp_s) <= 1e-5
        assert abs(by_dir["short"]["sl_price"] - sl_s) <= 1e-5


def test_trend_reversal_are_mirrors():
    """同一根 K, reversal 方向与 trend 相反 —— 结构不因参数漂移。"""
    for (o, h, l, c) in CASES:
        t = _mk_strategy("trend", 0.005, 0.008, 0.03).compute_signal("X-USDT-SWAP", _candle(o, h, l, c))
        r = _mk_strategy("reversal", 0.005, 0.008, 0.03).compute_signal("X-USDT-SWAP", _candle(o, h, l, c))
        assert {t["direction"], r["direction"]} == {"long", "short"}


def test_fade_legs_match_single_mode_entries():
    """fade 的多腿 == trend 阳天多腿公式; 空腿 == reversal 阳天空腿公式。
    (fade = trend 腿 + reversal 腿的合体, strategy_lab.py:136 注释)"""
    o, h, l, c = 64000.0, 66384.775, 63588.096, 65950.5  # 阳
    f, tp, sl = 0.005, 0.008, 0.03
    fade_sig = _mk_strategy("fade", f, tp, sl).compute_signal("X-USDT-SWAP", _candle(o, h, l, c))
    trend_sig = _mk_strategy("trend", f, tp, sl).compute_signal("X-USDT-SWAP", _candle(o, h, l, c))
    rev_sig = _mk_strategy("reversal", f, tp, sl).compute_signal("X-USDT-SWAP", _candle(o, h, l, c))
    by_dir = {leg["direction"]: leg for leg in fade_sig["legs"]}
    assert by_dir["long"]["entry_price"] == trend_sig["entry_price"]    # 阳天 trend=多
    assert by_dir["short"]["entry_price"] == rev_sig["entry_price"]     # 阳天 reversal=空
