"""Spec 32 ticket 25: `__version__` is derived from the installed package
metadata and pinned to the package version declared in pyproject.toml, so the
version reported by `tc status` and the `coopd.started` Headcount rows is the
real package version, never a stale literal."""

import tomllib
from importlib.metadata import version as package_version
from pathlib import Path

import turbochickens

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_version_pinned_to_pyproject_package_version() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert turbochickens.__version__ == pyproject["project"]["version"]


def test_version_matches_installed_package_metadata() -> None:
    assert turbochickens.__version__ == package_version("turbochickens")


def test_version_is_a_real_version_not_the_fallback() -> None:
    assert turbochickens.__version__ != "0.0.0+unknown"
    parts = turbochickens.__version__.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)
