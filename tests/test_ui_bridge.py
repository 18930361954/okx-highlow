"""ui.bridge: 运行控制逻辑 (pause/resume job id 匹配, 撤单走 daily_cancel)。
不启动真机器人 — 用假 handle 注入。"""
from unittest.mock import MagicMock, patch

from ui.bridge import BotBridge


class _FakeJob:
    def __init__(self, job_id):
        self.id = job_id
        self.paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


def _bridge_with_jobs(job_ids):
    b = BotBridge()
    sched = MagicMock()
    jobs = [_FakeJob(j) for j in job_ids]
    sched.get_jobs.return_value = jobs
    rt = MagicMock()
    rt.name = "acc1"
    b.handle = {"sched": sched, "runtimes": [rt],
                "shutdown": lambda: None, "base_logger": MagicMock()}
    return b, jobs, rt


def test_pause_resume_matches_only_signal_jobs_of_account():
    b, jobs, _ = _bridge_with_jobs([
        "acc1.signal_00", "acc1.signal_06", "acc1.cancel_0559",
        "acc1.reconcile", "acc2.signal_00",
    ])
    n = b.pause_signals("acc1")
    assert n == 2
    assert [j.paused for j in jobs] == [True, True, False, False, False]
    assert "acc1" in b.paused_accounts

    n = b.resume_signals("acc1")
    assert n == 2
    assert not any(j.paused for j in jobs)
    assert "acc1" not in b.paused_accounts


def test_pause_when_not_running_returns_zero():
    b = BotBridge()
    assert b.pause_signals("acc1") == 0
    assert b.resume_signals("acc1") == 0


def test_cancel_all_delegates_to_daily_cancel():
    b, _, rt = _bridge_with_jobs([])
    with patch("main.daily_cancel") as dc:
        msg = b.cancel_all_pending("acc1")
    dc.assert_called_once_with(rt, fire_hour=None)
    assert "撤单" in msg


def test_cancel_all_unknown_account():
    b, _, _ = _bridge_with_jobs([])
    assert "不在运行列表" in b.cancel_all_pending("nobody")


def test_cancel_all_not_running():
    b = BotBridge()
    assert b.cancel_all_pending("acc1") == "机器人未运行"


def test_stop_idempotent_and_clears_state():
    b, _, _ = _bridge_with_jobs([])
    called = {"n": 0}
    b.handle["shutdown"] = lambda: called.__setitem__("n", called["n"] + 1)
    b.paused_accounts.add("acc1")
    b.stop()
    assert called["n"] == 1
    assert b.handle is None and not b.paused_accounts
    b.stop()  # 第二次 no-op
    assert called["n"] == 1
