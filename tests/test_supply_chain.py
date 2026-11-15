"""Tests for spec 32 ticket 16 (review findings G22 + G23): the supply-chain
and compose contract of the coop container.

No Docker daemon is required (the repo's test environment has none) — these
are contract tests at the file seam, over the exact artifacts a `docker build`
would consume:

- the Dockerfile pins the **base image by digest**, pulls the **uv installer by
  digest** from ghcr.io, and installs dependencies **only from the hash-pinned
  `uv.lock`** (`uv export --frozen` + `uv pip install --require-hashes`, then
  the project itself with `--no-deps`, built by the build backend pinned
  exactly in `pyproject.toml` `[build-system]`);
- `.dockerignore` keeps `uv.lock` in the build context but excludes local
  state and secret files (`data/`, `.env*`, the root-level
  `providers.toml` / `connectors.toml` / `settings.toml`);
- `docker/compose.yaml` publishes 8737 on **127.0.0.1 only** and carries the
  **fixed-token settings mount commented out**;
- the CI audit job wires the image-vs-lock diff and these supply-chain checks.

The validators take parsed documents / raw texts, so the negative tests feed
them mutated copies (floating base tag, undigested uv ref, unpinned backend,
lock excluded from the context, hash checking dropped, non-loopback publish,
active token mount, unwired CI) and assert they refuse.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"
COMPOSE = REPO / "docker" / "compose.yaml"
PYPROJECT = REPO / "pyproject.toml"
UV_LOCK = REPO / "uv.lock"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"

HEX64 = r"[0-9a-f]{64}"


# ---------------------------------------------------------------- loaders


def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def ignore_entries(text: str | None = None) -> list[str]:
    text = text if text is not None else DOCKERIGNORE.read_text(encoding="utf-8")
    return [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def pyproject_doc() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def compose_doc(text: str | None = None) -> dict:
    return yaml.safe_load(text if text is not None else compose_text())


def lock_doc() -> dict:
    return tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))


def entry_matches(entry: str, path: str) -> bool:
    """Approximate .dockerignore matching for the root-level paths we check."""
    if entry.startswith("/"):
        return fnmatch.fnmatch(path, entry[1:]) or fnmatch.fnmatch("/" + path, entry)
    return fnmatch.fnmatch(path, entry)


# ---------------------------------------------------------------- validators


def assert_base_image_pinned_by_digest(text: str) -> None:
    froms = [l.split(None, 1)[1].strip() for l in text.splitlines() if l.startswith("FROM ")]
    assert froms, "Dockerfile has no FROM line"
    assert re.fullmatch(rf"python:3\.12-slim@sha256:{HEX64}", froms[0]), (
        f"base image must be python:3.12-slim pinned by digest, got {froms[0]!r}"
    )


def assert_uv_installer_pinned_by_digest(text: str) -> None:
    m = re.search(r"^COPY --from=(\S+)\s+\\?\s*$", text, re.M)
    assert m, "Dockerfile must pull the uv installer with COPY --from=<image>"
    ref = m.group(1)
    assert re.fullmatch(rf"ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+@sha256:{HEX64}", ref), (
        f"uv installer must be a digest-pinned ghcr.io/astral-sh/uv:<version>@sha256:…, got {ref!r}"
    )
    assert re.search(r"/uv\s+/usr/local/bin/uv", text), "the /uv binary must land on PATH"


def assert_image_installs_from_hash_pinned_lock(text: str, entries: list[str], pyproject: dict) -> None:
    # the lock is in the build context and copied into the image build
    assert not any(entry_matches(e, "uv.lock") for e in entries), (
        "uv.lock must NOT be excluded by .dockerignore — the image installs from the lock"
    )
    assert re.search(r"^COPY pyproject\.toml uv\.lock \./", text, re.M), (
        "Dockerfile must COPY pyproject.toml + uv.lock before installing"
    )
    # dependencies: frozen export (hashes on by default) + hash-checked install
    assert "uv export --frozen" in text, "deps must be exported from the FROZEN lock"
    assert "--no-emit-project" in text, "the project itself is installed separately"
    assert re.search(r"uv pip install --system --require-hashes", text), (
        "deps must be installed with --require-hashes (hash-pinned lock only)"
    )
    # the project is built from source with deps already present
    assert re.search(r"uv pip install --system --no-deps", text), (
        "the project must be installed with --no-deps (deps came from the lock)"
    )
    assert not re.search(r"^RUN pip ", text, re.M), "no floating `pip install` in the Dockerfile"
    # build backend pinned exactly
    requires = list(pyproject["build-system"]["requires"])
    assert any(re.fullmatch(r"hatchling==\d+\.\d+\.\d+", r) for r in requires), (
        f"build backend must be pinned exactly (hatchling==<version>), got {requires}"
    )
    assert all(not (r.split("=")[0].strip() == "hatchling" and "==" not in r) for r in requires), (
        f"hatchling must not float, got {requires}"
    )
    # the lock really is hash-pinned across the runtime closure
    pkgs = {p["name"]: p for p in lock_doc()["package"]}
    for pkg in pkgs.values():
        if pkg.get("source", {}).get("editable") is not None:
            continue
        hashes = ([pkg["sdist"]["hash"]] if pkg.get("sdist") else []) + [
            w["hash"] for w in pkg.get("wheels", [])
        ]
        assert hashes, f"{pkg['name']} is pinned in uv.lock without any hash"


def assert_dockerignore_excludes_local_state_and_secrets(entries: list[str]) -> None:
    # local state and secret files stay out of the build context
    assert any(e.rstrip("/") == "data" for e in entries), "data/ must be excluded"
    assert ".env*" in entries, ".env* must be excluded"
    for secret in ("/providers.toml", "/connectors.toml", "/settings.toml"):
        assert secret in entries, f"{secret} must be excluded (root-anchored)"
    # …while everything the image needs stays IN the context
    for keep in ("uv.lock", "pyproject.toml", "src", "docker", "docker/settings.toml"):
        assert not any(entry_matches(e, keep) for e in entries), (
            f"{keep} must stay in the build context but .dockerignore excludes it"
        )


def assert_compose_binds_loopback_only(doc: dict) -> None:
    ports = doc["services"]["coop"]["ports"]
    assert ports == ["127.0.0.1:8737:8737"], (
        f"compose must publish 8737 on 127.0.0.1 only, got {ports}"
    )


def assert_fixed_token_mount_present_but_commented(text: str, doc: dict) -> None:
    volumes = doc["services"]["coop"]["volumes"]
    assert volumes == ["coop-data:/data"], (
        f"the fixed-token settings mount must be COMMENTED OUT, active volumes: {volumes}"
    )
    comments = [l for l in text.splitlines() if l.lstrip().startswith("#")]
    assert any(re.search(r"#\s*-\s*\./\S+:/etc/turbochickens/settings\.toml:ro", l) for l in comments), (
        "a commented mount over /etc/turbochickens/settings.toml must be shown"
    )
    assert any("coop_auth_token" in l for l in comments), (
        "the commented mount must explain it pins coop_auth_token"
    )


def assert_ci_wires_the_supply_chain_checks(text: str | None = None) -> None:
    text = text if text is not None else CI_YML.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    jobs = doc.get("jobs", {})
    audit_cmds = [
        str(step["run"])
        for job in jobs.values()
        for step in job.get("steps", [])
        if isinstance(step, dict) and step.get("run") and "pytest" in str(step["run"])
        and "test_ci.py" in str(step["run"])
    ]
    assert audit_cmds, "no CI step runs the image-vs-lock check (tests/test_ci.py)"
    assert any("tests/test_supply_chain.py" in c for c in audit_cmds), (
        "the CI audit job must also run tests/test_supply_chain.py "
        f"(the ticket-16 supply-chain contract), got {audit_cmds}"
    )


def assert_supply_chain_contract(
    dockerfile: str, entries: list[str], pyproject: dict, compose: str, doc: dict
) -> None:
    assert_base_image_pinned_by_digest(dockerfile)
    assert_uv_installer_pinned_by_digest(dockerfile)
    assert_image_installs_from_hash_pinned_lock(dockerfile, entries, pyproject)
    assert_dockerignore_excludes_local_state_and_secrets(entries)
    assert_compose_binds_loopback_only(doc)
    assert_fixed_token_mount_present_but_commented(compose, doc)
    assert_ci_wires_the_supply_chain_checks()


# ---------------------------------------------------------------- positive


def test_base_image_is_pinned_by_digest():
    assert_base_image_pinned_by_digest(dockerfile_text())


def test_uv_installer_is_pinned_by_digest():
    assert_uv_installer_pinned_by_digest(dockerfile_text())


def test_image_installs_only_from_the_hash_pinned_lock():
    assert_image_installs_from_hash_pinned_lock(dockerfile_text(), ignore_entries(), pyproject_doc())


def test_build_backend_is_pinned_exactly():
    requires = pyproject_doc()["build-system"]["requires"]
    assert requires == ["hatchling==1.33.0"]


def test_lock_is_hash_pinned_across_every_registry_package():
    for pkg in lock_doc()["package"]:
        if pkg.get("source", {}).get("editable") is not None:
            continue
        assert pkg.get("sdist") or pkg.get("wheels"), pkg["name"]


def test_dockerignore_excludes_local_state_and_secret_files():
    assert_dockerignore_excludes_local_state_and_secrets(ignore_entries())


def test_compose_publishes_8737_on_loopback_only():
    assert_compose_binds_loopback_only(compose_doc())


def test_fixed_token_mount_is_present_but_commented():
    assert_fixed_token_mount_present_but_commented(compose_text(), compose_doc())


def test_ci_audit_job_runs_the_supply_chain_checks():
    assert_ci_wires_the_supply_chain_checks()


def test_full_supply_chain_contract_holds():
    assert_supply_chain_contract(
        dockerfile_text(), ignore_entries(), pyproject_doc(), compose_text(), compose_doc()
    )


# ---------------------------------------------------------------- negative


def test_contract_rejects_floating_base_tag():
    text = re.sub(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$", "FROM python:3.12-slim", dockerfile_text(), flags=re.M)
    with pytest.raises(AssertionError, match="digest"):
        assert_base_image_pinned_by_digest(text)


def test_contract_rejects_short_or_malformed_digest():
    text = re.sub(r"sha256:[0-9a-f]{64}", "sha256:abc123", dockerfile_text(), count=1)
    with pytest.raises(AssertionError, match="digest"):
        assert_base_image_pinned_by_digest(text)


def test_contract_rejects_uv_installer_without_digest():
    text = dockerfile_text().replace(
        "ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5",
        "ghcr.io/astral-sh/uv:0.11.26",
    )
    with pytest.raises(AssertionError, match="digest-pinned"):
        assert_uv_installer_pinned_by_digest(text)


def test_contract_rejects_lock_excluded_from_build_context():
    with pytest.raises(AssertionError, match="uv.lock must NOT be excluded"):
        assert_image_installs_from_hash_pinned_lock(
            dockerfile_text(), ignore_entries() + ["uv.lock"], pyproject_doc()
        )


def test_contract_rejects_missing_lock_copy():
    text = dockerfile_text().replace("COPY pyproject.toml uv.lock ./", "COPY pyproject.toml ./")
    with pytest.raises(AssertionError, match="COPY pyproject.toml \\+ uv.lock"):
        assert_image_installs_from_hash_pinned_lock(text, ignore_entries(), pyproject_doc())


def test_contract_rejects_non_frozen_export():
    text = dockerfile_text().replace("uv export --frozen", "uv export")
    with pytest.raises(AssertionError, match="FROZEN"):
        assert_image_installs_from_hash_pinned_lock(text, ignore_entries(), pyproject_doc())


def test_contract_rejects_install_without_hash_checking():
    text = dockerfile_text().replace("--system --require-hashes", "--system")
    with pytest.raises(AssertionError, match="require-hashes"):
        assert_image_installs_from_hash_pinned_lock(text, ignore_entries(), pyproject_doc())


def test_contract_rejects_floating_pip_install():
    text = dockerfile_text() + "\nRUN pip install --no-cache-dir requests\n"
    with pytest.raises(AssertionError, match="floating"):
        assert_image_installs_from_hash_pinned_lock(text, ignore_entries(), pyproject_doc())


def test_contract_rejects_unpinned_build_backend():
    pyproject = pyproject_doc()
    pyproject["build-system"]["requires"] = ["hatchling"]
    with pytest.raises(AssertionError, match="pinned exactly"):
        assert_image_installs_from_hash_pinned_lock(dockerfile_text(), ignore_entries(), pyproject)


def test_contract_rejects_ranged_build_backend():
    pyproject = pyproject_doc()
    pyproject["build-system"]["requires"] = ["hatchling>=1.30"]
    with pytest.raises(AssertionError, match="pinned exactly"):
        assert_image_installs_from_hash_pinned_lock(dockerfile_text(), ignore_entries(), pyproject)


def test_contract_rejects_unhashed_lock_pin():
    lock = lock_doc()
    for pkg in lock["package"]:
        if pkg["name"] == "yarl":
            pkg.pop("sdist", None)
            pkg["wheels"] = []
    # re-run the hash half of the validator against the mutated lock
    pkgs = {p["name"]: p for p in lock["package"]}
    with pytest.raises(AssertionError, match="without any hash"):
        for pkg in pkgs.values():
            if pkg.get("source", {}).get("editable") is not None:
                continue
            hashes = ([pkg["sdist"]["hash"]] if pkg.get("sdist") else []) + [
                w["hash"] for w in pkg.get("wheels", [])
            ]
            assert hashes, f"{pkg['name']} is pinned in uv.lock without any hash"


def test_contract_rejects_dockerignore_missing_data():
    entries = [e for e in ignore_entries() if e.rstrip("/") != "data"]
    with pytest.raises(AssertionError, match="data/"):
        assert_dockerignore_excludes_local_state_and_secrets(entries)


def test_contract_rejects_dockerignore_missing_env_files():
    entries = [e for e in ignore_entries() if e != ".env*"]
    with pytest.raises(AssertionError, match=".env"):
        assert_dockerignore_excludes_local_state_and_secrets(entries)


def test_contract_rejects_dockerignore_missing_secret_tomls():
    entries = [e for e in ignore_entries() if e != "/providers.toml"]
    with pytest.raises(AssertionError, match="providers.toml"):
        assert_dockerignore_excludes_local_state_and_secrets(entries)


def test_contract_rejects_dockerignore_swallowing_the_context():
    # a broad *.toml pattern would exclude pyproject.toml and docker/settings.toml
    with pytest.raises(AssertionError, match="stay in the build context"):
        assert_dockerignore_excludes_local_state_and_secrets(ignore_entries() + ["*.toml"])


def test_contract_rejects_all_interfaces_publish():
    doc = compose_doc()
    doc["services"]["coop"]["ports"] = ["8737:8737"]
    with pytest.raises(AssertionError, match="127.0.0.1"):
        assert_compose_binds_loopback_only(doc)


def test_contract_rejects_explicit_wildcard_publish():
    doc = compose_doc()
    doc["services"]["coop"]["ports"] = ["0.0.0.0:8737:8737"]
    with pytest.raises(AssertionError, match="127.0.0.1"):
        assert_compose_binds_loopback_only(doc)


def test_contract_rejects_active_token_mount():
    doc = compose_doc()
    doc["services"]["coop"]["volumes"] = [
        "coop-data:/data",
        "./coop-settings.toml:/etc/turbochickens/settings.toml:ro",
    ]
    with pytest.raises(AssertionError, match="COMMENTED OUT"):
        assert_fixed_token_mount_present_but_commented(compose_text(), doc)


def test_contract_rejects_missing_commented_token_mount():
    text = "\n".join(
        l for l in compose_text().splitlines() if ":/etc/turbochickens/settings.toml" not in l
    )
    with pytest.raises(AssertionError, match="commented mount"):
        assert_fixed_token_mount_present_but_commented(text, compose_doc())


def test_contract_rejects_unwired_ci():
    text = CI_YML.read_text(encoding="utf-8").replace(
        "tests/test_ci.py tests/test_supply_chain.py", "tests/test_ci.py"
    )
    with pytest.raises(AssertionError, match="test_supply_chain.py"):
        assert_ci_wires_the_supply_chain_checks(text)
