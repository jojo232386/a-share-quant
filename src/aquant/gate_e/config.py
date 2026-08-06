"""Canonical, immutable configuration for the v0.2 Gate E replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from aquant.rules import (
    CommissionAssumption,
    VerifiedFeePolicy,
    make_fee_policy,
)

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)

_SYMBOLS = (
    "000001",
    "000858",
    "510300",
    "510500",
    "600030",
    "600036",
    "600519",
    "600900",
    "601166",
    "601318",
)
_STAMP_DUTY_TEXT = (
    ("2008-09-19", "0.001"),
    ("2023-08-28", "0.0005"),
)
_TRANSFER_FEE_TEXT = (
    ("2015-08-01", "0.00002"),
    ("2022-04-29", "0.00001"),
)
_RELEASE_MANIFEST_SHA256 = (
    "9d9ad2ed7c351a9e06d86de6b3edea2221ba6b256de072e3744b478b65ca7422"
)
_UV_LOCK_SHA256 = (
    "c8dfc359f40afde9849f7704dafe5449efe47bdef55fd7e29da4ef35214ae712"
)
_UNIVERSE_ID = (
    "ef1a155c791be3f92c41c465da169c9a8c21cbc6981c01a2351f45d72441d130"
)
_CALENDAR_ID = (
    "2a00e22557afcb6e320c09650e1fb3a55ab324fac88b006c5c03e6e7532050bc"
)
_FEE_POLICY_DIGEST = (
    "6935d9e8727417370a69dd97c021514f5517b4f22107fb89b548145195dfa782"
)
_RELEASE_CLOSURE_DIGEST = (
    "1900dc63f2a1e6ed17bf361547161b16880cc839a6fdf765998fb8e810cd7ad1"
)

GATE_E_CONFIG_KEYS = frozenset(
    {
        "calendar_id",
        "corporate_action_manifest",
        "corporate_action_snapshots",
        "end_date",
        "etf_commission_rate",
        "etf_minimum_commission_yuan",
        "fee_policy_digest",
        "fee_schema_version",
        "gate",
        "gross_target_weight",
        "initial_cash_fen",
        "input_files",
        "manifest",
        "market_snapshots",
        "max_entry_attempts",
        "output",
        "portfolio_schema_version",
        "post_end_validation_date",
        "project_name",
        "project_version",
        "python_version",
        "release_manifest_sha256",
        "schema_version",
        "signal_date",
        "stamp_duty_schedule",
        "stock_commission_rate",
        "stock_minimum_commission_yuan",
        "strategy",
        "symbols",
        "transfer_fee_schedule",
        "universe_id",
        "uv_lock_sha256",
    }
)


class GateEConfigError(RuntimeError):
    """Stable fail-closed error for a Gate E configuration."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def canonical_config_bytes(payload: dict[str, object]) -> bytes:
    """Return the one accepted JSON serialization for the release config."""
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return f"{serialized}\n".encode()
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise GateEConfigError(
            "invalid_config",
            "Gate E config cannot be serialized canonically",
        ) from exc


def _read_regular_single_link(path: Path) -> bytes:
    absolute = path if path.is_absolute() else Path.cwd() / path
    components = tuple(
        component
        for component in absolute.parts
        if component not in {os.sep, "."}
    )
    if (
        not absolute.is_absolute()
        or not components
        or any(component in {"", ".."} for component in components)
    ):
        raise GateEConfigError(
            "unsafe_config_file",
            "Gate E config path is unsafe",
        )

    current = -1
    descriptor = -1
    try:
        current = os.open(os.sep, _DIRECTORY_FLAGS | _NOFOLLOW)
        for index, component in enumerate(components):
            final_component = index == len(components) - 1
            flags = _FILE_FLAGS if final_component else _DIRECTORY_FLAGS
            next_descriptor = -1
            try:
                next_descriptor = os.open(
                    component,
                    flags | _NOFOLLOW,
                    dir_fd=current,
                )
                metadata = os.fstat(next_descriptor)
                safe = (
                    stat.S_ISREG(metadata.st_mode)
                    and metadata.st_nlink == 1
                    if final_component
                    else stat.S_ISDIR(metadata.st_mode)
                )
                if not safe:
                    raise GateEConfigError(
                        "unsafe_config_file",
                        "Gate E config path is unsafe",
                    )
                if final_component:
                    descriptor = next_descriptor
                else:
                    previous = current
                    current = next_descriptor
                    os.close(previous)
                next_descriptor = -1
            finally:
                if next_descriptor >= 0:
                    os.close(next_descriptor)
        initial = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        content = b"".join(chunks)
        final = os.fstat(descriptor)
        path_metadata = os.stat(
            components[-1],
            dir_fd=current,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(final.st_mode)
            or not stat.S_ISREG(path_metadata.st_mode)
            or final.st_nlink != 1
            or path_metadata.st_nlink != 1
            or (initial.st_dev, initial.st_ino)
            != (final.st_dev, final.st_ino)
            or (initial.st_dev, initial.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
            or initial.st_size != final.st_size
            or initial.st_mtime_ns != final.st_mtime_ns
            or initial.st_ctime_ns != final.st_ctime_ns
            or len(content) != final.st_size
        ):
            raise GateEConfigError(
                "unsafe_config_file",
                "Gate E config binding changed while it was read",
            )
        return content
    except GateEConfigError:
        raise
    except OSError as exc:
        raise GateEConfigError(
            "unsafe_config_file",
            "Gate E config cannot be opened safely",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if current >= 0:
            os.close(current)


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _require_exact_keys(payload: dict[str, object]) -> None:
    if set(payload) != GATE_E_CONFIG_KEYS:
        raise GateEConfigError(
            "config_key_mismatch",
            "Gate E config keys differ from the approved contract",
        )


def _sha256_text(value: object) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise GateEConfigError(
            "invalid_sha256",
            "Gate E identity must be a lowercase SHA-256 value",
        )
    return value


def _decimal_text(value: object) -> Decimal:
    if type(value) is not str or _DECIMAL_RE.fullmatch(value) is None:
        raise GateEConfigError(
            "invalid_decimal_text",
            "Gate E decimal fields must be canonical strings",
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise GateEConfigError(
            "invalid_decimal_text",
            "Gate E decimal fields must be canonical strings",
        ) from exc
    if not parsed.is_finite():
        raise GateEConfigError(
            "invalid_decimal_text",
            "Gate E decimal fields must be finite",
        )
    return parsed


def _positive_integer(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise GateEConfigError(
            "invalid_integer",
            "Gate E integer fields must be positive exact integers",
        )
    return value


def _date_text(value: object) -> date:
    if type(value) is not str:
        raise GateEConfigError(
            "invalid_date",
            "Gate E dates must be ISO strings",
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise GateEConfigError(
            "invalid_date",
            "Gate E dates must be ISO strings",
        ) from exc
    if parsed.isoformat() != value:
        raise GateEConfigError(
            "invalid_date",
            "Gate E dates must be canonical ISO strings",
        )
    return parsed


def _safe_relative_path(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or "\x00" in value
        or "\\" in value
    ):
        raise GateEConfigError(
            "unsafe_path",
            "Gate E paths must be safe POSIX relative paths",
        )
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or parsed.as_posix() != value
        or not parsed.parts
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise GateEConfigError(
            "unsafe_path",
            "Gate E paths must be safe POSIX relative paths",
        )
    return value


def _schedule(
    value: object,
    *,
    expected: tuple[tuple[str, str], ...],
) -> tuple[tuple[date, Decimal], ...]:
    if type(value) is not list or len(value) != len(expected):
        raise GateEConfigError(
            "unexpected_config",
            "Gate E fee schedule differs from the approved contract",
        )
    parsed: list[tuple[date, Decimal]] = []
    raw: list[tuple[str, str]] = []
    for item in value:
        if type(item) is not list or len(item) != 2:
            raise GateEConfigError(
                "unexpected_config",
                "Gate E fee schedule differs from the approved contract",
            )
        effective = _date_text(item[0])
        rate = _decimal_text(item[1])
        parsed.append((effective, rate))
        raw.append((item[0], item[1]))
    if tuple(raw) != expected or any(
        left[0] >= right[0]
        for left, right in zip(parsed, parsed[1:], strict=False)
    ):
        raise GateEConfigError(
            "unexpected_config",
            "Gate E fee schedule differs from the approved contract",
        )
    return tuple(parsed)


def _symbol_mapping(
    value: object,
    *,
    field: str,
) -> dict[str, str]:
    if type(value) is not dict or tuple(value) != _SYMBOLS:
        raise GateEConfigError(
            "symbol_contract_mismatch",
            f"Gate E {field} symbols differ from the approved contract",
        )
    return {symbol: _sha256_text(value[symbol]) for symbol in _SYMBOLS}


def _input_file_mapping(value: object) -> dict[str, str]:
    if type(value) is not dict or len(value) != 25:
        raise GateEConfigError(
            "release_closure_mismatch",
            "Gate E input closure must contain exactly 25 files",
        )
    result: dict[str, str] = {}
    for raw_path, raw_digest in value.items():
        path = _safe_relative_path(raw_path)
        if path in result:
            raise GateEConfigError(
                "release_closure_mismatch",
                "Gate E input closure contains duplicate paths",
            )
        result[path] = _sha256_text(raw_digest)
    if tuple(result) != tuple(sorted(result)):
        raise GateEConfigError(
            "release_closure_mismatch",
            "Gate E input closure paths must be sorted",
        )
    return result


def _release_closure_digest(
    market_snapshots: Mapping[str, str],
    corporate_action_snapshots: Mapping[str, str],
    input_files: Mapping[str, str],
) -> str:
    payload = {
        "corporate_action_snapshots": dict(corporate_action_snapshots),
        "input_files": dict(input_files),
        "market_snapshots": dict(market_snapshots),
    }
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _freeze(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType(
            {
                key: _freeze(item)
                for key, item in sorted(value.items())
            }
        )
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, init=False)
class GateEConfig:
    """Verified configuration whose source strings remain identity-bearing."""

    payload: Mapping[str, object]
    canonical_bytes: bytes
    config_sha256: str
    gross_target_weight: Decimal
    signal_date: date
    end_date: date
    post_end_validation_date: date
    stock_commission_rate: Decimal
    stock_minimum_commission_yuan: Decimal
    etf_commission_rate: Decimal
    etf_minimum_commission_yuan: Decimal
    stamp_duty_schedule: tuple[tuple[date, Decimal], ...]
    transfer_fee_schedule: tuple[tuple[date, Decimal], ...]

    def to_fee_policy(self) -> VerifiedFeePolicy:
        verify_gate_e_config(self)
        policy = make_fee_policy(
            stock_commission=CommissionAssumption(
                self.stock_commission_rate,
                self.stock_minimum_commission_yuan,
            ),
            etf_commission=CommissionAssumption(
                self.etf_commission_rate,
                self.etf_minimum_commission_yuan,
            ),
            stamp_duty_schedule=self.stamp_duty_schedule,
            transfer_fee_schedule=self.transfer_fee_schedule,
        )
        if policy.policy_digest != self.payload["fee_policy_digest"]:
            raise GateEConfigError(
                "fee_policy_digest_mismatch",
                "Gate E fee policy cannot be reconstructed",
            )
        return policy

    def to_portfolio_namespace(
        self,
        *,
        project_root: str,
    ) -> argparse.Namespace:
        verify_gate_e_config(self)
        market = self.payload["market_snapshots"]
        corporate_actions = self.payload["corporate_action_snapshots"]
        return argparse.Namespace(
            command="run-config",
            project_root=project_root,
            manifest=self.payload["manifest"],
            corporate_action_manifest=self.payload[
                "corporate_action_manifest"
            ],
            output=self.payload["output"],
            calendar_id=self.payload["calendar_id"],
            universe_id=self.payload["universe_id"],
            market_snapshot=tuple(
                f"{symbol}={market[symbol]}" for symbol in _SYMBOLS
            ),
            corporate_action_snapshot=tuple(
                f"{symbol}={corporate_actions[symbol]}"
                for symbol in _SYMBOLS
            ),
            initial_cash_fen=str(self.payload["initial_cash_fen"]),
            gross_target_weight=self.payload["gross_target_weight"],
            signal_date=self.payload["signal_date"],
            end_date=self.payload["end_date"],
            max_entry_attempts=str(self.payload["max_entry_attempts"]),
            stock_commission_rate=self.payload[
                "stock_commission_rate"
            ],
            stock_minimum_commission=self.payload[
                "stock_minimum_commission_yuan"
            ],
            etf_commission_rate=self.payload["etf_commission_rate"],
            etf_minimum_commission=self.payload[
                "etf_minimum_commission_yuan"
            ],
        )


_VERIFIED_CONFIG_REGISTRY: dict[
    int,
    tuple[weakref.ReferenceType[GateEConfig], str],
] = {}


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _thaw(item)
            for key, item in sorted(value.items())
        }
    if type(value) is tuple:
        return [_thaw(item) for item in value]
    return value


def _config_state_digest(config: GateEConfig) -> str:
    payload = {
        "canonical_bytes_sha256": hashlib.sha256(
            config.canonical_bytes
        ).hexdigest(),
        "config_sha256": config.config_sha256,
        "end_date": config.end_date.isoformat(),
        "etf_commission_rate": str(config.etf_commission_rate),
        "etf_minimum_commission_yuan": str(
            config.etf_minimum_commission_yuan
        ),
        "gross_target_weight": str(config.gross_target_weight),
        "payload": _thaw(config.payload),
        "post_end_validation_date": (
            config.post_end_validation_date.isoformat()
        ),
        "signal_date": config.signal_date.isoformat(),
        "stamp_duty_schedule": [
            [effective.isoformat(), str(rate)]
            for effective, rate in config.stamp_duty_schedule
        ],
        "stock_commission_rate": str(config.stock_commission_rate),
        "stock_minimum_commission_yuan": str(
            config.stock_minimum_commission_yuan
        ),
        "transfer_fee_schedule": [
            [effective.isoformat(), str(rate)]
            for effective, rate in config.transfer_fee_schedule
        ],
    }
    return hashlib.sha256(canonical_config_bytes(payload)).hexdigest()


def _deeply_frozen(value: object) -> bool:
    if isinstance(value, Mapping):
        return type(value) is MappingProxyType and all(
            type(key) is str and _deeply_frozen(item)
            for key, item in value.items()
        )
    if type(value) is tuple:
        return all(_deeply_frozen(item) for item in value)
    return type(value) in {bool, int, str}


def verify_gate_e_config(config: GateEConfig) -> None:
    """Require the exact, unmodified object registered by the loader."""
    if type(config) is not GateEConfig:
        raise TypeError("config must be an exact GateEConfig")
    registered = _VERIFIED_CONFIG_REGISTRY.get(id(config))
    if registered is None or registered[0]() is not config:
        raise GateEConfigError(
            "unverified_config",
            "Gate E config was not created by the strict loader",
        )
    try:
        state_digest = _config_state_digest(config)
        payload = _thaw(config.payload)
        canonical_payload = canonical_config_bytes(payload)
    except (
        AttributeError,
        GateEConfigError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise GateEConfigError(
            "verified_config_modified",
            "Gate E config changed after verification",
        ) from exc
    if (
        not _deeply_frozen(config.payload)
        or state_digest != registered[1]
        or canonical_payload != config.canonical_bytes
        or hashlib.sha256(config.canonical_bytes).hexdigest()
        != config.config_sha256
    ):
        raise GateEConfigError(
            "verified_config_modified",
            "Gate E config changed after verification",
        )


def load_gate_e_config(path: Path) -> GateEConfig:
    """Load one exact a-share-quant v0.2 Gate E configuration."""
    raw = _read_regular_single_link(path)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GateEConfigError(
            "invalid_config",
            "Gate E config is invalid JSON",
        ) from exc
    if type(payload) is not dict:
        raise GateEConfigError(
            "invalid_config",
            "Gate E config must be a JSON object",
        )
    if raw != canonical_config_bytes(payload):
        raise GateEConfigError(
            "noncanonical_config",
            "Gate E config must use canonical JSON bytes",
        )
    _require_exact_keys(payload)

    hashes = {
        field: _sha256_text(payload[field])
        for field in (
            "calendar_id",
            "fee_policy_digest",
            "release_manifest_sha256",
            "universe_id",
            "uv_lock_sha256",
        )
    }
    paths = {
        field: _safe_relative_path(payload[field])
        for field in (
            "corporate_action_manifest",
            "manifest",
            "output",
        )
    }
    initial_cash_fen = _positive_integer(payload["initial_cash_fen"])
    max_entry_attempts = _positive_integer(payload["max_entry_attempts"])
    gross_target_weight = _decimal_text(payload["gross_target_weight"])
    stock_rate = _decimal_text(payload["stock_commission_rate"])
    stock_minimum = _decimal_text(
        payload["stock_minimum_commission_yuan"]
    )
    etf_rate = _decimal_text(payload["etf_commission_rate"])
    etf_minimum = _decimal_text(payload["etf_minimum_commission_yuan"])
    signal = _date_text(payload["signal_date"])
    end = _date_text(payload["end_date"])
    post_end = _date_text(payload["post_end_validation_date"])
    stamp = _schedule(
        payload["stamp_duty_schedule"],
        expected=_STAMP_DUTY_TEXT,
    )
    transfer = _schedule(
        payload["transfer_fee_schedule"],
        expected=_TRANSFER_FEE_TEXT,
    )

    if type(payload["symbols"]) is not list or tuple(payload["symbols"]) != _SYMBOLS:
        raise GateEConfigError(
            "symbol_contract_mismatch",
            "Gate E symbols differ from the approved contract",
        )
    market = _symbol_mapping(
        payload["market_snapshots"],
        field="market snapshot",
    )
    corporate_actions = _symbol_mapping(
        payload["corporate_action_snapshots"],
        field="corporate-action snapshot",
    )
    input_files = _input_file_mapping(payload["input_files"])
    if (
        _release_closure_digest(
            market,
            corporate_actions,
            input_files,
        )
        != _RELEASE_CLOSURE_DIGEST
    ):
        raise GateEConfigError(
            "release_closure_mismatch",
            "Gate E release closure differs from the frozen inputs",
        )

    policy = make_fee_policy(
        stock_commission=CommissionAssumption(
            stock_rate,
            stock_minimum,
        ),
        etf_commission=CommissionAssumption(etf_rate, etf_minimum),
        stamp_duty_schedule=stamp,
        transfer_fee_schedule=transfer,
    )
    if policy.policy_digest != hashes["fee_policy_digest"]:
        raise GateEConfigError(
            "fee_policy_digest_mismatch",
            "Gate E fee policy differs from the approved digest",
        )

    fixed = {
        "fee_schema_version": "date-effective-fees-v1",
        "gate": "E",
        "gross_target_weight": "0.95",
        "portfolio_schema_version": "0.2.0",
        "project_name": "a-share-quant",
        "project_version": "0.2.0",
        "python_version": "3.11.15",
        "schema_version": "1.0",
        "strategy": "buy_and_hold",
    }
    if (
        any(payload[key] != value for key, value in fixed.items())
        or initial_cash_fen != 100_000_000
        or max_entry_attempts != 5
        or signal != date(2018, 1, 2)
        or end != date(2026, 7, 23)
        or post_end != date(2026, 7, 24)
        or paths["manifest"] != "data/manifests/manifest.jsonl"
        or paths["corporate_action_manifest"]
        != "data/corporate_actions/manifest.jsonl"
        or paths["output"] != "outputs/portfolios"
        or hashes["universe_id"] != _UNIVERSE_ID
        or hashes["calendar_id"] != _CALENDAR_ID
        or hashes["release_manifest_sha256"]
        != _RELEASE_MANIFEST_SHA256
        or hashes["uv_lock_sha256"] != _UV_LOCK_SHA256
        or hashes["fee_policy_digest"] != _FEE_POLICY_DIGEST
        or payload["stock_commission_rate"] != "0.00025"
        or payload["stock_minimum_commission_yuan"] != "5.00"
        or payload["etf_commission_rate"] != "0.00025"
        or payload["etf_minimum_commission_yuan"] != "5.00"
    ):
        raise GateEConfigError(
            "unexpected_config",
            "Gate E config differs from the approved release contract",
        )

    frozen_payload = _freeze(payload)
    if not isinstance(frozen_payload, Mapping):
        raise AssertionError("frozen Gate E config must be a mapping")
    config = object.__new__(GateEConfig)
    values = {
        "payload": frozen_payload,
        "canonical_bytes": raw,
        "config_sha256": hashlib.sha256(raw).hexdigest(),
        "gross_target_weight": gross_target_weight,
        "signal_date": signal,
        "end_date": end,
        "post_end_validation_date": post_end,
        "stock_commission_rate": stock_rate,
        "stock_minimum_commission_yuan": stock_minimum,
        "etf_commission_rate": etf_rate,
        "etf_minimum_commission_yuan": etf_minimum,
        "stamp_duty_schedule": stamp,
        "transfer_fee_schedule": transfer,
    }
    for field, value in values.items():
        object.__setattr__(config, field, value)
    registry_key = id(config)

    def discard_registered_config(
        _reference: weakref.ReferenceType[GateEConfig],
        *,
        key: int = registry_key,
    ) -> None:
        _VERIFIED_CONFIG_REGISTRY.pop(key, None)

    _VERIFIED_CONFIG_REGISTRY[registry_key] = (
        weakref.ref(config, discard_registered_config),
        _config_state_digest(config),
    )
    return config
