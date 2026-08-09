# -*- mode: python ; coding: utf-8 -*-
# HighLow Bot 打包: onedir, 双 exe 共享 _internal/
#   hlbot.exe      GUI 窗口 (windowed, 入口 gui_main.py)
#   hlbot-cli.exe  终端/子命令 (console, 入口 main.py cli())
# 构建: pyinstaller hlbot.spec --noconfirm --clean
#
# ⚠ 个人自用构建: 真实 config.yaml / .env / data/trades.db 一并打进包,
#   首启自动落地到 exe 旁, 开箱即用。产物含 API key 与交易数据, 严禁外发!
import os

from PyInstaller.utils.hooks import copy_metadata

datas = [
    ("config.example.yaml", "."),
    (".env.example", "."),
]
# 个人自用: 真实配置与历史数据随包分发 (存在才打, 缺了不阻塞构建)
for src, dst in (
    ("config.yaml", "seed"),
    (".env", "seed"),
    (os.path.join("data", "trades.db"), os.path.join("seed", "data")),
):
    if os.path.exists(src):
        datas.append((src, dst))
# apscheduler 3.x import 时读自身 metadata, onedir 缺了会 PackageNotFoundError
datas += copy_metadata("apscheduler")

hiddenimports = [
    # main.cli() 用 importlib 动态加载, 静态分析抓不到
    "scripts.daily_report",
    "scripts.sync_balance",
    "scripts.reset_cooldown",
    "scripts.fix_orphan_trades",
    "scripts.refill_fees",
    "scripts.cleanup_before_restart",
    "scripts.cancel_stale_algos",
    "scripts.switch_env",
]

_excludes = [
    # 回测系脚本才用, 运行链路已验证不碰; 排除后体积大减
    "pandas", "numpy", "matplotlib", "scipy",
    "websocket",   # websocket-client: 全项目零 import 的死依赖
    "IPython", "pytest", "_pytest",
]

# 单 Analysis 同时收两个入口: 两个 EXE 从同一 pure/binaries 出, 天然共享 _internal/
a = Analysis(
    ["main.py", "gui_main.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=_excludes,
)
pyz = PYZ(a.pure)

exe_cli = EXE(
    pyz,
    [s for s in a.scripts if s[0] != "gui_main"],
    exclude_binaries=True,
    name="hlbot-cli",
    console=True,          # rich Live 面板 + 子命令输出需要 console
    upx=False,             # 降杀软误报
)
exe_gui = EXE(
    pyz,
    [s for s in a.scripts if s[0] != "main"],
    exclude_binaries=True,
    name="hlbot",
    console=False,         # GUI 窗口, 不弹黑框
    upx=False,
)

coll = COLLECT(
    exe_cli, exe_gui,
    a.binaries, a.datas,
    name="hlbot",
    upx=False,
)
