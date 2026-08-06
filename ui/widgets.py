"""自适应表格控件 (GUI 布局层)。

解决 v1.0.2 之前「表格写死 height/列宽 + 无滚动条 → 数据一多就被截断」:
  - ScrollableTree: Treeview + 纵横滚动条, grid weight 让它随窗口拉伸
  - autosize_columns: 按表头与实际内容量宽 (font.measure), 夹在 [min,max]
纯展示层, 不含任何业务逻辑。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# 列宽上下限 (像素)。上限防单个超长值 (如 AlgoID) 把整行挤出屏幕。
COL_MIN_W = 46
COL_MAX_W = 260
_CELL_PAD = 18       # 单元格左右内边距 + 排序箭头留白
_TREE_COL_INDENT = 22  # show="tree" 时 #0 列每层缩进


class ScrollableTree(ttk.Frame):
    """带滚动条、随父容器伸缩的 Treeview。

    用法与 Treeview 一致 —— self.tree 是真正的控件, 外层 frame 负责布局:
        st = ScrollableTree(parent, columns=cols, tree_column=False)
        st.pack(fill="both", expand=True)
        st.tree.insert(...)
    """

    def __init__(self, parent, columns, headers=None, tree_column=False,
                 height=6, anchor="center", **kw):
        super().__init__(parent, **kw)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        show = "tree headings" if tree_column else "headings"
        self.tree = ttk.Treeview(self, columns=columns, show=show, height=height)
        headers = headers or {}
        for c in columns:
            self.tree.heading(c, text=headers.get(c, c))
            # stretch=True: 窗口变宽时列跟着分摊剩余宽度
            self.tree.column(c, width=90, minwidth=COL_MIN_W,
                             anchor=anchor, stretch=True)
        if tree_column:
            self.tree.column("#0", width=180, minwidth=120, stretch=False)

        ys = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")

        self._tree_column = tree_column

    # 常用 Treeview 方法转发, 免得调用处到处写 .tree
    def insert(self, *a, **kw):
        return self.tree.insert(*a, **kw)

    def delete(self, *a):
        return self.tree.delete(*a)

    def get_children(self, item=""):
        return self.tree.get_children(item)

    def clear(self) -> None:
        self.tree.delete(*self.tree.get_children())

    def selection(self):
        return self.tree.selection()

    def bind_tree(self, seq, func):
        return self.tree.bind(seq, func)

    def tag_configure(self, *a, **kw):
        return self.tree.tag_configure(*a, **kw)

    def autosize(self, min_w: int = COL_MIN_W, max_w: int = COL_MAX_W) -> None:
        autosize_columns(self.tree, min_w=min_w, max_w=max_w,
                         include_tree_column=self._tree_column)


def _iter_rows(tree: ttk.Treeview, item="", depth=0):
    """深度优先遍历全部行 (含折叠的子节点), 返回 (iid, depth)。"""
    for iid in tree.get_children(item):
        yield iid, depth
        yield from _iter_rows(tree, iid, depth + 1)


def autosize_columns(tree: ttk.Treeview, min_w: int = COL_MIN_W,
                     max_w: int = COL_MAX_W, include_tree_column: bool = False) -> None:
    """按表头 + 全部行内容重算每列宽度。数据刷新后调用。

    用当前字体的 measure() 量真实像素宽 —— 中文/英文/数字混排都准,
    比按字符数 × 固定倍数靠谱。
    """
    import tkinter.font as tkfont

    try:
        font = tkfont.nametofont("TkDefaultFont")
    except Exception:
        return

    def w(text: str) -> int:
        try:
            return font.measure(str(text))
        except Exception:
            return len(str(text)) * 7

    cols = tree["columns"]

    if include_tree_column:
        widest = w(tree.heading("#0", "text") or "")
        for iid, depth in _iter_rows(tree):
            widest = max(widest, w(tree.item(iid, "text")) + depth * _TREE_COL_INDENT)
        tree.column("#0", width=min(max(widest + _CELL_PAD + _TREE_COL_INDENT,
                                        min_w), max_w))

    for i, c in enumerate(cols):
        widest = w(tree.heading(c, "text") or "")
        for iid, _ in _iter_rows(tree):
            vals = tree.item(iid, "values")
            if i < len(vals):
                widest = max(widest, w(vals[i]))
        tree.column(c, width=min(max(widest + _CELL_PAD, min_w), max_w))


def make_labeled_tree(parent, title, columns, headers=None, tree_column=False,
                      height=6, bold_font=None):
    """LabelFrame 包一个 ScrollableTree, 并配好红绿盈亏 / 合计行 tag。
    返回 (labelframe, ScrollableTree)。"""
    frame = ttk.LabelFrame(parent, text=title)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    st = ScrollableTree(frame, columns=columns, headers=headers,
                        tree_column=tree_column, height=height)
    st.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
    st.tag_configure("profit", foreground="#0a7d32")
    st.tag_configure("loss", foreground="#c62828")
    if bold_font is not None:
        st.tag_configure("total", font=bold_font, background="#eef2f7")
        st.tag_configure("group_total", font=bold_font, background="#f5f0e6")
        st.tag_configure("group", font=bold_font)
    return frame, st
