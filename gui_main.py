"""HighLow Bot GUI 入口 (打包为 hlbot.exe, windowed 无控制台)。
源码运行: python gui_main.py
"""
import sys
import traceback


def _show_fatal(msg: str) -> None:
    """windowed exe 无控制台, 致命错误必须弹窗否则用户什么都看不到。"""
    try:
        import tkinter as tk
        import tkinter.messagebox as mb
        root = tk.Tk()
        root.withdraw()
        mb.showerror("HighLow Bot 启动失败", msg)
        root.destroy()
    except Exception:
        print(msg, file=sys.stderr or sys.stdout)


def gui_entry() -> None:
    from utils.paths import bootstrap_user_files
    msg = bootstrap_user_files()
    if msg:
        import tkinter as tk
        import tkinter.messagebox as mb
        root = tk.Tk()
        root.withdraw()
        mb.showinfo("HighLow Bot 首次运行", msg)
        sys.exit(1)

    try:
        from ui.app import run_app
        run_app()
    except Exception:
        _show_fatal(f"界面异常退出:\n\n{traceback.format_exc(limit=8)}")
        sys.exit(1)


if __name__ == "__main__":
    gui_entry()
