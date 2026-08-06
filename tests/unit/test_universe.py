import hashlib
import json

import pytest

from aquant.universe import (
    UniverseError,
    UniverseMember,
    canonical_universe_bytes,
    load_verified_universe,
    verify_universe,
)

MEMBERS = (
    UniverseMember("510300", "domestic_equity_broad_based_etf"),
    UniverseMember("510500", "domestic_equity_broad_based_etf"),
    UniverseMember("600519", "main_board_stock"),
    UniverseMember("601318", "main_board_stock"),
    UniverseMember("000001", "main_board_stock"),
    UniverseMember("600036", "main_board_stock"),
    UniverseMember("600900", "main_board_stock"),
    UniverseMember("600030", "main_board_stock"),
    UniverseMember("000858", "main_board_stock"),
    UniverseMember("601166", "main_board_stock"),
)


def _write_universe(tmp_path, *, name="pilot-10", members=MEMBERS):
    content = canonical_universe_bytes(name, members)
    universe_id = hashlib.sha256(content).hexdigest()
    path = tmp_path / f"{universe_id}.json"
    path.write_bytes(content)
    return path, universe_id


def test_loads_content_addressed_ten_member_universe(tmp_path):
    path, universe_id = _write_universe(tmp_path)

    universe = load_verified_universe(path, expected_id=universe_id)

    assert universe.universe_id == universe_id
    assert universe.name == "pilot-10"
    assert universe.members == MEMBERS
    assert universe.contains("510500", "domestic_equity_broad_based_etf")
    assert universe.contains("600036", "main_board_stock")
    verify_universe(universe)


def test_rejects_filename_or_expected_id_that_does_not_match_content(tmp_path):
    path, _ = _write_universe(tmp_path)
    wrong_id = "f" * 64

    with pytest.raises(UniverseError, match="identity"):
        load_verified_universe(path, expected_id=wrong_id)

    renamed = tmp_path / f"{wrong_id}.json"
    renamed.write_bytes(path.read_bytes())
    with pytest.raises(UniverseError, match="identity"):
        load_verified_universe(renamed)


def test_rejects_noncanonical_or_duplicate_key_payload(tmp_path):
    document = {
        "schema_version": "1.0",
        "name": "pilot-10",
        "members": [
            {"symbol": item.symbol, "kind": item.kind}
            for item in MEMBERS
        ],
    }
    noncanonical = json.dumps(document, ensure_ascii=False, indent=2).encode()
    digest = hashlib.sha256(noncanonical).hexdigest()
    path = tmp_path / f"{digest}.json"
    path.write_bytes(noncanonical)

    with pytest.raises(UniverseError, match="canonical"):
        load_verified_universe(path)

    duplicate = (
        b'{"members":[],"name":"pilot","name":"shadow","schema_version":"1.0"}\n'
    )
    duplicate_digest = hashlib.sha256(duplicate).hexdigest()
    duplicate_path = tmp_path / f"{duplicate_digest}.json"
    duplicate_path.write_bytes(duplicate)
    with pytest.raises(UniverseError, match="duplicate"):
        load_verified_universe(duplicate_path)


@pytest.mark.parametrize(
    "members",
    [
        (),
        (UniverseMember("600519", "main_board_stock"),) * 2,
    ],
)
def test_rejects_empty_or_duplicate_membership(members):
    with pytest.raises(UniverseError):
        canonical_universe_bytes("invalid", members)


@pytest.mark.parametrize(
    ("symbol", "kind"),
    [
        ("300001", "main_board_stock"),
        ("600519", "domestic_equity_broad_based_etf"),
        ("510300", "main_board_stock"),
    ],
)
def test_rejects_board_or_kind_mismatch(symbol, kind):
    with pytest.raises(UniverseError, match="member"):
        UniverseMember(symbol, kind)


def test_verified_universe_cannot_be_forged_or_modified(tmp_path):
    path, _ = _write_universe(tmp_path)
    universe = load_verified_universe(path)

    object.__setattr__(universe, "name", "modified")
    with pytest.raises(UniverseError, match="verified universe"):
        verify_universe(universe)
