import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = ROOT / ".pytest_tmp"
_TMP.mkdir(exist_ok=True)
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_TMP))

_display: bool | None = None


def has_display() -> bool:
    """有无 GUI 环境。结果缓存 —— 每问一次就 Tk()+destroy() 一次的话,
    同进程内第二次探测在 Windows 上会失败, 导致后面的 UI 测试被误跳过。"""
    global _display
    if _display is None:
        try:
            import tkinter as tk
            root = tk.Tk()
            root.destroy()
            _display = True
        except Exception:
            _display = False
    return _display
