"""窗口几何 / 分栏位置持久化 (data/ui_state.json)。

不进 config.yaml —— 界面偏好和交易配置分开, 避免 UI 状态污染策略文件。
读取时做屏幕边界校验: 多屏拔掉后窗口跑到看不见的坐标是 tkinter 经典坑。
任何异常都静默回默认 —— UI 偏好丢了无所谓, 绝不能因此启动失败。
"""
from __future__ import annotations

import json
import re

from utils.paths import APP_ROOT

_GEOM_RE = re.compile(r"^(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?$")

DEFAULT_GEOMETRY = "1420x760"
MIN_W, MIN_H = 1100, 640      # 1366x768 笔记本可用


def state_path():
    return APP_ROOT / "data" / "ui_state.json"


def load() -> dict:
    try:
        with open(state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(state: dict) -> None:
    try:
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def sanitize_geometry(geom: str, screen_w: int, screen_h: int) -> str:
    """把保存的几何夹回当前屏幕范围内。返回可直接喂给 root.geometry() 的串。

    - 尺寸小于最小值 → 抬到最小值; 大于屏幕 → 压到屏幕
    - 位置整体跑出屏幕 (换了显示器 / 拔了副屏) → 丢掉位置, 让 tk 自己居中
    """
    m = _GEOM_RE.match(str(geom or "").strip())
    if not m:
        return DEFAULT_GEOMETRY
    w, h = int(m.group(1)), int(m.group(2))
    w = max(MIN_W, min(w, screen_w))
    h = max(MIN_H, min(h, screen_h))
    if m.group(3) is None:
        return f"{w}x{h}"
    x, y = int(m.group(3)), int(m.group(4))
    # 至少留 120x40 像素在屏幕内, 否则标题栏都抓不到
    if x + 120 < 0 or x > screen_w - 120 or y + 40 < 0 or y > screen_h - 40:
        return f"{w}x{h}"
    return f"{w}x{h}+{x}+{y}"


def restore_window(root) -> None:
    """启动时套用上次的窗口尺寸/位置与最大化状态。"""
    st = load()
    geom = sanitize_geometry(st.get("geometry") or DEFAULT_GEOMETRY,
                             root.winfo_screenwidth(), root.winfo_screenheight())
    try:
        root.geometry(geom)
    except Exception:
        root.geometry(DEFAULT_GEOMETRY)
    if st.get("zoomed"):
        try:
            root.state("zoomed")     # Windows 最大化
        except Exception:
            pass


def capture_window(root) -> dict:
    """关窗时读回当前几何。最大化时记标志位, 但几何存还原后的尺寸。"""
    out: dict = {}
    try:
        zoomed = root.state() == "zoomed"
        out["zoomed"] = zoomed
        if zoomed:
            root.state("normal")     # 先还原, 否则拿到的是全屏尺寸
            root.update_idletasks()
        out["geometry"] = root.geometry()
    except Exception:
        pass
    return out


def restore_sashes(paned, key: str) -> None:
    """还原 PanedWindow 分栏位置 (窗口已 update_idletasks 后调用才准)。"""
    pos = load().get("sashes", {}).get(key)
    if not isinstance(pos, list):
        return
    for i, p in enumerate(pos):
        try:
            paned.sashpos(i, int(p))
        except Exception:
            pass


def capture_sashes(paned, n: int) -> list:
    out = []
    for i in range(n):
        try:
            out.append(paned.sashpos(i))
        except Exception:
            break
    return out
