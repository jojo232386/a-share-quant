"""Load and validate the data-pipeline research configuration."""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError

from aquant.universe import (
    UniverseError,
    VerifiedUniverse,
    load_verified_universe,
    verify_universe,
)


class _DuplicateKeyError(yaml.YAMLError):
    def __init__(self, key: str):
        self.key = key
        super().__init__("duplicate mapping key")


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate mapping keys."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, Hashable):
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found unhashable key",
                key_node.start_mark,
            )
        if key in mapping:
            field = key if isinstance(key, str) and key.isidentifier() else "<unknown>"
            raise _DuplicateKeyError(field)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class ConfigError(ValueError):
    """Raised when the data-pipeline configuration is invalid."""


@dataclass(frozen=True)
class InstrumentConfig:
    """One member selected by a verified research universe."""

    symbol: str
    kind: str


@dataclass(frozen=True)
class DataConfig:
    """Validated settings and exact universe needed by the data pipeline."""

    adjust: str
    mode: str
    start: date
    end: str
    universe: VerifiedUniverse

    def __post_init__(self) -> None:
        validate_data_config(self)

    @property
    def universe_id(self) -> str:
        return self.universe.universe_id

    @property
    def instruments(self) -> tuple[InstrumentConfig, ...]:
        return tuple(
            InstrumentConfig(item.symbol, item.kind)
            for item in self.universe.members
        )

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(item.symbol for item in self.universe.members)


_DOCUMENT_FIELDS = {"pipeline"}
_PIPELINE_FIELDS = {"universe_id", "start", "end", "adjust", "mode"}


def validate_data_config(config: object) -> None:
    """Validate invariants without dispatching through an untrusted config object."""
    if type(config) is not DataConfig:
        raise ConfigError("data config object must be an exact DataConfig")
    if type(config.adjust) is not str or config.adjust != "":
        raise ConfigError('DataConfig.adjust must be explicitly set to ""')
    if type(config.mode) is not str or config.mode != "research_approx":
        raise ConfigError("DataConfig.mode must be research_approx")
    if type(config.start) is not date or config.start != date(2018, 1, 1):
        raise ConfigError("DataConfig.start must be the date 2018-01-01")
    if type(config.end) is not str or config.end != "latest_complete_trading_day":
        raise ConfigError("DataConfig.end must be latest_complete_trading_day")
    if type(config.universe) is not VerifiedUniverse:
        raise ConfigError("DataConfig.universe must be an exact VerifiedUniverse")
    try:
        verify_universe(config.universe)
    except UniverseError as exc:
        raise ConfigError("DataConfig.universe verification failed") from exc


def _require_exact_fields(
    value: dict[Any, Any],
    expected: set[str],
    *,
    config_path: Path,
    field: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(str(key) for key in actual - expected)
        label = "unknown fields" if unknown else "fields mismatch"
        raise ConfigError(
            f"{config_path}: structure validation failed at {field}: "
            f"{label}; missing={missing!r}; unknown={unknown!r}"
        )


def _load_yaml(config_path: Path) -> Any:
    try:
        text = config_path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ConfigError(
            f"{config_path}: UTF-8 decoding failed at file content"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"{config_path}: file read failed") from exc

    try:
        return yaml.load(text, Loader=_UniqueKeySafeLoader)
    except _DuplicateKeyError as exc:
        raise ConfigError(
            f"{config_path}: YAML parsing failed: duplicate mapping key {exc.key!r}"
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path}: YAML parsing failed") from exc


def load_data_config(path: str | Path) -> DataConfig:
    """Load YAML and a separate content-addressed universe definition."""
    config_path = Path(path)
    document = _load_yaml(config_path)
    if not isinstance(document, dict):
        raise ConfigError(
            f"{config_path}: structure validation failed at document"
        )
    _require_exact_fields(
        document,
        _DOCUMENT_FIELDS,
        config_path=config_path,
        field="document",
    )

    pipeline = document.get("pipeline")
    if not isinstance(pipeline, dict):
        raise ConfigError(
            f"{config_path}: structure validation failed at pipeline"
        )
    _require_exact_fields(
        pipeline,
        _PIPELINE_FIELDS,
        config_path=config_path,
        field="pipeline",
    )

    start_value = pipeline.get("start")
    if type(start_value) is not str:
        raise ConfigError(
            f"{config_path}: validation failed at pipeline.start: "
            "must be canonical 2018-01-01"
        )
    try:
        start = date.fromisoformat(start_value)
    except ValueError as exc:
        raise ConfigError(
            f"{config_path}: validation failed at pipeline.start: "
            "must be canonical 2018-01-01"
        ) from exc
    if start_value != start.isoformat() or start != date(2018, 1, 1):
        raise ConfigError(
            f"{config_path}: validation failed at pipeline.start: "
            "must be canonical 2018-01-01"
        )

    end = pipeline.get("end")
    if end != "latest_complete_trading_day":
        raise ConfigError(
            f"{config_path}: validation failed at pipeline.end: "
            "must be latest_complete_trading_day"
        )

    adjust = pipeline.get("adjust")
    if adjust != "":
        raise ConfigError(
            f'{config_path}: validation failed at pipeline.adjust: '
            'must be explicitly set to ""'
        )

    mode = pipeline.get("mode")
    if mode != "research_approx":
        raise ConfigError(
            f"{config_path}: validation failed at pipeline.mode: "
            "must be research_approx"
        )

    universe_id = pipeline.get("universe_id")
    try:
        universe = load_verified_universe(
            config_path.parent / "universes" / f"{universe_id}.json",
            expected_id=universe_id,
        )
    except (UniverseError, OSError, TypeError) as exc:
        raise ConfigError(
            f"{config_path}: validation failed at pipeline.universe_id: "
            "content-addressed universe verification failed"
        ) from exc

    return DataConfig(
        adjust=adjust,
        mode=mode,
        start=start,
        end=end,
        universe=universe,
    )
