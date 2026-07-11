import math
import threading
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


UTC = timezone.utc


def _trunc2(x: float) -> float:
    """向零截断到 2 位小数, 与 OKX 界面显示口径一致(避免 6.7774 四舍五入
    到 6.78 而 OKX 显示 6.77 的 0.01 差异)。"""
    if x >= 0:
        return math.floor(x * 100) / 100
    return -math.floor(-x * 100) / 100


def _fmt2(x: float, sign: bool = True) -> str:
    v = _trunc2(x)
    return f"{v:+,.2f}" if sign else f"{v:,.2f}"


def _fmt_uptime(seconds: float) -> str:
    """把秒数格式化成 Xd Yh Zm Ws 的紧凑字符串, 只显示最高的 2-3 段。"""
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _dir_zh(raw: str) -> str:
    """方向字段中文化。兼容 db.side/OKX posSide('long'/'short') 和 OKX side('buy'/'sell')。"""
    v = str(raw or "").lower()
    if v in ("long", "buy"):
        return "[green]做多[/green]"
    if v in ("short", "sell"):
        return "[red]做空[/red]"
    return raw or ""


class PositionMonitor:
    """终端面板 — 多账户版。5 秒刷新。

    数据来源:
      - 余额:db.state.current_balance (由 reconciler 按 OKX 净值累计更新)
      - 挂单/持仓:OKX list_pending_algos / get_positions 实时拉
      - 净 PnL:db.trades.pnl (reconciler 直接从 OKX positions-history.realizedPnl
        写入的净口径, 已扣手续费+资金费, 与 OKX 界面显示的"已实现收益"完全一致)
      - 手续费:db.trades.fee (仅展示用, |fee|, 来源 OKX positions-history.fee)
      - 资金费:db.trades.funding (仅展示用, |fundingFee|, 来源 OKX positions-history.fundingFee)
      - 名义 PnL:pnl + fee + funding (反推展示, 便于对账)

    构造两种模式:
      1. 多账户 (推荐): 传 runtimes=[...] 列表,面板显示所有账户
      2. 单账户 (旧兼容): 传 okx_client / account_state / config,面板显示一个账户
    """

    def __init__(self, okx_client=None, db=None, account_state=None, config=None,
                 logger=None, refresh_seconds: float = 5.0, runtimes=None):
        self.runtimes = runtimes or []
        # 兼容旧签名(单账户):把入参包装成一个"pseudo runtime"
        if not self.runtimes and okx_client is not None:
            class _One:
                pass
            r = _One()
            r.name = "default"
            r.okx = okx_client
            r.account = account_state
            r.db = db
            r.cfg = type("_C", (), {"pairs": [], "env": ""})()
            r.strategy = type("_S", (), {"signal_bar": "1D"})()
            self.runtimes = [r]

        self.db = db or (self.runtimes[0].db if self.runtimes else None)
        self.logger = logger
        self.refresh = refresh_seconds
        self.console = Console()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = datetime.now(UTC)

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

    # ---------- 数据采集(一次采,多处用)----------

    def _collect(self) -> list[dict]:
        """对每账户抓一次余额/pending/positions/today_trades,返回列表。"""
        today_iso = datetime.now(UTC).date().isoformat()
        results: list[dict] = []
        for rt in self.runtimes:
            try:
                bal = rt.account.get_balance()
            except Exception:
                bal = 0.0
            try:
                in_cd = rt.account.is_in_cooldown()
                losses = rt.account.get_consecutive_losses()
                max_losses = rt.account.max_losses
            except Exception:
                in_cd, losses, max_losses = False, 0, 3
            try:
                pendings = rt.okx.list_pending_algos(ordType="trigger")
            except Exception:
                pendings = []
            try:
                positions = [p for p in rt.okx.get_positions()
                             if float(p.get("pos", 0) or 0) != 0]
            except Exception:
                positions = []
            # 今日成交(按 exit_time 落在今天 UTC)
            # 排除从未入场的挂单清扫记录(exit_price=0 说明未成交,只是 reconciler 撤单登记)
            try:
                all_rows = rt.db.list_trades(limit=500, account=rt.name)
                today_filled = [r for r in all_rows
                                if (r.get("exit_price") or 0) > 0
                                and (r.get("exit_time") or "")[:10] == today_iso]
                # db.pnl 已是净口径; 名义 = 净 + 手续费 + 资金费(反推展示)
                today_net = sum((r.get("pnl") or 0) for r in today_filled)
                today_fee = sum((r.get("fee") or 0) for r in today_filled)
                today_funding = sum((r.get("funding") or 0) for r in today_filled)
                today_pnl = today_net + today_fee + today_funding
            except Exception:
                today_filled, today_pnl, today_fee, today_funding, today_net = [], 0.0, 0.0, 0.0, 0.0

            results.append({
                "rt": rt,
                "name": rt.name,
                "env": getattr(rt.cfg, "env", ""),
                "signal_bar": getattr(rt.strategy, "signal_bar", "1D"),
                "balance": bal,
                "in_cd": in_cd,
                "losses": losses,
                "max_losses": max_losses,
                "pendings": pendings,
                "positions": positions,
                "today_filled": today_filled,
                "today_pnl": today_pnl,
                "today_fee": today_fee,
                "today_funding": today_funding,
                "today_net": today_net,
            })
        return results

    # ---------- 渲染 ----------

    def _render(self) -> Panel:
        snap = self._collect()
        total_bal = sum(a["balance"] for a in snap)
        total_pending = sum(len(a["pendings"]) for a in snap)
        total_positions = sum(len(a["positions"]) for a in snap)
        total_today_pnl = sum(a["today_pnl"] for a in snap)
        total_today_fee = sum(a["today_fee"] for a in snap)
        total_today_funding = sum(a["today_funding"] for a in snap)
        total_today_net = sum(a["today_net"] for a in snap)

        # === 头部 ===
        header = Table.grid(expand=True)
        header.add_column(justify="left")
        header.add_column(justify="right")
        now = datetime.now(UTC)
        uptime = _fmt_uptime((now - self._started_at).total_seconds())
        header.add_row(
            f"[bold]账户[/bold] {len(snap)}   "
            f"[bold]总余额[/bold] {total_bal:,.2f}   "
            f"[bold]挂单[/bold] {total_pending}   "
            f"[bold]持仓[/bold] {total_positions}   "
            f"[bold]今日名义[/bold] {_fmt2(total_today_pnl)}   "
            f"[bold]手续费[/bold] {total_today_fee:.4f}   "
            f"[bold]资金费[/bold] {total_today_funding:.4f}   "
            f"[bold]净盈亏[/bold] {_fmt2(total_today_net)}",
            f"[bold]运行[/bold] {uptime}   "
            f"[bold]now[/bold] {now.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        )

        # === 账户一览 ===
        acc_tbl = Table(title="账户一览", show_header=True, header_style="bold cyan", expand=True)
        for c in ("账户", "环境", "周期", "余额", "熔断", "连亏", "pending", "持仓",
                  "今日笔数", "名义 PnL", "手续费", "资金费", "净 PnL"):
            acc_tbl.add_column(c, no_wrap=True)
        for a in snap:
            cd = "[red]是[/red]" if a["in_cd"] else "[green]否[/green]"
            losses = f"{a['losses']}/{a['max_losses']}"
            pnl_str = _fmt2(a['today_pnl'])
            net_str = _fmt2(a['today_net'])
            net_style = "green" if a["today_net"] > 0 else ("red" if a["today_net"] < 0 else "")
            acc_tbl.add_row(
                a["name"], a["env"], a["signal_bar"],
                f"{a['balance']:,.2f}", cd, losses,
                str(len(a["pendings"])), str(len(a["positions"])),
                str(len(a["today_filled"])),
                pnl_str, f"{a['today_fee']:.4f}", f"{a['today_funding']:.4f}",
                f"[{net_style}]{net_str}[/{net_style}]" if net_style else net_str,
            )
        if not snap:
            acc_tbl.add_row("(无账户)", "", "", "", "", "", "", "", "", "", "", "", "")

        # === 挂单表 (全账户合并,带账户名列) ===
        pending_tbl = Table(title="待触发挂单 (全账户)", show_header=True,
                             header_style="cyan", expand=True)
        for c in ("账户", "品种", "方向", "触发价", "TP", "SL", "AlgoID"):
            pending_tbl.add_column(c, no_wrap=True)
        any_p = False
        for a in snap:
            for o in a["pendings"]:
                any_p = True
                pending_tbl.add_row(
                    a["name"], str(o.get("instId", "")), _dir_zh(o.get("side", "")),
                    str(o.get("triggerPx", "")), str(o.get("tpTriggerPx", "")),
                    str(o.get("slTriggerPx", "")), str(o.get("algoId", ""))[:18],
                )
        if not any_p:
            pending_tbl.add_row("(无)", "", "", "", "", "", "")

        # === 当前持仓表 ===
        pos_tbl = Table(title="当前持仓 (全账户)", show_header=True,
                         header_style="magenta", expand=True)
        for c in ("账户", "品种", "方向", "张数", "均价", "未实现盈亏"):
            pos_tbl.add_column(c, no_wrap=True)
        any_pos = False
        for a in snap:
            for p in a["positions"]:
                any_pos = True
                upl = str(p.get("upl", ""))
                try:
                    upl_v = float(upl)
                    upl_style = "green" if upl_v > 0 else ("red" if upl_v < 0 else "")
                    upl_cell = f"[{upl_style}]{upl_v:+,.2f}[/{upl_style}]" if upl_style else upl
                except (ValueError, TypeError):
                    upl_cell = upl
                pos_tbl.add_row(
                    a["name"], str(p.get("instId", "")), _dir_zh(p.get("posSide", "")),
                    str(p.get("pos", "")), str(p.get("avgPx", "")), upl_cell,
                )
        if not any_pos:
            pos_tbl.add_row("(无)", "", "", "", "", "")

        # === 今日成交表 ===
        trade_tbl = Table(title="今日已成交 (全账户 · 按 UTC 日)",
                          show_header=True, header_style="green", expand=True)
        for c in ("时间", "账户", "品种", "方向", "入场", "出场", "原因",
                  "名义 PnL", "手续费", "资金费", "净 PnL"):
            trade_tbl.add_column(c, no_wrap=True)
        any_t = False
        # 汇总所有账户今日成交,按时间倒序
        all_today: list[tuple[str, dict]] = []
        for a in snap:
            for r in a["today_filled"]:
                all_today.append((a["name"], r))
        all_today.sort(key=lambda x: x[1].get("exit_time") or "", reverse=True)
        for aname, r in all_today[:20]:  # 最多 20 条
            any_t = True
            # db.pnl 已是净口径; 名义 = 净 + 手续费 + 资金费(反推展示)
            net = r.get("pnl") or 0
            fee = r.get("fee") or 0
            funding = r.get("funding") or 0
            pnl = net + fee + funding
            style = "green" if net > 0 else ("red" if net < 0 else "")
            net_str = _fmt2(net)
            net_cell = f"[{style}]{net_str}[/{style}]" if style else net_str
            trade_tbl.add_row(
                (r.get("exit_time") or "")[:19], aname,
                r.get("pair", ""), _dir_zh(r.get("side", "")),
                str(r.get("entry_price", "")), str(r.get("exit_price", "")),
                r.get("exit_reason", ""),
                _fmt2(pnl), f"{fee:.4f}", f"{funding:.4f}", net_cell,
            )
        if not any_t:
            trade_tbl.add_row("(无)", "", "", "", "", "", "", "", "", "", "")

        # === 组装 ===
        outer = Table.grid(expand=True)
        outer.add_row(header)
        outer.add_row(acc_tbl)
        outer.add_row(pending_tbl)
        outer.add_row(pos_tbl)
        outer.add_row(trade_tbl)

        return Panel(outer, title="HighLow Bot · 多账户监控 v2.0",
                     border_style="bright_blue")
