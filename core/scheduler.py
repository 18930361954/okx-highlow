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
