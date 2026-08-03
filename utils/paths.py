"""路径锚点唯一来源 — 兼容源码运行与 PyInstaller 打包。

frozen (exe) 模式:
  - app_root(): exe 所在目录。config.yaml/.env/data/logs 全在这, 用户可编辑。
  - bundle_root(): PyInstaller 解包目录 (_internal/), 只读资源 (config.example.yaml)。
源码模式: 两者都是项目根。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_root()))
    return app_root()


APP_ROOT: Path = app_root()


def bootstrap_user_files() -> str | None:
    """frozen 首启引导: 复制缺失的用户文件到 exe 旁。
    优先级: bundle 内 seed/ (个人自用构建: 真实 config/.env/历史 db, 开箱即用)
           → 无 seed 时落 config.example.yaml 模板并要求填写。
    返回需要用户处理的提示消息 (None = 一切就绪可直接启动)。不退出进程 —
    CLI 入口 print 后 exit, GUI 入口弹 messagebox 后退出。"""
    if not is_frozen():
        return None
    seed = bundle_root() / "seed"
    cfg = APP_ROOT / "config.yaml"
    if not cfg.exists():
        seed_cfg = seed / "config.yaml"
        if seed_cfg.exists():
            shutil.copy(seed_cfg, cfg)
        else:
            tmpl = bundle_root() / "config.example.yaml"
            if not tmpl.exists():
                return "缺少 config.yaml 且打包内无模板, 请手动创建后重新启动。"
            shutil.copy(tmpl, cfg)
            return ("首次运行: 已在程序目录生成 config.yaml 模板。"
                    "请填写账户 API key 并按需启用账户后重新启动。")
    env = APP_ROOT / ".env"
    if not env.exists():
        for cand in (seed / ".env", bundle_root() / ".env.example"):
            if cand.exists():
                shutil.copy(cand, env)
                break
    db = APP_ROOT / "data" / "trades.db"
    seed_db = seed / "data" / "trades.db"
    if not db.exists() and seed_db.exists():
        db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(seed_db, db)
    return None


def ensure_user_files() -> None:
    """CLI 版首启引导: 有待办提示则打印并 exit(1)。源码模式 no-op。"""
    msg = bootstrap_user_files()
    if msg:
        print(msg)
        sys.exit(1)
