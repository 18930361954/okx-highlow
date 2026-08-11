"""测试日志系统在高频错误场景下的稳定性（模拟 OKX 50001 风暴）"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import time
import threading
from utils.logger import get_logger


def test_high_frequency_warnings():
    """模拟大量重复警告，确保日志系统不会挂掉"""
    logger = get_logger("test-stress")

    # 模拟 3 个账户同时遇到 OKX 50001 错误
    def spam_warnings(account: str, count: int):
        for i in range(count):
            logger.warning(f"[{account}] OKX error code=50001 msg=Service temporarily unavailable. Please try again later. endpoint=/api/v5/trade/orders-algo-pending")
            logger.warning(f"[{account}] OKX retryable code=50001 (attempt 1); retry in 1s")
            if i % 20 == 0:
                logger.info(f"[{account}] [reconcile] tick {i}")

    threads = []
    for acc in ["账户1", "账户2", "账户3"]:
        t = threading.Thread(target=spam_warnings, args=(acc, 100))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # 验证最后还能输出日志
    logger.info("Test completed - logger still working")
    print("[OK] Logger survived high-frequency warning storm")


def test_logger_with_unicode():
    """测试日志系统处理中文账户名"""
    logger = get_logger("test-unicode")
    logger.info("[初级炼气士-模拟] 测试中文日志")
    logger.warning("[初级炼气士2-模拟] OKX error code=50001")
    print("[OK] Unicode logging works")


if __name__ == "__main__":
    test_high_frequency_warnings()
    test_logger_with_unicode()
    print("\n[PASS] All logger stability tests passed")
