"""ui.widgets / ui.ui_state: 自适应布局层。

widgets 需要显示环境 (Treeview + 字体测量), ui_state 是纯函数可直接测。
"""
import pytest

from tests.conftest import has_display

# ==================== ui_state (无需显示环境) ====================

from ui import ui_state  # noqa: E402


def test_sanitize_geometry_keeps_valid_value():
    got = ui_state.sanitize_geometry("1500x820+100+60", 1920, 1080)
    assert got == "1500x820+100+60"


def test_sanitize_geometry_clamps_below_minimum():
    got = ui_state.sanitize_geometry("400x300", 1920, 1080)
    assert got == f"{ui_state.MIN_W}x{ui_state.MIN_H}"


def test_sanitize_geometry_clamps_to_screen():
    got = ui_state.sanitize_geometry("3000x2000", 1366, 768)
    assert got == "1366x768"


@pytest.mark.parametrize("geom", [
    "1400x760+5000+100",     # 副屏被拔掉, x 跑到屏幕右外
    "1400x760+100+3000",     # y 跑到屏幕下外
    "1400x760+-2000+100",    # 跑到屏幕左外
])
def test_sanitize_geometry_drops_offscreen_position(geom):
    """位置跑出屏幕时只保留尺寸, 让 tk 自己摆 — 否则窗口永远抓不回来。"""
    got = ui_state.sanitize_geometry(geom, 1920, 1080)
    assert "+" not in got.replace("x", "")
    assert got == "1400x760"


def test_sanitize_geometry_garbage_falls_back_to_default():
    assert ui_state.sanitize_geometry("", 1920, 1080) == ui_state.DEFAULT_GEOMETRY
    assert ui_state.sanitize_geometry("not-a-geometry", 1920, 1080) == \
        ui_state.DEFAULT_GEOMETRY


def test_load_save_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_state, "APP_ROOT", tmp_path)
    ui_state.save({"geometry": "1400x760+10+20", "sashes": {"monitor": [100, 200]}})
    got = ui_state.load()
    assert got["geometry"] == "1400x760+10+20"
    assert got["sashes"]["monitor"] == [100, 200]


def test_load_corrupt_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_state, "APP_ROOT", tmp_path)
    p = tmp_path / "data"
    p.mkdir()
    (p / "ui_state.json").write_text("{not json", encoding="utf-8")
    assert ui_state.load() == {}      # UI 偏好损坏绝不能拖垮启动


# ==================== widgets (需要显示环境) ====================

display = pytest.mark.skipif(not has_display(), reason="无显示环境")


@display
def test_autosize_columns_widens_for_long_content():
    import tkinter as tk

    from ui.widgets import COL_MAX_W, ScrollableTree, autosize_columns

    root = tk.Tk()
    root.withdraw()
    try:
        st = ScrollableTree(root, columns=("a", "b"))
        st.insert("", "end", values=("x", "y"))
        autosize_columns(st.tree)
        narrow = st.tree.column("b", "width")

        st.clear()
        st.insert("", "end", values=("x", "非常非常非常长的一个单元格内容用来撑宽列"))
        autosize_columns(st.tree)
        wide = st.tree.column("b", "width")

        assert wide > narrow
        assert wide <= COL_MAX_W        # 上限生效, 不会把整行挤出屏幕
    finally:
        root.destroy()


@display
def test_autosize_columns_respects_min_width():
    import tkinter as tk

    from ui.widgets import COL_MIN_W, ScrollableTree, autosize_columns

    root = tk.Tk()
    root.withdraw()
    try:
        st = ScrollableTree(root, columns=("a",), headers={"a": ""})
        st.insert("", "end", values=("",))
        autosize_columns(st.tree)
        assert st.tree.column("a", "width") >= COL_MIN_W
    finally:
        root.destroy()


@display
def test_autosize_counts_nested_rows():
    """组节点折叠时子行也要参与量宽 —— 否则展开后内容被截断。"""
    import tkinter as tk

    from ui.widgets import ScrollableTree, autosize_columns

    root = tk.Tk()
    root.withdraw()
    try:
        st = ScrollableTree(root, columns=("a",), tree_column=True)
        gid = st.insert("", "end", text="组", values=("短",))
        st.insert(gid, "end", text="账号", values=("很长很长很长很长的子行内容",))
        st.tree.item(gid, open=False)
        autosize_columns(st.tree, include_tree_column=True)
        assert st.tree.column("a", "width") > 60
    finally:
        root.destroy()
