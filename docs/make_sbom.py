#!/usr/bin/env python3
"""Regenerate docs/sbom.cdx.json from uv.lock — Python stdlib only.

Usage, from the repo root:

    .venv/bin/python docs/make_sbom.py

No network access and no third-party packages: the lock is parsed with
``tomllib`` (stdlib, Python >= 3.11; this project requires >= 3.12) and the BOM
is written as CycloneDX 1.5 JSON with ``json``.

What the BOM contains:

- ``metadata.component`` — the root component: the ``turbochickens`` project
  itself, name/version taken from ``pyproject.toml`` and cross-checked against
  the lock's editable ``turbochickens`` entry (the two must agree);
- ``components`` — every ``[[package]]`` entry in ``uv.lock`` (61 entries at
  lock revision 3), each a ``library`` component with ``name``, ``version``,
  ``purl`` of the form ``pkg:pypi/<name>@<version>`` (name PEP 503-normalized),
  and the sha256 hashes the lock carries for the package's sdist/wheels.

The serial number is deterministic: a UUIDv5 over the sha256 of ``uv.lock``, so
regenerating from an unchanged lock reproduces the same serial.

After writing, the script reads the file back with ``json.load`` and asserts the
BOM shape and the component count against the package entries in ``uv.lock``;
it exits non-zero on any mismatch.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = REPO_ROOT / "uv.lock"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
OUT_PATH = REPO_ROOT / "docs" / "sbom.cdx.json"

BOM_FORMAT = "CycloneDX"
SPEC_VERSION = "1.5"
BOM_VERSION = 1
GENERATOR_NAME = "make_sbom.py"
GENERATOR_VERSION = "1.0.0"
#: stable namespace for the deterministic serial number
SBOM_NAMESPACE_URL = "https://github.com/Korsdal/TurboChickens/blob/main/docs/sbom.cdx.json"


def pep503_normalize(name: str) -> str:
    """PEP 503 normalization: lowercase, runs of ``-``/``_``/``.`` become ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pypi_purl(name: str, version: str) -> str:
    """``pkg:pypi/<normalized-name>@<version>`` (version percent-encoded)."""
    return f"pkg:pypi/{quote(pep503_normalize(name), safe='')}@{quote(version, safe='')}"


def lock_sha256_hashes(package: dict) -> list[dict[str, str]]:
    """The sha256 hashes the lock carries for one package's sdist and wheels,
    as CycloneDX hash entries (alg names per the CycloneDX hash algorithm enum)."""
    hashes: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        if not raw or not raw.startswith("sha256:"):
            return
        content = raw.split(":", 1)[1]
        if content not in seen:
            seen.add(content)
            hashes.append({"alg": "SHA-256", "content": content})

    add(package.get("sdist", {}).get("hash"))
    for wheel in package.get("wheels", []) or []:
        add(wheel.get("hash"))
    return hashes


def build_bom(lock: dict, project: dict) -> dict:
    packages = lock["package"]
    root_name = project["project"]["name"]
    root_version = project["project"]["version"]

    lock_root = next((p for p in packages if p["name"] == root_name), None)
    if lock_root is None:
        raise SystemExit(f"error: {root_name!r} from pyproject.toml has no entry in uv.lock")
    if lock_root["version"] != root_version:
        raise SystemExit(
            f"error: version mismatch — pyproject.toml says {root_version},"
            f" uv.lock says {lock_root['version']}"
        )

    lock_bytes = LOCK_PATH.read_bytes()
    lock_digest = hashlib.sha256(lock_bytes).hexdigest()
    serial = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"{SBOM_NAMESPACE_URL}#{lock_digest}"))

    components = []
    for package in sorted(packages, key=lambda p: p["name"]):
        name = package["name"]
        version = package["version"]
        purl = pypi_purl(name, version)
        component = {
            "type": "library",
            "bom-ref": purl,
            "name": name,
            "version": version,
            "purl": purl,
        }
        hashes = lock_sha256_hashes(package)
        if hashes:
            component["hashes"] = hashes
        components.append(component)

    root_purl = pypi_purl(root_name, root_version)
    return {
        "bomFormat": BOM_FORMAT,
        "specVersion": SPEC_VERSION,
        "serialNumber": serial,
        "version": BOM_VERSION,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools": [
                {
                    "type": "application",
                    "name": GENERATOR_NAME,
                    "version": GENERATOR_VERSION,
                }
            ],
            "component": {
                "type": "application",
                "bom-ref": root_purl,
                "name": root_name,
                "version": root_version,
                "purl": root_purl,
            },
        },
        "components": components,
    }


def validate(bom: dict, lock: dict, project: dict) -> None:
    """Sanity-check the written BOM against the lock: format, root component,
    and component count vs the ``[[package]]`` entries in uv.lock."""
    packages = lock["package"]
    assert bom["bomFormat"] == BOM_FORMAT, "bomFormat"
    assert bom["specVersion"] == SPEC_VERSION, "specVersion"
    assert bom["version"] == BOM_VERSION, "BOM version"
    root = bom["metadata"]["component"]
    assert root["name"] == project["project"]["name"], "root name"
    assert root["version"] == project["project"]["version"], "root version"
    assert root["purl"] == pypi_purl(root["name"], root["version"]), "root purl"
    components = bom["components"]
    assert len(components) == len(packages), (
        f"component count {len(components)} != lock package count {len(packages)}"
    )
    lock_by_name = {p["name"]: p for p in packages}
    for component in components:
        lock_pkg = lock_by_name[component["name"]]
        assert component["version"] == lock_pkg["version"], f"version of {component['name']}"
        assert component["purl"] == pypi_purl(lock_pkg["name"], lock_pkg["version"]), (
            f"purl of {component['name']}"
        )
        assert component["bom-ref"] == component["purl"], f"bom-ref of {component['name']}"
    assert root["bom-ref"] in {c["bom-ref"] for c in components}, "root not among components"


def main() -> int:
    lock = tomllib.loads(LOCK_PATH.read_text(encoding="utf-8"))
    project = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))

    bom = build_bom(lock, project)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(bom, indent=2, ensure_ascii=False) + "\n"
    OUT_PATH.write_text(text, encoding="utf-8")

    # validate by loading the written file back and comparing against the lock
    written = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    validate(written, lock, project)

    n = len(written["components"])
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} ({OUT_PATH.stat().st_size} bytes)")
    print(f"bomFormat={written['bomFormat']} specVersion={written['specVersion']} serialNumber={written['serialNumber']}")
    print(
        f"root component: {written['metadata']['component']['name']}"
        f" {written['metadata']['component']['version']} ({written['metadata']['component']['purl']})"
    )
    print(f"components: {n} (uv.lock [[package]] entries: {len(lock['package'])}) — validated with json.load")
    return 0


if __name__ == "__main__":
    sys.exit(main())
