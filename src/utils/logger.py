import sys
import time
from collections import deque
from loguru import logger

# In-memory ring buffer for the dashboard live feed
_log_buffer: deque = deque(maxlen=500)


def get_log_buffer() -> list[dict]:
    return list(_log_buffer)


def _buffer_sink(message) -> None:
    record = message.record
    _log_buffer.append({
        "t": record["time"].timestamp(),
        "level": record["level"].name,
        "name": record["name"],
        "msg": record["message"],
    })


def setup_logger(level: str = "INFO") -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=level,
        colorize=True,
    )
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="7 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{line} - {message}",
    )
    logger.add(_buffer_sink, level=level, format="{message}")


__all__ = ["logger", "setup_logger", "get_log_buffer"]
