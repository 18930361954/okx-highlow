"""测试 missing_signal_check 模块"""
import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from core.missing_signal_check import check_missing_signals


UTC = timezone.utc


@pytest.fixture
def mock_runtime():
    """Mock AccountRuntime"""
    runtime = Mock()
    runtime.name = "test_account"
    runtime.logger = Mock()

    # Mock cfg with pairs
    runtime.cfg = Mock()
    runtime.cfg.pairs = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]

    # Mock strategy with signal_bar_for method
    runtime.strategy = Mock()
    runtime.strategy.signal_bar_for.return_value = "1D"

    # Mock db
    runtime.db = Mock()
    runtime.db.list_trades_by_date.return_value = []

    # Mock okx
    runtime.okx = Mock()
    runtime.okx.list_pending_algos.return_value = []
    runtime.okx.get_positions.return_value = []

    # Mock account
    runtime.account = Mock()
    runtime.account.can_trade.return_value = (True, None)

    # Mock reconciler
    runtime.reconciler = Mock()
    runtime.reconciler._catchup_after_exit.return_value = None

    return runtime


def test_check_uses_strategy_signal_bar_for(mock_runtime):
    """测试使用正确的 strategy.signal_bar_for() 方法"""
    runtime = mock_runtime

    # 执行检测
    with patch('core.missing_signal_check.previous_bucket_start') as mock_bucket:
        mock_bucket.return_value = datetime.now(UTC)

        result = check_missing_signals(runtime)

    # 验证调用了 strategy.signal_bar_for，而不是 cfg.get_pair_signal_bar
    assert runtime.strategy.signal_bar_for.call_count == 2  # 两个 pair
    runtime.strategy.signal_bar_for.assert_any_call("BTC-USDT-SWAP")
    runtime.strategy.signal_bar_for.assert_any_call("ETH-USDT-SWAP")

    # 验证 cfg 没有 get_pair_signal_bar 调用
    assert not hasattr(runtime.cfg, 'get_pair_signal_bar') or \
           not runtime.cfg.get_pair_signal_bar.called


def test_check_handles_dict_pair_config(mock_runtime):
    """测试处理字典形式的 pair 配置"""
    runtime = mock_runtime

    # 使用字典形式的 pair 配置
    runtime.cfg.pairs = [
        {"symbol": "BTC-USDT-SWAP", "leverage": 5},
        {"symbol": "ETH-USDT-SWAP", "leverage": 3}
    ]

    # 执行检测
    with patch('core.missing_signal_check.previous_bucket_start') as mock_bucket:
        mock_bucket.return_value = datetime.now(UTC)

        result = check_missing_signals(runtime)

    # 验证正确提取了 symbol
    runtime.strategy.signal_bar_for.assert_any_call("BTC-USDT-SWAP")
    runtime.strategy.signal_bar_for.assert_any_call("ETH-USDT-SWAP")


def test_check_detects_missing_signal(mock_runtime):
    """测试检测到漏挂信号并补挂"""
    runtime = mock_runtime

    # Mock 无 db 记录、无 pending、无持仓
    runtime.db.list_trades_by_date.return_value = []
    runtime.okx.list_pending_algos.return_value = []
    runtime.okx.get_positions.return_value = []

    # 执行检测
    with patch('core.missing_signal_check.previous_bucket_start') as mock_bucket:
        # 返回 1 小时前（未过期）
        now = datetime.now(UTC)
        mock_bucket.return_value = now.replace(hour=now.hour - 1)

        result = check_missing_signals(runtime)

    # 验证触发了补挂
    assert runtime.reconciler._catchup_after_exit.call_count == 2  # 两个 pair
    assert result == 2


def test_check_skips_when_has_db_record(mock_runtime):
    """测试有 db 记录时不补挂"""
    runtime = mock_runtime

    # Mock 有 db 记录（BTC 有记录，ETH 没有）
    def mock_list_trades(sig_id, account):
        if "BTC" in str(runtime.strategy.signal_bar_for.call_args):
            return [{"pair": "BTC-USDT-SWAP", "signal_date": sig_id}]
        return []

    runtime.db.list_trades_by_date.side_effect = mock_list_trades

    # 执行检测
    with patch('core.missing_signal_check.previous_bucket_start') as mock_bucket:
        mock_bucket.return_value = datetime.now(UTC)

        result = check_missing_signals(runtime)

    # 验证 BTC 没有补挂，ETH 补挂了
    assert runtime.reconciler._catchup_after_exit.call_count == 1
    assert result == 1


def test_check_skips_when_signal_expired(mock_runtime):
    """测试信号过期时不补挂"""
    runtime = mock_runtime

    # Mock 无记录
    runtime.db.list_trades_by_date.return_value = []
    runtime.okx.list_pending_algos.return_value = []
    runtime.okx.get_positions.return_value = []

    # 执行检测
    with patch('core.missing_signal_check.previous_bucket_start') as mock_bucket:
        # 返回 20 小时前（已过期，1D 信号的 50% = 12h）
        from datetime import timedelta
        now = datetime.now(UTC)
        mock_bucket.return_value = now - timedelta(hours=20)

        result = check_missing_signals(runtime)

    # 验证没有触发补挂（信号已过期）
    runtime.reconciler._catchup_after_exit.assert_not_called()
    assert result == 0
