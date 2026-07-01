from datetime import date, datetime, timedelta, timezone


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def today_utc_date(now: datetime | None = None) -> date:
    return (now or utc_now()).astimezone(UTC).date()


def yesterday_utc_date(now: datetime | None = None) -> date:
    return today_utc_date(now) - timedelta(days=1)


def next_utc_midnight(now: datetime | None = None) -> datetime:
    n = (now or utc_now()).astimezone(UTC)
    tomorrow = n.date() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)


def utc_day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    return start, end


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def from_ms(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)
