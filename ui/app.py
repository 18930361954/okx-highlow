"""HighLow Bot 主窗口 (tkinter)。

四个标签页: 监控 / 配置 / 控制 / 日志。
- 监控数据复用 PositionMonitor._collect() (worker 线程采集 → queue → after 消费)
- 配置编辑走 ui.config_store (ruamel round-trip, 保注释), 保存后提示重启生效
- 运行控制只调既有编排函数 (scheduler pause/resume, main.daily_cancel)
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ui.bridge import BotBridge
from utils.app_config import ADVANCED_DEFAULTS, ADVANCED_LABELS
from utils.paths import APP_ROOT

_POLL_MS = 500          # UI 消费 queue 的节拍
_SNAPSHOT_SEC = 5.0     # worker 采集间隔
_LOG_TAIL_LINES = 200


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


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.bridge = BotBridge()
        self.snapshots: queue.Queue = queue.Queue()
        self._snap_stop = threading.Event()

        root.title(f"HighLow Bot v{_app_version()}")
        root.geometry("1180x720")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 状态栏先建 (标签页构建过程会写 self.status), side=bottom 先 pack 保证在底部
        self.status = tk.StringVar(value="未启动 — 到「控制」页启动机器人")
        ttk.Label(root, textvariable=self.status, anchor="w",
                  relief="sunken").pack(fill="x", side="bottom")

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

        threading.Thread(target=self._snapshot_worker, name="gui-snapshot",
                         daemon=True).start()
        self.root.after(_POLL_MS, self._drain_queues)

    # ================= 监控页 =================

    def _build_monitor_tab(self):
        f = self.tab_mon

        def mk_tree(parent, title, cols, height):
            frame = ttk.LabelFrame(parent, text=title)
            tree = ttk.Treeview(frame, columns=cols, show="headings", height=height)
            for c in cols:
                tree.heading(c, text=c)
                tree.column(c, width=90, anchor="center", stretch=True)
            tree.pack(fill="both", expand=True)
            return frame, tree

        top = ttk.Frame(f); top.pack(fill="x")
        self.mon_header = tk.StringVar(value="(等待数据 — 机器人未启动)")
        ttk.Label(top, textvariable=self.mon_header, anchor="w").pack(fill="x", padx=6, pady=4)

        fr1, self.tree_acc = mk_tree(f, "账户概览", (
            "账户", "环境", "周期", "余额", "熔断", "挂单", "持仓", "今日净", "总笔", "胜率", "净PnL"), 4)
        fr1.pack(fill="x", padx=6, pady=3)

        fr2, self.tree_pos = mk_tree(f, "当前持仓", (
            "账户", "品种", "方向", "张数", "均价", "现价", "TP", "SL", "未实现盈亏"), 4)
        fr2.pack(fill="x", padx=6, pady=3)
        self.tree_pos.tag_configure("unprotected", background="#ffd6d6")

        fr3, self.tree_pend = mk_tree(f, "待触发挂单", (
            "账户", "品种", "方向", "触发价", "TP", "SL"), 5)
        fr3.pack(fill="x", padx=6, pady=3)

        fr4, self.tree_recent = mk_tree(f, "最近成交", (
            "时间", "账户", "品种", "方向", "入场", "出场", "原因", "净PnL"), 5)
        fr4.pack(fill="both", expand=True, padx=6, pady=3)

    def _refresh_monitor(self, snap: list[dict]):
        from execution.position_monitor import _pending_tp_sl

        total_bal = sum(a["balance"] for a in snap)
        total_net = sum(a["today_net"] for a in snap)
        n_pos = sum(len(a["positions"]) for a in snap)
        n_pend = sum(len(a["pendings"]) for a in snap)
        self.mon_header.set(
            f"总余额 {_fmt(total_bal)} USDT   今日净盈亏 {_fmt(total_net)}   "
            f"挂单 {n_pend}   持仓 {n_pos}")

        for tree in (self.tree_acc, self.tree_pos, self.tree_pend, self.tree_recent):
            tree.delete(*tree.get_children())

        for a in snap:
            lt = a["lifetime"]
            self.tree_acc.insert("", "end", values=(
                a["name"], a["env"], a["signal_bar"], _fmt(a["balance"]),
                "是" if a["in_cd"] else "否", len(a["pendings"]), len(a["positions"]),
                _fmt(a["today_net"]), lt["total"], f"{lt['win_rate']:.1f}%",
                _fmt(lt["net_pnl"])))

            for p in a["positions"]:
                tp = sl = ""
                for o in a.get("protect_algos") or []:
                    if (o.get("instId") == p.get("instId")
                            and str(o.get("posSide") or "").lower()
                            == str(p.get("posSide") or "").lower()):
                        tp, sl = _pending_tp_sl(o)
                        break
                unprotected = not tp and not sl
                self.tree_pos.insert("", "end", values=(
                    a["name"], p.get("instId", ""), p.get("posSide", ""),
                    p.get("pos", ""), p.get("avgPx", ""), p.get("last", ""),
                    tp or "无!", sl or "无!", _fmt(p.get("upl"))),
                    tags=("unprotected",) if unprotected else ())

            for o in a["pendings"]:
                tp, sl = _pending_tp_sl(o)
                self.tree_pend.insert("", "end", values=(
                    a["name"], o.get("instId", ""), o.get("side", ""),
                    o.get("triggerPx", ""), tp, sl))

        recent = []
        for a in snap:
            for r in a["valid_trades"]:
                recent.append((a["name"], r))
        recent.sort(key=lambda x: x[1].get("exit_time") or "", reverse=True)
        for name, r in recent[:20]:
            self.tree_recent.insert("", "end", values=(
                (r.get("exit_time") or "")[:19], name, r.get("pair", ""),
                r.get("side", ""), _fmt(r.get("entry_price")),
                _fmt(r.get("exit_price")), r.get("exit_reason", ""),
                _fmt(r.get("pnl"))))

    # ================= 配置页 =================

    def _build_config_tab(self):
        f = self.tab_cfg
        bar = ttk.Frame(f); bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="重新加载", command=self._cfg_load).pack(side="left")
        ttk.Button(bar, text="保存到 config.yaml", command=self._cfg_save).pack(side="left", padx=6)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="切到模拟盘",
                   command=lambda: self._cfg_switch_env("demo")).pack(side="left")
        ttk.Button(bar, text="切到实盘",
                   command=lambda: self._cfg_switch_env("live")).pack(side="left", padx=6)
        ttk.Label(bar, text="保存后需重启机器人生效 (控制页: 停止 → 启动)",
                  foreground="#a04000").pack(side="left", padx=10)

        body = ttk.Frame(f); body.pack(fill="both", expand=True, padx=6, pady=3)

        left = ttk.LabelFrame(body, text="账户 (双击「启用」列切换)")
        left.pack(side="left", fill="both", expand=True)
        cols = ("启用", "账户", "环境", "策略", "币种")
        self.tree_cfg_acc = ttk.Treeview(left, columns=cols, show="headings", height=8)
        for c in cols:
            self.tree_cfg_acc.heading(c, text=c)
            self.tree_cfg_acc.column(c, width=80 if c == "启用" else 150, anchor="center")
        self.tree_cfg_acc.pack(fill="both", expand=True)
        self.tree_cfg_acc.bind("<Double-1>", self._cfg_toggle_enabled)

        po_frame = ttk.LabelFrame(body, text="选中账户的 pair 参数 (双击单元格编辑)")
        po_frame.pack(side="left", fill="both", expand=True, padx=(6, 0))
        po_cols = ("pair", "signal_bar", "mode", "float_pct", "tp_pct", "sl_pct", "leverage")
        po_headers = {"pair": "币种", "signal_bar": "信号周期", "mode": "模式",
                      "float_pct": "浮动价%", "tp_pct": "止盈%", "sl_pct": "止损%",
                      "leverage": "杠杆"}
        self.tree_cfg_po = ttk.Treeview(po_frame, columns=po_cols, show="headings", height=8)
        for c in po_cols:
            self.tree_cfg_po.heading(c, text=po_headers[c])
            self.tree_cfg_po.column(c, width=88, anchor="center")
        self.tree_cfg_po.pack(fill="both", expand=True)
        self.tree_cfg_acc.bind("<<TreeviewSelect>>", self._cfg_show_pair_overrides)
        self.tree_cfg_po.bind("<Double-1>", self._cfg_edit_po_cell)

        adv_frame = ttk.LabelFrame(f, text="运行参数 (留空 = 默认值, 保存进 config.yaml 的 advanced 段)")
        adv_frame.pack(fill="x", padx=6, pady=4)
        self.adv_vars: dict[str, tk.StringVar] = {}
        grid = ttk.Frame(adv_frame); grid.pack(fill="x", padx=4, pady=4)
        for i, (key, default) in enumerate(ADVANCED_DEFAULTS.items()):
            r, c = divmod(i, 4)
            label = ADVANCED_LABELS.get(key, key)
            ttk.Label(grid, text=label).grid(row=r, column=c * 2, sticky="e", padx=(8, 2), pady=2)
            var = tk.StringVar()
            self.adv_vars[key] = var
            ent = ttk.Entry(grid, textvariable=var, width=10)
            ent.grid(row=r, column=c * 2 + 1, sticky="w", pady=2)
            _tip = f"{label} ({key}) — 默认 {default}"
            ent.bind("<FocusIn>", lambda e, t=_tip: self.status.set(t))

        self._cfg_data = None
        self._cfg_load()

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

    def _cfg_load(self):
        from ui import config_store
        try:
            self._cfg_data = config_store.load_raw()
        except Exception as e:
            messagebox.showerror("加载失败", f"config.yaml 读取失败:\n{e}")
            return
        self.tree_cfg_acc.delete(*self.tree_cfg_acc.get_children())
        for acc in config_store.list_accounts(self._cfg_data):
            self.tree_cfg_acc.insert("", "end", iid=str(acc["index"]), values=(
                "✓" if acc["enabled"] else "✗", acc["name"], acc["env"],
                acc["strategy_name"],
                ",".join(p.split("-")[0] for p in acc["pairs"])))
        self.tree_cfg_po.delete(*self.tree_cfg_po.get_children())
        adv = config_store.get_advanced(self._cfg_data)
        for key, var in self.adv_vars.items():
            var.set(str(adv.get(key, "")))
        self.status.set("配置已加载")

    def _cfg_toggle_enabled(self, event):
        if self._cfg_data is None:
            return
        row = self.tree_cfg_acc.identify_row(event.y)
        col = self.tree_cfg_acc.identify_column(event.x)
        if not row or col != "#1":
            return
        from ui import config_store
        idx = int(row)
        cur = bool(self._cfg_data["accounts"][idx].get("enabled", True))
        config_store.set_account_enabled(self._cfg_data, idx, not cur)
        vals = list(self.tree_cfg_acc.item(row, "values"))
        vals[0] = "✓" if not cur else "✗"
        self.tree_cfg_acc.item(row, values=vals)
        self.status.set(f"账户 {vals[1]} → {'启用' if not cur else '禁用'} (未保存)")

    def _cfg_show_pair_overrides(self, _event=None):
        if self._cfg_data is None:
            return
        sel = self.tree_cfg_acc.selection()
        self.tree_cfg_po.delete(*self.tree_cfg_po.get_children())
        if not sel:
            return
        from ui import config_store
        idx = int(sel[0])
        po = config_store.get_pair_overrides(self._cfg_data, idx)
        for pair, ov in po.items():
            self.tree_cfg_po.insert("", "end", iid=pair, values=(
                pair.split("-")[0], ov.get("signal_bar", ""), ov.get("mode", ""),
                ov.get("float_pct", ""), ov.get("tp_pct", ""), ov.get("sl_pct", ""),
                ov.get("leverage", "")))

    _PO_COLS = ("pair", "signal_bar", "mode", "float_pct", "tp_pct", "sl_pct", "leverage")
    _PO_LABELS = {"signal_bar": "信号周期", "mode": "模式", "float_pct": "浮动价%",
                  "tp_pct": "止盈%", "sl_pct": "止损%", "leverage": "杠杆"}

    def _cfg_edit_po_cell(self, event):
        if self._cfg_data is None:
            return
        row = self.tree_cfg_po.identify_row(event.y)
        col_id = self.tree_cfg_po.identify_column(event.x)
        if not row or col_id == "#1":
            return  # pair 名不可改
        col_idx = int(col_id[1:]) - 1
        key = self._PO_COLS[col_idx]
        label = self._PO_LABELS.get(key, key)
        sel_acc = self.tree_cfg_acc.selection()
        if not sel_acc:
            return
        old = self.tree_cfg_po.set(row, key)
        new = _ask_string(self.root, f"{row} · {label}",
                          f"{label} 新值 (清空 = 删除该覆盖, 回退全局默认):", old)
        if new is None:
            return
        from ui import config_store
        acc_idx = int(sel_acc[0])
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
        self.tree_cfg_po.set(row, key, "" if value is None else value)
        self.status.set(f"{row} {label} = {value!r} (未保存)")

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
                messagebox.showerror("类型错误", f"{label} = {raw!r} 无法转成 {type(default).__name__}")
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
        messagebox.showinfo("已保存", "config.yaml 已保存 (旧文件备份为 config.yaml.bak)。\n"
                                     "重启机器人后生效: 控制页 停止 → 启动。")
        self.status.set("配置已保存, 重启生效")

    # ================= 控制页 =================

    def _build_control_tab(self):
        f = self.tab_ctl
        life = ttk.LabelFrame(f, text="机器人")
        life.pack(fill="x", padx=6, pady=6)
        self.btn_start = ttk.Button(life, text="启动", command=self._ctl_start)
        self.btn_start.pack(side="left", padx=6, pady=6)
        self.btn_stop = ttk.Button(life, text="停止", command=self._ctl_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Label(life, text="停止 = 优雅退出 (撤 job + 停面板, 挂单/持仓不动, OKX 侧继续有效)"
                  ).pack(side="left", padx=10)

        acc = ttk.LabelFrame(f, text="账户级控制 (运行中可用)")
        acc.pack(fill="both", expand=True, padx=6, pady=6)
        cols = ("账户", "信号挂单", "操作说明")
        self.tree_ctl = ttk.Treeview(acc, columns=cols, show="headings", height=6)
        for c in cols:
            self.tree_ctl.heading(c, text=c)
            self.tree_ctl.column(c, width=180, anchor="center")
        self.tree_ctl.pack(fill="x", padx=4, pady=4)

        btns = ttk.Frame(acc); btns.pack(fill="x", padx=4, pady=4)
        ttk.Button(btns, text="暂停信号挂单", command=lambda: self._ctl_pause(True)).pack(side="left", padx=4)
        ttk.Button(btns, text="恢复信号挂单", command=lambda: self._ctl_pause(False)).pack(side="left", padx=4)
        ttk.Button(btns, text="撤销全部挂单", command=self._ctl_cancel_all).pack(side="left", padx=16)
        ttk.Label(btns, text="暂停只停新挂单, 对账/结算继续跑; 撤单不动已成交持仓"
                  ).pack(side="left", padx=8)

    def _ctl_refresh_accounts(self):
        self.tree_ctl.delete(*self.tree_ctl.get_children())
        for name in self.bridge.account_names():
            paused = name in self.bridge.paused_accounts
            self.tree_ctl.insert("", "end", iid=name, values=(
                name, "已暂停" if paused else "运行中", ""))

    def _ctl_start(self):
        self.btn_start.config(state="disabled")
        self.status.set("正在启动 (连接 OKX 中, 可能需要几十秒)...")
        self.bridge.start_async()

    def _ctl_stop(self):
        if not messagebox.askyesno("确认停止", "停止机器人?\n(挂单/持仓不撤, OKX 侧继续有效)"):
            return
        self.bridge.stop()

    def _ctl_selected_account(self) -> str | None:
        sel = self.tree_ctl.selection()
        if not sel:
            messagebox.showinfo("提示", "先在表格里选中一个账户")
            return None
        return sel[0]

    def _ctl_pause(self, pause: bool):
        name = self._ctl_selected_account()
        if not name:
            return
        n = (self.bridge.pause_signals(name) if pause
             else self.bridge.resume_signals(name))
        self._ctl_refresh_accounts()
        self.status.set(f"{name}: {'暂停' if pause else '恢复'} {n} 个信号 job")

    def _ctl_cancel_all(self):
        name = self._ctl_selected_account()
        if not name:
            return
        if not messagebox.askyesno("确认撤单", f"撤销 {name} 的全部待触发挂单?"):
            return
        msg = self.bridge.cancel_all_pending(name)
        self.status.set(f"{name}: {msg}")

    # ================= 日志页 =================

    def _build_log_tab(self):
        f = self.tab_log
        self.log_text = tk.Text(f, wrap="none", state="disabled",
                                font=("Consolas", 9))
        ys = ttk.Scrollbar(f, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        self._log_pos = 0
        self.root.after(2000, self._tail_log)

    def _tail_log(self):
        try:
            path = APP_ROOT / "logs" / "bot.log"
            if path.exists():
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(self._log_pos)
                    new = fh.read()
                    self._log_pos = fh.tell()
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
                    self.status.set(f"运行中: {len(payload)} 个账户 — {', '.join(payload)}")
                    self.btn_start.config(state="disabled")
                    self.btn_stop.config(state="normal")
                    self._ctl_refresh_accounts()
                elif kind == "stopped":
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
