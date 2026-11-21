"""Shared daily log policy for CLI and ASGI startup."""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ACCESS_LOGGERS = ("uvicorn.access", "granian.access")

# HTTP client libraries log every request line with its full URL at INFO.
HTTP_CLIENT_LOGGERS = ("httpx", "httpx2", "httpcore")
_QUERY_VALUE_RE = re.compile(r"""([?&][^=&\s"']+=)[^&\s"']*""")
_USERINFO_RE = re.compile(r"(://)[^/\s@]+@")


def redact_url(value: str) -> str:
    """A URL with every query-string value and any user:password removed."""
    return _USERINFO_RE.sub(r"\1REDACTED@", _QUERY_VALUE_RE.sub(r"\1REDACTED", value))


class OutboundUrlRedactFilter(logging.Filter):
    """Keep credentials in outbound URLs out of the log.

    Some hosted services take their API key in the query string. The HTTP
    client then wrote `HTTP Request: POST https://host/path?apiKey=<the key>`
    to the console and to pernix.log on every request (box, 2026-11-21). No
    list of secret parameter names can be complete, so every query value
    goes; the host, path and parameter names stay, which is what a reader of
    the log needs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                a if isinstance(a, (int, float, bool, type(None))) else redact_url(str(a)) for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = redact_url(record.msg)
        return True


def install_outbound_redaction() -> None:
    for name in HTTP_CLIENT_LOGGERS:
        log = logging.getLogger(name)
        if not any(isinstance(f, OutboundUrlRedactFilter) for f in log.filters):
            log.addFilter(OutboundUrlRedactFilter())


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
    # Access lines go to access.log whichever server wrote them.
    handler.addFilter(lambda record: (record.name in ACCESS_LOGGERS) == access)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    return handler


def setup_logging(directory: Path = Path("data/logs")) -> None:
    install_outbound_redaction()
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(console)
    root.addHandler(daily_handler(directory / "pernix.log", access=False))
    root.addHandler(daily_handler(directory / "access.log", access=True))
