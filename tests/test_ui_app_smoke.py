"""ui.app 冒烟: 真实例化 App (走完全部 _build_*_tab), 防初始化顺序回归。
无显示环境 (CI) 时跳过。"""
import pytest

from tests.conftest import has_display

pytestmark = pytest.mark.skipif(not has_display(), reason="无显示环境")


GROUPED_CFG = """\
strategy: {pairs: [BTC-USDT-SWAP, ETH-USDT-SWAP]}
system: {log_level: INFO, db_path: data/trades.db}
network: {proxy_enabled: true, proxy_url: "http://127.0.0.1:18081"}
accounts:
  - {account_name: a1, group: 实盘, enabled: true, api_key: k1, env_adapt: live}
  - {account_name: a2, group: 实盘, enabled: false, api_key: k2, env_adapt: live}
  - {account_name: d1, group: 模拟盘, enabled: true, api_key: k3, env_adapt: demo}
  - {account_name: solo, enabled: true, api_key: k4}
"""


@pytest.fixture(scope="module")
def root():
    """整个模块共用一个 Tk root。

    每个测试各自 Tk()+destroy() 的话, 同进程内反复创建销毁在 Windows 上会偶发
    TclError ("tk wasn't installed properly") —— build.bat 是 pytest 失败即中止,
    偶发失败会随机卡住发版。共用一个 root, 每次只清空子控件。
    """
    import tkinter as tk
    r = tk.Tk()
    r.withdraw()
    yield r
    r.destroy()


def _make_app(root, tmp_path, monkeypatch, text=GROUPED_CFG):
    import ui.app as app_mod
    from ui import config_store, ui_state

    cfg = tmp_path / "config.yaml"
    cfg.write_text(text, encoding="utf-8")
    # 隔离: 配置读写 / 日志 tail / UI 偏好都指向临时目录
    monkeypatch.setattr(config_store, "APP_ROOT", tmp_path)
    monkeypatch.setattr(app_mod, "APP_ROOT", tmp_path)
    monkeypatch.setattr(ui_state, "APP_ROOT", tmp_path)

    for child in root.winfo_children():      # 清掉上一个 App 的控件
        child.destroy()
    app = app_mod.App(root)
    return app


@pytest.fixture
def app(root, tmp_path, monkeypatch):
    """默认配置 (含分组与代理) 的 App, 用完停掉采集线程。"""
    a = _make_app(root, tmp_path, monkeypatch)
    yield a
    a._snap_stop.set()


def test_app_constructs_and_destroys(root, tmp_path, monkeypatch):
    a = _make_app(
        root, tmp_path, monkeypatch,
        "strategy: {pairs: [BTC-USDT-SWAP]}\nsystem: {log_level: INFO}\n"
        "accounts:\n  - {account_name: a1, enabled: true, api_key: k}\n")
    try:
        # 状态栏在标签页构建前就绪 (2026-08-01 GUI 首启崩溃回归)
        assert a.status.get()
        # 配置页真的加载了账户
        assert a.tree_cfg_acc.get_children()
        root.update()
    finally:
        a._snap_stop.set()


def test_config_tree_renders_groups_with_account_children(app):
    tree = app.tree_cfg_acc
    top = list(tree.get_children())
    # 三个顶层节点 = 实盘 / 模拟盘 / 未分组, 全是组节点
    assert len(top) == 3
    assert all(iid.startswith("g:") for iid in top)
    labels = [tree.tree.item(i, "text") for i in top]
    assert any("实盘" in x for x in labels)
    assert any("模拟盘" in x for x in labels)
    # 实盘组下挂 2 个账号
    live = next(i for i in top if i == "g:实盘")
    kids = tree.get_children(live)
    assert len(kids) == 2
    assert all(k.startswith("a:") for k in kids)
    assert tree.tree.item(live, "values")[0] == "1/2"   # 启用数/总数


def test_group_toggle_flips_all_members(app):
    from ui import config_store
    app._cfg_toggle_group("实盘")
    accs = {a["name"]: a["enabled"] for a in config_store.list_accounts(app._cfg_data)}
    assert accs["a1"] and accs["a2"]          # 有未启用的 → 全开
    app._cfg_toggle_group("实盘")
    accs = {a["name"]: a["enabled"] for a in config_store.list_accounts(app._cfg_data)}
    assert not accs["a1"] and not accs["a2"]  # 全启用 → 全关
    assert accs["d1"]                          # 别的组不受影响


def test_proxy_section_reflects_config(app):
    assert "127.0.0.1:18081" in app.proxy_text.get()
    # 已有 1 个 → 「添加」禁用 (只支持 1 个)
    assert str(app.btn_px_add["state"]) == "disabled"
    assert str(app.btn_px_edit["state"]) == "normal"


def test_proxy_buttons_when_absent(root, tmp_path, monkeypatch):
    a = _make_app(
        root, tmp_path, monkeypatch,
        "strategy: {pairs: [BTC-USDT-SWAP]}\nsystem: {log_level: INFO}\n"
        "accounts:\n  - {account_name: a1, enabled: true, api_key: k}\n")
    try:
        assert "未配置" in a.proxy_text.get()
        assert str(a.btn_px_add["state"]) == "normal"
        assert str(a.btn_px_del["state"]) == "disabled"
    finally:
        a._snap_stop.set()


def test_monitor_refresh_groups_snapshot(app):
    """带 group 的快照能渲染出组节点 + 组内账号行, 且不崩。"""
    lt = {"total": 2, "win_rate": 50.0, "net_pnl": 1.5, "profit_factor": 1.2,
          "max_dd_pct": 3.0, "sum_fee": 0.1, "sum_funding": 0.0}

    def acct(name, group, env):
        return {"name": name, "group": group, "env": env, "signal_bar": "6H",
                "balance": 100.0, "in_cd": False, "pendings": [], "positions": [],
                "protect_algos": [], "today_net": 1.0, "today_pnl": 1.0,
                "today_fee": 0.0, "today_funding": 0.0, "today_cancelled": 0,
                "today_orphan": 0, "lifetime": dict(lt), "valid_trades": [],
                "pair_bars": {}}

    app._refresh_monitor([acct("a1", "实盘", "live"), acct("a2", "实盘", "live"),
                          acct("d1", "模拟盘", "demo")])
    top = list(app.tree_acc.get_children())
    texts = [app.tree_acc.tree.item(i, "text") for i in top]
    assert any("实盘" in t and "2 账号" in t for t in texts)
    assert any("模拟盘" in t for t in texts)
    assert any("合计" in t for t in texts)     # env 合计行仍在
    live_node = next(i for i, t in zip(top, texts) if "实盘" in t and "账号" in t)
    assert len(app.tree_acc.get_children(live_node)) == 2


def test_monitor_group_nodes_keep_stable_iids(app):
    """组节点 iid 必须稳定 —— 每 5s 重建表格, iid 一变展开状态就丢, 组会一直被收起。"""
    lt = {"total": 0, "win_rate": 0.0, "net_pnl": 0.0, "profit_factor": 1.0,
          "max_dd_pct": 0.0, "sum_fee": 0.0, "sum_funding": 0.0}
    snap = [{"name": "a1", "group": "实盘", "env": "live", "signal_bar": "6H",
             "balance": 1.0, "in_cd": False, "pendings": [], "positions": [],
             "protect_algos": [], "today_net": 0.0, "today_pnl": 0.0,
             "today_fee": 0.0, "today_funding": 0.0, "today_cancelled": 0,
             "today_orphan": 0, "lifetime": dict(lt), "valid_trades": [],
             "pair_bars": {}}]
    app._refresh_monitor(snap)
    first = list(app.tree_acc.get_children())
    app._refresh_monitor(snap)
    assert list(app.tree_acc.get_children()) == first
    assert "g:实盘" in first
