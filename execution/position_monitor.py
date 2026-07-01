import threading
import time
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


UTC = timezone.utc


class PositionMonitor:
    """终端面板，5 秒刷新一次。所有 IO 在后台线程。"""

    def __init__(self, okx_client, db, account_state, config: dict, logger=None,
                 refresh_seconds: float = 5.0):
        self.okx = okx_client
        self.db = db
        self.account = account_state
        self.config = config
        self.logger = logger
        self.refresh = refresh_seconds
        self.console = Console()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="PositionMonitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        with Live(self._render(), refresh_per_second=1, console=self.console, screen=False) as live:
            while not self._stop.is_set():
                try:
                    live.update(self._render())
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"monitor render error: {e}")
                self._stop.wait(self.refresh)

    def _render(self) -> Panel:
        bal = self.account.get_balance()
        mode = "FIXED" if self.account.is_fixed_mode() else "PCT"
        in_cd = self.account.is_in_cooldown()
        losses = self.account.get_consecutive_losses()
        max_losses = self.account.max_losses

        header = Table.grid(expand=True)
        header.add_column(justify="left")
        header.add_column(justify="right")
        header.add_row(
            f"[bold]余额[/bold] {bal:.2f} USDT   [bold]模式[/bold] {mode}   [bold]熔断[/bold] {'是' if in_cd else '否'}",
            f"[bold]连亏[/bold] {losses}/{max_losses}   [bold]now[/bold] {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        )

        pending_tbl = Table(title="今日待触发挂单", show_header=True, header_style="cyan", expand=True)
        for c in ("品种", "方向", "触发价", "TP", "SL", "AlgoID"):
            pending_tbl.add_column(c)
        try:
            pendings = self.okx.list_pending_algos(ordType="trigger")
        except Exception:
            pendings = []
        for o in pendings:
            pending_tbl.add_row(
                str(o.get("instId", "")),
                str(o.get("side", "")),
                str(o.get("triggerPx", "")),
                str(o.get("tpTriggerPx", "")),
                str(o.get("slTriggerPx", "")),
                str(o.get("algoId", ""))[:18],
            )
        if not pendings:
            pending_tbl.add_row("(无)", "", "", "", "", "")

        pos_tbl = Table(title="当前持仓", show_header=True, header_style="magenta", expand=True)
        for c in ("品种", "方向", "张数", "均价", "未实现盈亏"):
            pos_tbl.add_column(c)
        try:
            positions = self.okx.get_positions()
        except Exception:
            positions = []
        positions = [p for p in positions if float(p.get("pos", 0) or 0) != 0]
        for p in positions:
            pos_tbl.add_row(
                str(p.get("instId", "")),
                str(p.get("posSide", "")),
                str(p.get("pos", "")),
                str(p.get("avgPx", "")),
                str(p.get("upl", "")),
            )
        if not positions:
            pos_tbl.add_row("(无)", "", "", "", "")

        trade_tbl = Table(title="今日已成交", show_header=True, header_style="green", expand=True)
        for c in ("时间", "品种", "方向", "出场", "原因", "PnL"):
            trade_tbl.add_column(c)
        # 今日执行的交易在 db 里 signal_date=昨日（昨日 K → 今日单）
        sig_date = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        rows = self.db.list_trades_by_date(sig_date)
        filled = [r for r in rows if r.get("exit_price") is not None]
        for r in filled:
            trade_tbl.add_row(
                (r.get("exit_time") or "")[:19],
                r.get("pair", ""),
                r.get("side", ""),
                str(r.get("exit_price", "")),
                r.get("exit_reason", ""),
                f"{r.get('pnl', 0):+.2f}",
            )
        if not filled:
            trade_tbl.add_row("(无)", "", "", "", "", "")

        outer = Table.grid(expand=True)
        outer.add_row(header)
        outer.add_row(pending_tbl)
        outer.add_row(pos_tbl)
        outer.add_row(trade_tbl)

        return Panel(outer, title="HighLow Bot v1.0", border_style="bright_blue")
