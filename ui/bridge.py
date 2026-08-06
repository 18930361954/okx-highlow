"""GUI 后端桥: 机器人生命周期 + 数据采集 + 运行控制。

核心逻辑零改动 —— 只调用 main.start_bot / PositionMonitor._collect /
scheduler pause_job / main.daily_cancel 等既有编排函数。
tkinter 单线程约束: 所有耗时操作在 worker 线程跑, 结果放 queue,
UI 线程用 root.after 消费。
"""
from __future__ import annotations

import queue
import threading
import traceback

from utils.paths import APP_ROOT


class BotBridge:
    """GUI 与机器人后台的桥。start/stop 幂等, 线程安全。"""

    def __init__(self):
        self.handle: dict | None = None       # main.start_bot 返回值
        self.monitor = None                    # 仅用 _collect, 不 start 渲染线程
        self.events: queue.Queue = queue.Queue()  # ("started"|"stopped"|"error"|"snapshot", payload)
        self._lock = threading.Lock()
        self._starting = False
        self.paused_accounts: set[str] = set()

    # ---------- 生命周期 ----------

    @property
    def running(self) -> bool:
        return self.handle is not None

    def start_async(self) -> None:
        """后台线程启动机器人 (连 OKX 可能要几十秒)。结果经 events 上报。"""
        with self._lock:
            if self.handle is not None or self._starting:
                return
            self._starting = True
        threading.Thread(target=self._start_worker, name="bot-start", daemon=True).start()

    def _start_worker(self) -> None:
        try:
            from dotenv import load_dotenv
            from data.db import DB
            from execution.position_monitor import PositionMonitor
            from main import load_config, start_bot
            from utils.app_config import adv, validate_config
            from utils.logger import get_logger

            load_dotenv(APP_ROOT / ".env")
            config = load_config()
            errors = validate_config(config)
            if errors:
                self.events.put(("error", "config.yaml 校验失败:\n" + "\n".join(errors)))
                return
            base_logger = get_logger(
                "hl-bot", level=config["system"]["log_level"],
                keep_days=int(config["system"]["log_keep_days"]),
            )
            db = DB(APP_ROOT / config["system"]["db_path"],
                    busy_timeout=adv(config, "db_busy_timeout_sec"))
            handle = start_bot(config, base_logger, db, with_monitor=False)
            # 仅作数据采集器使用 (不 start 渲染线程)
            monitor = PositionMonitor(runtimes=handle["runtimes"], db=db,
                                      logger=base_logger)
            with self._lock:
                self.handle = handle
                self.monitor = monitor
                self._starting = False
            self.events.put(("started", [rt.name for rt in handle["runtimes"]]))
        except SystemExit as e:
            with self._lock:
                self._starting = False
            self.events.put(("error", f"启动失败 (exit {e.code}), 详见 logs/bot.log"))
        except Exception:
            with self._lock:
                self._starting = False
            self.events.put(("error", f"启动异常:\n{traceback.format_exc(limit=5)}"))

    def stop(self) -> None:
        with self._lock:
            handle, self.handle = self.handle, None
            self.monitor = None
            self.paused_accounts.clear()
        if handle is not None:
            try:
                handle["base_logger"].info("[shutdown] GUI stop requested")
            except Exception:
                pass
            handle["shutdown"]()
            self.events.put(("stopped", None))

    # ---------- 数据采集 (worker 线程调用) ----------

    def collect_snapshot(self) -> list[dict] | None:
        """复用 PositionMonitor._collect() 的聚合结果。"""
        mon = self.monitor
        if mon is None:
            return None
        return mon._collect()

    # ---------- 运行控制 ----------

    def pause_signals(self, account: str) -> int:
        """暂停某账户信号挂单 job (对账 reconcile 继续跑保结算)。返回暂停的 job 数。"""
        h = self.handle
        if h is None:
            return 0
        n = 0
        for job in h["sched"].get_jobs():
            if job.id.startswith(f"{account}.signal_"):
                job.pause()
                n += 1
        if n:
            self.paused_accounts.add(account)
        return n

    def resume_signals(self, account: str) -> int:
        h = self.handle
        if h is None:
            return 0
        n = 0
        for job in h["sched"].get_jobs():
            if job.id.startswith(f"{account}.signal_"):
                job.resume()
                n += 1
        self.paused_accounts.discard(account)
        return n

    def cancel_all_pending(self, account: str) -> str:
        """撤某账户全部挂单 — 复用 main.daily_cancel 的编排 (fire_hour=None=全撤)。"""
        h = self.handle
        if h is None:
            return "机器人未运行"
        rt = next((r for r in h["runtimes"] if r.name == account), None)
        if rt is None:
            return f"账户 {account} 不在运行列表"
        from main import daily_cancel
        try:
            daily_cancel(rt, fire_hour=None)
            return "已发起全部撤单"
        except Exception as e:
            return f"撤单失败: {e}"

    def account_names(self) -> list[str]:
        h = self.handle
        return [rt.name for rt in h["runtimes"]] if h else []

    def account_groups(self) -> list[tuple[str, list[str]]]:
        """[(组名, [账户名...])], 保持 runtime 顺序。组为空的归到 '' (界面显示未分组)。"""
        h = self.handle
        if not h:
            return []
        order: list[str] = []
        by_group: dict[str, list[str]] = {}
        for rt in h["runtimes"]:
            g = str(getattr(rt.cfg, "group", "") or "")
            if g not in by_group:
                by_group[g] = []
                order.append(g)
            by_group[g].append(rt.name)
        return [(g, by_group[g]) for g in order]
