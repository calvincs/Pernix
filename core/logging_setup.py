"""Shared daily log policy for CLI and ASGI startup."""

from __future__ import annotations

import gzip
import logging
import os
import shutil
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def _compress(source: str, destination: str) -> None:
    # Keep the original if compression fails; never lose the only copy.
    temp = destination + ".tmp"
    try:
        with open(source, "rb") as src, gzip.open(temp, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(temp, destination)
        os.unlink(source)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def daily_handler(path: Path, *, access: bool) -> TimedRotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(path, when="midnight", backupCount=35, utc=True, encoding="utf-8")
    handler.namer = lambda name: name + ".gz"
    handler.rotator = _compress
    handler.addFilter(lambda record: (record.name == "uvicorn.access") == access)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    return handler


def setup_logging(directory: Path = Path("data/logs")) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(console)
    root.addHandler(daily_handler(directory / "pernix.log", access=False))
    root.addHandler(daily_handler(directory / "access.log", access=True))
