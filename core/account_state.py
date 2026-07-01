from datetime import datetime, timedelta, timezone


UTC = timezone.utc


KEY_BALANCE = "current_balance"
KEY_LOSSES = "consecutive_losses"
KEY_COOLDOWN_UNTIL = "cooldown_until"
KEY_FIXED_LOCKED = "fixed_mode_locked"


class AccountState:
    """
    余额、连亏、熔断、切档状态的持久层。
    全部从 SQLite `state` 表读写。
    """

    def __init__(self, db, config: dict, logger=None):
        self.db = db
        s = config["strategy"]
        self.position_pct = float(s["position_pct"])
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

    # ---------- raw helpers ----------

    def _get_float(self, key: str, default: float = 0.0) -> float:
        v = self.db.get_state(key)
        return float(v) if v is not None else default

    def _get_int(self, key: str, default: int = 0) -> int:
        v = self.db.get_state(key)
        return int(v) if v is not None else default

    def _get_bool(self, key: str, default: bool = False) -> bool:
        v = self.db.get_state(key)
        if v is None:
            return default
        return str(v).lower() in ("1", "true", "yes")

    # ---------- public API ----------

    def get_balance(self) -> float:
        return self._get_float(KEY_BALANCE, 0.0)

    def set_balance(self, balance: float) -> None:
        self.db.set_state(KEY_BALANCE, f"{balance:.6f}")
        if balance >= self.fixed_threshold and not self.is_fixed_mode():
            self.db.set_state(KEY_FIXED_LOCKED, "true")
            if self.logger:
                self.logger.info(
                    f"[切档] balance={balance:.2f} >= {self.fixed_threshold} → FIXED 永久锁定"
                )

    def get_consecutive_losses(self) -> int:
        return self._get_int(KEY_LOSSES, 0)

    def is_fixed_mode(self) -> bool:
        return self._get_bool(KEY_FIXED_LOCKED, False)

    def is_in_cooldown(self, now: datetime | None = None) -> bool:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        until = self.db.get_state(KEY_COOLDOWN_UNTIL)
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
        v = self.db.get_state(KEY_COOLDOWN_UNTIL)
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
                self.db.set_state(KEY_FIXED_LOCKED, "true")
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
        self.db.set_state(KEY_LOSSES, str(losses))

        if losses >= self.max_losses:
            until = now + timedelta(hours=self.cooldown_hours)
            self.db.set_state(KEY_COOLDOWN_UNTIL, until.isoformat())
            self.db.set_state(KEY_LOSSES, "0")
            if self.logger:
                self.logger.warning(
                    f"[熔断] 连亏 {losses} 次，暂停至 {until.isoformat()}"
                )

    def reset_cooldown(self) -> None:
        self.db.set_state(KEY_COOLDOWN_UNTIL, "")
        self.db.set_state(KEY_LOSSES, "0")
        if self.logger:
            self.logger.info("[manual] cooldown reset")
