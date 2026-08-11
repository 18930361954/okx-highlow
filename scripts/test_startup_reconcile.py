"""快速测试启动对账功能

模拟断网恢复场景：
1. 创建测试 db，插入不一致数据
2. 运行启动对账
3. 验证修复结果
"""
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock
from core.startup_reconcile import startup_full_reconcile


def test_startup_reconcile_integration():
    """集成测试：启动对账修复不一致数据"""

    # 创建临时测试 db
    test_db_path = Path("data/test_startup_reconcile.db")
    test_db_path.parent.mkdir(exist_ok=True)

    # 清理已存在的测试 db
    if test_db_path.exists():
        test_db_path.unlink()

    conn = sqlite3.connect(test_db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY,
            account TEXT,
            pair TEXT,
            side TEXT,
            signal_date TEXT,
            signal_bar TEXT,
            entry_price REAL,
            entry_time TEXT,
            exit_price REAL,
            exit_reason TEXT,
            exit_time TEXT,
            okx_order_id TEXT,
            okx_tp_id TEXT,
            okx_sl_id TEXT,
            pnl REAL,
            pnl_gross REAL,
            fee REAL,
            funding REAL,
            leg_group TEXT,
            created_at TEXT
        )
    """)

    # 插入不一致数据：OKX 已撤但 db 未标记
    conn.execute("""
        INSERT INTO trades (
            id, account, pair, side, signal_date, signal_bar,
            entry_price, entry_time, exit_price, exit_reason, exit_time,
            okx_order_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        901, "test-account", "ETH-USDT-SWAP", "long",
        "2026-08-09T00:00Z", "6H",
        1800.0, None, None, None, None,
        "test_algo_901", datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()

    # 创建 mock runtime
    runtime = MagicMock()
    runtime.name = "test-account"
    runtime.pairs = ["ETH-USDT-SWAP"]
    runtime.logger = MagicMock()
    runtime.db = conn

    # Mock OKX 返回：无持仓、无 pending
    runtime.okx.get_positions.return_value = []
    runtime.okx.list_pending_algos.return_value = []
    runtime.okx._request.return_value = {
        "data": [{"state": "canceled"}]
    }
    runtime.okx.get_cash_balance.return_value = 1000.0
    runtime.account.get_balance.return_value = 1000.0
    runtime.account.set_balance = MagicMock()

    # 执行启动对账
    print("=" * 60)
    print("启动对账测试")
    print("=" * 60)

    startup_full_reconcile(runtime)

    # 验证修复结果
    cursor = conn.execute("""
        SELECT exit_reason FROM trades WHERE id=901
    """)
    result = cursor.fetchone()

    print()
    print("验证结果:")
    print(f"  trade#901 exit_reason = {result[0] if result else 'NULL'}")

    if result and result[0] == "CANCELLED":
        print("  [OK] 修复成功！")
        success = True
    else:
        print("  [FAIL] 修复失败！")
        success = False

    # 清理
    conn.close()
    test_db_path.unlink(missing_ok=True)

    print("=" * 60)
    print("测试完成")
    print("=" * 60)

    return success


if __name__ == "__main__":
    success = test_startup_reconcile_integration()
    sys.exit(0 if success else 1)
