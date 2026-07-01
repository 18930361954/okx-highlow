from datetime import datetime, timedelta, timezone

import pytest

from core.account_state import AccountState
from data.db import DB


UTC = timezone.utc

CONFIG = {"strategy": {
    "position_pct": 0.10,
    "max_consecutive_losses": 3,
    "cooldown_hours": 24,
    "fixed_mode_threshold": 800_000,
    "fixed_mode_margin": 1000,
}}


@pytest.fixture
def acc(tmp_path):
    db = DB(tmp_path / "t.db")
    a = AccountState(db, CONFIG)
    a.set_balance(75.0)
    return a


def test_initial_state(acc):
    assert acc.get_balance() == 75.0
    assert acc.get_consecutive_losses() == 0
    assert not acc.is_in_cooldown()
    assert not acc.is_fixed_mode()


def test_compute_margin_pct_mode(acc):
    margin, mode = acc.compute_margin(75.0)
    assert mode == "PCT"
    assert margin == pytest.approx(7.5)


def test_compute_margin_fixed_at_threshold(acc):
    margin, mode = acc.compute_margin(800_000)
    assert mode == "FIXED"
    assert margin == 1000


def test_fixed_mode_locks_permanently(acc):
    acc.compute_margin(900_000)  # 触发切档
    assert acc.is_fixed_mode()
    # 即使余额回落，仍是 FIXED
    margin, mode = acc.compute_margin(50_000)
    assert mode == "FIXED"
    assert margin == 1000


def test_three_consecutive_losses_trigger_cooldown(acc):
    now = datetime.now(UTC)
    acc.on_trade_filled(pnl=-1.0, exit_time=now)
    acc.on_trade_filled(pnl=-1.0, exit_time=now)
    assert not acc.is_in_cooldown(now)
    acc.on_trade_filled(pnl=-1.0, exit_time=now)
    assert acc.is_in_cooldown(now)
    # 24h+1m 后应解除
    later = now + timedelta(hours=24, minutes=1)
    assert not acc.is_in_cooldown(later)


def test_win_resets_loss_streak(acc):
    now = datetime.now(UTC)
    acc.on_trade_filled(pnl=-1.0, exit_time=now)
    acc.on_trade_filled(pnl=-1.0, exit_time=now)
    acc.on_trade_filled(pnl=+2.0, exit_time=now)  # 盈利，归零
    assert acc.get_consecutive_losses() == 0
    acc.on_trade_filled(pnl=-1.0, exit_time=now)
    assert not acc.is_in_cooldown(now)  # 只是 1 次连亏


def test_can_trade_blocked_in_cooldown(acc):
    now = datetime.now(UTC)
    for _ in range(3):
        acc.on_trade_filled(pnl=-1.0, exit_time=now)
    ok, why = acc.can_trade(now)
    assert not ok
    assert "cooldown" in why
