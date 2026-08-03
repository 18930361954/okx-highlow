"""信号桶时间计算 — 与调度/对账共用的纯函数。

从 main.py 原样迁入 (2026-07-31): reconciler 曾用 `from main import ...`,
frozen (PyInstaller) 后入口模块是 __main__, 该 import 会静默失败并退化 1D 语义
→ 混周期账户补挂错桶。迁到中立模块根治。
"""
from datetime import timedelta

from core.scheduler import signal_hours_for


def current_bucket_start(now, signal_bar: str):
    """给定 now 和 signal_bar,返回「当前正在进行的桶」的起始 UTC datetime。
    1D 桶起始 = 当天 00:00;4H 桶起始 = 最近一个 0/4/8/12/16/20 时。
    """
    hours = signal_hours_for(signal_bar)
    day_start = now.replace(minute=0, second=0, microsecond=0)
    # 找 <= now.hour 的最大 h
    h = max((x for x in hours if x <= now.hour), default=hours[-1] if hours else 0)
    if h > now.hour:
        # 当前 hour 小于最小 signal_hour → 用昨天最后一次
        day_start = day_start - timedelta(days=1)
        h = hours[-1]
    return day_start.replace(hour=h)


def previous_bucket_start(now, signal_bar: str):
    """上一桶(即 signal 依据的那一桶)起始 UTC datetime。"""
    cur = current_bucket_start(now, signal_bar)
    hours = signal_hours_for(signal_bar)
    # 找 cur.hour 前面一个 h
    idx = hours.index(cur.hour)
    if idx == 0:
        prev_day = cur - timedelta(days=1)
        return prev_day.replace(hour=hours[-1])
    return cur.replace(hour=hours[idx - 1])


def bucket_id(start_dt) -> str:
    """桶标识,存到 db.signal_date。短、可读、UTC。"""
    return start_dt.strftime("%Y-%m-%dT%H:00Z")
