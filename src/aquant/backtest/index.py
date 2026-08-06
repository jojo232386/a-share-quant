"""Atomic status index for immutable historical backtest bundles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_SUPERSEDED_REASONS = (
    "missing_corporate_actions",
    "ex_right_reference_price",
    "fixed_one_lot_baseline",
)


class BacktestIndexError(RuntimeError):
    """Raised when the immutable run set cannot be indexed safely."""


def _canonical_bytes(run_ids: tuple[str, ...]) -> bytes:
    payload = {
        "schema_version": "1.0",
        "superseded": {
            "status": "superseded_semantic_bug",
            "run_ids": list(run_ids),
            "reasons": list(_SUPERSEDED_REASONS),
        },
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _validate_runs(root: Path, run_ids: tuple[str, ...]) -> None:
    if (
        type(run_ids) is not tuple
        or len(run_ids) != 16
        or len(set(run_ids)) != 16
        or any(type(run_id) is not str or _HASH_RE.fullmatch(run_id) is None for run_id in run_ids)
    ):
        raise BacktestIndexError("exactly 16 unique run IDs are required")
    actual_directories = {
        path.name
        for path in root.iterdir()
        if path.is_dir() and not path.is_symlink()
    }
    if actual_directories != set(run_ids):
        raise BacktestIndexError("backtest directory set does not match the index")
    for run_id in run_ids:
        directory = root / run_id
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink():
            raise BacktestIndexError("run target is not a safe directory")
        for filename in ("run.json", "artifact_manifest.json"):
            path = directory / filename
            file_metadata = path.lstat()
            if (
                not stat.S_ISREG(file_metadata.st_mode)
                or file_metadata.st_nlink != 1
                or path.is_symlink()
            ):
                raise BacktestIndexError("run bundle has an unsafe identity file")
        try:
            run_values = json.loads((directory / "run.json").read_text(encoding="utf-8"))
            manifest_values = json.loads(
                (directory / "artifact_manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BacktestIndexError("run identity file is invalid") from exc
        if (
            type(run_values) is not dict
            or run_values.get("run_id") != run_id
            or type(manifest_values) is not dict
            or manifest_values.get("run_id") != run_id
        ):
            raise BacktestIndexError("run bundle identity does not match its directory")


def publish_superseded_index(
    output_root: str | Path,
    run_ids: tuple[str, ...],
) -> Path:
    """Publish the one-time old-semantics index without changing any run."""
    root = Path(output_root)
    if root.is_symlink() or not root.is_dir():
        raise BacktestIndexError("backtest root must be a safe directory")
    index_path = root / "index.json"
    if index_path.is_symlink():
        raise BacktestIndexError("index target must not be a symlink")
    _validate_runs(root, run_ids)
    content = _canonical_bytes(run_ids)
    if index_path.exists():
        metadata = index_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or index_path.read_bytes() != content
        ):
            raise BacktestIndexError("existing index conflicts with the run set")
        return index_path

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".index.",
        suffix=".tmp",
        dir=root,
    )
    temporary = Path(temporary_name)
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, index_path)
        root_descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(root_descriptor)
        finally:
            os.close(root_descriptor)
    except OSError as exc:
        raise BacktestIndexError("index could not be published atomically") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if hashlib.sha256(index_path.read_bytes()).digest() != hashlib.sha256(content).digest():
        raise BacktestIndexError("published index failed its content check")
    return index_path
