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

    # windowed exe (GUI, --noconsole) 下 sys.stderr 为 None, 不挂 console handler
    # (GUI 日志页直接 tail bot.log, 不依赖 stderr)。
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)

    log_file = _LOG_DIR / "bot.log"
    file_handler = TimedRotatingFileHandler(
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
    h = TimedRotatingFileHandler(
        log_file, when="midnight", interval=1,
        backupCount=keep_days, encoding="utf-8", utc=True,
    )
    h.suffix = "%Y-%m-%d"
    fmt = logging.Formatter(_FMT, datefmt=_DATE_FMT)
    fmt.converter = time.gmtime  # UTC 时间戳, 与主 logger 一致
    h.setFormatter(fmt)
    return h
