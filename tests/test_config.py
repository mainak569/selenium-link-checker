"""Config loading: YAML values, CLI overrides on top, and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

import main
from checker.config import Config, load_config, merge


def write_yaml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_yaml_values_are_loaded_and_defaults_fill_the_rest(tmp_path):
    path = write_yaml(tmp_path, "start_url: https://example.com\nmax_pages: 5\n")
    config = load_config(path)
    assert config.start_url == "https://example.com"
    assert config.max_pages == 5
    assert config.max_depth == Config().max_depth  # not in the file -> default


def test_cli_overrides_yaml(tmp_path):
    path = write_yaml(tmp_path, "start_url: https://example.com\nmax_pages: 5\nfail_on_broken: true\n")
    config = load_config(path, {"start_url": "https://other.test", "max_pages": 50, "fail_on_broken": False})
    assert config.start_url == "https://other.test"
    assert config.max_pages == 50
    assert config.fail_on_broken is False  # an explicit False must win too


def test_options_not_given_on_the_cli_keep_yaml_values():
    assert merge({"max_pages": 5, "max_depth": 3}, {"max_pages": None, "max_depth": 1}) == {
        "max_pages": 5,
        "max_depth": 1,
    }


def test_argparse_flags_flow_through_to_config(tmp_path):
    path = write_yaml(tmp_path, "max_pages: 5\nfail_on_broken: true\n")
    args = main.parse_args(["--config", str(path), "--max-depth", "1", "--no-fail-on-broken", "--headed"])
    config = load_config(args.config, {
        "max_depth": args.max_depth,
        "max_pages": args.max_pages,  # not passed -> None -> YAML value stays
        "fail_on_broken": args.fail_on_broken,
        "headed": args.headed or None,
    })
    assert (config.max_pages, config.max_depth, config.fail_on_broken, config.headed) == (5, 1, False, True)


def test_fail_on_broken_flag_is_none_when_not_given():
    assert main.parse_args([]).fail_on_broken is None
    assert main.parse_args(["--fail-on-broken"]).fail_on_broken is True


def test_empty_yaml_file_uses_defaults(tmp_path):
    assert load_config(write_yaml(tmp_path, "")) == Config()


def test_project_config_file_is_valid():
    config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    assert config.start_url == "https://the-internet.herokuapp.com"
    assert config.fail_on_broken is False


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("max_page: 5\n", "Unknown config key"),  # typo
        ("start_url: example.com\n", r"absolute http\(s\) URL"),
        ("max_pages: 0\n", "max_pages must be at least 1"),
        ("timeout_seconds: 0\n", "timeout_seconds must be greater than 0"),
        ("ignore_patterns: ['([']\n", "Invalid ignore pattern"),
        ("- just\n- a list\n", "must contain a mapping"),
    ],
)
def test_invalid_config_is_rejected(tmp_path, yaml_text, message):
    with pytest.raises(ValueError, match=message):
        load_config(write_yaml(tmp_path, yaml_text))
