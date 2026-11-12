"""Tests for settings loading: defaults, overrides, derived paths, errors."""

from __future__ import annotations

import math
import os

import pytest

import turbochickens.settings as settings_mod
from turbochickens.settings import (
    Settings,
    SettingsError,
    default_weight_budget,
    load_settings,
)


@pytest.fixture(autouse=True)
def no_env_settings(monkeypatch, tmp_path):
    """Isolate from any ambient TURBOCHICKENS_SETTINGS and default settings file."""
    monkeypatch.delenv("TURBOCHICKENS_SETTINGS", raising=False)
    monkeypatch.setattr(settings_mod, "DEFAULT_DATA_DIR", tmp_path / "home" / ".turbochickens")
    return tmp_path


def test_defaults_when_no_settings_file() -> None:
    s = load_settings(None)
    assert s.data_dir == settings_mod.DEFAULT_DATA_DIR
    assert s.weight_budget_override is None
    assert s.audit_retention == "forever"
    assert s.results_retention_months == 6
    assert s.weight_budget == default_weight_budget()
    assert s.socket_path == s.data_dir / "daemon.sock"
    assert s.db_path == s.data_dir / "turbochickens.db"
    assert s.audit_log_path == s.data_dir / "audit.jsonl"


def test_model_defaults_match_documented() -> None:
    s = Settings()
    assert s.audit_retention == "forever"
    assert s.results_retention_months == 6


def test_override_file(tmp_path) -> None:
    data_dir = tmp_path / "data"
    f = tmp_path / "settings.toml"
    f.write_text(
        f'data_dir = "{data_dir}"\n'
        "weight_budget_override = 4\n"
        "results_retention_months = 3\n"
    )
    s = load_settings(f)
    assert s.data_dir == data_dir
    assert s.weight_budget_override == 4
    assert s.weight_budget == 4  # override wins over the machine formula
    assert s.results_retention_months == 3
    assert s.audit_retention == "forever"
    assert s.socket_path == data_dir / "daemon.sock"
    assert s.db_path == data_dir / "turbochickens.db"
    assert s.audit_log_path == data_dir / "audit.jsonl"


def test_env_var_selects_settings_file(tmp_path, monkeypatch) -> None:
    f = tmp_path / "env-settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "env-data"}"\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    s = load_settings(None)
    assert s.data_dir == tmp_path / "env-data"


def test_explicit_missing_file_raises(tmp_path) -> None:
    with pytest.raises(SettingsError, match="settings file not found"):
        load_settings(tmp_path / "nope.toml")


def test_env_var_missing_file_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(tmp_path / "nope.toml"))
    with pytest.raises(SettingsError, match="settings file not found"):
        load_settings(None)


def test_unknown_key_rejected(tmp_path) -> None:
    f = tmp_path / "settings.toml"
    f.write_text('bogus_key = 1\n')
    with pytest.raises(SettingsError, match="bogus_key"):
        load_settings(f)


def test_bad_retention_value_rejected(tmp_path) -> None:
    f = tmp_path / "settings.toml"
    f.write_text('audit_retention = "30d"\n')
    with pytest.raises(SettingsError, match="audit_retention"):
        load_settings(f)


def test_invalid_toml_rejected(tmp_path) -> None:
    f = tmp_path / "settings.toml"
    f.write_text("data_dir = [unclosed\n")
    with pytest.raises(SettingsError, match="cannot load settings"):
        load_settings(f)


def test_default_weight_budget_formula() -> None:
    vcpu = os.cpu_count() or 1
    ram_gb = (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024**3)
    expected = max(1, min(vcpu, math.floor(ram_gb / 1.5)))
    assert default_weight_budget() == expected
    assert default_weight_budget() >= 1
