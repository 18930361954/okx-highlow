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
        # 策略模式: trend(现行,阳低吸多/阴高空) / reversal(反转,阳高空/阴低吸多)
        # / fade(双向,同桶挂多腿+空腿,先触发者成交、另一腿由 reconciler 撤)。
        # 与回测 scripts/strategy_lab.py simulate_mode 逐位对齐。
        self.mode = str(s.get("mode", "trend"))
        # 信号周期: 1D / 12H / 6H / 4H / 2H / 1H。scheduler 按此生成 cron。
        self.signal_bar = str(s.get("signal_bar", "1D"))
        # 单笔张数封顶(与回测口径一致):BTC 1000, ETH/SOL 5000。
        # pair_overrides.max_contracts 可覆盖。类型 float 以支持 0.01 张精度。
        self.max_contracts_map = s.get("max_contracts") or {}
        # 持仓超时倍数(信号桶时长的倍数)。0 = 不启用。pair_overrides 可覆盖。
        self.max_hold_bars = float(s.get("max_hold_bars", 0) or 0)
        self.logger = logger

    def max_contracts_for(self, pair: str) -> float | None:
        """返回 pair 的单笔张数上限(float,支持 0.01 张精度),None 表示无限。"""
        ov = self.pair_overrides.get(pair) or {}
        if "max_contracts" in ov:
            return float(ov["max_contracts"])
        v = self.max_contracts_map.get(pair)
        return float(v) if v else None

    def _tp_sl_for(self, pair: str) -> tuple[float, float]:
        ov = self.pair_overrides.get(pair) or {}
        return (
            float(ov.get("tp_pct", self.tp_pct)),
            float(ov.get("sl_pct", self.sl_pct)),
        )

    def tp_sl_for(self, pair: str) -> tuple[float, float]:
        """公开版：外部（如 reconciler 兜底分类 TP/SL）需要拿 pair 级 tp/sl 百分比。"""
        return self._tp_sl_for(pair)

    def signal_bar_for(self, pair: str | None = None) -> str:
        """信号周期。目前一个账户共用一个 signal_bar,per-pair 覆盖预留但不启用。"""
        if pair:
            ov = self.pair_overrides.get(pair) or {}
            if "signal_bar" in ov:
                return str(ov["signal_bar"])
        return self.signal_bar

    def _float_for(self, pair: str) -> float:
        ov = self.pair_overrides.get(pair) or {}
        return float(ov.get("float_pct", self.float_pct))

    def _mode_for(self, pair: str) -> str:
        """pair 级策略模式覆盖 → 顶层 mode → 默认 trend。"""
        ov = self.pair_overrides.get(pair) or {}
        return str(ov.get("mode", self.mode))

    def mode_for(self, pair: str) -> str:
        """公开版:外部(scheduler/report)需要拿 pair 级 mode。"""
        return self._mode_for(pair)

    def max_hold_bars_for(self, pair: str) -> float:
        """持仓超时倍数(信号桶时长的倍数)。0/缺省 = 不启用超时强平。

        回测在信号桶末按收盘价强平, 实盘 daily_cancel 只撤未成交挂单、持仓一直持到
        TP/SL, 单边行情下逆势单会从"桶末小亏"变成"打满 SL"。这个配置把回测的
        EOB 语义以时间止损形式补回来。"""
        ov = self.pair_overrides.get(pair) or {}
        v = ov.get("max_hold_bars", self.max_hold_bars)
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return 0.0

    def reentry_floats_for(self, pair: str) -> list[float]:
        """pair 的日内重挂浮动序列。若无配置或为空 → 返回 []（不启用重挂）。
        序列长度即最大入场次数（含第 1 次）。例如 [0.0015, 0.006] 表示：
        第 1 次挂单用 0.15%，若 SL 后第 2 次用 0.6%。"""
        ov = self.pair_overrides.get(pair) or {}
        seq = ov.get("reentry_floats") or []
        return [float(x) for x in seq]

    def compute_reentry_signal(
        self,
        pair: str,
        direction: str,
        day_candles_so_far: list,
        attempt: int,
        signal_date: date | str | None = None,
    ) -> dict | None:
        """日内重挂：用"当日日初到现在"的 K 线段计算新的入场价。
        - direction: 沿用前日方向（'long'/'short'），不重判
        - day_candles_so_far: 今日 UTC 已发生的 1H K 列表（含或不含 partial 当前根均可，只用 high/low）
        - attempt: 本次是第几次入场（1-indexed；attempt=2 用 reentry_floats[1]）
        返回 {'pair','direction','entry_price','tp_price','sl_price','signal_date','reason'} 或 None
        """
        seq = self.reentry_floats_for(pair)
        if not seq or attempt < 1 or attempt > len(seq):
            return None
        if not day_candles_so_far:
            return None

        normed = [_normalize_candle(c) for c in day_candles_so_far]
        day_high = max(c["high"] for c in normed)
        day_low = min(c["low"] for c in normed)

        fp = seq[attempt - 1]
        tp_pct, sl_pct = self._tp_sl_for(pair)

        if direction == "long":
            entry_price = round(day_low * (1 - fp), 6)
            tp_price = round(entry_price * (1 + tp_pct), 6)
            sl_price = round(entry_price * (1 - sl_pct), 6)
            reason = (f"日内重挂#{attempt} fp={fp} low_so_far={day_low} "
                      f"挂多 @ {entry_price}")
        elif direction == "short":
            entry_price = round(day_high * (1 + fp), 6)
            tp_price = round(entry_price * (1 - tp_pct), 6)
            sl_price = round(entry_price * (1 + sl_pct), 6)
            reason = (f"日内重挂#{attempt} fp={fp} high_so_far={day_high} "
                      f"挂空 @ {entry_price}")
        else:
            return None

        sd = signal_date.isoformat() if isinstance(signal_date, date) else (signal_date or "")

        return {
            "pair": pair,
            "direction": direction,
            "entry_price": entry_price,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "day_open": None,
            "day_close": None,
            "day_high": day_high,
            "day_low": day_low,
            "signal_date": sd,
            "signal_bar": self.signal_bar_for(pair),
            "reason": reason,
            "attempt": attempt,
        }

    def compute_signal(
        self,
        pair: str,
        candles_1h: list,
        signal_date: date | str | None = None,
    ) -> dict | None:
        """
        candles_1h 是上一个「信号桶」内的原始 K 列表(用于聚合 OHLC)。
        - 1D 信号 → 上一日 24 根 1H K
        - 4H 信号 → 上一 4H 段内 K,可以直接是「1 根 4H K」或「4 根 1H K」
        signal_date 是桶标识(字符串或 date),用作 db.signal_date 存储;
        对 1D 桶 = '2026-07-08';对 4H 桶 = '2026-07-08T04:00Z' 之类。

        candles_1h 只有 1 根时,该根本身就是聚合结果(直接当 day_o/h/l/c)。
        """
        if not candles_1h:
            if self.logger:
                self.logger.warning(f"{pair}: no candles")
            return None

        normed = [_normalize_candle(c) for c in candles_1h]
        normed.sort(key=lambda c: c["ts"])

        day_open = normed[0]["open"]
        day_close = normed[-1]["close"]
        day_high = max(c["high"] for c in normed)
        day_low = min(c["low"] for c in normed)

        # 行情过滤器：pair_overrides 可配 min_prev_amp / max_prev_amp
        pv = self.pair_overrides.get(pair) or {}
        min_amp = float(pv.get("min_prev_amp", 0.0))
        max_amp = float(pv.get("max_prev_amp", 1.0))
        if day_open > 0:
            amp = (day_high - day_low) / day_open
            if amp < min_amp or amp > max_amp:
                if self.logger:
                    self.logger.info(f"{pair}: amp={amp*100:.2f}% 越界 "
                                      f"[{min_amp*100:g}%, {max_amp*100:g}%]，skip")
                return None

        if day_close > day_open:
            prev_dir = "long"   # 前桶阳
        elif day_close < day_open:
            prev_dir = "short"  # 前桶阴
        else:
            if self.logger:
                self.logger.info(f"{pair}: flat day, skip")
            return None

        sd = signal_date.isoformat() if isinstance(signal_date, date) else (signal_date or "")
        day_ctx = {"day_open": day_open, "day_close": day_close,
                   "day_high": day_high, "day_low": day_low}
        mode = self._mode_for(pair)

        # fade: 双向挂单。同桶挂多腿(low×(1-f)) + 空腿(high×(1+f)),共享 leg_group,
        # 先触发者成交、另一腿由 reconciler 撤(OCO)。对齐 strategy_lab.py:137-138。
        if mode == "fade":
            coin = pair.split("-")[0]
            leg_group = f"f{coin}{sd}"
            leg_long = self._build_leg(pair, "long", day_high, day_low, sd, mode, day_ctx, leg_group)
            leg_short = self._build_leg(pair, "short", day_high, day_low, sd, mode, day_ctx, leg_group)
            return {
                "pair": pair,
                "mode": "fade",
                "signal_date": sd,
                "signal_bar": self.signal_bar_for(pair),
                "leg_group": leg_group,
                "legs": [leg_long, leg_short],
                "reason": (f"[fade] {coin} 双向挂单 多@{leg_long['entry_price']} "
                           f"空@{leg_short['entry_price']} (open={day_open} close={day_close})"),
                **day_ctx,
            }

        # trend: 跟随前桶方向 (d=d_prev)。reversal: 反向 (d=-d_prev)。对齐 strategy_lab.py:125/129。
        if mode == "reversal":
            direction = "short" if prev_dir == "long" else "long"
        else:  # trend
            direction = prev_dir
        return self._build_leg(pair, direction, day_high, day_low, sd, mode, day_ctx, None)

    def _build_leg(self, pair: str, direction: str, day_high: float, day_low: float,
                   sd: str, mode: str, day_ctx: dict, leg_group: str | None) -> dict:
        """构造单腿 signal dict。入场价只取决于方向:
          long  → day_low×(1-float);  short → day_high×(1+float)
        TP/SL 相对入场价 ±tp_pct/sl_pct。与回测 strategy_lab.py 逐位对齐。"""
        tp_pct, sl_pct = self._tp_sl_for(pair)
        float_pct = self._float_for(pair)
        if direction == "long":
            entry_price = round(day_low * (1 - float_pct), 6)
            tp_price = round(entry_price * (1 + tp_pct), 6)
            sl_price = round(entry_price * (1 - sl_pct), 6)
            reason = (f"[{mode}] 挂多 @ {entry_price} (low={day_low}×{1 - float_pct})")
        else:
            entry_price = round(day_high * (1 + float_pct), 6)
            tp_price = round(entry_price * (1 - tp_pct), 6)
            sl_price = round(entry_price * (1 + sl_pct), 6)
            reason = (f"[{mode}] 挂空 @ {entry_price} (high={day_high}×{1 + float_pct})")
        return {
            "pair": pair,
            "direction": direction,
            "entry_price": entry_price,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "signal_date": sd,
            "signal_bar": self.signal_bar_for(pair),
            "mode": mode,
            "leg_group": leg_group,
            "reason": reason,
            **day_ctx,
        }
