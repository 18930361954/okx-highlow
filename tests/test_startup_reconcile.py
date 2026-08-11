"""测试启动全量对账功能"""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from core.startup_reconcile import (
    startup_full_reconcile,
    _check_okx_positions_in_db,
    _check_db_trades_in_okx,
    _recover_missing_trades_from_okx,
    _validate_and_sync_balance,
)


UTC = timezone.utc


@pytest.fixture
def mock_runtime():
    """创建 mock runtime"""
    runtime = MagicMock()
    runtime.name = "test-account"
    runtime.cfg = MagicMock()
    runtime.cfg.strategy_config = {"pairs": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]}
    runtime.logger = MagicMock()

    # Mock db._conn() context manager
    mock_conn = MagicMock()
    runtime.db = MagicMock()
    runtime.db._conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
    runtime.db._conn.return_value.__exit__ = MagicMock(return_value=False)
    runtime.db._mock_conn = mock_conn  # 保存引用供测试使用

    runtime.okx = MagicMock()
    runtime.account = MagicMock()
    return runtime


def test_check_okx_positions_in_db_normal(mock_runtime):
    """测试：OKX 持仓在 db 有记录 - 正常情况"""
    # OKX 有持仓
    mock_runtime.okx.get_positions.return_value = [
        {
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "pos": "1.0",
            "avgPx": "60000.0"
        }
    ]

    # db 有对应 open trade
    mock_runtime.db._mock_conn.execute.return_value.fetchone.return_value = (123, 60000.0)

    _check_okx_positions_in_db(mock_runtime)

    # 应该查询 db
    mock_runtime.db._mock_conn.execute.assert_called()
    # 不应该报错
    assert not any("🔴" in str(call) for call in mock_runtime.logger.error.call_args_list)


def test_check_okx_positions_missing_in_db(mock_runtime):
    """测试：OKX 持仓但 db 无记录 - 报错"""
    # OKX 有持仓
    mock_runtime.okx.get_positions.return_value = [
        {
            "instId": "ETH-USDT-SWAP",
            "posSide": "short",
            "pos": "-10.0",
            "avgPx": "1800.0"
        }
    ]

    # db 无记录
    mock_runtime.db._mock_conn.execute.return_value.fetchone.return_value = None

    _check_okx_positions_in_db(mock_runtime)

    # 应该报错
    assert any("🔴" in str(call) for call in mock_runtime.logger.error.call_args_list)
    assert any("OKX 有持仓但 db 无记录" in str(call) for call in mock_runtime.logger.error.call_args_list)


def test_check_db_trades_cancelled_in_okx(mock_runtime):
    """测试：db open trade 在 OKX 已撤销 - 标记 CANCELLED"""
    # db 有 open trade
    mock_runtime.db._mock_conn.execute.return_value.fetchall.return_value = [
        (901, "ETH-USDT-SWAP", "long", "algo123", "2026-08-09T00:00Z", "2026-08-09 04:02:04", None)
    ]

    # OKX pending 列表为空
    mock_runtime.okx.list_pending_algos.return_value = []

    # OKX 查询订单状态：已撤销
    mock_runtime.okx._request.return_value = {
        "data": [{"state": "canceled"}]
    }

    _check_db_trades_in_okx(mock_runtime)

    # 应该标记为 CANCELLED
    calls = [str(call) for call in mock_runtime.db._mock_conn.execute.call_args_list]
    assert any("CANCELLED" in call for call in calls)


def test_recover_missing_trades_from_okx(mock_runtime):
    """测试：从 OKX 历史持仓回填缺失记录"""
    # OKX 历史持仓
    mock_runtime.okx._request.return_value = {
        "data": [
            {
                "posId": "pos123",
                "posSide": "long",
                "cTime": "1786248124000",  # 2026-08-09 13:35:24
                "uTime": "1786334524000",  # +24h
                "openAvgPx": "1894.93",
                "closeAvgPx": "1847.18",
                "realizedPnl": "-112.27",
                "pnl": "-112.27",
                "fee": "-1.5",
                "fundingFee": "0.0"
            }
        ]
    }

    # Mock 策略启动时间查询（返回一个早于历史持仓的时间）
    from datetime import datetime
    mock_conn = mock_runtime.db._mock_conn

    # 第一次查询：策略首次启动时间（早于持仓时间，所以会回填）
    mock_conn.execute.return_value.fetchone.side_effect = [
        ("2026-08-01 00:00:00",),  # 策略启动时间
        None,  # posId 查询：不存在
    ]

    _recover_missing_trades_from_okx(mock_runtime, days=7)

    # 应该插入新记录
    calls = [str(call) for call in mock_runtime.db._mock_conn.execute.call_args_list]
    assert any("INSERT INTO trades" in call for call in calls)
    assert any("RECOVERED" in call for call in calls)


def test_validate_and_sync_balance(mock_runtime):
    """测试：余额校验与同步"""
    # OKX 余额
    mock_runtime.okx.get_cash_balance.return_value = 1000.0

    # db 余额
    mock_runtime.account.get_balance.return_value = 990.0

    _validate_and_sync_balance(mock_runtime)

    # 应该同步到 OKX 余额
    mock_runtime.account.set_balance.assert_called_once_with(1000.0)


def test_validate_and_sync_balance_large_diff(mock_runtime):
    """测试：余额偏差超过 10 USDT - 告警"""
    # OKX 余额
    mock_runtime.okx.get_cash_balance.return_value = 1000.0

    # db 余额（差 50）
    mock_runtime.account.get_balance.return_value = 950.0

    _validate_and_sync_balance(mock_runtime)

    # 应该告警
    assert any("⚠️" in str(call) for call in mock_runtime.logger.warning.call_args_list)
    assert any("余额偏差超过 10 USDT" in str(call) for call in mock_runtime.logger.warning.call_args_list)


def test_startup_full_reconcile_integration(mock_runtime):
    """测试：完整启动对账流程"""
    # Mock 所有依赖
    mock_runtime.okx.get_positions.return_value = []
    mock_runtime.db._mock_conn.execute.return_value.fetchall.return_value = []
    mock_runtime.okx._request.return_value = {"data": []}
    mock_runtime.okx.get_cash_balance.return_value = 1000.0
    mock_runtime.account.get_balance.return_value = 1000.0

    # 执行
    startup_full_reconcile(mock_runtime)

    # 应该执行所有 4 步
    log_calls = [str(call) for call in mock_runtime.logger.info.call_args_list]
    assert any("1/4 检查 OKX 持仓" in call for call in log_calls)
    assert any("2/4 检查 db 未平仓记录" in call for call in log_calls)
    assert any("3/4 扫描" in call for call in log_calls)
    assert any("4/4 余额校验" in call for call in log_calls)
    assert any("全量对账完成" in call for call in log_calls)
