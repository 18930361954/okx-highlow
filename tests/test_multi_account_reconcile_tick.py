"""AccountRuntime.reconcile_tick 熔断退避行为 (P3)。"""
from __future__ import annotations

import time

import pytest
import requests

from core.multi_account import (
    AccountRuntime,
    _RECON_BACKOFF_BASE_SECS,
    _RECON_BACKOFF_MAX_SECS,
    _RECON_NET_FAIL_THRESHOLD,
)
from core.okx_client import OKXError


class FakeReconciler:
    """按调用次数分别返回结果/抛异常/上报 last_run_had_net_error。"""

    def __init__(self, script):
        self._script = list(script)  # list of ('ok', n) | ('net_flag',) | ('raise', exc)
        self._i = 0
        self.last_run_had_net_error = False
        self.calls = 0

    def run_once(self):
        self.calls += 1
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if step[0] == "ok":
            self.last_run_had_net_error = False
            return step[1]
        if step[0] == "net_flag":
            self.last_run_had_net_error = True
            return 0
        if step[0] == "raise":
            raise step[1]
        raise AssertionError(f"unknown step: {step}")


def _make_runtime(rec):
    # 大部分字段熔断逻辑用不到,直接塞 None
    return AccountRuntime(
        cfg=None, okx=None, db=None, account=None, strategy=None,
        order_manager=None, reconciler=rec, logger=None,
    )


def test_tick_success_leaves_counters_zero():
    rec = FakeReconciler([("ok", 0)])
    rt = _make_runtime(rec)
    rt.reconcile_tick()
    assert rt._net_fail_count == 0
    assert rt._skip_until == 0.0


def test_tick_net_error_via_flag_increments_count():
    rec = FakeReconciler([("net_flag",)])
    rt = _make_runtime(rec)
    rt.reconcile_tick()
    assert rt._net_fail_count == 1
    assert rt._skip_until == 0.0  # 未到阈值


def test_tick_backs_off_after_threshold():
    rec = FakeReconciler([("net_flag",)] * (_RECON_NET_FAIL_THRESHOLD + 1))
    rt = _make_runtime(rec)
    for _ in range(_RECON_NET_FAIL_THRESHOLD):
        rt.reconcile_tick()
    # 达到阈值:_skip_until 被设置
    assert rt._net_fail_count == _RECON_NET_FAIL_THRESHOLD
    assert rt._skip_until > time.monotonic()


def test_tick_skips_during_backoff_window():
    rec = FakeReconciler([("net_flag",)] * (_RECON_NET_FAIL_THRESHOLD + 3))
    rt = _make_runtime(rec)
    for _ in range(_RECON_NET_FAIL_THRESHOLD):
        rt.reconcile_tick()
    calls_before = rec.calls
    # 退避窗口内再 tick,应直接返回不调用 reconciler
    rt.reconcile_tick()
    assert rec.calls == calls_before


def test_tick_success_after_failure_resets():
    rec = FakeReconciler([("net_flag",), ("net_flag",), ("ok", 0)])
    rt = _make_runtime(rec)
    rt.reconcile_tick()
    rt.reconcile_tick()
    assert rt._net_fail_count == 2  # 未达阈值,未退避
    rt.reconcile_tick()  # 成功,清零
    assert rt._net_fail_count == 0
    assert rt._skip_until == 0.0


def test_tick_backoff_grows_exponentially_and_caps():
    """每多一次网络失败,退避时间翻倍,不超过 max。"""
    rec = FakeReconciler([("net_flag",)] * 20)
    rt = _make_runtime(rec)
    # 触发到阈值,记录首次退避基数
    for _ in range(_RECON_NET_FAIL_THRESHOLD):
        rt.reconcile_tick()
    first_backoff_end = rt._skip_until
    # 强制过期以便下次能继续走进 run_once
    rt._skip_until = 0.0
    rt.reconcile_tick()  # 第 N+1 次失败:2 倍
    second_end = rt._skip_until
    rt._skip_until = 0.0
    rt.reconcile_tick()  # 第 N+2 次失败:4 倍
    third_end = rt._skip_until

    now = time.monotonic()
    span1 = first_backoff_end - now  # ~30
    span2 = second_end - now         # ~60
    span3 = third_end - now          # ~120
    assert span1 == pytest.approx(_RECON_BACKOFF_BASE_SECS, abs=2)
    assert span2 > span1
    assert span3 > span2
    assert span3 <= _RECON_BACKOFF_MAX_SECS + 2


def test_tick_raised_okx_error_is_not_net_failure():
    """OKXError 不算网络错误,不应该被计入熔断计数。"""
    rec = FakeReconciler([("raise", OKXError("boom", code="51000"))])
    rt = _make_runtime(rec)
    rt.reconcile_tick()
    assert rt._net_fail_count == 0
    assert rt._skip_until == 0.0


def test_tick_raised_requests_error_counts_as_net():
    rec = FakeReconciler([("raise", requests.ConnectionError("dns"))])
    rt = _make_runtime(rec)
    rt.reconcile_tick()
    assert rt._net_fail_count == 1
