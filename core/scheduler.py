from datetime import timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


UTC = timezone.utc


def build_scheduler(
    daily_signal_fn,
    daily_report_fn,
    daily_cancel_fn,
    reconcile_fn=None,
    reconcile_interval_seconds: int = 20,
    signal_hour: int = 0,
    signal_minute: int = 0,
    report_hour: int = 23,
    report_minute: int = 55,
    cancel_hour: int = 23,
    cancel_minute: int = 59,
) -> BackgroundScheduler:
    """
    构造一个含 3 个 cron job 的 BackgroundScheduler，时区 UTC。
    job:
      - daily_signal   00:00 UTC
      - daily_report   23:55 UTC
      - daily_cancel   23:59 UTC
    """
    sched = BackgroundScheduler(timezone=UTC)

    sched.add_job(
        daily_signal_fn,
        trigger=CronTrigger(hour=signal_hour, minute=signal_minute, timezone=UTC),
        id="daily_signal",
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    sched.add_job(
        daily_report_fn,
        trigger=CronTrigger(hour=report_hour, minute=report_minute, timezone=UTC),
        id="daily_report",
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    sched.add_job(
        daily_cancel_fn,
        trigger=CronTrigger(hour=cancel_hour, minute=cancel_minute, timezone=UTC),
        id="daily_cancel",
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    if reconcile_fn is not None:
        sched.add_job(
            reconcile_fn,
            trigger=IntervalTrigger(seconds=int(reconcile_interval_seconds), timezone=UTC),
            id="reconcile",
            coalesce=True,       # 上一轮没跑完就跳过累计的
            max_instances=1,     # 保证不并发
            replace_existing=True,
        )
    return sched


SIGNAL_BAR_HOURS = {
    "1D": [0],
    "12H": [0, 12],
    "6H": [0, 6, 12, 18],
    "4H": [0, 4, 8, 12, 16, 20],
    "2H": [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22],
    "1H": list(range(24)),
}


def signal_hours_for(signal_bar: str) -> list[int]:
    """给定信号周期,返回一天内触发的整点 hour 列表(UTC)。"""
    return list(SIGNAL_BAR_HOURS.get(signal_bar, [0]))


def _cancel_hour_minute(signal_hour: int) -> tuple[int, int]:
    """撤单时刻:该桶结束前 1 分钟。上一桶 = signal_hour → 该桶结束时刻 = 下一个 signal_hour。
    这里直接用「触发 hour 减 1 分钟」的偷懒法:
      1D  signal 00:00 → 撤在 23:59
      4H  signal 04:00 → 撤在 03:59
    实际都是「下一次 signal 到来前 1 分钟」,即上一桶末尾。"""
    if signal_hour == 0:
        return 23, 59
    return signal_hour - 1, 59


def add_account_jobs(
    sched: BackgroundScheduler,
    account_name: str,
    daily_signal_fn,
    daily_report_fn,
    daily_cancel_fn,
    reconcile_fn=None,
    reconcile_interval_seconds: int = 20,
    signal_bar: str = "1D",
    report_hour: int = 23,
    report_minute: int = 55,
    signal_second_offset: int = 0,
) -> None:
    """把一个账户的所有 job 注册进已有 scheduler。
    signal_bar → 每天 N 次 signal / N 次 cancel cron。
    report 每日 1 次(账户级报告在 main 里全局出一份)。
    signal_second_offset: 秒偏移,不同账户错开(避免 OKX 51149 并发超时)。
    """
    prefix = account_name
    hours = signal_hours_for(signal_bar)

    # signal: 每个 hour 挂一个;job id 带 hour 区分。加秒偏移防多账户并发
    for h in hours:
        sched.add_job(
            daily_signal_fn,
            trigger=CronTrigger(hour=h, minute=0, second=signal_second_offset, timezone=UTC),
            id=f"{prefix}.signal_{h:02d}",
            misfire_grace_time=300, coalesce=True, max_instances=1, replace_existing=True,
        )

    # cancel: 每个桶末尾撤单 (下一次 signal 前 1 分钟)
    for h in hours:
        ch, cm = _cancel_hour_minute(h)
        sched.add_job(
            daily_cancel_fn,
            trigger=CronTrigger(hour=ch, minute=cm, timezone=UTC),
            id=f"{prefix}.cancel_{ch:02d}{cm:02d}",
            misfire_grace_time=180, coalesce=True, max_instances=1, replace_existing=True,
        )

    # report: 每日 1 次
    sched.add_job(
        daily_report_fn,
        trigger=CronTrigger(hour=report_hour, minute=report_minute, timezone=UTC),
        id=f"{prefix}.daily_report",
        misfire_grace_time=300, coalesce=True, max_instances=1, replace_existing=True,
    )

    # reconcile: 20s 一次
    if reconcile_fn is not None:
        sched.add_job(
            reconcile_fn,
            trigger=IntervalTrigger(seconds=int(reconcile_interval_seconds), timezone=UTC),
            id=f"{prefix}.reconcile",
            coalesce=True, max_instances=1, replace_existing=True,
        )
