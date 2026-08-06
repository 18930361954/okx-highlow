"""GUI 模态对话框: 账号编辑 / 代理编辑。

约定: 所有对话框校验失败时**不关窗**, 把错误显示在窗内红字提示区,
用户改完可以直接重试 —— 比弹二层 messagebox 再回来友好。
`show()` 返回 dict (确定) 或 None (取消)。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from urllib.parse import urlparse

from core.scheduler import SIGNAL_BAR_HOURS

# 与 strategy/high_low.py:50-53 的三种模式对齐
STRATEGY_MODES = ("trend", "reversal", "fade")
SIGNAL_BARS = tuple(SIGNAL_BAR_HOURS)          # 1D/12H/6H/4H/2H/1H
ENVS = ("demo", "live")
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")

# 每币可覆盖的策略字段 → (中文名, 类型)
_PO_FIELDS = (
    ("signal_bar", "信号周期", "bar"),
    ("mode", "模式", "mode"),
    ("float_pct", "浮动价%", float),
    ("tp_pct", "止盈%", float),
    ("sl_pct", "止损%", float),
    ("leverage", "杠杆", int),
)


class _Modal(tk.Toplevel):
    """居中、模态、Esc 取消的对话框骨架。"""

    def __init__(self, parent, title: str):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(True, False)
        self.result: dict | None = None
        self._err = tk.StringVar(value="")
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda _e: self._on_cancel())

    def _build_footer(self, parent, ok_text="确定"):
        ttk.Label(parent, textvariable=self._err, foreground="#c62828",
                  anchor="w", wraplength=560).pack(fill="x", padx=10, pady=(4, 0))
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=10, pady=8)
        ttk.Button(bar, text="取消", command=self._on_cancel).pack(side="right")
        ttk.Button(bar, text=ok_text, command=self._on_ok).pack(side="right", padx=6)

    def _center_and_wait(self, parent):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()
        self.wait_window(self)
        return self.result

    def _fail(self, msg: str) -> None:
        self._err.set(msg)

    def _on_cancel(self):
        self.result = None
        self.destroy()

    def _on_ok(self):
        raise NotImplementedError


def _parse_num(raw: str, caster, label: str):
    """空串 → None (= 不覆盖/回退默认)。返回 (值, 错误信息)。"""
    raw = (raw or "").strip()
    if raw == "":
        return None, None
    try:
        return caster(raw), None
    except (TypeError, ValueError):
        kind = "整数" if caster is int else "数字"
        return None, f"{label} = {raw!r} 不是合法{kind}"


class AccountDialog(_Modal):
    """新增 / 编辑一个账号。策略参数(账户级 + 三币覆盖)都在这里配。"""

    def __init__(self, parent, groups: list[str], all_pairs: list[str],
                 taken_names: set[str], initial: dict | None = None,
                 title: str = "添加账号"):
        super().__init__(parent, title)
        init = initial or {}
        self._taken = {str(n) for n in taken_names}
        self._all_pairs = list(all_pairs) or ["BTC-USDT-SWAP", "ETH-USDT-SWAP",
                                              "SOL-USDT-SWAP"]

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        # ---------- 基本信息 ----------
        base = ttk.LabelFrame(body, text="基本信息")
        base.pack(fill="x", padx=10, pady=(10, 4))
        grid = ttk.Frame(base)
        grid.pack(fill="x", padx=8, pady=6)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        self.v_name = tk.StringVar(value=str(init.get("account_name") or ""))
        self.v_group = tk.StringVar(value=str(init.get("group") or ""))
        self.v_env = tk.StringVar(value=str(init.get("env") or "demo"))
        self.v_strat = tk.StringVar(value=str(init.get("strategy_name") or ""))
        self.v_enabled = tk.BooleanVar(value=bool(init.get("enabled", False)))

        ttk.Label(grid, text="账户名").grid(row=0, column=0, sticky="e", padx=(0, 4), pady=3)
        ttk.Entry(grid, textvariable=self.v_name).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(grid, text="所属组").grid(row=0, column=2, sticky="e", padx=(12, 4), pady=3)
        # 可编辑 Combobox: 既能选已有组, 也能直接敲一个新组名
        ttk.Combobox(grid, textvariable=self.v_group, values=groups).grid(
            row=0, column=3, sticky="ew", pady=3)

        ttk.Label(grid, text="环境").grid(row=1, column=0, sticky="e", padx=(0, 4), pady=3)
        ttk.Combobox(grid, textvariable=self.v_env, values=ENVS,
                     state="readonly").grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(grid, text="策略名").grid(row=1, column=2, sticky="e", padx=(12, 4), pady=3)
        ttk.Entry(grid, textvariable=self.v_strat).grid(row=1, column=3, sticky="ew", pady=3)

        ttk.Checkbutton(grid, text="启用该账号 (启用必须填 api_key)",
                        variable=self.v_enabled).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # ---------- API 凭证 ----------
        api = ttk.LabelFrame(body, text="OKX API 凭证")
        api.pack(fill="x", padx=10, pady=4)
        ag = ttk.Frame(api)
        ag.pack(fill="x", padx=8, pady=6)
        ag.columnconfigure(1, weight=1)

        self.v_key = tk.StringVar(value=str(init.get("api_key") or ""))
        self.v_sec = tk.StringVar(value=str(init.get("secret_key") or ""))
        self.v_pass = tk.StringVar(value=str(init.get("passphrase") or ""))
        self._secret_entries = []
        for r, (label, var) in enumerate((("api_key", self.v_key),
                                          ("secret_key", self.v_sec),
                                          ("passphrase", self.v_pass))):
            ttk.Label(ag, text=label).grid(row=r, column=0, sticky="e", padx=(0, 4), pady=3)
            ent = ttk.Entry(ag, textvariable=var, show="*")
            ent.grid(row=r, column=1, sticky="ew", pady=3)
            self._secret_entries.append(ent)
        self.v_reveal = tk.BooleanVar(value=False)
        ttk.Checkbutton(ag, text="显示明文", variable=self.v_reveal,
                        command=self._toggle_reveal).grid(row=3, column=1, sticky="w")

        # ---------- 账户级策略 ----------
        strat = ttk.LabelFrame(body, text="账户级策略 (留空 = 沿用 config.yaml 顶层 strategy)")
        strat.pack(fill="x", padx=10, pady=4)
        sg = ttk.Frame(strat)
        sg.pack(fill="x", padx=8, pady=6)
        sf = init.get("strategy_fields") or {}
        self.v_pos = tk.StringVar(value=_s(sf.get("position_pct")))
        self.v_bar = tk.StringVar(value=_s(sf.get("signal_bar")))
        self.v_mode = tk.StringVar(value=_s(sf.get("mode")))
        ttk.Label(sg, text="仓位比例").grid(row=0, column=0, sticky="e", padx=(0, 4))
        ttk.Entry(sg, textvariable=self.v_pos, width=10).grid(row=0, column=1, sticky="w")
        ttk.Label(sg, text="信号周期").grid(row=0, column=2, sticky="e", padx=(12, 4))
        ttk.Combobox(sg, textvariable=self.v_bar, values=("",) + SIGNAL_BARS,
                     width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(sg, text="模式").grid(row=0, column=4, sticky="e", padx=(12, 4))
        ttk.Combobox(sg, textvariable=self.v_mode, values=("",) + STRATEGY_MODES,
                     width=10).grid(row=0, column=5, sticky="w")

        # ---------- 交易币种 + 每币覆盖 ----------
        po = ttk.LabelFrame(
            body, text="交易币种与每币策略覆盖 (勾选 = 该账号跑这个币; 单元格留空 = 沿用账户级)")
        po.pack(fill="both", expand=True, padx=10, pady=4)
        pg = ttk.Frame(po)
        pg.pack(fill="x", padx=8, pady=6)

        for c, (_k, label, _t) in enumerate(_PO_FIELDS):
            ttk.Label(pg, text=label).grid(row=0, column=c + 1, padx=4)

        sel_pairs = set(init.get("pairs") or self._all_pairs)
        overrides = init.get("pair_overrides") or {}
        self.pair_vars: dict[str, tk.BooleanVar] = {}
        self.po_vars: dict[str, dict[str, tk.StringVar]] = {}
        for r, pair in enumerate(self._all_pairs, start=1):
            pv = tk.BooleanVar(value=pair in sel_pairs)
            self.pair_vars[pair] = pv
            ttk.Checkbutton(pg, text=pair.split("-")[0], variable=pv,
                            width=6).grid(row=r, column=0, sticky="w", pady=2)
            ov = overrides.get(pair) or {}
            self.po_vars[pair] = {}
            for c, (key, _label, typ) in enumerate(_PO_FIELDS):
                var = tk.StringVar(value=_s(ov.get(key)))
                self.po_vars[pair][key] = var
                if typ == "bar":
                    w = ttk.Combobox(pg, textvariable=var,
                                     values=("",) + SIGNAL_BARS, width=7)
                elif typ == "mode":
                    w = ttk.Combobox(pg, textvariable=var,
                                     values=("",) + STRATEGY_MODES, width=9)
                else:
                    w = ttk.Entry(pg, textvariable=var, width=9)
                w.grid(row=r, column=c + 1, padx=4, pady=2)

        self._build_footer(body)
        self._toggle_reveal()

    def _toggle_reveal(self):
        show = "" if self.v_reveal.get() else "*"
        for ent in self._secret_entries:
            ent.configure(show=show)

    def _on_ok(self):
        name = self.v_name.get().strip()
        if not name:
            return self._fail("账户名不能为空")
        if name in self._taken:
            return self._fail(f"账户名 {name} 已存在 (账户名必须唯一, 它是日志与 DB 的标识)")
        api_key = self.v_key.get().strip()
        if self.v_enabled.get() and not api_key:
            return self._fail("启用该账号必须填 api_key (否则启动时校验会失败)")

        pairs = [p for p, v in self.pair_vars.items() if v.get()]
        if not pairs:
            return self._fail("至少勾选一个交易币种")

        pos, err = _parse_num(self.v_pos.get(), float, "仓位比例")
        if err:
            return self._fail(err)
        if pos is not None and not (0 < pos <= 1):
            return self._fail(f"仓位比例应在 0~1 之间 (0.05 = 5%), 当前 {pos}")

        pair_overrides: dict[str, dict] = {}
        for pair in pairs:
            ov: dict = {}
            for key, label, typ in _PO_FIELDS:
                raw = self.po_vars[pair][key].get().strip()
                if typ in ("bar", "mode"):
                    ov[key] = raw or None
                    continue
                val, err = _parse_num(raw, typ, f"{pair.split('-')[0]} {label}")
                if err:
                    return self._fail(err)
                ov[key] = val
            pair_overrides[pair] = ov

        self.result = {
            "account_name": name,
            "group": self.v_group.get().strip(),
            "enabled": bool(self.v_enabled.get()),
            "env": self.v_env.get().strip() or "demo",
            "strategy_name": self.v_strat.get().strip() or None,
            "api_key": api_key,
            "secret_key": self.v_sec.get().strip(),
            "passphrase": self.v_pass.get().strip(),
            "pairs": pairs,
            "strategy_fields": {
                "position_pct": pos,
                "signal_bar": self.v_bar.get().strip() or None,
                "mode": self.v_mode.get().strip() or None,
            },
            "pair_overrides": pair_overrides,
        }
        self.destroy()

    @classmethod
    def show(cls, parent, **kw) -> dict | None:
        dlg = cls(parent, **kw)
        return dlg._center_and_wait(parent)


class ProxyDialog(_Modal):
    """代理地址编辑 (全局唯一 1 个)。"""

    def __init__(self, parent, url: str = "", enabled: bool = True,
                 title: str = "添加代理"):
        super().__init__(parent, title)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        f = ttk.Frame(body)
        f.pack(fill="x", padx=12, pady=(12, 4))
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="代理地址").grid(row=0, column=0, sticky="e", padx=(0, 6))
        self.v_url = tk.StringVar(value=str(url or ""))
        ent = ttk.Entry(f, textvariable=self.v_url, width=46)
        ent.grid(row=0, column=1, sticky="ew")
        ent.focus_set()
        self.v_enabled = tk.BooleanVar(value=bool(enabled))
        ttk.Checkbutton(f, text="启用 (关闭 = 保留地址但 OKX 请求直连)",
                        variable=self.v_enabled).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(body, text=f"支持 {'/'.join(PROXY_SCHEMES)}，例: http://127.0.0.1:18081",
                  foreground="#666").pack(fill="x", padx=12, pady=(2, 0), anchor="w")

        self._build_footer(body)

    def _on_ok(self):
        url = self.v_url.get().strip()
        err = validate_proxy_url(url)
        if err:
            return self._fail(err)
        self.result = {"url": url, "enabled": bool(self.v_enabled.get())}
        self.destroy()

    @classmethod
    def show(cls, parent, **kw) -> dict | None:
        dlg = cls(parent, **kw)
        return dlg._center_and_wait(parent)


def validate_proxy_url(url: str) -> str | None:
    """返回错误信息, None = 合法。requests 的 proxies 需要 scheme://host:port。"""
    url = (url or "").strip()
    if not url:
        return "代理地址不能为空 (要停用代理请用「删除」)"
    if "://" not in url:
        return f"缺少协议前缀, 应形如 http://host:port (支持 {'/'.join(PROXY_SCHEMES)})"
    try:
        p = urlparse(url)
    except Exception:
        return "地址无法解析"
    if p.scheme not in PROXY_SCHEMES:
        return f"协议 {p.scheme!r} 不支持, 只能是 {'/'.join(PROXY_SCHEMES)}"
    if not p.hostname:
        return "缺少主机名"
    try:
        port = p.port
    except ValueError:
        return "端口不是合法数字"
    if port is None:
        return "缺少端口, 应形如 http://127.0.0.1:18081"
    if not (0 < port < 65536):
        return f"端口 {port} 超出范围 1-65535"
    return None


def _s(v) -> str:
    """None → 空串 (界面上「空 = 不覆盖」)。"""
    return "" if v is None else str(v)
