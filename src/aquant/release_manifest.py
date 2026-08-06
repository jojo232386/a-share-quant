"""Strict manifest and frozen-input verification for research releases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}")
_SYMBOL_RE = re.compile(r"[0-9]{6}")
_ALLOWED_INPUT_ROOTS = frozenset({"configs", "data"})
_ALLOWED_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "release_name",
        "implementation_commit",
        "python_version",
        "akshare_version",
        "backtrader_version",
        "universe_id",
        "calendar_id",
        "market_snapshots",
        "corporate_action_snapshots",
        "input_files",
        "baseline_run_ids",
        "candidate_run_ids",
        "risk_report_id",
        "week5_experiment_id",
        "expected_counts",
        "research_boundary",
    }
)
_EXPECTED_COUNT_FIELDS = frozenset(
    {"symbols", "baseline_runs", "candidate_runs", "replay_rows"}
)
_RESEARCH_BOUNDARY = {
    "live_trading": False,
    "profit_claim": False,
    "research_only": True,
    "simulation_only": True,
}


class ReleaseVerificationError(RuntimeError):
    """Sanitized failure with a stable machine-readable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExpectedCounts:
    """Exact cardinalities for the fixed v0.1 research reconstruction."""

    symbols: int
    baseline_runs: int
    candidate_runs: int
    replay_rows: int


@dataclass(frozen=True)
class ReleaseManifest:
    """Validated immutable release contract with sorted mapping entries."""

    schema_version: str
    release_name: str
    implementation_commit: str
    python_version: str
    akshare_version: str
    backtrader_version: str
    universe_id: str
    calendar_id: str
    market_snapshots: tuple[tuple[str, str], ...]
    corporate_action_snapshots: tuple[tuple[str, str], ...]
    input_files: tuple[tuple[str, str], ...]
    baseline_run_ids: tuple[tuple[str, str], ...]
    candidate_run_ids: tuple[tuple[str, str], ...]
    risk_report_id: str
    week5_experiment_id: str
    expected_counts: ExpectedCounts
    research_boundary: tuple[tuple[str, bool], ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(symbol for symbol, _snapshot_id in self.market_snapshots)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseVerificationError("duplicate_manifest_key")
        result[key] = value
    return result


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _require_hash(value: object) -> str:
    if not _is_hash(value):
        raise ReleaseVerificationError("manifest_schema_invalid")
    return value


def _require_version(value: object) -> str:
    if type(value) is not str or _VERSION_RE.fullmatch(value) is None:
        raise ReleaseVerificationError("manifest_schema_invalid")
    return value


def _safe_input_path(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or "\\" in value
        or "//" in value
    ):
        raise ReleaseVerificationError("unsafe_input_path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.parts[0] not in _ALLOWED_INPUT_ROOTS
    ):
        raise ReleaseVerificationError("unsafe_input_path")
    if pure.parts[0] == "configs" and (
        len(pure.parts) != 3 or pure.parts[1] != "universes"
    ):
        raise ReleaseVerificationError("unsafe_input_path")
    return value


def _hash_mapping(
    value: object,
    *,
    key_pattern: re.Pattern[str] | None = None,
    input_paths: bool = False,
) -> tuple[tuple[str, str], ...]:
    if type(value) is not dict or not value:
        raise ReleaseVerificationError("manifest_schema_invalid")
    entries: list[tuple[str, str]] = []
    for raw_key, raw_hash in value.items():
        if type(raw_key) is not str:
            raise ReleaseVerificationError("manifest_schema_invalid")
        key = _safe_input_path(raw_key) if input_paths else raw_key
        if key_pattern is not None and key_pattern.fullmatch(key) is None:
            raise ReleaseVerificationError("manifest_schema_invalid")
        entries.append((key, _require_hash(raw_hash)))
    return tuple(sorted(entries))


def _read_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReleaseVerificationError("manifest_unreadable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ReleaseVerificationError("unsafe_manifest_file")
    try:
        raw = path.read_bytes()
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except ReleaseVerificationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError("manifest_unreadable") from exc
    if type(value) is not dict:
        raise ReleaseVerificationError("manifest_schema_invalid")
    return value, raw


def _validate_counts(value: object, symbol_count: int) -> ExpectedCounts:
    if type(value) is not dict or set(value) != _EXPECTED_COUNT_FIELDS:
        raise ReleaseVerificationError("manifest_schema_invalid")
    if any(type(item) is not int or item < 1 for item in value.values()):
        raise ReleaseVerificationError("manifest_schema_invalid")
    counts = ExpectedCounts(
        symbols=value["symbols"],
        baseline_runs=value["baseline_runs"],
        candidate_runs=value["candidate_runs"],
        replay_rows=value["replay_rows"],
    )
    if counts != ExpectedCounts(
        symbols=symbol_count,
        baseline_runs=symbol_count * 2,
        candidate_runs=symbol_count * 3,
        replay_rows=symbol_count * 10,
    ):
        raise ReleaseVerificationError("manifest_schema_invalid")
    return counts


def load_release_manifest(path: Path) -> ReleaseManifest:
    """Load one canonical v0.1 release manifest or fail closed."""
    if not isinstance(path, Path):
        raise TypeError("path must be a Path")
    payload, raw = _read_manifest(path)
    if set(payload) != _ALLOWED_TOP_LEVEL:
        raise ReleaseVerificationError("manifest_schema_invalid")
    if (
        payload["schema_version"] != "1.0"
        or payload["release_name"] != "v0.1-research"
        or type(payload["implementation_commit"]) is not str
        or _COMMIT_RE.fullmatch(payload["implementation_commit"]) is None
    ):
        raise ReleaseVerificationError("manifest_schema_invalid")

    market_snapshots = _hash_mapping(
        payload["market_snapshots"],
        key_pattern=_SYMBOL_RE,
    )
    corporate_actions = _hash_mapping(
        payload["corporate_action_snapshots"],
        key_pattern=_SYMBOL_RE,
    )
    symbols = tuple(symbol for symbol, _snapshot in market_snapshots)
    if (
        tuple(symbol for symbol, _snapshot in corporate_actions) != symbols
        or len(symbols) != len(set(symbols))
    ):
        raise ReleaseVerificationError("manifest_schema_invalid")

    baseline_pattern = re.compile(
        rf"(?:{'|'.join(re.escape(symbol) for symbol in symbols)})"
        r"\|(?:buy_and_hold|sma20)"
    )
    candidate_pattern = re.compile(
        rf"(?:{'|'.join(re.escape(symbol) for symbol in symbols)})"
        r"\|(?:sma10|sma20|sma60)"
    )
    baseline_ids = _hash_mapping(
        payload["baseline_run_ids"],
        key_pattern=baseline_pattern,
    )
    candidate_ids = _hash_mapping(
        payload["candidate_run_ids"],
        key_pattern=candidate_pattern,
    )
    expected_baseline_keys = {
        f"{symbol}|{strategy}"
        for symbol in symbols
        for strategy in ("buy_and_hold", "sma20")
    }
    expected_candidate_keys = {
        f"{symbol}|sma{period}"
        for symbol in symbols
        for period in (10, 20, 60)
    }
    if (
        {key for key, _value in baseline_ids} != expected_baseline_keys
        or {key for key, _value in candidate_ids} != expected_candidate_keys
    ):
        raise ReleaseVerificationError("manifest_schema_invalid")

    boundary = payload["research_boundary"]
    if type(boundary) is not dict or boundary != _RESEARCH_BOUNDARY:
        raise ReleaseVerificationError("manifest_schema_invalid")

    manifest = ReleaseManifest(
        schema_version=payload["schema_version"],
        release_name=payload["release_name"],
        implementation_commit=payload["implementation_commit"],
        python_version=_require_version(payload["python_version"]),
        akshare_version=_require_version(payload["akshare_version"]),
        backtrader_version=_require_version(payload["backtrader_version"]),
        universe_id=_require_hash(payload["universe_id"]),
        calendar_id=_require_hash(payload["calendar_id"]),
        market_snapshots=market_snapshots,
        corporate_action_snapshots=corporate_actions,
        input_files=_hash_mapping(payload["input_files"], input_paths=True),
        baseline_run_ids=baseline_ids,
        candidate_run_ids=candidate_ids,
        risk_report_id=_require_hash(payload["risk_report_id"]),
        week5_experiment_id=_require_hash(payload["week5_experiment_id"]),
        expected_counts=_validate_counts(payload["expected_counts"], len(symbols)),
        research_boundary=tuple(sorted(boundary.items())),
    )
    canonical = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    if raw != canonical:
        raise ReleaseVerificationError("manifest_not_canonical")
    return manifest


def _actual_input_files(inputs_root: Path) -> tuple[str, ...]:
    try:
        root_metadata = inputs_root.lstat()
    except OSError as exc:
        raise ReleaseVerificationError("input_file_set_mismatch") from exc
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ReleaseVerificationError("unsafe_input_link")

    found: list[str] = []

    def visit(directory: Path) -> None:
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise ReleaseVerificationError("input_unreadable") from exc
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ReleaseVerificationError("input_unreadable") from exc
            path = Path(entry.path)
            if entry.is_symlink():
                raise ReleaseVerificationError("unsafe_input_link")
            if stat.S_ISDIR(metadata.st_mode):
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise ReleaseVerificationError("unsafe_input_link")
                found.append(path.relative_to(inputs_root).as_posix())
            else:
                raise ReleaseVerificationError("unsafe_input_link")

    visit(inputs_root)
    return tuple(sorted(found))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseVerificationError("input_unreadable") from exc
    return digest.hexdigest()


def verify_release_inputs(
    manifest: ReleaseManifest,
    release_root: Path,
) -> tuple[Path, ...]:
    """Verify the exact frozen file set, safe link count, and every digest."""
    if type(manifest) is not ReleaseManifest or not isinstance(release_root, Path):
        raise TypeError("manifest and release_root require release types")
    inputs_root = release_root / "inputs"
    declared = tuple(path for path, _digest in manifest.input_files)
    actual = _actual_input_files(inputs_root)
    if actual != declared:
        raise ReleaseVerificationError("input_file_set_mismatch")
    verified: list[Path] = []
    for relative_path, expected_digest in manifest.input_files:
        path = inputs_root / relative_path
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ReleaseVerificationError("input_file_set_mismatch") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ReleaseVerificationError("unsafe_input_link")
        if _sha256(path) != expected_digest:
            raise ReleaseVerificationError("input_hash_mismatch")
        verified.append(path)
    return tuple(verified)
