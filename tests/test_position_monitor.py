"""position_monitor 纯工具函数测试, 不启渲染线程。"""
from execution.position_monitor import (
    _compute_lifetime_stats,
    _exit_reason_zh,
    _pending_tp_sl,
)


def _mk(pnl, reason="TP", exit_time="2026-07-11T00:00:00+00:00"):
    return {"pnl": pnl, "exit_reason": reason, "exit_time": exit_time}


def test_lifetime_excludes_orphan_and_cancelled():
    """ORPHAN / CANCELLED / 空 exit_reason 不算真实成交, 不计入胜率分母。"""
    trades = [
        _mk(10, "TP"),
        _mk(-5, "SL"),
        _mk(0, "ORPHAN"),        # 排除
        _mk(0, "CANCELLED"),     # 排除
        _mk(3, "EXIT"),          # 也算真实成交
        {"pnl": None, "exit_reason": None},  # 未闭合, 排除
    ]
    s = _compute_lifetime_stats(trades)
    assert s["total"] == 3
    assert s["wins"] == 2
    assert s["losses"] == 1
    assert s["net_pnl"] == 8
    assert s["win_rate"] == 2 / 3 * 100


def test_lifetime_zero_trades_all_zero():
    s = _compute_lifetime_stats([])
    assert s["total"] == 0
    assert s["win_rate"] == 0.0
    assert s["profit_factor"] == 0.0
    assert s["max_dd_pct"] == 0.0


def test_lifetime_profit_factor():
    """盈亏比 = 总盈 / 总亏|绝对值|。"""
    trades = [_mk(20), _mk(10), _mk(-6, "SL"), _mk(-4, "SL")]
    s = _compute_lifetime_stats(trades)
    assert s["sum_win"] == 30
    assert s["sum_loss_abs"] == 10
    assert s["profit_factor"] == 3.0
    assert s["avg_win"] == 15
    assert s["avg_loss"] == 5


def test_lifetime_profit_factor_no_loss_returns_inf():
    """全胜 → 盈亏比 = 无穷。"""
    s = _compute_lifetime_stats([_mk(10), _mk(5)])
    assert s["profit_factor"] == float("inf")


def test_lifetime_max_drawdown():
    """按 exit_time 升序算 equity curve, 峰后回撤取最大。
    序列: +30 → +10 (dd 20) → +25 → -15 (dd 40, 从峰 25 起)
    """
    trades = [
        _mk(30, exit_time="2026-01-01T00:00:00+00:00"),
        _mk(-20, "SL", "2026-01-02T00:00:00+00:00"),  # cum=10, peak=30, dd=20
        _mk(15, exit_time="2026-01-03T00:00:00+00:00"),  # cum=25, peak=30 (还没超), dd=5
        _mk(-40, "SL", "2026-01-04T00:00:00+00:00"),  # cum=-15, peak=30, dd=45 (最大)
    ]
    # 用 100 的当前余额, 初始估算 100 - (-15) = 115, peak_equity 时 = 115 + 30 = 145
    s = _compute_lifetime_stats(trades, current_balance=100.0)
    # max_dd_abs = 45, peak_equity ≈ 145 → dd% ≈ 31%
    assert 30.0 < s["max_dd_pct"] < 32.0


def test_exit_reason_zh():
    assert _exit_reason_zh("TP") == "止盈"
    assert _exit_reason_zh("SL") == "止损"
    assert _exit_reason_zh("EXIT") == "平仓"
    assert _exit_reason_zh("ORPHAN") == "过期"
    assert _exit_reason_zh("CANCELLED") == "撤单"
    assert _exit_reason_zh("") == ""
    assert _exit_reason_zh("UNKNOWN_STATE") == "UNKNOWN_STATE"


def test_pending_tp_sl_reads_attach():
    """trigger 单的 TP/SL 在 attachAlgoOrds[0] 里, 顶层是空串。"""
    o = {
        "tpTriggerPx": "", "slTriggerPx": "",
        "attachAlgoOrds": [{"tpTriggerPx": "64371.6", "slTriggerPx": "66055.4"}],
    }
    assert _pending_tp_sl(o) == ("64371.6", "66055.4")


def test_pending_tp_sl_falls_back_to_top_level():
    """attachAlgoOrds 缺失 → 兜底顶层 (兼容非 trigger 类算法单)。"""
    o = {"tpTriggerPx": "100", "slTriggerPx": "90"}
    assert _pending_tp_sl(o) == ("100", "90")


def test_pending_tp_sl_all_empty_returns_dash():
    assert _pending_tp_sl({}) == ("-", "-")
