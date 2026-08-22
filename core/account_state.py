from datetime import datetime, timedelta, timezone

from data.db import DEFAULT_ACCOUNT


UTC = timezone.utc


KEY_BALANCE = "current_balance"
KEY_LOSSES = "consecutive_losses"
KEY_COOLDOWN_UNTIL = "cooldown_until"
KEY_FIXED_LOCKED = "fixed_mode_locked"
# 起始本金(首次启动时落一次, 之后只由充提调整)。收益率与回撤的分母来源。
# 不记的话只能用"当前余额 - 累计盈亏"倒推, 中途充值会让分母虚高、回撤被严重低估。
KEY_BASELINE = "baseline_capital"
# 历史峰值权益。回撤 = (峰值 - 当前) / 峰值。持久化才能跨重启保留。
KEY_PEAK_EQUITY = "peak_equity"


class AccountState:
    """
    余额、连亏、熔断、切档状态的持久层。
    全部从 SQLite `state` 表读写。所有读写都带 account 维度 → 多账户共享同一份 db。
    """

    def __init__(self, db, config: dict, logger=None, account: str = DEFAULT_ACCOUNT):
        self.db = db
        self.account = account
        s = config["strategy"]
        self.position_pct = float(s["position_pct"])
        self.leverage = int(s.get("leverage", 100))  # 默认 100x，缺省时兼容旧配置
        self.max_losses = int(s["max_consecutive_losses"])
        self.cooldown_hours = int(s["cooldown_hours"])
        self.fixed_threshold = float(s["fixed_mode_threshold"])
        self.fixed_margin = float(s["fixed_mode_margin"])
        self.pair_overrides = s.get("pair_overrides") or {}
        self.logger = logger

    def _position_pct_for(self, pair: str | None) -> float:
        if not pair:
            return self.position_pct
        ov = self.pair_overrides.get(pair) or {}
        return float(ov.get("position_pct", self.position_pct))

    def leverage_for(self, pair: str | None) -> int:
        """获取 pair 级 leverage；缺省走全局。SOL 走 50x，BTC/ETH 走 100x。"""
        if not pair:
            return self.leverage
        ov = self.pair_overrides.get(pair) or {}
        return int(ov.get("leverage", self.leverage))

    # ---------- raw helpers ----------

    def _get_float(self, key: str, default: float = 0.0) -> float:
        v = self.db.get_state(key, account=self.account)
        return float(v) if v is not None else default

    def _get_int(self, key: str, default: int = 0) -> int:
        v = self.db.get_state(key, account=self.account)
        return int(v) if v is not None else default

    def _get_bool(self, key: str, default: bool = False) -> bool:
        v = self.db.get_state(key, account=self.account)
        if v is None:
            return default
        return str(v).lower() in ("1", "true", "yes")

    # ---------- public API ----------

    def get_balance(self) -> float:
        return self._get_float(KEY_BALANCE, 0.0)

    def set_balance(self, balance: float) -> None:
        self.db.set_state(KEY_BALANCE, f"{balance:.6f}", account=self.account)
        if balance >= self.fixed_threshold and not self.is_fixed_mode():
            self.db.set_state(KEY_FIXED_LOCKED, "true", account=self.account)
            if self.logger:
                self.logger.info(
                    f"[切档] balance={balance:.2f} >= {self.fixed_threshold} → FIXED 永久锁定"
                )

    # ---------- 起始本金 / 峰值权益 (收益率与回撤的分母) ----------

    def get_baseline(self) -> float:
        """起始本金。0 = 还没落过(首次启动前)。"""
        return self._get_float(KEY_BASELINE, 0.0)

    def init_baseline(self, balance: float) -> bool:
        """首次落起始本金。已有值就不动 —— 否则每次重启都会把当前余额
        当成本金, 收益率永远显示 0%。返回是否真的写入。"""
        if self.get_baseline() > 0 or balance <= 0:
            return False
        self.db.set_state(KEY_BASELINE, f"{balance:.6f}", account=self.account)
        if self.logger:
            self.logger.info(f"[baseline] 起始本金记为 {balance:.2f} USDT")
        return True

    def adjust_baseline(self, delta: float, reason: str = "") -> None:
        """充值/提现时同步调整起始本金。
        不调的话: 充 500 会被算成"赚了 500", 收益率虚高。"""
        base = self.get_baseline()
        if base <= 0:
            return
        new = max(0.0, base + delta)
        self.db.set_state(KEY_BASELINE, f"{new:.6f}", account=self.account)
        if self.logger:
            self.logger.info(
                f"[baseline] 起始本金 {base:.2f} → {new:.2f} "
                f"({delta:+.2f}{' ' + reason if reason else ''})"
            )

    def get_peak_equity(self) -> float:
        return self._get_float(KEY_PEAK_EQUITY, 0.0)

    def update_peak_equity(self, equity: float) -> float:
        """权益创新高则更新峰值。返回当前峰值。"""
        peak = self.get_peak_equity()
        if equity > peak:
            self.db.set_state(KEY_PEAK_EQUITY, f"{equity:.6f}",
                              account=self.account)
            return equity
        return peak

    def get_consecutive_losses(self) -> int:
        return self._get_int(KEY_LOSSES, 0)

    def is_fixed_mode(self) -> bool:
        return self._get_bool(KEY_FIXED_LOCKED, False)

    def is_in_cooldown(self, now: datetime | None = None) -> bool:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        until = self.db.get_state(KEY_COOLDOWN_UNTIL, account=self.account)
        if not until:
            return False
        try:
            dt = datetime.fromisoformat(until)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return now < dt
        except ValueError:
            return False

    def cooldown_until(self) -> datetime | None:
        v = self.db.get_state(KEY_COOLDOWN_UNTIL, account=self.account)
        if not v:
            return None
        try:
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            return None

    def can_trade(self, now: datetime | None = None) -> tuple[bool, str]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        if self.is_in_cooldown(now):
            until = self.cooldown_until()
            return False, f"in cooldown until {until.isoformat() if until else '?'}"
        bal = self.get_balance()
        if bal <= 0:
            return False, f"balance={bal} not initialized or zero"
        return True, "ok"

    def compute_margin(self, balance: float, pair: str | None = None) -> tuple[float, str]:
        if self.is_fixed_mode() or balance >= self.fixed_threshold:
            if not self.is_fixed_mode():
                self.db.set_state(KEY_FIXED_LOCKED, "true", account=self.account)
            return self.fixed_margin, "FIXED"
        pct = self._position_pct_for(pair)
        return round(balance * pct, 6), "PCT"

    def on_trade_filled(
        self,
        pnl: float,
        exit_time: datetime | None = None,
        new_balance: float | None = None,
    ) -> None:
        """成交结算后调用：更新余额 + 连亏 + 触发熔断 + 切档"""
        now = (exit_time or datetime.now(UTC)).astimezone(UTC)

        if new_balance is not None:
            self.set_balance(new_balance)
        else:
            self.set_balance(self.get_balance() + pnl)

        losses = self.get_consecutive_losses()
        if pnl < 0:
            losses += 1
        else:
            losses = 0
        self.db.set_state(KEY_LOSSES, str(losses), account=self.account)

        if losses >= self.max_losses:
            until = now + timedelta(hours=self.cooldown_hours)
            self.db.set_state(KEY_COOLDOWN_UNTIL, until.isoformat(), account=self.account)
            self.db.set_state(KEY_LOSSES, "0", account=self.account)
            if self.logger:
                self.logger.warning(
                    f"[熔断] 连亏 {losses} 次，暂停至 {until.isoformat()}"
                )

    def reset_cooldown(self) -> None:
        self.db.set_state(KEY_COOLDOWN_UNTIL, "", account=self.account)
        self.db.set_state(KEY_LOSSES, "0", account=self.account)
        if self.logger:
            self.logger.info("[manual] cooldown reset")
