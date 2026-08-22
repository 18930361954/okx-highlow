"""HighLow Bot 主窗口 (tkinter)。

四个标签页: 监控 / 配置 / 控制 / 日志。
- 监控数据复用 PositionMonitor._collect() (worker 线程采集 → queue → after 消费)
- 配置编辑走 ui.config_store (ruamel round-trip, 保注释), 保存后提示重启生效
- 运行控制只调既有编排函数 (scheduler pause/resume, main.daily_cancel)

账号以「组」组织 (accounts[].group, 见 ui/config_store.py): 1 组最多 3 个账号,
组只是分类标签 —— 策略全部挂在账号上。三个页面统一按组呈现。
布局全部自适应: ScrollableTree + autosize_columns, 窗口几何存 data/ui_state.json。
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime, timezone
from tkinter import messagebox, ttk

from ui import ui_state
from ui.bridge import BotBridge
from ui.dialogs import AccountDialog, ProxyDialog
from ui.widgets import ScrollableTree, make_labeled_tree
from utils.app_config import ADVANCED_DEFAULTS, ADVANCED_LABELS, GROUP_MAX_ACCOUNTS
from utils.paths import APP_ROOT

_POLL_MS = 500          # UI 消费 queue 的节拍
_SNAPSHOT_SEC = 5.0     # worker 采集间隔
_LOG_TAIL_LINES = 200
_LOG_TAIL_BYTES = 256 * 1024   # 单次最多回读的尾部字节数 (首次加载/轮转后)

_GROUP_PREFIX = "g:"    # 配置/控制页树节点 iid 前缀
_ACC_PREFIX = "a:"


def _app_version() -> str:
    try:
        from version import __version__
        return __version__
    except Exception:
        return "?"


def _fmt(v, nd=2) -> str:
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return str(v or "")


def _fmt_signed(v, nd=2) -> str:
    try:
        return f"{float(v):+,.{nd}f}"
    except (TypeError, ValueError):
        return str(v or "")


def _dir_plain(raw) -> str:
    v = str(raw or "").lower()
    if v in ("long", "buy"):
        return "做多"
    if v in ("short", "sell"):
        return "做空"
    return str(raw or "")


def _pnl_tag(v) -> tuple:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ()
    if v > 0:
        return ("profit",)
    if v < 0:
        return ("loss",)
    return ()


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def _group_label(raw_group: str) -> str:
    from ui.config_store import UNGROUPED_LABEL
    return raw_group or UNGROUPED_LABEL


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.bridge = BotBridge()
        self.snapshots: queue.Queue = queue.Queue()
        self._snap_stop = threading.Event()
        # 界面上新建但还没加账号的组 —— 扁平 accounts 结构下空组无处持久化,
        # 只活在本次会话, 保存时提示用户。
        self._pending_groups: list[str] = []

        root.title(f"HighLow Bot v{_app_version()}")
        root.minsize(ui_state.MIN_W, ui_state.MIN_H)
        ui_state.restore_window(root)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 状态栏先建 (标签页构建过程会写 self.status), side=bottom 先 pack 保证在底部
        self.status = tk.StringVar(value="未启动 — 到「控制」页启动机器人")
        ttk.Label(root, textvariable=self.status, anchor="w",
                  relief="sunken").pack(fill="x", side="bottom")

        import tkinter.font as tkfont
        self._bold_font = tkfont.nametofont("TkDefaultFont").copy()
        self._bold_font.configure(weight="bold")

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.tab_mon = ttk.Frame(self.nb)
        self.tab_cfg = ttk.Frame(self.nb)
        self.tab_ctl = ttk.Frame(self.nb)
        self.tab_log = ttk.Frame(self.nb)
        self.nb.add(self.tab_mon, text="  监控  ")
        self.nb.add(self.tab_cfg, text="  配置  ")
        self.nb.add(self.tab_ctl, text="  控制  ")
        self.nb.add(self.tab_log, text="  日志  ")

        self._build_monitor_tab()
        self._build_config_tab()
        self._build_control_tab()
        self._build_log_tab()

        self.root.after(200, self._restore_sashes)
        threading.Thread(target=self._snapshot_worker, name="gui-snapshot",
                         daemon=True).start()
        self.root.after(_POLL_MS, self._drain_queues)

    def _restore_sashes(self):
        try:
            self.root.update_idletasks()
            ui_state.restore_sashes(self.mon_paned, "monitor")
        except Exception:
            pass

    # ================= 监控页 =================

    def _build_monitor_tab(self):
        f = self.tab_mon
        self._bot_started_at = None

        top = ttk.Frame(f)
        top.pack(fill="x")
        self.mon_header = tk.StringVar(value="(等待数据 — 机器人未启动)")
        ttk.Label(top, textvariable=self.mon_header, anchor="w",
                  font=self._bold_font).pack(fill="x", padx=6, pady=(4, 0))
        self.mon_header2 = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.mon_header2, anchor="w").pack(
            fill="x", padx=6, pady=(0, 4))

        # 四张表放进可拖分栏 — 用户想看哪块就把哪块拉大, 每块内部自带滚动条
        self.mon_paned = ttk.PanedWindow(f, orient="vertical")
        self.mon_paned.pack(fill="both", expand=True, padx=6, pady=3)

        def add_pane(title, cols, headers=None, tree_column=False, weight=1):
            frame, st = make_labeled_tree(
                self.mon_paned, title, cols, headers=headers,
                tree_column=tree_column, height=5, bold_font=self._bold_font)
            self.mon_paned.add(frame, weight=weight)
            return st

        # 收益率列: 本金=起始本金, 总收益率=累计净盈亏/本金, 今日%=今日净/本金,
        # 均笔%=总收益率/笔数, 回撤%=从峰值权益跌下来多少(含持仓浮亏)
        self.tree_acc = add_pane(
            "账户概览 (按组折叠; 组行 = 组合计, 底部 env 合计)",
            ("环境", "周期", "本金", "余额", "权益", "总收益率", "今日%",
             "熔断", "挂单", "持仓", "今日净", "撤/过",
             "总笔", "胜率", "净PnL", "均笔%", "手续费累", "资金费累",
             "盈亏比", "回撤%"),
            tree_column=True, weight=2)
        self.tree_acc.tree.heading("#0", text="组 / 账户")

        # 持仓表: 回报率 = 未实现盈亏 / 该仓占用保证金 (这一笔自己的收益率)
        self.tree_pos = add_pane("当前持仓", (
            "组", "账户", "品种", "方向", "张数", "均价", "现价", "TP", "SL",
            "未实现盈亏", "回报率", "占本金%"))
        self.tree_pos.tag_configure("unprotected", background="#ffd6d6")

        self.tree_pend = add_pane("待触发挂单", (
            "组", "账户", "品种", "周期", "方向", "触发价", "TP", "SL", "AlgoID"))

        # 最近成交: 回报率 = 净盈亏 / 该笔保证金; 占本金% = 净盈亏 / 起始本金
        self.tree_recent = add_pane("最近成交", (
            "时间", "组", "账户", "品种", "周期", "方向", "入场", "出场", "原因",
            "名义PnL", "手续费", "资金费", "净PnL", "回报率", "占本金%"), weight=2)

    def _refresh_monitor(self, snap: list[dict]):
        from execution.position_monitor import (_bar_of, _compute_lifetime_stats,
                                                _exit_reason_zh, _fmt_uptime,
                                                _pending_tp_sl, _pos_pct_cells,
                                                _trade_pct_cells)

        total_bal = sum(a["balance"] for a in snap)
        total_net = sum(a["today_net"] for a in snap)
        total_pnl = sum(a.get("today_pnl", 0.0) for a in snap)
        total_fee = sum(a.get("today_fee", 0.0) for a in snap)
        total_funding = sum(a.get("today_funding", 0.0) for a in snap)
        total_life_fee = sum(a["lifetime"].get("sum_fee", 0.0) for a in snap)
        total_life_funding = sum(a["lifetime"].get("sum_funding", 0.0) for a in snap)
        total_cancelled = sum(a.get("today_cancelled", 0) for a in snap)
        total_orphan = sum(a.get("today_orphan", 0) for a in snap)
        n_pos = sum(len(a["positions"]) for a in snap)
        n_pend = sum(len(a["pendings"]) for a in snap)

        now = datetime.now(timezone.utc)
        uptime = (_fmt_uptime((now - self._bot_started_at).total_seconds())
                  if self._bot_started_at else "-")
        self.mon_header.set(
            f"账户 {len(snap)}   总余额 {_fmt(total_bal)} USDT   挂单 {n_pend}   持仓 {n_pos}   "
            f"今日撤单 {total_cancelled}   今日过期 {total_orphan}   "
            f"运行 {uptime}   {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        self.mon_header2.set(
            f"今日名义 {_fmt_signed(total_pnl)}   "
            f"手续费(今/累) {total_fee:.4f}/{total_life_fee:.4f}   "
            f"资金费(今/累) {total_funding:+.4f}/{total_life_funding:+.4f}   "
            f"今日净盈亏 {_fmt_signed(total_net)}")

        # 记住展开状态, 刷新后还原 (每 5s 重建一次表格, 否则组会自动全收起)
        expanded = {iid for iid in self.tree_acc.get_children()
                    if self.tree_acc.tree.item(iid, "open")}
        for tree in (self.tree_acc, self.tree_pos, self.tree_pend, self.tree_recent):
            tree.clear()

        def _acc_values(env, period, balance, in_cd, pendings, positions,
                        today_net, cancelled, orphan, lt, equity=0.0):
            base = lt.get("baseline") or 0.0
            eq = equity if equity > 0 else balance
            today_pct = (today_net / base * 100) if base > 0 else 0.0
            return (env, period,
                    _fmt(base) if base > 0 else "-",
                    _fmt(balance), _fmt(eq),
                    f"{lt.get('return_pct', 0.0):+.2f}%" if base > 0 else "-",
                    f"{today_pct:+.2f}%" if base > 0 else "-",
                    "是" if in_cd else "否",
                    pendings, positions, _fmt_signed(today_net),
                    f"{cancelled}/{orphan}", lt["total"], f"{lt['win_rate']:.1f}%",
                    _fmt_signed(lt["net_pnl"]),
                    f"{lt.get('avg_trade_pct', 0.0):+.3f}%" if lt["total"] else "-",
                    f"{lt.get('sum_fee', 0.0):.4f}", f"{lt.get('sum_funding', 0.0):+.4f}",
                    _pf_str(lt["profit_factor"]), f"{lt['max_dd_pct']:.1f}%")

        def _agg_row(parent, label, accts, tags, iid=None):
            """一组/一个 env 的合计行 — 复用 _compute_lifetime_stats, 与账号行同口径。"""
            merged_trades = [t for a in accts for t in a["valid_trades"]]
            merged_bal = sum(a["balance"] for a in accts)
            agg = _compute_lifetime_stats(
                merged_trades, current_balance=merged_bal,
                baseline=sum(a.get("baseline") or 0.0 for a in accts),
                equity=sum(a.get("equity") or 0.0 for a in accts))
            envs = sorted({a["env"] or "" for a in accts})
            kw = {"iid": iid} if iid else {}
            return self.tree_acc.insert(
                parent, "end", text=label,
                values=_acc_values(
                    envs[0] if len(envs) == 1 else "混合", "", merged_bal, False,
                    sum(len(a["pendings"]) for a in accts),
                    sum(len(a["positions"]) for a in accts),
                    sum(a["today_net"] for a in accts),
                    sum(a.get("today_cancelled", 0) for a in accts),
                    sum(a.get("today_orphan", 0) for a in accts), agg,
                    equity=sum(a.get("equity") or 0.0 for a in accts)),
                tags=tags, open=True, **kw)

        # ---- 按组分块: 组节点行本身就是组合计 (折叠后仍看得到) ----
        by_group: dict[str, list[dict]] = {}
        for a in snap:
            by_group.setdefault(a.get("group") or "", []).append(a)

        for raw_g, accts in by_group.items():
            # 固定 iid: 每 5s 重建表格, 用自动 iid 的话展开状态永远对不上, 组会一直被收起
            gid = _agg_row("", f"{_group_label(raw_g)}  ({len(accts)} 账号)", accts,
                           ("group_total",), iid=_GROUP_PREFIX + raw_g)
            self.tree_acc.tree.item(gid, open=(gid in expanded) if expanded else True)
            for a in accts:
                lt = a["lifetime"]
                self.tree_acc.insert(
                    gid, "end", text=a["name"],
                    values=_acc_values(
                        a["env"], a["signal_bar"], a["balance"], a["in_cd"],
                        len(a["pendings"]), len(a["positions"]), a["today_net"],
                        a.get("today_cancelled", 0), a.get("today_orphan", 0), lt,
                        equity=a.get("equity") or 0.0),
                    tags=_pnl_tag(lt["net_pnl"]))

        # ---- env 合计留在最底部 (实盘/模拟盘口径不能混) ----
        by_env: dict[str, list[dict]] = {}
        for a in snap:
            by_env.setdefault(a["env"] or "unknown", []).append(a)
        env_order = [e for e in ("real", "live", "demo") if e in by_env] + \
                    [e for e in by_env if e not in ("real", "live", "demo")]
        for env in env_order:
            _agg_row("", f"{env} 合计", by_env[env], ("total",), iid="env:" + env)

        for a in snap:
            g = _group_label(a.get("group") or "")
            for p in a["positions"]:
                tp = sl = ""
                for o in a.get("protect_algos") or []:
                    if (o.get("instId") == p.get("instId")
                            and str(o.get("posSide") or "").lower()
                            == str(p.get("posSide") or "").lower()):
                        tp, sl = _pending_tp_sl(o)
                        break
                unprotected = not tp and not sl
                tags = ("unprotected",) if unprotected else _pnl_tag(p.get("upl"))
                # 回报率 = 浮动盈亏 / 该仓占用保证金(OKX imr); 占本金% = 浮动盈亏 / 起始本金
                roi_cell, base_cell = _pos_pct_cells(p, a.get("baseline") or 0.0)
                self.tree_pos.insert("", "end", values=(
                    g, a["name"], p.get("instId", ""), _dir_plain(p.get("posSide", "")),
                    p.get("pos", ""), p.get("avgPx", ""), p.get("last", ""),
                    tp or "无!", sl or "无!", _fmt_signed(p.get("upl")),
                    roi_cell, base_cell),
                    tags=tags)

            for o in a["pendings"]:
                tp, sl = _pending_tp_sl(o)
                self.tree_pend.insert("", "end", values=(
                    g, a["name"], o.get("instId", ""),
                    _bar_of(o, a.get("pair_bars") or {}),
                    _dir_plain(o.get("side", "")),
                    o.get("triggerPx", ""), tp, sl,
                    str(o.get("algoId", ""))[:18]))

        recent = []
        for a in snap:
            for r in a["valid_trades"]:
                recent.append((_group_label(a.get("group") or ""), a["name"], r,
                               a.get("pair_bars") or {}, a.get("baseline") or 0.0))
        recent.sort(key=lambda x: x[2].get("exit_time") or "", reverse=True)
        for g, name, r, pbars, base in recent[:50]:
            net = r.get("pnl") or 0
            fee = r.get("fee") or 0
            funding = r.get("funding") or 0
            gross = r.get("pnl_gross") or (net + fee - funding)
            # 回报率 = 净盈亏 / 该笔保证金; 占本金% = 净盈亏 / 起始本金
            roi_cell, base_cell = _trade_pct_cells(r, base)
            self.tree_recent.insert("", "end", values=(
                (r.get("exit_time") or "")[:19], g, name, r.get("pair", ""),
                _bar_of(r, pbars), _dir_plain(r.get("side", "")),
                _fmt(r.get("entry_price")), _fmt(r.get("exit_price")),
                _exit_reason_zh(r.get("exit_reason", "")), _fmt_signed(gross),
                f"{fee:.4f}", f"{funding:+.4f}", _fmt_signed(net),
                roi_cell, base_cell),
                tags=_pnl_tag(net))

        for tree in (self.tree_acc, self.tree_pos, self.tree_pend, self.tree_recent):
            tree.autosize()

    # ================= 配置页 =================

    def _build_config_tab(self):
        f = self.tab_cfg
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="重新加载", command=self._cfg_load).pack(side="left")
        ttk.Button(bar, text="保存到 config.yaml",
                   command=self._cfg_save).pack(side="left", padx=6)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="切到模拟盘",
                   command=lambda: self._cfg_switch_env("demo")).pack(side="left")
        ttk.Button(bar, text="切到实盘",
                   command=lambda: self._cfg_switch_env("live")).pack(side="left", padx=6)
        ttk.Label(bar, text="保存后需重启机器人生效 (控制页: 停止 → 启动)",
                  foreground="#a04000").pack(side="left", padx=10)

        # ---- 组/账号 增删改 工具栏 ----
        bar2 = ttk.Frame(f)
        bar2.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(bar2, text="组:").pack(side="left")
        ttk.Button(bar2, text="新建", command=self._cfg_add_group).pack(side="left", padx=2)
        ttk.Button(bar2, text="重命名", command=self._cfg_rename_group).pack(side="left", padx=2)
        ttk.Button(bar2, text="删除", command=self._cfg_delete_group).pack(side="left", padx=2)
        ttk.Separator(bar2, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(bar2, text="账号:").pack(side="left")
        self.btn_acc_add = ttk.Button(bar2, text="添加", command=self._cfg_add_account)
        self.btn_acc_add.pack(side="left", padx=2)
        ttk.Button(bar2, text="编辑", command=self._cfg_edit_account).pack(side="left", padx=2)
        ttk.Button(bar2, text="删除", command=self._cfg_delete_account).pack(side="left", padx=2)
        ttk.Label(bar2, text=f"(1 组最多 {GROUP_MAX_ACCOUNTS} 个账号; "
                             "删除账号 = 配置里注释掉, 历史成交数据保留)",
                  foreground="#666").pack(side="left", padx=10)

        body = ttk.Frame(f)
        body.pack(fill="both", expand=True, padx=6, pady=3)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)

        left = ttk.LabelFrame(body, text="账户 (双击「启用」列切换; 组行双击 = 整组启用/停用)")
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        cols = ("启用", "环境", "策略", "币种")
        self.tree_cfg_acc = ScrollableTree(left, columns=cols, tree_column=True, height=10)
        self.tree_cfg_acc.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        self.tree_cfg_acc.tree.heading("#0", text="组 / 账户")
        self.tree_cfg_acc.tag_configure("group", font=self._bold_font)
        self.tree_cfg_acc.bind_tree("<Double-1>", self._cfg_on_double_click)
        self.tree_cfg_acc.bind_tree("<<TreeviewSelect>>", self._cfg_on_select)

        po_frame = ttk.LabelFrame(body, text="选中账户的 pair 参数 (双击单元格编辑)")
        po_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        po_frame.rowconfigure(0, weight=1)
        po_frame.columnconfigure(0, weight=1)
        po_cols = ("pair", "signal_bar", "mode", "float_pct", "tp_pct", "sl_pct", "leverage")
        po_headers = {"pair": "币种", "signal_bar": "信号周期", "mode": "模式",
                      "float_pct": "浮动价%", "tp_pct": "止盈%", "sl_pct": "止损%",
                      "leverage": "杠杆"}
        self.tree_cfg_po = ScrollableTree(po_frame, columns=po_cols,
                                          headers=po_headers, height=10)
        self.tree_cfg_po.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        self.tree_cfg_po.bind_tree("<Double-1>", self._cfg_edit_po_cell)

        # ---- 代理设置 (全局唯一 1 个) ----
        px = ttk.LabelFrame(f, text="代理设置 (全局唯一, OKX REST 全部走它)")
        px.pack(fill="x", padx=6, pady=4)
        pxb = ttk.Frame(px)
        pxb.pack(fill="x", padx=8, pady=6)
        self.proxy_text = tk.StringVar(value="")
        ttk.Label(pxb, textvariable=self.proxy_text, width=52,
                  anchor="w").pack(side="left")
        self.btn_px_add = ttk.Button(pxb, text="添加", command=self._px_add)
        self.btn_px_add.pack(side="left", padx=3)
        self.btn_px_edit = ttk.Button(pxb, text="编辑", command=self._px_edit)
        self.btn_px_edit.pack(side="left", padx=3)
        self.btn_px_del = ttk.Button(pxb, text="删除", command=self._px_delete)
        self.btn_px_del.pack(side="left", padx=3)
        self.btn_px_test = ttk.Button(pxb, text="测试连通性", command=self._px_test)
        self.btn_px_test.pack(side="left", padx=(12, 3))

        # ---- 运行参数 ----
        adv_frame = ttk.LabelFrame(f, text="运行参数 (留空 = 默认值, 保存进 config.yaml 的 advanced 段)")
        adv_frame.pack(fill="x", padx=6, pady=4)
        self.adv_vars: dict[str, tk.StringVar] = {}
        grid = ttk.Frame(adv_frame)
        grid.pack(fill="x", padx=4, pady=4)
        for i, (key, default) in enumerate(ADVANCED_DEFAULTS.items()):
            r, c = divmod(i, 5)
            label = ADVANCED_LABELS.get(key, key)
            ttk.Label(grid, text=label).grid(row=r, column=c * 2, sticky="e",
                                             padx=(8, 2), pady=2)
            var = tk.StringVar()
            self.adv_vars[key] = var
            ent = ttk.Entry(grid, textvariable=var, width=10)
            ent.grid(row=r, column=c * 2 + 1, sticky="w", pady=2)
            _tip = f"{label} ({key}) — 默认 {default}"
            ent.bind("<FocusIn>", lambda e, t=_tip: self.status.set(t))

        self._cfg_data = None
        self._cfg_load()

    # ---------- 配置页: 加载与渲染 ----------

    def _cfg_load(self):
        from ui import config_store
        try:
            self._cfg_data = config_store.load_raw()
        except Exception as e:
            messagebox.showerror("加载失败", f"config.yaml 读取失败:\n{e}")
            return
        self._pending_groups = []
        self._cfg_render_tree()
        self.tree_cfg_po.clear()
        adv = config_store.get_advanced(self._cfg_data)
        for key, var in self.adv_vars.items():
            var.set(str(adv.get(key, "")))
        self._px_refresh()
        self.status.set("配置已加载")

    def _cfg_render_tree(self, select_iid: str | None = None):
        """重建组/账号树。组节点 iid = 'g:<组名>', 账号节点 iid = 'a:<index>'。"""
        from ui import config_store
        tree = self.tree_cfg_acc
        tree.clear()
        groups = config_store.list_groups(self._cfg_data)
        seen = {g["raw_name"] for g in groups}
        for g in groups:
            accs = g["accounts"]
            n_on = sum(1 for a in accs if a["enabled"])
            envs = sorted({a["env"] for a in accs if a["env"]})
            strats = sorted({a["strategy_name"] for a in accs if a["strategy_name"]})
            coins = sorted({p.split("-")[0] for a in accs for p in a["pairs"]})
            gid = tree.insert("", "end", iid=_GROUP_PREFIX + g["raw_name"],
                              text=f"{g['name']}  ({len(accs)}/{GROUP_MAX_ACCOUNTS})",
                              values=(f"{n_on}/{len(accs)}", ",".join(envs),
                                      ",".join(strats), ",".join(coins)),
                              tags=("group",), open=True)
            for a in accs:
                tree.insert(gid, "end", iid=_ACC_PREFIX + str(a["index"]),
                            text=a["name"],
                            values=("✓" if a["enabled"] else "✗", a["env"],
                                    a["strategy_name"],
                                    ",".join(p.split("-")[0] for p in a["pairs"])))
        # 本次会话新建、还没加账号的空组
        for g in self._pending_groups:
            if g in seen:
                continue
            tree.insert("", "end", iid=_GROUP_PREFIX + g,
                        text=f"{g}  (0/{GROUP_MAX_ACCOUNTS})",
                        values=("0/0", "", "", ""), tags=("group",))
        tree.autosize()
        if select_iid and tree.tree.exists(select_iid):
            tree.tree.selection_set(select_iid)
            tree.tree.see(select_iid)

    # ---------- 配置页: 选中与双击 ----------

    def _cfg_selected(self) -> tuple[str | None, int | None]:
        """返回 (组名, 账号 index)。选中组节点 → (组名, None);
        选中账号 → (该账号所在组, index)。"""
        sel = self.tree_cfg_acc.selection()
        if not sel:
            return None, None
        iid = sel[0]
        if iid.startswith(_GROUP_PREFIX):
            return iid[len(_GROUP_PREFIX):], None
        idx = int(iid[len(_ACC_PREFIX):])
        parent = self.tree_cfg_acc.tree.parent(iid)
        return parent[len(_GROUP_PREFIX):], idx

    def _cfg_on_select(self, _event=None):
        from ui import config_store
        group, idx = self._cfg_selected()
        self.tree_cfg_po.clear()
        # 组满 3 个 → 「添加账号」灰掉
        full = False
        if group is not None and self._cfg_data is not None:
            full = config_store.group_count(self._cfg_data, group) >= GROUP_MAX_ACCOUNTS
        self.btn_acc_add.config(state="disabled" if full else "normal")
        if full:
            self.status.set(f"组 {_group_label(group)} 已满 {GROUP_MAX_ACCOUNTS} 个账号")
        if idx is None:
            return
        po = config_store.get_pair_overrides(self._cfg_data, idx)
        for pair, ov in po.items():
            self.tree_cfg_po.insert("", "end", iid=pair, values=(
                pair.split("-")[0], ov.get("signal_bar", ""), ov.get("mode", ""),
                ov.get("float_pct", ""), ov.get("tp_pct", ""), ov.get("sl_pct", ""),
                ov.get("leverage", "")))
        self.tree_cfg_po.autosize()

    def _cfg_on_double_click(self, event):
        """双击「启用」列: 账号行切自己, 组行切整组。双击账号其它列 = 打开编辑。"""
        if self._cfg_data is None:
            return
        tree = self.tree_cfg_acc.tree
        row = tree.identify_row(event.y)
        col = tree.identify_column(event.x)
        if not row:
            return
        tree.selection_set(row)
        if row.startswith(_GROUP_PREFIX):
            if col == "#1":
                self._cfg_toggle_group(row[len(_GROUP_PREFIX):])
            return
        if col == "#1":
            self._cfg_toggle_account(int(row[len(_ACC_PREFIX):]))
        else:
            self._cfg_edit_account()

    def _cfg_toggle_account(self, idx: int):
        from ui import config_store
        cur = bool(self._cfg_data["accounts"][idx].get("enabled", True))
        config_store.set_account_enabled(self._cfg_data, idx, not cur)
        name = config_store.list_accounts(self._cfg_data)[idx]["name"]
        self._cfg_render_tree(select_iid=_ACC_PREFIX + str(idx))
        self.status.set(f"账户 {name} → {'启用' if not cur else '禁用'} (未保存)")

    def _cfg_toggle_group(self, group: str):
        from ui import config_store
        accs = [a for a in config_store.list_accounts(self._cfg_data)
                if a["group"] == group]
        if not accs:
            self.status.set(f"组 {_group_label(group)} 还没有账号")
            return
        # 有任一未启用 → 全开; 全部已启用 → 全关
        target = not all(a["enabled"] for a in accs)
        n = config_store.set_group_enabled(self._cfg_data, group, target)
        self._cfg_render_tree(select_iid=_GROUP_PREFIX + group)
        self.status.set(f"组 {_group_label(group)}: {n} 个账号 → "
                        f"{'启用' if target else '禁用'} (未保存)")

    # ---------- 配置页: 组的增删改 ----------

    def _cfg_add_group(self):
        from ui import config_store
        if self._cfg_data is None:
            return
        name = _ask_string(self.root, "新建组", "组名:")
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showwarning("组名无效", "组名不能为空")
            return
        if name in config_store.group_names(self._cfg_data) or name in self._pending_groups:
            messagebox.showwarning("组名重复", f"组 {name} 已存在")
            return
        self._pending_groups.append(name)
        self._cfg_render_tree(select_iid=_GROUP_PREFIX + name)
        self.status.set(f"已新建组 {name} — 空组无法写进 config.yaml, 请给它添加账号")
        self._cfg_add_account()

    def _cfg_rename_group(self):
        from ui import config_store
        group, _idx = self._cfg_selected()
        if group is None:
            messagebox.showinfo("提示", "先在左侧选中一个组")
            return
        if not group:
            messagebox.showinfo("提示", "「未分组」不是真实的组, 请直接编辑账号来设置它的组")
            return
        new = _ask_string(self.root, "重命名组", "新组名:", group)
        if new is None:
            return
        new = new.strip()
        if not new or new == group:
            return
        if new in config_store.group_names(self._cfg_data):
            messagebox.showwarning("组名重复", f"组 {new} 已存在")
            return
        n = config_store.rename_group(self._cfg_data, group, new)
        if group in self._pending_groups:
            self._pending_groups[self._pending_groups.index(group)] = new
        self._cfg_render_tree(select_iid=_GROUP_PREFIX + new)
        self.status.set(f"组 {group} → {new} ({n} 个账号, 未保存)")

    def _cfg_delete_group(self):
        from ui import config_store
        group, _idx = self._cfg_selected()
        if group is None:
            messagebox.showinfo("提示", "先在左侧选中一个组")
            return
        accs = [a for a in config_store.list_accounts(self._cfg_data)
                if a["group"] == group]
        if not accs:
            if group in self._pending_groups:
                self._pending_groups.remove(group)
                self._cfg_render_tree()
                self.status.set(f"已移除空组 {group}")
            return
        if not group:
            messagebox.showinfo("提示", "「未分组」不能删除, 请逐个删除其中的账号")
            return
        if not messagebox.askyesno(
                "删除组", f"删除组 {group} 及其 {len(accs)} 个账号?\n\n"
                f"账号: {', '.join(a['name'] for a in accs)}\n\n"
                "配置会被注释掉 (可手动去掉 # 恢复), data/trades.db 里的"
                "历史成交记录全部保留。", icon="warning"):
            return
        n = config_store.delete_group(self._cfg_data, group)
        self._pending_groups = [g for g in self._pending_groups if g != group]
        self._cfg_render_tree()
        self.status.set(f"已删除组 {group} ({n} 个账号注释掉, 未保存)")

    # ---------- 配置页: 账号的增删改 ----------

    def _cfg_all_pairs(self) -> list[str]:
        top = (self._cfg_data or {}).get("strategy") or {}
        pairs = list(top.get("pairs") or [])
        for a in (self._cfg_data or {}).get("accounts") or []:
            for p in a.get("pairs") or []:
                if p not in pairs:
                    pairs.append(str(p))
        return pairs or ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]

    def _cfg_group_choices(self) -> list[str]:
        from ui import config_store
        names = config_store.group_names(self._cfg_data)
        return names + [g for g in self._pending_groups if g not in names]

    def _cfg_add_account(self):
        from ui import config_store
        if self._cfg_data is None:
            return
        group, _idx = self._cfg_selected()
        group = group or ""
        if group and config_store.group_count(self._cfg_data, group) >= GROUP_MAX_ACCOUNTS:
            messagebox.showwarning(
                "组已满", f"组 {group} 已有 {GROUP_MAX_ACCOUNTS} 个账号, "
                          f"一个组最多 {GROUP_MAX_ACCOUNTS} 个。\n"
                          "请新建一个组, 或先删除组内已有账号。")
            return
        taken = {a["name"] for a in config_store.list_accounts(self._cfg_data)}
        res = AccountDialog.show(
            self.root, groups=self._cfg_group_choices(),
            all_pairs=self._cfg_all_pairs(), taken_names=taken,
            initial={"group": group, "env": "demo"}, title="添加账号")
        if res is None:
            return
        try:
            idx = config_store.add_account(self._cfg_data, res["group"], {
                "account_name": res["account_name"],
                "enabled": res["enabled"],
                "strategy_name": res["strategy_name"] or "",
                "api_key": res["api_key"],
                "secret_key": res["secret_key"],
                "passphrase": res["passphrase"],
                "env_adapt": res["env"],
                "pairs": res["pairs"],
            })
        except config_store.GroupFullError as e:
            messagebox.showwarning("组已满", str(e))
            return
        if not res["strategy_name"]:
            config_store.update_account(self._cfg_data, idx, {"strategy_name": None})
        self._apply_strategy_result(idx, res)
        self._pending_groups = [g for g in self._pending_groups if g != res["group"]]
        self._cfg_render_tree(select_iid=_ACC_PREFIX + str(idx))
        self.status.set(f"已添加账号 {res['account_name']} (未保存)")

    def _cfg_edit_account(self):
        from ui import config_store
        _group, idx = self._cfg_selected()
        if idx is None:
            messagebox.showinfo("提示", "先在左侧选中一个账号 (不是组)")
            return
        acc = self._cfg_data["accounts"][idx]
        info = config_store.list_accounts(self._cfg_data)[idx]
        sf = config_store.get_account_strategy_fields(self._cfg_data, idx)
        taken = {a["name"] for a in config_store.list_accounts(self._cfg_data)
                 if a["index"] != idx}
        res = AccountDialog.show(
            self.root, groups=self._cfg_group_choices(),
            all_pairs=self._cfg_all_pairs(), taken_names=taken,
            initial={
                "account_name": info["name"],
                "group": info["group"],
                "enabled": info["enabled"],
                "env": info["env"] or "demo",
                "strategy_name": info["strategy_name"],
                "api_key": str(acc.get("api_key") or ""),
                "secret_key": str(acc.get("secret_key") or ""),
                "passphrase": str(acc.get("passphrase") or ""),
                "pairs": info["pairs"],
                "strategy_fields": sf,
                "pair_overrides": config_store.get_pair_overrides(self._cfg_data, idx),
            },
            title=f"编辑账号 — {info['name']}")
        if res is None:
            return
        # 换组时先检查目标组容量
        if res["group"] != info["group"] and res["group"]:
            if config_store.group_count(self._cfg_data, res["group"]) >= GROUP_MAX_ACCOUNTS:
                messagebox.showwarning(
                    "组已满", f"组 {res['group']} 已有 {GROUP_MAX_ACCOUNTS} 个账号")
                return
        config_store.update_account(self._cfg_data, idx, {
            "account_name": res["account_name"],
            "group": res["group"] or None,
            "enabled": res["enabled"],
            "strategy_name": res["strategy_name"],
            "api_key": res["api_key"],
            "secret_key": res["secret_key"],
            "passphrase": res["passphrase"],
            "env_adapt": res["env"],
            "pairs": res["pairs"],
        })
        # 老配置可能用 name / env 键, 避免和新写的 account_name / env_adapt 打架
        for legacy in ("name", "env"):
            if legacy in acc:
                del acc[legacy]
        self._apply_strategy_result(idx, res)
        self._pending_groups = [g for g in self._pending_groups if g != res["group"]]
        self._cfg_render_tree(select_iid=_ACC_PREFIX + str(idx))
        self._cfg_on_select()
        self.status.set(f"已编辑账号 {res['account_name']} (未保存)")

    def _apply_strategy_result(self, idx: int, res: dict):
        """把对话框里的账户级策略与每币覆盖写回 config (None = 删除该覆盖)。"""
        from ui import config_store
        for key, val in (res.get("strategy_fields") or {}).items():
            config_store.set_account_strategy_field(self._cfg_data, idx, key, val)
        overrides = res.get("pair_overrides") or {}
        for pair, ov in overrides.items():
            for key, val in ov.items():
                config_store.set_pair_override_field(self._cfg_data, idx, pair, key, val)
        # 取消勾选的币种, 连带删掉它的覆盖块
        existing = config_store.get_pair_overrides(self._cfg_data, idx)
        stale = [p for p in existing if p not in overrides]
        if stale:
            po = (self._cfg_data["accounts"][idx].get("strategy") or {}).get("pair_overrides")
            for p in stale:
                if po is not None:
                    po.pop(p, None)
        # 覆盖块被清空 → 删掉空壳, 保持 yaml 干净
        acc_strategy = self._cfg_data["accounts"][idx].get("strategy")
        if acc_strategy is not None:
            po = acc_strategy.get("pair_overrides")
            if po is not None:
                for p in [k for k, v in po.items() if not v]:
                    po.pop(p, None)
                if not po:
                    acc_strategy.pop("pair_overrides", None)
            if not acc_strategy:
                self._cfg_data["accounts"][idx].pop("strategy", None)

    def _cfg_delete_account(self):
        from ui import config_store
        _group, idx = self._cfg_selected()
        if idx is None:
            messagebox.showinfo("提示", "先在左侧选中一个账号 (不是组)")
            return
        info = config_store.list_accounts(self._cfg_data)[idx]
        if not messagebox.askyesno(
                "删除账号", f"删除账号 {info['name']}?\n\n"
                "它在 config.yaml 里会被注释掉 (可手动去掉 # 恢复),\n"
                "data/trades.db 里的历史成交记录全部保留。", icon="warning"):
            return
        config_store.soft_delete_account(self._cfg_data, idx)
        self._cfg_render_tree()
        self.tree_cfg_po.clear()
        self.status.set(f"已删除账号 {info['name']} (注释掉, 未保存)")

    # ---------- 配置页: pair 覆盖单元格编辑 ----------

    _PO_COLS = ("pair", "signal_bar", "mode", "float_pct", "tp_pct", "sl_pct", "leverage")
    _PO_LABELS = {"signal_bar": "信号周期", "mode": "模式", "float_pct": "浮动价%",
                  "tp_pct": "止盈%", "sl_pct": "止损%", "leverage": "杠杆"}

    def _cfg_edit_po_cell(self, event):
        if self._cfg_data is None:
            return
        tree = self.tree_cfg_po.tree
        row = tree.identify_row(event.y)
        col_id = tree.identify_column(event.x)
        if not row or col_id == "#1":
            return  # pair 名不可改
        col_idx = int(col_id[1:]) - 1
        key = self._PO_COLS[col_idx]
        label = self._PO_LABELS.get(key, key)
        _group, acc_idx = self._cfg_selected()
        if acc_idx is None:
            return
        old = tree.set(row, key)
        new = _ask_string(self.root, f"{row} · {label}",
                          f"{label} 新值 (清空 = 删除该覆盖, 回退全局默认):", old)
        if new is None:
            return
        from ui import config_store
        if new.strip() == "":
            value = None
        elif key in ("signal_bar", "mode"):
            value = new.strip()
        elif key == "leverage":
            try:
                value = int(new)
            except ValueError:
                messagebox.showerror("类型错误", f"{label} 需要整数")
                return
        else:
            try:
                value = float(new)
            except ValueError:
                messagebox.showerror("类型错误", f"{label} 需要数字")
                return
        config_store.set_pair_override_field(self._cfg_data, acc_idx, row, key, value)
        tree.set(row, key, "" if value is None else value)
        self.status.set(f"{row} {label} = {value!r} (未保存)")

    # ---------- 配置页: 代理 ----------

    def _px_refresh(self):
        from ui import config_store
        if self._cfg_data is None:
            return
        px = config_store.get_proxy(self._cfg_data)
        url, enabled = px["url"], px["enabled"]
        if not url:
            self.proxy_text.set("未配置代理 — OKX 请求直连")
        else:
            self.proxy_text.set(f"{url}    [{'启用' if enabled else '已配置但停用'}]")
        has = bool(url)
        # 只支持 1 个 → 已有时禁用「添加」
        self.btn_px_add.config(state="disabled" if has else "normal")
        for btn in (self.btn_px_edit, self.btn_px_del, self.btn_px_test):
            btn.config(state="normal" if has else "disabled")

    def _px_add(self):
        from ui import config_store
        res = ProxyDialog.show(self.root, url="", enabled=True, title="添加代理")
        if res is None:
            return
        config_store.set_proxy(self._cfg_data, res["url"], res["enabled"])
        self._px_refresh()
        self.status.set(f"代理已设为 {res['url']} (未保存)")

    def _px_edit(self):
        from ui import config_store
        px = config_store.get_proxy(self._cfg_data)
        res = ProxyDialog.show(self.root, url=px["url"], enabled=px["enabled"],
                               title="编辑代理")
        if res is None:
            return
        config_store.set_proxy(self._cfg_data, res["url"], res["enabled"])
        self._px_refresh()
        self.status.set(f"代理已改为 {res['url']} (未保存)")

    def _px_delete(self):
        from ui import config_store
        if not messagebox.askyesno(
                "删除代理", "删除代理配置?\n\n删除后 OKX 请求直连 —— "
                "如果你所在网络需要代理才能访问 OKX, 机器人会连不上。"):
            return
        config_store.clear_proxy(self._cfg_data)
        self._px_refresh()
        self.status.set("代理已删除 (未保存)")

    def _px_test(self):
        """走当前填的代理打一次 OKX 公共接口。worker 线程跑, 不卡 UI。"""
        from ui import config_store
        from utils.app_config import net
        px = config_store.get_proxy(self._cfg_data)
        url = px["url"]
        if not url:
            return
        base = net(self._cfg_data, "okx_base_url")
        timeout = net(self._cfg_data, "http_timeout_sec")
        self.btn_px_test.config(state="disabled")
        self.status.set(f"正在通过 {url} 测试连接 {base} ...")

        def worker():
            import time
            import requests
            t0 = time.time()
            try:
                r = requests.get(f"{base}/api/v5/public/time",
                                 proxies={"http": url, "https": url},
                                 timeout=timeout)
                ms = (time.time() - t0) * 1000
                if r.status_code == 200:
                    msg = ("ok", f"代理可用: {base} 返回 200, 耗时 {ms:.0f} ms")
                else:
                    msg = ("warn", f"代理连通但 OKX 返回 HTTP {r.status_code} "
                                   f"(耗时 {ms:.0f} ms)")
            except Exception as e:
                msg = ("err", f"代理不可用: {type(e).__name__}: {e}")
            self.root.after(0, lambda: self._px_test_done(msg))

        threading.Thread(target=worker, name="proxy-test", daemon=True).start()

    def _px_test_done(self, msg):
        kind, text = msg
        self.btn_px_test.config(state="normal")
        self.status.set(text)
        if kind == "ok":
            messagebox.showinfo("代理测试", text)
        elif kind == "warn":
            messagebox.showwarning("代理测试", text)
        else:
            messagebox.showerror("代理测试", text)

    # ---------- 配置页: 环境切换与保存 ----------

    def _cfg_switch_env(self, env: str):
        """一键切环境: 启用目标环境全部账户, 禁用其余, 确认后立即保存。"""
        if self._cfg_data is None:
            return
        from ui import config_store
        label = "模拟盘 (demo)" if env == "demo" else "实盘 (live)"
        if env != "demo":
            if not messagebox.askyesno(
                    "切到实盘", "确认切到实盘环境?\n\n将启用全部实盘账户 (真实资金!), "
                    "禁用全部模拟账户。\n保存后重启机器人生效。",
                    icon="warning"):
                return
        else:
            if not messagebox.askyesno(
                    "切到模拟盘", "启用全部模拟账户, 禁用全部实盘账户。\n"
                    "保存后重启机器人生效。继续?"):
                return
        n = config_store.set_env_enabled(self._cfg_data, env)
        if n == 0:
            messagebox.showwarning(
                "无法切换", f"没有可启用的{label}账户 (env 不匹配或 api_key 为空), "
                "config.yaml 未修改。")
            return
        self._cfg_save()
        self._cfg_load()  # 刷新表格勾选状态
        self.status.set(f"已切到{label}: 启用 {n} 个账户, 重启机器人生效")

    def _cfg_save(self):
        if self._cfg_data is None:
            return
        from ui import config_store
        # advanced: 空串 = 删除键回默认; 数字字符串转数值
        for key, var in self.adv_vars.items():
            raw = var.get().strip()
            if raw == "":
                config_store.set_advanced_field(self._cfg_data, key, None)
                continue
            default = ADVANCED_DEFAULTS[key]
            try:
                config_store.set_advanced_field(self._cfg_data, key, type(default)(raw))
            except (TypeError, ValueError):
                label = ADVANCED_LABELS.get(key, key)
                messagebox.showerror("类型错误",
                                     f"{label} = {raw!r} 无法转成 {type(default).__name__}")
                return
        # 保存前校验
        import yaml as _pyyaml
        from utils.app_config import validate_config
        try:
            import io

            from ui.config_store import _yaml
            buf = io.StringIO()
            _yaml.dump(self._cfg_data, buf)
            plain = _pyyaml.safe_load(buf.getvalue())
            errors = validate_config(plain)
        except Exception as e:
            messagebox.showerror("校验异常", str(e))
            return
        if errors:
            messagebox.showerror("校验失败", "\n".join(errors))
            return
        try:
            config_store.save_raw(self._cfg_data)
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return
        # 空组无处落地 (accounts 扁平结构), 提示而不是静默丢
        empty = [g for g in self._pending_groups
                 if config_store.group_count(self._cfg_data, g) == 0]
        extra = (f"\n\n注意: 组 {', '.join(empty)} 还没有账号, 未写入配置 "
                 "(空组无法保存, 给它添加账号后再保存)。" if empty else "")
        messagebox.showinfo("已保存", "config.yaml 已保存 (旧文件备份为 config.yaml.bak)。\n"
                                     "重启机器人后生效: 控制页 停止 → 启动。" + extra)
        self.status.set("配置已保存, 重启生效")

    # ================= 控制页 =================

    def _build_control_tab(self):
        f = self.tab_ctl
        life = ttk.LabelFrame(f, text="机器人")
        life.pack(fill="x", padx=6, pady=6)
        self.btn_start = ttk.Button(life, text="启动", command=self._ctl_start)
        self.btn_start.pack(side="left", padx=6, pady=6)
        self.btn_stop = ttk.Button(life, text="停止", command=self._ctl_stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Label(life, text="停止 = 优雅退出 (撤 job + 停面板, 挂单/持仓不动, OKX 侧继续有效)"
                  ).pack(side="left", padx=10)

        acc = ttk.LabelFrame(f, text="账户级控制 (运行中可用; 选中组 = 对整组生效)")
        acc.pack(fill="both", expand=True, padx=6, pady=6)
        acc.rowconfigure(0, weight=1)
        acc.columnconfigure(0, weight=1)
        cols = ("信号挂单", "环境")
        self.tree_ctl = ScrollableTree(acc, columns=cols, tree_column=True, height=8)
        self.tree_ctl.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.tree_ctl.tree.heading("#0", text="组 / 账户")
        self.tree_ctl.tag_configure("group", font=self._bold_font)

        btns = ttk.Frame(acc)
        btns.grid(row=1, column=0, sticky="ew", padx=4, pady=4)
        ttk.Button(btns, text="暂停信号挂单",
                   command=lambda: self._ctl_pause(True)).pack(side="left", padx=4)
        ttk.Button(btns, text="恢复信号挂单",
                   command=lambda: self._ctl_pause(False)).pack(side="left", padx=4)
        ttk.Button(btns, text="撤销全部挂单",
                   command=self._ctl_cancel_all).pack(side="left", padx=16)
        ttk.Label(btns, text="暂停只停新挂单, 对账/结算继续跑; 撤单不动已成交持仓"
                  ).pack(side="left", padx=8)

    def _ctl_refresh_accounts(self):
        self.tree_ctl.clear()
        for raw_g, names in self.bridge.account_groups():
            gid = self.tree_ctl.insert(
                "", "end", iid=_GROUP_PREFIX + raw_g,
                text=f"{_group_label(raw_g)}  ({len(names)} 账号)",
                values=("", ""), tags=("group",), open=True)
            for name in names:
                paused = name in self.bridge.paused_accounts
                self.tree_ctl.insert(gid, "end", iid=_ACC_PREFIX + name, text=name,
                                     values=("已暂停" if paused else "运行中", ""))
        self.tree_ctl.autosize()

    def _ctl_start(self):
        self.btn_start.config(state="disabled")
        self.status.set("正在启动 (连接 OKX 中, 可能需要几十秒)...")
        self.bridge.start_async()

    def _ctl_stop(self):
        if not messagebox.askyesno("确认停止", "停止机器人?\n(挂单/持仓不撤, OKX 侧继续有效)"):
            return
        self.bridge.stop()

    def _ctl_selected_accounts(self) -> tuple[str, list[str]]:
        """返回 (描述, 账户名列表)。选中组节点 → 组内全部账户。"""
        sel = self.tree_ctl.selection()
        if not sel:
            messagebox.showinfo("提示", "先在表格里选中一个账户或一个组")
            return "", []
        iid = sel[0]
        if iid.startswith(_GROUP_PREFIX):
            raw_g = iid[len(_GROUP_PREFIX):]
            names = [n for g, ns in self.bridge.account_groups() if g == raw_g
                     for n in ns]
            return f"组 {_group_label(raw_g)}", names
        name = iid[len(_ACC_PREFIX):]
        return name, [name]

    def _ctl_pause(self, pause: bool):
        desc, names = self._ctl_selected_accounts()
        if not names:
            return
        total = 0
        for name in names:
            total += (self.bridge.pause_signals(name) if pause
                      else self.bridge.resume_signals(name))
        self._ctl_refresh_accounts()
        self.status.set(f"{desc}: {'暂停' if pause else '恢复'} {total} 个信号 job "
                        f"({len(names)} 个账户)")

    def _ctl_cancel_all(self):
        desc, names = self._ctl_selected_accounts()
        if not names:
            return
        if not messagebox.askyesno(
                "确认撤单", f"撤销 {desc} 的全部待触发挂单?\n\n"
                f"涉及账户: {', '.join(names)}"):
            return
        msgs = [f"{n}: {self.bridge.cancel_all_pending(n)}" for n in names]
        self.status.set(f"{desc} — " + "; ".join(msgs))

    # ================= 日志页 =================

    def _build_log_tab(self):
        f = self.tab_log
        f.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        self.log_text = tk.Text(f, wrap="none", state="disabled",
                                font=("Consolas", 9))
        ys = ttk.Scrollbar(f, orient="vertical", command=self.log_text.yview)
        xs = ttk.Scrollbar(f, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        self._log_pos = 0
        self.root.after(2000, self._tail_log)

    def _tail_log(self):
        try:
            path = APP_ROOT / "logs" / "bot.log"
            if path.exists():
                # TimedRotatingFileHandler 每天把 bot.log 改名归档后新建空文件,
                # 此时 _log_pos 停在旧文件末尾, 直接 seek 会越过新文件 EOF ->
                # read() 恒为空, 日志页永久停更。文件变小即视为已轮转, 从头读。
                size = path.stat().st_size
                if size < self._log_pos:
                    self._log_pos = 0
                # 首次加载/轮转后从 0 读, 断网刷屏时单个文件可达数 MB,
                # 一次性 insert 会冻住 UI。日志页只留 _LOG_TAIL_LINES 行, 读尾部即可。
                if size - self._log_pos > _LOG_TAIL_BYTES:
                    self._log_pos = size - _LOG_TAIL_BYTES
                    truncated = True
                else:
                    truncated = False
                # 必须用二进制读: 文本模式下 seek/tell 的偏移量与 st_size 的字节数
                # 不是一个量纲(中文日志尤甚), 混用会把读取位置带偏。
                with open(path, "rb") as fh:
                    fh.seek(self._log_pos)
                    raw = fh.read()
                    self._log_pos = fh.tell()
                new = raw.decode("utf-8", errors="replace")
                if new:
                    # 从中间字节切入时首行多半是半行(errors=replace 还可能留下乱码),
                    # 丢掉它只损失一行, 换来干净输出。
                    if truncated:
                        nl = new.find("\n")
                        new = new[nl + 1:] if nl >= 0 else ""
                if new:
                    self.log_text.config(state="normal")
                    self.log_text.insert("end", new)
                    # 只保留末尾 N 行
                    lines = int(self.log_text.index("end-1c").split(".")[0])
                    if lines > _LOG_TAIL_LINES:
                        self.log_text.delete("1.0", f"{lines - _LOG_TAIL_LINES}.0")
                    self.log_text.see("end")
                    self.log_text.config(state="disabled")
        except Exception:
            pass
        self.root.after(2000, self._tail_log)

    # ================= 事件循环 =================

    def _snapshot_worker(self):
        """worker 线程: 定期采集监控快照放入 queue。"""
        while not self._snap_stop.is_set():
            try:
                snap = self.bridge.collect_snapshot()
                if snap is not None:
                    self.snapshots.put(snap)
            except Exception:
                pass
            self._snap_stop.wait(_SNAPSHOT_SEC)

    def _drain_queues(self):
        try:
            while True:
                kind, payload = self.bridge.events.get_nowait()
                if kind == "started":
                    self._bot_started_at = datetime.now(timezone.utc)
                    self.status.set(f"运行中: {len(payload)} 个账户 — {', '.join(payload)}")
                    self.btn_start.config(state="disabled")
                    self.btn_stop.config(state="normal")
                    self._ctl_refresh_accounts()
                elif kind == "stopped":
                    self._bot_started_at = None
                    self.status.set("已停止")
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self._ctl_refresh_accounts()
                    self.mon_header.set("(机器人已停止)")
                elif kind == "error":
                    self.status.set("启动失败")
                    self.btn_start.config(state="normal")
                    messagebox.showerror("HighLow Bot", payload)
        except queue.Empty:
            pass
        try:
            snap = None
            while True:  # 只取最新一帧
                snap = self.snapshots.get_nowait()
        except queue.Empty:
            pass
        if snap is not None:
            try:
                self._refresh_monitor(snap)
            except Exception:
                pass
        self.root.after(_POLL_MS, self._drain_queues)

    def _on_close(self):
        if self.bridge.running:
            if not messagebox.askyesno(
                    "退出", "机器人仍在运行。\n退出将优雅停止 (挂单/持仓不动)。继续?"):
                return
        try:
            state = ui_state.capture_window(self.root)
            state["sashes"] = {"monitor": ui_state.capture_sashes(self.mon_paned, 3)}
            ui_state.save(state)
        except Exception:
            pass
        self._snap_stop.set()
        self.bridge.stop()
        self.root.destroy()


def _ask_string(root, title: str, prompt: str, initial: str = "") -> str | None:
    from tkinter import simpledialog
    return simpledialog.askstring(title, prompt, initialvalue=initial, parent=root)


def run_app() -> None:
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)  # 高分屏不模糊
    except Exception:
        pass
    App(root)
    root.mainloop()
