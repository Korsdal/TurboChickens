"""TurboChickens — local-first platform for ephemeral AI agent jobs."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

try:
    # The real version comes from the installed package metadata, which is
    # generated from pyproject.toml at install time — so `tc status` and the
    # `coopd.started` Headcount rows record the version actually running.
    __version__ = _package_version("turbochickens")
except PackageNotFoundError:  # running from a bare source checkout, not installed
    __version__ = "0.0.0+unknown"
