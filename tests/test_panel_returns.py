"""面板收益率与回撤计算。

重点覆盖修复前的三个 bug:
  1. 持仓浮亏不计入回撤(风险盲区)
  2. 余额传 0 时分母退化成利润本身, 回撤被夸大
  3. 中途充值让起始本金估算失真, 回撤被低估
以及 TIME(超时强平) 必须计入统计。
"""
import pytest

from core.account_state import AccountState
from data.db import DB
from execution.position_monitor import (
    _compute_lifetime_stats, _pos_pct_cells, _trade_pct_cells,
)


def t(pnl, ts, reason="TP", margin=None):
    d = {"pnl": pnl, "exit_reason": reason, "exit_time": ts,
         "fee": 0.0, "funding": 0.0}
    if margin is not None:
        d["margin"] = margin
    return d


# ---------------- 收益率 ----------------

def test_return_pct_uses_baseline_not_current_balance():
    """收益率分母必须是起始本金 —— 用当前余额会让同样盈亏显示不同收益率。"""
    r = _compute_lifetime_stats([t(30, "2026-08-22T01:00")],
                                current_balance=180, baseline=150)
    assert r["return_pct"] == pytest.approx(20.0)   # 30/150
    assert r["baseline"] == pytest.approx(150)


def test_return_pct_falls_back_when_no_baseline():
    """没有起始本金时退化为「当前余额 - 累计盈亏」倒推。"""
    r = _compute_lifetime_stats([t(30, "2026-08-22T01:00")],
                                current_balance=180, baseline=0)
    assert r["baseline"] == pytest.approx(150)
    assert r["return_pct"] == pytest.approx(20.0)


def test_avg_trade_pct():
    rows = [t(10, "2026-08-22T01:00"), t(20, "2026-08-22T02:00")]
    r = _compute_lifetime_stats(rows, current_balance=180, baseline=150)
    assert r["return_pct"] == pytest.approx(20.0)
    assert r["avg_trade_pct"] == pytest.approx(10.0)   # 20% / 2 笔


def test_negative_return():
    r = _compute_lifetime_stats([t(-30, "2026-08-22T01:00", "SL")],
                                current_balance=120, baseline=150)
    assert r["return_pct"] == pytest.approx(-20.0)


# ---------------- 回撤 ----------------

def test_drawdown_simple_loss():
    """150 → 120，回撤 20%。"""
    r = _compute_lifetime_stats([t(-30, "2026-08-22T01:00", "SL")],
                                current_balance=120, baseline=150)
    assert r["max_dd_pct"] == pytest.approx(20.0)
    assert r["cur_dd_pct"] == pytest.approx(20.0)


def test_drawdown_from_peak():
    """150 →赚到 200(峰值)→ 跌回 170，回撤按峰值算 = 15%。"""
    rows = [t(50, "2026-08-22T01:00"), t(-30, "2026-08-22T02:00", "SL")]
    r = _compute_lifetime_stats(rows, current_balance=170, baseline=150)
    assert r["max_dd_pct"] == pytest.approx(15.0)


def test_drawdown_includes_unrealized_loss():
    """BUG 1 修复: 持仓浮亏必须计入回撤，否则浮亏时显示 0% 是风险盲区。"""
    # 已平仓赚 10 (权益 160)，但持仓浮亏 -40 → 真实权益 120
    r = _compute_lifetime_stats([t(10, "2026-08-22T01:00")],
                                current_balance=160, baseline=150,
                                equity=120)
    assert r["cur_dd_pct"] == pytest.approx(25.0)   # 160 → 120
    assert r["max_dd_pct"] == pytest.approx(25.0)


def test_drawdown_ignores_unrealized_profit_for_peak_only():
    """浮盈会抬高峰值但不产生回撤。"""
    r = _compute_lifetime_stats([t(10, "2026-08-22T01:00")],
                                current_balance=160, baseline=150,
                                equity=200)
    assert r["cur_dd_pct"] == pytest.approx(0.0)


def test_drawdown_not_inflated_when_balance_zero():
    """BUG 2 修复: 余额/本金都取不到时不能拿利润当分母把回撤放大 4 倍。"""
    rows = [t(50, "2026-08-22T01:00"), t(-30, "2026-08-22T02:00", "SL")]
    r = _compute_lifetime_stats(rows, current_balance=0, baseline=0)
    # 旧实现这里会算出 60%; 新实现分母为 0 → 不产生虚假回撤
    assert r["max_dd_pct"] == pytest.approx(0.0)


def test_drawdown_unaffected_by_deposit():
    """BUG 3 修复: 中途充值后, 传入正确 baseline 就不会稀释回撤。"""
    rows = [t(50, "2026-08-22T01:00"), t(-30, "2026-08-22T02:00", "SL")]
    # 充值 500 → baseline 由 150 调整为 650, 当前余额 670
    r = _compute_lifetime_stats(rows, current_balance=670, baseline=650)
    # 权益曲线 650 → 700(峰值) → 670，回撤 = 30/700
    assert r["max_dd_pct"] == pytest.approx(30 / 700 * 100)


def test_empty_trades_still_reports_unrealized_drawdown():
    """一笔都没成交但持仓浮亏时，回撤仍要显示。"""
    r = _compute_lifetime_stats([], current_balance=150, baseline=150,
                                equity=120)
    assert r["total"] == 0
    assert r["max_dd_pct"] == pytest.approx(20.0)


# ---------------- TIME 超时强平必须计入 ----------------

def test_time_exit_counted():
    r = _compute_lifetime_stats([t(-20, "2026-08-22T01:00", "TIME")],
                                current_balance=130, baseline=150)
    assert r["total"] == 1
    assert r["net_pnl"] == pytest.approx(-20)
    assert r["return_pct"] == pytest.approx(-20 / 150 * 100)


def test_cancelled_and_orphan_still_excluded():
    """撤单/过期不是真实成交，不能计入。"""
    rows = [t(0, "2026-08-22T01:00", "CANCELLED"),
            t(0, "2026-08-22T02:00", "ORPHAN")]
    assert _compute_lifetime_stats(rows, current_balance=150)["total"] == 0


# ---------------- 单仓位 / 单笔收益率 ----------------

def test_position_roi_prefers_okx_ratio():
    """OKX 已给 uplRatio，直接用，口径与 OKX 界面一致。"""
    roi, base = _pos_pct_cells({"upl": "15", "uplRatio": "0.8989", "imr": "10"},
                               baseline=150)
    assert roi == "+89.89%"
    assert base == "+10.00%"      # 15/150


def test_position_roi_falls_back_to_imr():
    roi, _ = _pos_pct_cells({"upl": "5", "imr": "10"}, baseline=150)
    assert roi == "+50.00%"


def test_position_roi_dash_when_no_data():
    roi, base = _pos_pct_cells({"upl": "5"}, baseline=0)
    assert roi == "-"
    assert base == "-"


def test_position_roi_handles_bad_input():
    assert _pos_pct_cells({"upl": "abc"}, baseline=150) == ("-", "-")


def test_trade_roi_uses_margin():
    """单笔回报率 = 净盈亏 / 该笔保证金。"""
    roi, base = _trade_pct_cells(t(7.4, "x", margin=7.4), baseline=150)
    assert roi == "+100.00%"
    assert base == "+4.93%"       # 7.4/150


def test_trade_roi_negative():
    roi, _ = _trade_pct_cells(t(-3.7, "x", "SL", margin=7.4), baseline=150)
    assert roi == "-50.00%"


def test_trade_roi_dash_without_margin():
    roi, _ = _trade_pct_cells(t(5, "x"), baseline=150)
    assert roi == "-"


# ---------------- 起始本金持久化 ----------------

CONFIG = {"strategy": {
    "pairs": ["BTC-USDT-SWAP"], "position_pct": 0.10,
    "max_consecutive_losses": 3, "cooldown_hours": 24,
    "fixed_mode_threshold": 800_000, "fixed_mode_margin": 1000,
    "leverage": 100, "float_pct": 0.0015, "tp_pct": 0.012, "sl_pct": 0.005,
}}


def _acc(tmp_path):
    return AccountState(DB(tmp_path / "b.db"), CONFIG)


def test_baseline_only_set_once(tmp_path):
    """重启不能把当前余额重新当本金 —— 否则收益率永远显示 0%。"""
    a = _acc(tmp_path)
    assert a.init_baseline(150) is True
    assert a.get_baseline() == pytest.approx(150)
    assert a.init_baseline(999) is False      # 已有值不动
    assert a.get_baseline() == pytest.approx(150)


def test_baseline_ignores_nonpositive(tmp_path):
    a = _acc(tmp_path)
    assert a.init_baseline(0) is False
    assert a.get_baseline() == 0.0


def test_baseline_adjusted_by_deposit(tmp_path):
    """充值 500 要同步加到本金，否则会被算成「赚了 500」。"""
    a = _acc(tmp_path)
    a.init_baseline(150)
    a.adjust_baseline(500, "充值")
    assert a.get_baseline() == pytest.approx(650)


def test_baseline_adjusted_by_withdrawal(tmp_path):
    a = _acc(tmp_path)
    a.init_baseline(650)
    a.adjust_baseline(-500, "提现")
    assert a.get_baseline() == pytest.approx(150)


def test_baseline_never_negative(tmp_path):
    a = _acc(tmp_path)
    a.init_baseline(100)
    a.adjust_baseline(-500)
    assert a.get_baseline() == pytest.approx(0.0)


def test_adjust_baseline_noop_before_init(tmp_path):
    a = _acc(tmp_path)
    a.adjust_baseline(500)
    assert a.get_baseline() == 0.0


def test_peak_equity_tracks_high_water(tmp_path):
    a = _acc(tmp_path)
    assert a.update_peak_equity(150) == pytest.approx(150)
    assert a.update_peak_equity(200) == pytest.approx(200)
    assert a.update_peak_equity(170) == pytest.approx(200)   # 不回落
    assert a.get_peak_equity() == pytest.approx(200)
