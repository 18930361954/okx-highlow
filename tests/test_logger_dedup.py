"""日志折叠过滤器测试 —— 断网刷屏不能淹没业务日志。

背景: 2026-08-08 断网 7h, bot.log 一天写了 21503 行, 其中 21303 行(99.1%)
是几乎全同的重试 WARNING, 有效业务日志仅 200 行。
"""
import logging

from utils.logger import _DedupFilter


def _rec(msg, level=logging.WARNING, created=0.0):
    r = logging.LogRecord("t", level, "", 0, msg, (), None)
    r.created = created
    return r


def test_info_never_suppressed():
    """业务日志(挂单/成交/对账)全部放行 —— 哪怕内容完全相同。"""
    f = _DedupFilter(window_sec=300)
    msg = "[order] place algo BTC-USDT-SWAP dir=long"
    assert all(f.filter(_rec(msg, logging.INFO, created=t)) for t in range(10))


def test_repeated_warning_collapsed_within_window():
    """窗口内同一条 WARNING 只放行首条。"""
    f = _DedupFilter(window_sec=300)
    msg = "OKX request failed: Connection aborted"
    assert f.filter(_rec(msg, created=0.0)) is True
    assert [f.filter(_rec(msg, created=float(t))) for t in range(1, 50)] == [False] * 49


def test_attempt_and_backoff_normalized():
    """attempt=1/2/3 与 retry in Ns 只是同一故障的重试, 折叠成一条。"""
    f = _DedupFilter(window_sec=300)
    base = "OKX request failed (attempt {}): Connection aborted; retry in {}s"
    assert f.filter(_rec(base.format(1, 1), created=0.0)) is True
    assert f.filter(_rec(base.format(2, 2), created=1.0)) is False
    assert f.filter(_rec(base.format(3, 4), created=3.0)) is False


def test_window_expiry_reports_suppressed_count():
    """窗口结束后重新放行, 并把期间抑制的条数补进消息。"""
    f = _DedupFilter(window_sec=300)
    msg = "OKX request failed: Connection aborted"
    f.filter(_rec(msg, created=0.0))
    for t in range(1, 101):
        f.filter(_rec(msg, created=float(t)))

    r = _rec(msg, created=400.0)
    assert f.filter(r) is True
    assert "已抑制 100 条" in r.getMessage()


def test_distinct_errors_not_collapsed_into_each_other():
    """不同故障各自独立计窗 —— 撤单失败不能被行情超时吃掉。"""
    f = _DedupFilter(window_sec=300)
    assert f.filter(_rec("list_pending_algos failed", created=0.0)) is True
    assert f.filter(_rec("cancel_algo_order failed", created=1.0)) is True
    assert f.filter(_rec("list_pending_algos failed", created=2.0)) is False


def test_key_table_bounded():
    """长期运行下 key 表不无限增长。"""
    f = _DedupFilter(window_sec=300)
    for i in range(2000):
        f.filter(_rec(f"unique error {i}", created=float(i)))
    assert len(f._seen) <= 512
