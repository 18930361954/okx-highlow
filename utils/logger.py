import logging
import re
import sys
import time
from logging.handlers import TimedRotatingFileHandler

from utils.paths import APP_ROOT

_LOG_DIR = APP_ROOT / "logs"
try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # exe 被放进只读目录时不炸 import, 让后续 handler 报错给出可读信息
    pass

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_configured: dict[str, logging.Logger] = {}

# 断网时 3 账户 × 每 20s 对账 × 3 次重试 = 每小时约 5000 条几乎全同的 WARNING
# (2026-08-08 断网 7h 产出 21503 行, 其中 21303 行是重试噪声, 有效业务日志仅 200 行)。
# 同一条消息在窗口内重复出现时只放行首条, 窗口结束时补一行汇总。
_DEDUP_WINDOW_SEC = 300
# 变化的部分(尝试次数/退避秒数/端点)归一化掉, 否则 attempt=1/2/3 会被当成三条不同消息
_DEDUP_NORMALIZE = re.compile(r"\(attempt \d+\)|retry in \d+s|\(下轮重试\)")


class _DedupFilter(logging.Filter):
    """把窗口内重复的 WARNING/ERROR 折叠成 1 条 + 1 条汇总。

    INFO 全部放行 —— 业务日志(挂单/成交/对账)每条都要留, 且本来就不刷屏。

    CRITICAL FIX: filter() 返回 False 时，getMessage() 等方法不应再被调用，
    否则在高频日志场景下 record 对象复用会导致状态污染。
    """

    def __init__(self, window_sec: int = _DEDUP_WINDOW_SEC):
        super().__init__()
        self.window = window_sec
        self._seen: dict[str, list] = {}   # key -> [首次时间, 抑制条数]

    def filter(self, record: logging.LogRecord) -> bool:
        # INFO 全部放行
        if record.levelno < logging.WARNING:
            return True

        # 防御：filter 内部异常不应导致日志系统挂掉
        try:
            msg = record.getMessage()
        except Exception:
            # getMessage 失败说明 record 对象本身有问题，但至少让它通过
            return True

        key = _DEDUP_NORMALIZE.sub("", msg)
        now = record.created
        ent = self._seen.get(key)

        # 新窗口或窗口过期：放行并重置计数
        if ent is None or now - ent[0] >= self.window:
            if ent is not None and ent[1] > 0:
                # 上一窗口攒下的抑制数, 挂到这条放行的消息后面一起说清楚
                try:
                    record.msg = f"{msg}  [上一窗口同类已抑制 {ent[1]} 条]"
                    record.args = ()
                except Exception:
                    # 修改 record 失败不应阻止日志输出
                    pass
            self._seen[key] = [now, 0]
            # 防止长期运行下 key 无限增长(端点/错误码组合有限, 但保险)
            if len(self._seen) > 512:
                cutoff = now - self.window
                self._seen = {k: v for k, v in self._seen.items() if v[0] >= cutoff}
            return True

        # 窗口内重复：抑制
        ent[1] += 1
        return False


class _SafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """加固版 TimedRotatingFileHandler，防止 emit/rollover 异常导致日志系统挂掉"""

    def emit(self, record):
        try:
            super().emit(record)
        except Exception:
            # 静默处理，防止日志系统因异常停摆
            # 最坏情况是丢几条日志，但不会导致整个系统失去日志能力
            self.handleError(record)

    def doRollover(self):
        try:
            super().doRollover()
        except Exception:
            # 轮转失败不应导致日志系统完全失效
            # 继续用当前文件，总比没有日志好
            pass


def get_logger(
    name: str = "hl-bot",
    level: str = "INFO",
    keep_days: int = 30,
) -> logging.Logger:
    if name in _configured:
        return _configured[name]

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:
        _configured[name] = logger
        return logger

    formatter = logging.Formatter(_FMT, datefmt=_DATE_FMT)
    # 日志时间戳统一 UTC, 与 signal_date / prev_bucket 等业务字段对齐, 免手动 +/-8 换算
    formatter.converter = time.gmtime

    # 挂在 logger 上而非某个 handler: 分账户 handler 后续动态 addHandler,
    # 挂 logger 才能让主日志与分账户日志一起受益。
    logger.addFilter(_DedupFilter())

    # windowed exe (GUI, --noconsole) 下 sys.stderr 为 None, 不挂 console handler
    # (GUI 日志页直接 tail bot.log, 不依赖 stderr)。
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)

    log_file = _LOG_DIR / "bot.log"
    file_handler = _SafeTimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=keep_days,
        encoding="utf-8",
        utc=True,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    _configured[name] = logger
    return logger


def get_account_file_handler(account_name: str, keep_days: int = 30) -> logging.Handler:
    """给每账户返回一个独立的按天 rotate 的 FileHandler。
    logs/bot_<safe_name>.log,主日志 bot.log 仍会收所有日志。
    """
    safe = re.sub(r"[^\w\-.]", "_", account_name)
    log_file = _LOG_DIR / f"bot_{safe}.log"
    h = _SafeTimedRotatingFileHandler(
        log_file, when="midnight", interval=1,
        backupCount=keep_days, encoding="utf-8", utc=True,
    )
    h.suffix = "%Y-%m-%d"
    fmt = logging.Formatter(_FMT, datefmt=_DATE_FMT)
    fmt.converter = time.gmtime  # UTC 时间戳, 与主 logger 一致
    h.setFormatter(fmt)
    return h
