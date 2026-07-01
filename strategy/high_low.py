from datetime import date
from typing import Any


def _to_float(x: Any) -> float:
    return float(x)


def _normalize_candle(c: Any) -> dict:
    """统一兼容两种输入：
      - dict: {'ts','open','high','low','close','volume'}
      - list/tuple: OKX 原生 [ts, o, h, l, c, vol, ...]（按时间倒序）
    """
    if isinstance(c, dict):
        return {
            "ts": int(c.get("ts", 0)),
            "open": _to_float(c["open"]),
            "high": _to_float(c["high"]),
            "low": _to_float(c["low"]),
            "close": _to_float(c["close"]),
        }
    return {
        "ts": int(c[0]),
        "open": _to_float(c[1]),
        "high": _to_float(c[2]),
        "low": _to_float(c[3]),
        "close": _to_float(c[4]),
    }


class HighLowStrategy:
    """
    入场逻辑：
      - 看前一日 (UTC) 24 根 1H K 线
      - day_open = 第一根 open，day_close = 最后一根 close
      - high/low = 当日最高/最低
      - 若 close > open（阳）→ 次日只挂多单，触发价 = low * (1 - float_pct)
      - 若 close < open（阴）→ 次日只挂空单，触发价 = high * (1 + float_pct)
      - close == open 或数据不足 → None
    TP/SL：相对入场价 ± tp_pct / sl_pct
    """

    def __init__(self, config: dict, logger=None):
        s = config["strategy"]
        self.float_pct = float(s["float_pct"])
        self.tp_pct = float(s["tp_pct"])
        self.sl_pct = float(s["sl_pct"])
        self.trend_filter = bool(s.get("trend_filter", True))
        self.pair_overrides = s.get("pair_overrides") or {}
        self.logger = logger

    def _tp_sl_for(self, pair: str) -> tuple[float, float]:
        ov = self.pair_overrides.get(pair) or {}
        return (
            float(ov.get("tp_pct", self.tp_pct)),
            float(ov.get("sl_pct", self.sl_pct)),
        )

    def compute_signal(
        self,
        pair: str,
        candles_1h: list,
        signal_date: date | str | None = None,
    ) -> dict | None:
        if not candles_1h or len(candles_1h) < 2:
            if self.logger:
                self.logger.warning(f"{pair}: not enough candles ({len(candles_1h) if candles_1h else 0})")
            return None

        normed = [_normalize_candle(c) for c in candles_1h]
        normed.sort(key=lambda c: c["ts"])

        day_open = normed[0]["open"]
        day_close = normed[-1]["close"]
        day_high = max(c["high"] for c in normed)
        day_low = min(c["low"] for c in normed)

        if day_close > day_open:
            direction = "long"
        elif day_close < day_open:
            direction = "short"
        else:
            if self.logger:
                self.logger.info(f"{pair}: flat day, skip")
            return None

        if not self.trend_filter:
            direction = direction

        tp_pct, sl_pct = self._tp_sl_for(pair)

        if direction == "long":
            entry_price = round(day_low * (1 - self.float_pct), 6)
            tp_price = round(entry_price * (1 + tp_pct), 6)
            sl_price = round(entry_price * (1 - sl_pct), 6)
            reason = (f"day阳 open={day_open} close={day_close} low={day_low} "
                      f"挂多 @ {entry_price} (low×{1 - self.float_pct})")
        else:
            entry_price = round(day_high * (1 + self.float_pct), 6)
            tp_price = round(entry_price * (1 - tp_pct), 6)
            sl_price = round(entry_price * (1 + sl_pct), 6)
            reason = (f"day阴 open={day_open} close={day_close} high={day_high} "
                      f"挂空 @ {entry_price} (high×{1 + self.float_pct})")

        sd = signal_date.isoformat() if isinstance(signal_date, date) else (signal_date or "")

        return {
            "pair": pair,
            "direction": direction,
            "entry_price": entry_price,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "day_open": day_open,
            "day_close": day_close,
            "day_high": day_high,
            "day_low": day_low,
            "signal_date": sd,
            "reason": reason,
        }
