"""ui.app 冒烟: 真实例化 App (走完全部 _build_*_tab), 防初始化顺序回归。
无显示环境 (CI) 时跳过。"""
import pytest


def _has_display() -> bool:
    try:
        import tkinter as tk
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _has_display(), reason="无显示环境")


def test_app_constructs_and_destroys(tmp_path, monkeypatch):
    import tkinter as tk
    import ui.app as app_mod
    from ui import config_store

    # 隔离: 配置页加载指向临时 config, 日志 tail 指向临时目录
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "strategy: {pairs: [BTC-USDT-SWAP]}\nsystem: {log_level: INFO}\n"
        "accounts:\n  - {account_name: a1, enabled: true, api_key: k}\n",
        encoding="utf-8")
    monkeypatch.setattr(config_store, "APP_ROOT", tmp_path)
    monkeypatch.setattr(app_mod, "APP_ROOT", tmp_path)

    root = tk.Tk()
    root.withdraw()
    try:
        app = app_mod.App(root)
        # 状态栏在标签页构建前就绪 (2026-08-01 GUI 首启崩溃回归)
        assert app.status.get()
        # 配置页真的加载了账户
        assert app.tree_cfg_acc.get_children()
        # 事件循环跑一拍不崩
        root.update()
        app._snap_stop.set()
    finally:
        root.destroy()
