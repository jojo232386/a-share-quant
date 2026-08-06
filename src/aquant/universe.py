"""Immutable, content-addressed research-universe configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SYMBOL_RE = re.compile(r"[0-9]{6}")
_MAIN_BOARD_SYMBOL_RE = re.compile(r"(?:60[0135][0-9]{3}|00[0123][0-9]{3})")
_ETF_SYMBOL_RE = re.compile(r"5[0-9]{5}")
_KINDS = frozenset(
    {
        "domestic_equity_broad_based_etf",
        "main_board_stock",
    }
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_VERIFIED_UNIVERSE_REGISTRY: dict[int, tuple[VerifiedUniverse, str]] = {}


class UniverseError(RuntimeError):
    """Raised when a research universe is invalid, forged, or damaged."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def is_supported_instrument_identity(symbol: object, kind: object) -> bool:
    """Return whether an identity belongs to a supported v0.1 instrument class."""
    if (
        type(symbol) is not str
        or _SYMBOL_RE.fullmatch(symbol) is None
        or type(kind) is not str
        or kind not in _KINDS
    ):
        return False
    pattern = (
        _ETF_SYMBOL_RE
        if kind == "domestic_equity_broad_based_etf"
        else _MAIN_BOARD_SYMBOL_RE
    )
    return pattern.fullmatch(symbol) is not None


@dataclass(frozen=True)
class UniverseMember:
    """One explicitly classified member of a research universe."""

    symbol: str
    kind: str

    def __post_init__(self) -> None:
        if not is_supported_instrument_identity(self.symbol, self.kind):
            raise UniverseError("invalid_member", "universe member is invalid")


def _validate_definition(name: object, members: object) -> None:
    if (
        type(name) is not str
        or _NAME_RE.fullmatch(name) is None
        or type(members) is not tuple
        or not 1 <= len(members) <= 100
        or any(type(item) is not UniverseMember for item in members)
    ):
        raise UniverseError("invalid_definition", "universe definition is invalid")
    symbols = tuple(item.symbol for item in members)
    if len(symbols) != len(set(symbols)):
        raise UniverseError("duplicate_member", "universe symbols must be unique")


def canonical_universe_bytes(
    name: str,
    members: tuple[UniverseMember, ...],
) -> bytes:
    """Return the stable byte representation used as the universe identity."""
    _validate_definition(name, members)
    payload = {
        "members": [
            {"kind": item.kind, "symbol": item.symbol}
            for item in members
        ],
        "name": name,
        "schema_version": "1.0",
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise UniverseError(
                "duplicate_json_key",
                "universe contains a duplicate JSON key",
            )
        result[key] = value
    return result


@dataclass(frozen=True, init=False)
class VerifiedUniverse:
    """Universe data obtainable only through the content-addressed loader."""

    name: str
    members: tuple[UniverseMember, ...]
    universe_id: str

    def contains(self, symbol: str, kind: str) -> bool:
        return any(
            item.symbol == symbol and item.kind == kind
            for item in self.members
        )


def verify_universe(universe: VerifiedUniverse) -> None:
    """Recompute identity and require the exact loader-created object."""
    if type(universe) is not VerifiedUniverse:
        raise UniverseError(
            "invalid_verified_universe",
            "verified universe is invalid",
        )
    registered = _VERIFIED_UNIVERSE_REGISTRY.get(id(universe))
    if registered is None or registered[0] is not universe:
        raise UniverseError(
            "invalid_verified_universe",
            "verified universe is invalid",
        )
    try:
        digest = hashlib.sha256(
            canonical_universe_bytes(universe.name, universe.members)
        ).hexdigest()
    except UniverseError as exc:
        raise UniverseError(
            "invalid_verified_universe",
            "verified universe is invalid",
        ) from exc
    if universe.universe_id != digest or registered[1] != digest:
        raise UniverseError(
            "invalid_verified_universe",
            "verified universe is invalid",
        )


def _read_regular_file(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        raise UniverseError(
            "unsafe_path",
            "universe must be a readable regular file",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise UniverseError(
                "unsafe_path",
                "universe must be a readable regular file",
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def load_verified_universe(
    path: str | Path,
    *,
    expected_id: str | None = None,
) -> VerifiedUniverse:
    """Load a canonical universe whose filename and content share one SHA-256."""
    universe_path = Path(path)
    if expected_id is not None and (
        type(expected_id) is not str or _HASH_RE.fullmatch(expected_id) is None
    ):
        raise UniverseError("identity_mismatch", "universe identity is invalid")
    content = _read_regular_file(universe_path)
    digest = hashlib.sha256(content).hexdigest()
    if (
        universe_path.name != f"{digest}.json"
        or expected_id is not None
        and expected_id != digest
    ):
        raise UniverseError("identity_mismatch", "universe identity is invalid")
    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except UniverseError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UniverseError("invalid_json", "universe JSON is invalid") from exc
    if type(document) is not dict or set(document) != {
        "members",
        "name",
        "schema_version",
    }:
        raise UniverseError("invalid_schema", "universe schema is invalid")
    if document["schema_version"] != "1.0" or type(document["members"]) is not list:
        raise UniverseError("invalid_schema", "universe schema is invalid")
    members: list[UniverseMember] = []
    try:
        for item in document["members"]:
            if type(item) is not dict or set(item) != {"kind", "symbol"}:
                raise UniverseError(
                    "invalid_schema",
                    "universe member schema is invalid",
                )
            members.append(UniverseMember(item["symbol"], item["kind"]))
        checked_members = tuple(members)
        canonical = canonical_universe_bytes(document["name"], checked_members)
    except (KeyError, TypeError) as exc:
        raise UniverseError("invalid_schema", "universe schema is invalid") from exc
    if content != canonical:
        raise UniverseError(
            "noncanonical_content",
            "universe content must use canonical JSON",
        )
    universe = object.__new__(VerifiedUniverse)
    object.__setattr__(universe, "name", document["name"])
    object.__setattr__(universe, "members", checked_members)
    object.__setattr__(universe, "universe_id", digest)
    _VERIFIED_UNIVERSE_REGISTRY[id(universe)] = (universe, digest)
    return universe
