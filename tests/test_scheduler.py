"""混周期调度 (2026-07 新增): pair_signal_bars → job hours 并集;
bucket_signal_and_place(fire_hour=...) / daily_cancel(fire_hour=...) 按 pair 周期过滤。
不起真 scheduler 线程, 只验证注册的 job 集合与过滤逻辑。"""
from datetime import timezone

from apscheduler.schedulers.background import BackgroundScheduler

from core.scheduler import add_account_jobs, signal_hours_for
from main import _pairs_for_fire_hour, daily_cancel
from strategy.high_low import HighLowStrategy


UTC = timezone.utc


def _mk_sched():
    return BackgroundScheduler(timezone=UTC)


def _job_ids(sched, prefix):
    return sorted(j.id for j in sched.get_jobs() if j.id.startswith(prefix))


# ---------- add_account_jobs: hours 并集 ----------

def test_single_bar_registers_expected_hours():
    """旧行为回归: 单周期 6H → signal_00/06/12/18。"""
    sched = _mk_sched()
    add_account_jobs(sched, account_name="acc",
                     daily_signal_fn=lambda: None,
                     daily_report_fn=lambda: None,
                     daily_cancel_fn=lambda: None,
                     signal_bar="6H")
    ids = _job_ids(sched, "acc.signal_")
    assert ids == ["acc.signal_00", "acc.signal_06", "acc.signal_12", "acc.signal_18"]


def test_mixed_bars_register_union_of_hours():
    """混周期 1D BTC + 12H ETH + 6H SOL → hours = {0} ∪ {0,12} ∪ {0,6,12,18}。"""
    sched = _mk_sched()
    calls = []
    add_account_jobs(sched, account_name="acc",
                     daily_signal_fn=lambda fire_hour=None: calls.append(fire_hour),
                     daily_report_fn=lambda: None,
                     daily_cancel_fn=lambda fire_hour=None: None,
                     pair_signal_bars={
                         "BTC-USDT-SWAP": "1D",
                         "ETH-USDT-SWAP": "12H",
                         "SOL-USDT-SWAP": "6H",
                     })
    ids = _job_ids(sched, "acc.signal_")
    assert ids == ["acc.signal_00", "acc.signal_06", "acc.signal_12", "acc.signal_18"]
    # cancel job 同样按并集注册 (每桶末尾一个)
    cancel_ids = _job_ids(sched, "acc.cancel_")
    assert len(cancel_ids) == 4

    # 触发 signal_06 的 job → fn 收到 fire_hour=6
    job = next(j for j in sched.get_jobs() if j.id == "acc.signal_06")
    job.func()
    assert calls == [6]


def test_mixed_bars_all_1d_only_hour_zero():
    sched = _mk_sched()
    add_account_jobs(sched, account_name="acc",
                     daily_signal_fn=lambda fire_hour=None: None,
                     daily_report_fn=lambda: None,
                     daily_cancel_fn=lambda fire_hour=None: None,
                     pair_signal_bars={"BTC-USDT-SWAP": "1D"})
    assert _job_ids(sched, "acc.signal_") == ["acc.signal_00"]


# ---------- _pairs_for_fire_hour: pair 过滤 ----------

class _FakeRT:
    """_pairs_for_fire_hour 只需要 cfg.pairs + strategy.signal_bar_for。"""
    def __init__(self, pair_bars: dict):
        class _C:
            pairs = list(pair_bars)
        self.cfg = _C()
        self.strategy = HighLowStrategy({"strategy": {
            "float_pct": 0.005, "tp_pct": 0.008, "sl_pct": 0.03,
            "signal_bar": "6H",
            "pair_overrides": {p: {"signal_bar": b} for p, b in pair_bars.items()},
        }})
        self.logger = None
        class _OM:
            cancelled_pairs = []
            def cancel_all_pending(self, pair=None):
                _OM.cancelled_pairs.append(pair)
        self.order_manager = _OM()


MIXED = {"BTC-USDT-SWAP": "1D", "ETH-USDT-SWAP": "12H", "SOL-USDT-SWAP": "6H"}


def test_fire_hour_0_selects_all_pairs():
    """hour=0 是三个周期的公共起点 → 全部 pair 轮到。"""
    rt = _FakeRT(MIXED)
    assert set(_pairs_for_fire_hour(rt, 0)) == set(MIXED)


def test_fire_hour_12_selects_12h_and_6h():
    rt = _FakeRT(MIXED)
    assert set(_pairs_for_fire_hour(rt, 12)) == {"ETH-USDT-SWAP", "SOL-USDT-SWAP"}


def test_fire_hour_6_selects_only_6h():
    rt = _FakeRT(MIXED)
    assert _pairs_for_fire_hour(rt, 6) == ["SOL-USDT-SWAP"]


def test_fire_hour_none_selects_all():
    """单周期兼容路径: fire_hour=None → 全部 pair (旧行为)。"""
    rt = _FakeRT(MIXED)
    assert set(_pairs_for_fire_hour(rt, None)) == set(MIXED)


def test_fire_hour_with_no_due_pairs_is_empty():
    """hour=3 不是任何周期的起点 → 空 (job 不会注册到 3 点, 防御检查)。"""
    rt = _FakeRT(MIXED)
    assert _pairs_for_fire_hour(rt, 3) == []


# ---------- daily_cancel 按 fire_hour 过滤 ----------

def test_daily_cancel_mixed_only_cancels_due_pairs():
    """12H 桶末 (fire_hour=12): 只撤 ETH/SOL, 不动 1D BTC 未到期的挂单。"""
    rt = _FakeRT(MIXED)
    rt.order_manager.cancelled_pairs.clear()

    class _Log:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
    rt.logger = _Log()

    daily_cancel(rt, fire_hour=12)
    assert set(rt.order_manager.cancelled_pairs) == {"ETH-USDT-SWAP", "SOL-USDT-SWAP"}


def test_daily_cancel_none_cancels_all_pairs_once():
    """单周期兼容: fire_hour=None → cancel_all_pending() 一次撤全部 (pair=None)。"""
    rt = _FakeRT(MIXED)
    rt.order_manager.cancelled_pairs.clear()

    class _Log:
        def info(self, *a, **k): pass
    rt.logger = _Log()

    daily_cancel(rt, fire_hour=None)
    assert rt.order_manager.cancelled_pairs == [None]
