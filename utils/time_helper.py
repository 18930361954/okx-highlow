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


def okx_bar_utc(signal_bar: str) -> str:
    """把策略 signal_bar 映射成 OKX K 线接口的 bar 参数,保证 UTC 对齐。

    OKX 默认 bar=6H/12H/1D 用 HK 时区(UTC+8)对齐,桶起点是 UTC 04/10/16/22 等——
    必须显式加 utc 后缀才是 UTC 00/06/12/18 对齐。4H 及更短周期本身就是 UTC 对齐,不加。
    """
    if signal_bar in ("6H", "12H", "1D", "1W", "1M"):
        return signal_bar + "utc"
    return signal_bar


def fetch_prev_bucket_candles(okx, pair: str, signal_bar: str,
                               prev_bkt_dt: datetime, logger=None) -> list:
    """拉「signal 依据的那一桶」K 线,按 ts 精挑,避免 OKX 服务端桶延迟返回时错位到上上一桶。

    - 非 1D:拉 bar=signal_bar 多根,按 ts == to_ms(prev_bkt_dt) 精挑那 1 根返回 [k]
    - 1D:拉 bar=1Hutc 多根,按 ts ∈ [prev_bkt_dt, prev_bkt_dt+24h) 精挑 24 根返回
         (1H 本身 UTC 对齐;这里 1Hutc 等价 1H,写全是为语义一致)

    找不到 → 返回 []。调用方判空跳过,不下单(宁可漏一次也不挂错价)。

    重要:6H/12H/1D 必须显式用 utc 后缀,否则 OKX 默认返回 HK 桶,策略会跑偏 4 小时。
    """
    target_ms = to_ms(prev_bkt_dt)
    if signal_bar == "1D":
        end_ms = to_ms(prev_bkt_dt + timedelta(days=1))
        # 1H 本身就是 UTC 对齐,不需要后缀
        raw = okx.get_candles(pair, bar="1H", limit=48)
        picked = [k for k in raw if target_ms <= int(k[0]) < end_ms]
        if len(picked) < 24:
            if logger:
                logger.warning(
                    f"{pair}: 1D 桶 K 线不齐,期望 24 根 got={len(picked)} "
                    f"target_ms={target_ms} —— 不下单")
            return []
        return picked

    bar = okx_bar_utc(signal_bar)
    raw = okx.get_candles(pair, bar=bar, limit=5)
    for k in raw:
        if int(k[0]) == target_ms:
            return [k]
    if logger:
        got_ts = [int(k[0]) for k in raw]
        logger.warning(
            f"{pair}: {bar} 桶 K 线未找到 target_ms={target_ms} "
            f"got_ts={got_ts} —— 不下单")
    return []
