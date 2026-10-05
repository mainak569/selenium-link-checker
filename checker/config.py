"""Load settings from config.yaml and apply command-line overrides on top."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; selenium-link-checker/1.0; "
    "+https://github.com/mainak569/selenium-link-checker)"
)


@dataclass
class Config:
    start_url: str = "https://the-internet.herokuapp.com"
    max_pages: int = 30
    max_depth: int = 2
    delay_seconds: float = 1.0
    timeout_seconds: float = 10.0
    retries: int = 1
    page_load_timeout_seconds: float = 60.0
    max_workers: int = 10
    slow_page_ms: int = 3000
    fail_on_broken: bool = False
    ignore_patterns: list[str] = field(default_factory=list)
    user_agent: str = DEFAULT_USER_AGENT
    # The two settings below are CLI-only, but living here keeps one source of truth.
    output_dir: str = "report"
    headed: bool = False

    def validate(self) -> None:
        """Fail fast on values that would otherwise break the run halfway through."""
        parts = urlsplit(self.start_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"start_url must be an absolute http(s) URL, got {self.start_url!r}")
        for name in ("max_pages", "max_workers"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        for name in ("max_depth", "retries", "delay_seconds", "slow_page_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        for name in ("timeout_seconds", "page_load_timeout_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than 0")
        for pattern in self.ignore_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"Invalid ignore pattern {pattern!r}: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_yaml(path: str | Path) -> dict[str, Any]:
    """Read a YAML config file. An empty file counts as an empty config."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping of settings, got {type(data).__name__}")
    return data


def merge(file_values: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Return file values updated with CLI overrides.

    argparse leaves options the user didn't pass as None, so None means
    "not given" and must not overwrite the value from the file.
    """
    merged = dict(file_values)
    merged.update({key: value for key, value in overrides.items() if value is not None})
    return merged


def load_config(path: str | Path | None, overrides: Mapping[str, Any] | None = None) -> Config:
    """Build a validated Config from built-in defaults, then the YAML file, then CLI flags."""
    file_values = read_yaml(path) if path else {}
    values = merge(file_values, overrides or {})

    known = set(Config.__dataclass_fields__)
    unknown = sorted(set(values) - known)
    if unknown:
        # A typo like "max_page" would otherwise be silently ignored.
        raise ValueError(f"Unknown config key(s): {', '.join(unknown)}")

    if values.get("ignore_patterns") is None:
        values["ignore_patterns"] = []
    config = Config(**values)
    config.validate()
    return config
