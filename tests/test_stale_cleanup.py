"""测试 stale_order_cleanup 模块"""
import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, MagicMock, patch
from core.stale_order_cleanup import cleanup_stale_orders


UTC = timezone.utc


@pytest.fixture
def mock_runtime():
    """Mock AccountRuntime"""
    runtime = Mock()
    runtime.name = "test_account"
    runtime.logger = Mock()

    # Mock DB with _conn context manager
    mock_db = Mock()
    mock_conn = MagicMock()
    mock_db._conn.return_value.__enter__ = Mock(return_value=mock_conn)
    mock_db._conn.return_value.__exit__ = Mock(return_value=None)
    runtime.db = mock_db

    # Mock OKX client
    runtime.okx = Mock()

    return runtime, mock_conn


def test_cleanup_query_uses_db_conn(mock_runtime):
    """测试清理函数使用正确的 DB._conn() 方法"""
    runtime, mock_conn = mock_runtime

    # Mock 返回空列表（无过期订单）
    mock_conn.execute.return_value.fetchall.return_value = []

    # 执行清理
    result = cleanup_stale_orders(runtime)

    # 验证使用了 _conn() 上下文管理器
    runtime.db._conn.assert_called_once()

    # 验证执行了查询
    mock_conn.execute.assert_called_once()
    call_args = mock_conn.execute.call_args[0]
    assert "SELECT id, pair, signal_date" in call_args[0]
    assert call_args[1] == ("test_account",)

    assert result == 0


def test_cleanup_marks_expired_order(mock_runtime):
    """测试标记过期订单使用正确的 DB._conn() 方法"""
    runtime, mock_conn = mock_runtime

    # Mock 返回一个过期订单（创建于 100 小时前）
    old_time = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    old_time = old_time.replace(day=old_time.day - 5)  # 5天前

    mock_conn.execute.return_value.fetchall.return_value = [
        (1, "BTC-USDT-SWAP", "2026-08-07T00:00Z", "1D", "algo123", old_time.isoformat())
    ]

    # Mock OKX 撤单成功
    runtime.okx.cancel_algo_order.return_value = None

    # 执行清理
    result = cleanup_stale_orders(runtime)

    # 验证调用了两次 _conn()：一次查询，一次更新
    assert runtime.db._conn.call_count == 2

    # 验证第二次调用是 UPDATE
    update_call = mock_conn.execute.call_args_list[1]
    assert "UPDATE trades" in update_call[0][0]
    assert "exit_reason='EXPIRED'" in update_call[0][0]

    assert result == 1


def test_cleanup_handles_db_error_gracefully(mock_runtime):
    """测试 DB 错误时不会崩溃"""
    runtime, mock_conn = mock_runtime

    # Mock DB 查询失败
    mock_conn.execute.side_effect = Exception("DB connection failed")

    # 执行清理
    result = cleanup_stale_orders(runtime)

    # 验证记录了错误日志
    runtime.logger.error.assert_called_once()
    assert "查询 open trades 失败" in runtime.logger.error.call_args[0][0]

    # 返回 0（清理 0 个）
    assert result == 0
