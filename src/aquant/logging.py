"""Small, opt-in JSON event logging helpers."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pandas as pd

_RESERVED_FIELDS = {"timestamp_utc", "level", "event"}
_SENSITIVE_FIELDS = {
    "authorization",
    "proxy_authorization",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "cookie",
    "set_cookie",
    "credentials",
}
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_apikey",
    "_token",
    "_secret",
    "_password",
    "_passwd",
    "_authorization",
    "_cookie",
    "_credentials",
)


def _is_sensitive_field(field_name: str) -> bool:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", field_name)
    normalized = re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")
    compact = normalized.replace("_", "")
    compact_sensitive = {value.replace("_", "") for value in _SENSITIVE_FIELDS}
    return (
        normalized in _SENSITIVE_FIELDS
        or compact in compact_sensitive
        or normalized.endswith(_SENSITIVE_SUFFIXES)
    )


def _safe_netloc(parts) -> str:
    hostname = parts.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{hostname}:{port}" if port is not None else hostname


def _redacted_url(value: str) -> str:
    if "://" in value:
        parts = urlsplit(value)
        return urlunsplit((parts.scheme, _safe_netloc(parts), parts.path, "", ""))
    if value.startswith("//"):
        parts = urlsplit(value)
        return urlunsplit(("", _safe_netloc(parts), parts.path, "", ""))
    if value.startswith(("/", "./", "../", "?", "#")):
        return urlsplit(value).path
    parts = urlsplit(f"//{value}")
    return f"{_safe_netloc(parts)}{parts.path}"


def _is_url_field(field_name: str | None) -> bool:
    return field_name is not None and (
        field_name.lower() in {"url", "endpoint"}
        or field_name.lower().endswith(("_url", "_endpoint"))
    )


def _safe_value(value, *, field_name: str | None = None):
    if isinstance(value, pd.DataFrame):
        raise TypeError("DataFrame values must not be written to structured logs")
    if isinstance(value, BaseException):
        return {"exception_type": type(value).__name__}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            field = str(key)
            result[field] = (
                "[REDACTED]" if _is_sensitive_field(field) else _safe_value(item, field_name=field)
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_value(item, field_name=field_name) for item in value]
    if isinstance(value, str) and (_is_url_field(field_name) or "://" in value):
        return _redacted_url(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("structured log numbers must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported structured log value type: {type(value).__name__}")


class JsonEventFormatter(logging.Formatter):
    """Format helper-created events as one canonical JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "aquant_event", None)
        fields = getattr(record, "aquant_fields", None)
        if not isinstance(event, str) or not event:
            raise ValueError("JSON event records require a non-empty event")
        if not isinstance(fields, Mapping):
            raise ValueError("JSON event records require structured fields")
        payload = {
            "timestamp_utc": datetime.fromtimestamp(record.created, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "event": event,
            **_safe_value(fields),
        }
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


def log_event(logger: logging.Logger | None, severity: int, event_name: str, **fields) -> None:
    """Emit one structured event without configuring any global logger."""
    if not isinstance(event_name, str) or not event_name.strip():
        raise ValueError("event must be a non-empty string")
    reserved = _RESERVED_FIELDS.intersection(fields)
    if reserved:
        raise ValueError(f"structured log fields use reserved names: {sorted(reserved)!r}")
    safe_fields = _safe_value(fields)
    if logger is not None:
        logger.log(
            severity,
            event_name,
            extra={"aquant_event": event_name, "aquant_fields": safe_fields},
        )
