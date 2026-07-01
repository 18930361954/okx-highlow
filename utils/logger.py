import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

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
