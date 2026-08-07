"""Private compatibility helpers shared by non-audit command-line modules."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

ErrorFactory = Callable[[str, str], Exception]


class _SafeArgumentParser(argparse.ArgumentParser):
    def __init__(
        self,
        *args,
        error_factory: ErrorFactory,
        invalid_arguments_message: str,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._error_factory = error_factory
        self._invalid_arguments_message = invalid_arguments_message

    def error(self, message: str) -> None:
        raise self._error_factory("invalid_arguments", self._invalid_arguments_message)


def make_safe_argument_parser(
    *,
    error_factory: ErrorFactory,
    invalid_arguments_message: str,
) -> Callable[..., _SafeArgumentParser]:
    """Return an argparse constructor that raises the caller's sanitized error."""
    return partial(
        _SafeArgumentParser,
        error_factory=error_factory,
        invalid_arguments_message=invalid_arguments_message,
    )


def path_beneath(
    root: Path,
    value: str,
    *,
    label: str,
    error_factory: ErrorFactory,
) -> Path:
    """Resolve one path and reject values outside an already-resolved project root."""
    candidate = Path(value)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise error_factory("unsafe_path", f"{label} must stay beneath project root") from exc
    return path


def write_json(stream, payload: Mapping[str, object]) -> None:
    """Emit the established compact, sorted UTF-8 JSON line contract."""
    stream.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    )
