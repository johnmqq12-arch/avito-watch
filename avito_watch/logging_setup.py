"""Логирование: консоль + ротация файла."""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import LoggingConfig


def setup_logging(cfg: LoggingConfig) -> None:
    level = getattr(logging, cfg.level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if cfg.file:
        Path(cfg.file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            cfg.file, maxBytes=cfg.max_bytes, backupCount=cfg.backups, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)