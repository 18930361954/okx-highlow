from datetime import datetime, timezone

from utils.time_helper import (
    next_utc_midnight,
    today_utc_date,
    utc_day_bounds,
    yesterday_utc_date,
)


UTC = timezone.utc


def test_today_and_yesterday():
    now = datetime(2026, 6, 29, 15, 30, tzinfo=UTC)
    assert today_utc_date(now).isoformat() == "2026-06-29"
    assert yesterday_utc_date(now).isoformat() == "2026-06-28"


def test_next_midnight():
    now = datetime(2026, 6, 29, 23, 59, tzinfo=UTC)
    nxt = next_utc_midnight(now)
    assert nxt == datetime(2026, 6, 30, 0, 0, tzinfo=UTC)


def test_day_bounds():
    from datetime import date
    s, e = utc_day_bounds(date(2026, 6, 29))
    assert s == datetime(2026, 6, 29, tzinfo=UTC)
    assert e == datetime(2026, 6, 30, tzinfo=UTC)


def test_handles_non_utc_input():
    # 输入是带其他时区的 datetime 也应被转 UTC
    from datetime import timedelta
    tz_east = timezone(timedelta(hours=8))
    # 2026-06-30 07:30 +08 == 2026-06-29 23:30 UTC
    local = datetime(2026, 6, 30, 7, 30, tzinfo=tz_east)
    assert today_utc_date(local).isoformat() == "2026-06-29"
