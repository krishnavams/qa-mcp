"""Structured JSON-lines logging to stderr (stdout belongs to the MCP stdio transport)."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from coded_autonomous_qa.safety.masking import mask_value

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": mask_value(record.getMessage()),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload["ctx"] = mask_value(ctx)
        if record.exc_info:
            payload["exc"] = mask_value(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("coded_autonomous_qa")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    prefix = "coded_autonomous_qa"
    return logging.getLogger(name if name.startswith(prefix) else f"{prefix}.{name}")
