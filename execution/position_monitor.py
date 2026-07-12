import math
import threading
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


_LIFETIME_CACHE_TTL_MS = 30_000  # 累计业绩 30s 缓存, 防每 5s render 都全表扫

# 计入"累计业绩"的成交口径 (ORPHAN/CANCELLED 不算真实成交)
_VALID_EXIT_REASONS = {"TP", "SL", "EXIT"}


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


_EXIT_REASON_ZH = {
    "TP": "止盈",
    "SL": "止损",
    "EXIT": "平仓",
    "ORPHAN": "过期",
    "CANCELLED": "撤单",
}


def _exit_reason_zh(raw: str) -> str:
    """db.exit_reason 中文化, 未知值原样返回。"""
    if not raw:
        return ""
    return _EXIT_REASON_ZH.get(str(raw).upper(), str(raw))


def _compute_lifetime_stats(trades: list[dict], current_balance: float = 0.0) -> dict:
    """按传入的 trades 集合聚合业绩指标, 只统计真实成交(TP/SL/EXIT)。

    返回字段:
      raw: total, wins, losses, sum_win, sum_loss_abs, net_pnl, max_win, max_loss
      derived: win_rate, avg_win, avg_loss, profit_factor, max_dd_pct
    """
    valid = [t for t in trades
             if str(t.get("exit_reason") or "").upper() in _VALID_EXIT_REASONS]
    total = len(valid)
    if total == 0:
        return {"total": 0, "wins": 0, "losses": 0,
                "sum_win": 0.0, "sum_loss_abs": 0.0, "net_pnl": 0.0,
                "max_win": 0.0, "max_loss": 0.0,
                "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "profit_factor": 0.0, "max_dd_pct": 0.0}

    wins = [t for t in valid if (t.get("pnl") or 0) > 0]
    losses = [t for t in valid if (t.get("pnl") or 0) < 0]
    net_pnl = sum((t.get("pnl") or 0) for t in valid)
    sum_win = sum((t.get("pnl") or 0) for t in wins)
    sum_loss_abs = sum(abs(t.get("pnl") or 0) for t in losses)

    avg_win = sum_win / len(wins) if wins else 0.0
    avg_loss = sum_loss_abs / len(losses) if losses else 0.0
    if sum_loss_abs > 0:
        profit_factor = sum_win / sum_loss_abs
    elif sum_win > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    max_win = max((t.get("pnl") or 0) for t in valid)
    max_loss = min((t.get("pnl") or 0) for t in valid)

    # 回撚%: 按 exit_time 排序 cumulative pnl → peak → dd。
    # 基准权益 = 估算初始余额 + peak 时的累计 pnl。
    # 初始余额估算: 当前余额 - 全部累计 pnl (无法拿到历史 balance 时的近似)。
    sorted_by_time = sorted(valid, key=lambda t: t.get("exit_time") or "")
    cum = 0.0
    peak = 0.0
    max_dd_abs = 0.0
    peak_cum_at_max_dd = 0.0
    for t in sorted_by_time:
        cum += t.get("pnl") or 0
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd_abs:
            max_dd_abs = dd
            peak_cum_at_max_dd = peak
    initial_est = max(0.0, current_balance - cum) if current_balance > 0 else 0.0
    peak_equity = initial_est + peak_cum_at_max_dd
    if peak_equity > 0:
        max_dd_pct = max_dd_abs / peak_equity * 100
    else:
        # 无余额上下文: 用 peak cum 自身当分母, 至少能反映相对回撚幅度
        max_dd_pct = (max_dd_abs / peak_cum_at_max_dd * 100) if peak_cum_at_max_dd > 0 else 0.0

    return {
        "total": total, "wins": len(wins), "losses": len(losses),
        "sum_win": sum_win, "sum_loss_abs": sum_loss_abs, "net_pnl": net_pnl,
        "max_win": max_win, "max_loss": max_loss,
        "win_rate": len(wins) / total * 100,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "max_dd_pct": max_dd_pct,
    }


def _pending_tp_sl(o: dict) -> tuple[str, str]:
    """从 OKX pending algo dict 中抽 TP / SL 触发价。

    trigger 单的 TP/SL 挂在 attachAlgoOrds[0] 里, 顶层 tpTriggerPx/slTriggerPx 是空串
    (order_manager 用 attachAlgoOrds 挂载, 顶层字段在 trigger 单上会被 OKX 静默丢弃)。
    这里优先读 attachAlgoOrds[0], 兜底再看顶层, 空串返回 '-'。
    """
    tp = sl = ""
    attach = o.get("attachAlgoOrds") or []
    if attach and isinstance(attach, list):
        first = attach[0] if isinstance(attach[0], dict) else {}
        tp = str(first.get("tpTriggerPx") or "")
        sl = str(first.get("slTriggerPx") or "")
    if not tp:
        tp = str(o.get("tpTriggerPx") or "")
    if not sl:
        sl = str(o.get("slTriggerPx") or "")
    return (tp or "-", sl or "-")


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
        # 累计业绩缓存: {account_name: (expiry_ms, valid_trades_list)}
        # 存 raw valid trades 而非 stats, 方便 env 合计时二次聚合。
        self._lifetime_cache: dict[str, tuple[int, list[dict]]] = {}

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
        # vertical_overflow='visible': 内容超过终端高度时按自然顺序打印, 溢出的部分留在
        # 终端 scrollback buffer, 用户可向上滚动查看("今日已成交"等靠底部的表)。
        # 默认 'ellipsis' 会把超出部分砍成省略号且丢弃, 且 Live 反复重绘 → 无法向上滚看。
        with Live(self._render(), refresh_per_second=1, console=self.console,
                  screen=False, vertical_overflow="visible") as live:
            while not self._stop.is_set():
                try:
                    live.update(self._render())
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"monitor render error: {e}")
                self._stop.wait(self.refresh)

    # ---------- 累计业绩: 缓存 30s 防止每 5s 全表扫 ----------

    def _valid_trades_for(self, rt) -> list[dict]:
        """拿该账户全历史"真实成交" trades (TP/SL/EXIT)。30s 缓存。"""
        now_ms = int(time.time() * 1000)
        cached = self._lifetime_cache.get(rt.name)
        if cached and cached[0] > now_ms:
            return cached[1]
        try:
            rows = rt.db.list_trades(limit=100_000, account=rt.name)
        except Exception:
            rows = []
        valid = [r for r in rows
                 if str(r.get("exit_reason") or "").upper() in _VALID_EXIT_REASONS]
        self._lifetime_cache[rt.name] = (now_ms + _LIFETIME_CACHE_TTL_MS, valid)
        return valid

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
            # 但 ORPHAN / CANCELLED 计数单独统计, 供面板做"对账器 housekeeping"可视化
            try:
                all_rows = rt.db.list_trades(limit=500, account=rt.name)
                today_all = [r for r in all_rows
                             if (r.get("exit_time") or "")[:10] == today_iso]
                today_filled = [r for r in today_all
                                if (r.get("exit_price") or 0) > 0]
                today_orphan = sum(1 for r in today_all
                                   if str(r.get("exit_reason") or "").upper() == "ORPHAN")
                today_cancelled = sum(1 for r in today_all
                                      if str(r.get("exit_reason") or "").upper() == "CANCELLED")
                # db.pnl 已是净口径; 名义 = 净 + 手续费 + 资金费(反推展示)
                today_net = sum((r.get("pnl") or 0) for r in today_filled)
                today_fee = sum((r.get("fee") or 0) for r in today_filled)
                today_funding = sum((r.get("funding") or 0) for r in today_filled)
                today_pnl = today_net + today_fee + today_funding
            except Exception:
                today_filled, today_pnl, today_fee, today_funding, today_net = [], 0.0, 0.0, 0.0, 0.0
                today_orphan, today_cancelled = 0, 0

            # 累计业绩 (30s 缓存)
            valid_trades = self._valid_trades_for(rt)
            lifetime = _compute_lifetime_stats(valid_trades, current_balance=bal)

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
                "today_orphan": today_orphan,
                "today_cancelled": today_cancelled,
                "lifetime": lifetime,
                "valid_trades": valid_trades,  # env 合计时二次聚合用
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
        total_today_orphan = sum(a.get("today_orphan", 0) for a in snap)
        total_today_cancelled = sum(a.get("today_cancelled", 0) for a in snap)

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
            f"[bold]净盈亏[/bold] {_fmt2(total_today_net)}   "
            f"[bold]今日撤单[/bold] {total_today_cancelled}   "
            f"[bold]今日过期[/bold] {total_today_orphan}",
            f"[bold]运行[/bold] {uptime}   "
            f"[bold]now[/bold] {now.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        )

        # === 账户一览 ===
        acc_tbl = Table(title="账户一览", show_header=True, header_style="bold cyan", expand=True)
        for c in ("账户", "环境", "周期", "余额", "熔断", "连亏", "pending", "持仓",
                  "今日笔数", "名义 PnL", "手续费", "资金费", "净 PnL", "今日撤单", "今日过期"):
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
                str(a.get("today_cancelled", 0)), str(a.get("today_orphan", 0)),
            )
        if not snap:
            acc_tbl.add_row("(无账户)", "", "", "", "", "", "", "", "", "", "", "", "", "", "")

        # === 累计业绩 (全历史 · TP/SL/EXIT · 按环境分组合计) ===
        perf_tbl = Table(title="累计业绩 (全历史)", show_header=True,
                          header_style="bold yellow", expand=True)
        for c in ("账户", "环境", "总笔", "盈单", "亏单", "胜率", "净PnL",
                  "平均盈", "平均亏", "盈亏比", "最大盈", "最大亏", "回撚%"):
            perf_tbl.add_column(c, no_wrap=True)

        def _pf_str(pf: float) -> str:
            if pf == float("inf"):
                return "∞"
            return f"{pf:.2f}"

        def _add_perf_row(name: str, env: str, s: dict, bold: bool = False) -> None:
            pnl_style = "green" if s["net_pnl"] > 0 else ("red" if s["net_pnl"] < 0 else "")
            net_str = _fmt2(s["net_pnl"])
            net_cell = f"[{pnl_style}]{net_str}[/{pnl_style}]" if pnl_style else net_str
            name_cell = f"[bold]{name}[/bold]" if bold else name
            perf_tbl.add_row(
                name_cell, env,
                str(s["total"]), str(s["wins"]), str(s["losses"]),
                f"{s['win_rate']:.1f}%",
                net_cell,
                _fmt2(s["avg_win"], sign=False),
                _fmt2(s["avg_loss"], sign=False),
                _pf_str(s["profit_factor"]),
                _fmt2(s["max_win"]),
                _fmt2(s["max_loss"]),
                f"{s['max_dd_pct']:.1f}%",
            )

        # 按 env 分组: real 在前, demo 在后, 其它兜底
        by_env: dict[str, list[dict]] = {}
        for a in snap:
            env = a["env"] or "unknown"
            by_env.setdefault(env, []).append(a)
        env_order = [e for e in ("real", "live", "demo") if e in by_env] + \
                    [e for e in by_env if e not in ("real", "live", "demo")]

        for env in env_order:
            accts = by_env[env]
            for a in accts:
                _add_perf_row(a["name"], env, a["lifetime"])
            # env 合计: 至少 1 个账户就出, 便于快速看总数; 合并 valid_trades 重算
            merged = []
            for a in accts:
                merged.extend(a["valid_trades"])
            merged_bal = sum(a["balance"] for a in accts)
            agg = _compute_lifetime_stats(merged, current_balance=merged_bal)
            _add_perf_row(f"{env} 合计", "", agg, bold=True)

        if not snap:
            perf_tbl.add_row("(无账户)", "", "", "", "", "", "", "", "", "", "", "", "")

        # === 挂单表 (全账户合并,带账户名列) ===
        pending_tbl = Table(title="待触发挂单 (全账户)", show_header=True,
                             header_style="cyan", expand=True)
        for c in ("账户", "品种", "方向", "触发价", "TP", "SL", "AlgoID"):
            pending_tbl.add_column(c, no_wrap=True)
        any_p = False
        for a in snap:
            for o in a["pendings"]:
                any_p = True
                tp, sl = _pending_tp_sl(o)
                pending_tbl.add_row(
                    a["name"], str(o.get("instId", "")), _dir_zh(o.get("side", "")),
                    str(o.get("triggerPx", "")), tp, sl,
                    str(o.get("algoId", ""))[:18],
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
                _exit_reason_zh(r.get("exit_reason", "")),
                _fmt2(pnl), f"{fee:.4f}", f"{funding:.4f}", net_cell,
            )
        if not any_t:
            trade_tbl.add_row("(无)", "", "", "", "", "", "", "", "", "", "")

        # === 组装 ===
        outer = Table.grid(expand=True)
        outer.add_row(header)
        outer.add_row(acc_tbl)
        outer.add_row(perf_tbl)
        outer.add_row(pending_tbl)
        outer.add_row(pos_tbl)
        outer.add_row(trade_tbl)

        return Panel(outer, title="HighLow Bot · 多账户监控 v2.0",
                     border_style="bright_blue")
