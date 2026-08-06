import hashlib
from copy import deepcopy
from datetime import date

import pytest
import yaml

from aquant.config import ConfigError, DataConfig, load_data_config
from aquant.universe import UniverseMember, canonical_universe_bytes

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


def _universe_id(tmp_path, *, members=MEMBERS):
    content = canonical_universe_bytes("pilot-10", members)
    universe_id = hashlib.sha256(content).hexdigest()
    directory = tmp_path / "universes"
    directory.mkdir(exist_ok=True)
    (directory / f"{universe_id}.json").write_bytes(content)
    return universe_id


def _valid_config(tmp_path):
    return {
        "pipeline": {
            "universe_id": _universe_id(tmp_path),
            "start": "2018-01-01",
            "end": "latest_complete_trading_day",
            "adjust": "",
            "mode": "research_approx",
        }
    }


def _write_config(tmp_path, data):
    path = tmp_path / "data.yaml"
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_loads_verified_ten_member_universe(tmp_path):
    config = load_data_config(_write_config(tmp_path, _valid_config(tmp_path)))

    assert config.adjust == ""
    assert config.mode == "research_approx"
    assert config.start.isoformat() == "2018-01-01"
    assert config.end == "latest_complete_trading_day"
    assert config.universe.name == "pilot-10"
    assert config.universe_id == hashlib.sha256(
        canonical_universe_bytes("pilot-10", MEMBERS)
    ).hexdigest()
    assert config.symbols == tuple(item.symbol for item in MEMBERS)
    assert tuple((item.symbol, item.kind) for item in config.instruments) == tuple(
        (item.symbol, item.kind) for item in MEMBERS
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"adjust": "qfq"},
        {"mode": "strict_point_in_time"},
        {"start": date(2019, 1, 1)},
        {"end": "2050-01-01"},
        {"universe": object()},
    ],
)
def test_data_config_constructor_cannot_bypass_invariants(tmp_path, changes):
    loaded = load_data_config(_write_config(tmp_path, _valid_config(tmp_path)))
    values = {
        "adjust": "",
        "mode": "research_approx",
        "start": date(2018, 1, 1),
        "end": "latest_complete_trading_day",
        "universe": loaded.universe,
    }
    values.update(changes)

    with pytest.raises(ConfigError):
        DataConfig(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start", "2018/01/01"),
        ("end", "2050-01-01"),
        ("universe_id", "f" * 64),
    ],
)
def test_rejects_invalid_boundary_or_universe_identity(tmp_path, field, value):
    data = _valid_config(tmp_path)
    data["pipeline"][field] = value

    with pytest.raises(ConfigError, match=rf"pipeline\.{field}"):
        load_data_config(_write_config(tmp_path, data))


@pytest.mark.parametrize("extra", ["broker", "api_key", "strategy", "instruments"])
def test_rejects_unknown_pipeline_fields(tmp_path, extra):
    data = _valid_config(tmp_path)
    data["pipeline"][extra] = "must-not-be-accepted"

    with pytest.raises(ConfigError, match="pipeline.*unknown fields") as error:
        load_data_config(_write_config(tmp_path, data))

    assert "must-not-be-accepted" not in str(error.value)


def test_rejects_unknown_document_fields(tmp_path):
    data = _valid_config(tmp_path)
    data["secrets"] = {"token": "must-not-leak"}

    with pytest.raises(ConfigError, match="document.*unknown fields") as error:
        load_data_config(_write_config(tmp_path, data))

    assert "must-not-leak" not in str(error.value)


def test_rejects_tampered_universe_content(tmp_path):
    data = _valid_config(tmp_path)
    universe_id = data["pipeline"]["universe_id"]
    path = tmp_path / "universes" / f"{universe_id}.json"
    path.write_bytes(path.read_bytes().replace(b"510500", b"510501"))

    with pytest.raises(ConfigError, match=r"pipeline\.universe_id"):
        load_data_config(_write_config(tmp_path, data))


def test_rejects_qfq_adjustment_without_echoing_value(tmp_path):
    data = _valid_config(tmp_path)
    data["pipeline"]["adjust"] = "qfq"
    path = _write_config(tmp_path, data)

    with pytest.raises(ConfigError) as error:
        load_data_config(path)

    message = str(error.value)
    assert str(path) in message
    assert "validation failed at pipeline.adjust" in message
    assert 'must be explicitly set to ""' in message
    assert "qfq" not in message.removeprefix(f"{path}:")


def test_rejects_unsupported_mode_without_echoing_value(tmp_path):
    data = _valid_config(tmp_path)
    data["pipeline"]["mode"] = "strict_point_in_time"
    path = _write_config(tmp_path, data)

    with pytest.raises(ConfigError) as error:
        load_data_config(path)

    message = str(error.value)
    assert str(path) in message
    assert "validation failed at pipeline.mode" in message
    assert "strict_point_in_time" not in message


def test_rejects_duplicate_yaml_mapping_key(tmp_path):
    universe_id = _universe_id(tmp_path)
    path = tmp_path / "data.yaml"
    path.write_text(
        f"""pipeline:
  universe_id: "{universe_id}"
  start: "2018-01-01"
  end: latest_complete_trading_day
  adjust: ""
  adjust: qfq
  mode: research_approx
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_data_config(path)

    message = str(error.value)
    assert str(path) in message
    assert "YAML parsing" in message
    assert "duplicate mapping key 'adjust'" in message
    assert universe_id not in message


def test_rejects_non_utf8_config_without_leaking_bytes(tmp_path):
    path = tmp_path / "data.yaml"
    path.write_bytes(b"pipeline:\n  mode: research_approx\n\xffsecret-body")

    with pytest.raises(ConfigError) as error:
        load_data_config(path)

    message = str(error.value)
    assert str(path) in message
    assert "UTF-8 decoding failed" in message
    assert "secret-body" not in message


def test_wraps_unhashable_yaml_mapping_key_as_config_error(tmp_path):
    path = tmp_path / "data.yaml"
    path.write_text("? [a, b]\n: value\n", encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        load_data_config(path)

    message = str(error.value)
    assert str(path) in message
    assert "YAML parsing failed" in message
    assert "a, b" not in message


def test_copying_universe_to_a_non_content_addressed_filename_is_rejected(tmp_path):
    data = deepcopy(_valid_config(tmp_path))
    universe_id = data["pipeline"]["universe_id"]
    source = tmp_path / "universes" / f"{universe_id}.json"
    wrong_id = "e" * 64
    (tmp_path / "universes" / f"{wrong_id}.json").write_bytes(source.read_bytes())
    data["pipeline"]["universe_id"] = wrong_id

    with pytest.raises(ConfigError, match=r"pipeline\.universe_id"):
        load_data_config(_write_config(tmp_path, data))
