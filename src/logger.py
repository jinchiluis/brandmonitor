"""Centralized logging configuration.

All modules should use:
    from src.logger import get_logger
    logger = get_logger(__name__)

Logs go to:
  - console (INFO and above)
  - data/log/<date>/<time>_<pipeline>.log (DEBUG and above), once a pipeline entry
    point calls set_pipeline_log()

Error alerting is deliberately absent: it belongs with scheduled runs, which the MVP
does not have yet.
"""

import logging
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.config import LOG_DIR

_ROOT_NAME = "bm"
_FORMAT = logging.Formatter(
    "%(asctime)s  %(name)-30s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_LOCK = threading.Lock()
_CONFIGURED = False
_PIPELINE_LOG_PATH: Optional[Path] = None


def _setup_root() -> None:
    """Attach the console handler to the bm root logger exactly once."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    with _LOCK:
        if _CONFIGURED:
            return
        root = logging.getLogger(_ROOT_NAME)
        root.setLevel(logging.DEBUG)
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(_FORMAT)
        root.addHandler(ch)
        _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _setup_root()
    return logging.getLogger(f"{_ROOT_NAME}.{name}")


def set_pipeline_log(suffix: str) -> Path:
    """Add a pipeline-specific log file under data/log/.

    Call once at the top of each entry point. Child loggers inherit the handler.
    Repeated calls with the same suffix return the existing path.
    """
    global _PIPELINE_LOG_PATH
    _setup_root()
    root = logging.getLogger(_ROOT_NAME)
    tag = f"_pipeline_{suffix}"
    with _LOCK:
        if getattr(root, tag, False):
            return _PIPELINE_LOG_PATH
        now = datetime.now()
        day_dir = LOG_DIR / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        log_path = day_dir / f"{now.strftime('%H%M%S')}_{suffix}.log"
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_FORMAT)
        root.addHandler(fh)
        setattr(root, tag, True)
        _PIPELINE_LOG_PATH = log_path
    return log_path


def set_verbose(enabled: bool = True) -> None:
    """Lower the console handler to DEBUG for a single run."""
    _setup_root()
    for handler in logging.getLogger(_ROOT_NAME).handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            handler.setLevel(logging.DEBUG if enabled else logging.INFO)


def install_excepthook() -> None:
    """Route uncaught exceptions through the logger so they reach the log file."""
    _logger = get_logger("uncaught")

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        _logger.exception("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _hook
