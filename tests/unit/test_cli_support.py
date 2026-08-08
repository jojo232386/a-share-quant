"""Compatibility contracts for shared, non-audit CLI plumbing."""

from __future__ import annotations

import json
from hashlib import sha256
from io import StringIO

import pytest

from aquant.backtest_cli import _parser as backtest_parser
from aquant.backtest_cli import main as backtest_main
from aquant.cli import _parser as data_parser
from aquant.cli import main as data_main
from aquant.cli_support import make_safe_argument_parser, path_beneath, write_json
from aquant.experiment_cli import _parser as experiment_parser
from aquant.experiment_cli import main as experiment_main
from aquant.portfolio_cli import _parser as portfolio_parser
from aquant.portfolio_cli import main as portfolio_main
from aquant.release_cli import _parser as release_parser
from aquant.release_cli import main as release_main
from aquant.report_cli import _parser as report_parser
from aquant.report_cli import main as report_main


class _CliSupportError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def test_safe_argument_parser_sanitizes_root_and_subcommand_errors():
    parser_class = make_safe_argument_parser(
        error_factory=_CliSupportError,
        invalid_arguments_message="command arguments are invalid",
    )
    parser = parser_class(prog="safe-command")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=parser_class,
    )
    run = subparsers.add_parser("run")
    run.add_argument("--required", required=True)

    with pytest.raises(_CliSupportError) as missing_command:
        parser.parse_args(())
    assert missing_command.value.code == "invalid_arguments"
    assert str(missing_command.value) == "command arguments are invalid"

    with pytest.raises(_CliSupportError) as missing_option:
        parser.parse_args(("run",))
    assert missing_option.value.code == "invalid_arguments"
    assert str(missing_option.value) == "command arguments are invalid"


def test_path_beneath_rejects_parent_and_symlink_escapes(tmp_path):
    root = (tmp_path / "project").resolve()
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (root / "linked").symlink_to(external, target_is_directory=True)

    assert path_beneath(
        root,
        "outputs/result.json",
        label="output",
        error_factory=_CliSupportError,
    ) == (root / "outputs/result.json").resolve()

    for value in ("../outside.json", "linked/result.json"):
        with pytest.raises(_CliSupportError) as escaped:
            path_beneath(
                root,
                value,
                label="output",
                error_factory=_CliSupportError,
            )
        assert escaped.value.code == "unsafe_path"
        assert str(escaped.value) == "output must stay beneath project root"


def test_write_json_preserves_sorted_utf8_compact_newline_output():
    stream = StringIO()

    write_json(stream, {"z": "中文", "a": 1})

    assert stream.getvalue() == '{"a":1,"z":"中文"}\n'


@pytest.mark.parametrize(
    ("parser", "expected_sha256"),
    (
        (backtest_parser, "ba8189f69ebfb92152b2830047a41e48ec0f4909fbad34070b8de67ffd2d8c27"),
        (data_parser, "7fd566e6f70d0cce186a5e7794dfd0d470075ba2a1df64d3840dc3cb45f66677"),
        (report_parser, "82db437c5aae8774b99b173fad74e54d931c9e1fa4ad64abb7f90426939999f5"),
        (experiment_parser, "35cb7b3b7c5b5916b8336f01de73a740c65dce41cc49ac31ec119364acb2dfbd"),
        (portfolio_parser, "e0585dfe5aa1ca8a6eed6427cdf20978b004412028783a780da1f9da64d05824"),
        (release_parser, "ad6df0d93ab4100c8164af2a08dab7878db828e6e83cfd2fbc9b7637709ff79d"),
    ),
)
def test_non_audit_cli_help_output_is_byte_compatible(parser, expected_sha256):
    assert sha256(parser().format_help().encode("utf-8")).hexdigest() == expected_sha256


@pytest.mark.parametrize(
    ("command", "error_type"),
    (
        (backtest_main, "BacktestCliError"),
        (data_main, "DataCliError"),
        (report_main, "ReportCliError"),
        (experiment_main, "ExperimentCliError"),
        (portfolio_main, "PortfolioCliError"),
        (release_main, "ReleaseCliError"),
    ),
)
def test_non_audit_cli_invalid_arguments_keep_sanitized_exit_contract(
    command,
    error_type,
    capsys,
):
    assert command(("--private-input",)) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error_code": "invalid_arguments",
        "error_type": error_type,
        "status": "error",
    }
