"""Tests for spec 32 ticket 17 (review finding G13): the CI workflow contract.

The artifact under test is `.github/workflows/ci.yml` plus the repo files it
consumes (`pyproject.toml`, `uv.lock`, `tests/`). These tests pin the
workflow's external shape at the file seam:

- a workflow exists under `.github/workflows/` and is valid YAML, triggering on
  both push and pull request;
- the test job matrices Python 3.12 and 3.13 and runs `uv sync --frozen` and
  then the FULL pytest suite (no subset), with no Docker service and no docker
  command — the suite itself is loopback-only (AST-scanned: no test shells out
  to the `docker` CLI);
- a separate job runs the dependency audit (`uv audit`) and the image-vs-lock
  dependency diff (consumed by ticket 16's supply-chain check);
- every action is pinned by commit SHA, not a floating tag.

The image-vs-lock check parses `pyproject.toml` (what the image installs) and
`uv.lock` (the pinned graph) and asserts the lock's root metadata mirrors
pyproject, every declared runtime dependency is pinned exactly once with a
version satisfying its specifier, and every package in the runtime closure is
hash-pinned.

The contract validators take parsed documents, so the negative tests feed them
mutated copies (missing PR trigger, matrix without 3.13, non-frozen sync,
subset pytest, no audit job, floating action tag, drifted/missing/unhashed
lock pin) and assert they refuse.
"""

from __future__ import annotations

import ast
import copy
import re
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO / ".github" / "workflows"
PYPROJECT = REPO / "pyproject.toml"
UV_LOCK = REPO / "uv.lock"
TESTS_DIR = REPO / "tests"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# ---------------------------------------------------------------- workflows


def workflow_paths() -> list[Path]:
    paths = sorted(WORKFLOWS_DIR.glob("*.y*ml")) if WORKFLOWS_DIR.is_dir() else []
    assert paths, "no workflow file exists under .github/workflows/"
    return paths


def load_workflow(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path.name} did not parse to a YAML mapping"
    return doc


def on_raw(doc: dict):
    """The `on:` block — YAML 1.1 parses the bare key `on` as boolean True."""
    for key in ("on", True):
        if key in doc:
            return key, doc[key]
    return None, None


def triggers(doc: dict) -> set:
    _, raw = on_raw(doc)
    if raw is None:
        return set()
    if isinstance(raw, list):
        return {str(t) for t in raw}
    if isinstance(raw, dict):
        return {str(t) for t in raw}
    return {str(raw)}


def jobs_of(doc: dict) -> dict:
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict) and jobs, "workflow declares no jobs"
    return jobs


def matrix_pythons(job: dict) -> list[str]:
    strategy = job.get("strategy") or {}
    matrix = strategy.get("matrix") or {}
    return [str(v) for v in (matrix.get("python-version") or [])]


def matrix_jobs_of(doc: dict) -> list[tuple[str, dict]]:
    return [(jid, job) for jid, job in jobs_of(doc).items() if matrix_pythons(job)]


def run_commands(job: dict) -> list[str]:
    return [
        str(step["run"])
        for step in job.get("steps", [])
        if isinstance(step, dict) and step.get("run")
    ]


def uses_of(job: dict) -> list[str]:
    return [
        str(step["uses"])
        for step in job.get("steps", [])
        if isinstance(step, dict) and step.get("uses")
    ]


def assert_test_job(job: dict, jid: str) -> None:
    """The matrix job must run uv sync --frozen then the FULL suite, Docker-free."""
    pythons = matrix_pythons(job)
    assert "3.12" in pythons and "3.13" in pythons, (
        f"job {jid!r} matrix must cover Python 3.12 and 3.13, got {pythons}"
    )
    cmds = run_commands(job)
    sync_idx = [i for i, c in enumerate(cmds) if "uv sync --frozen" in c]
    assert sync_idx, f"job {jid!r} must run `uv sync --frozen`"
    pytest_idx = [i for i, c in enumerate(cmds) if "pytest" in c]
    assert pytest_idx, f"job {jid!r} must run pytest"
    assert pytest_idx[0] > sync_idx[0], f"job {jid!r} must run the suite after uv sync --frozen"
    suite_cmd = cmds[pytest_idx[0]]
    assert "-q" in suite_cmd, f"job {jid!r} pytest run: {suite_cmd!r}"
    assert "tests/" not in suite_cmd and " -k " not in suite_cmd, (
        f"job {jid!r} must run the FULL suite, not a subset: {suite_cmd!r}"
    )
    assert "services" not in job, f"job {jid!r} must not declare Docker services"
    assert not any("docker" in c.lower() for c in cmds), (
        f"job {jid!r} must not invoke the docker CLI"
    )


def assert_audit_job(doc: dict) -> None:
    """A job separate from the test matrix must audit deps and diff image-vs-lock."""
    test_jids = {jid for jid, _ in matrix_jobs_of(doc)}
    others = [(jid, job) for jid, job in jobs_of(doc).items() if jid not in test_jids]
    assert others, "no job separate from the test matrix (the audit job is missing)"
    for jid, job in others:
        cmds = run_commands(job)
        if any(re.search(r"\buv audit\b", c) for c in cmds) and any(
            "pytest" in c and "test_ci.py" in c for c in cmds
        ):
            return
    raise AssertionError(
        "the separate audit job must run the dependency audit (`uv audit`) and "
        "the image-vs-lock check (pytest tests/test_ci.py)"
    )


def assert_actions_sha_pinned(doc: dict) -> None:
    for jid, job in jobs_of(doc).items():
        for uses in uses_of(job):
            ref = uses.split("@", 1)[1] if "@" in uses else ""
            assert SHA_RE.fullmatch(ref), (
                f"job {jid!r} action {uses!r} is not pinned by 40-char commit SHA"
            )


def assert_ci_contract(doc: dict) -> None:
    assert {"push", "pull_request"} <= triggers(doc), (
        f"workflow must trigger on push and pull_request, got {sorted(triggers(doc))}"
    )
    assert doc.get("permissions") == {"contents": "read"}, (
        "workflow must hold the minimal permissions: contents: read"
    )
    assert matrix_jobs_of(doc), "no job carries a python-version matrix"
    for jid, job in matrix_jobs_of(doc):
        assert_test_job(job, jid)
    assert_audit_job(doc)
    assert_actions_sha_pinned(doc)


def ci_docs() -> list[dict]:
    docs = [load_workflow(p) for p in workflow_paths()]
    with_matrix = [d for d in docs if any(matrix_jobs_of(d))]
    assert with_matrix, "no workflow file carries a python-version matrix job"
    return with_matrix


# ------------------------------------------------------------ docker-freedom


def _docker_cli_hits(path: Path) -> list[str]:
    """subprocess/shutil calls that invoke the `docker` binary in this file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner, attr = func.value.id, func.attr
            if owner == "subprocess" and attr in {"run", "Popen", "check_output", "check_call", "call"}:
                if node.args:
                    a0 = node.args[0]
                    if isinstance(a0, (ast.List, ast.Tuple)) and a0.elts:
                        first = a0.elts[0]
                        if isinstance(first, ast.Constant) and isinstance(first.value, str):
                            if first.value.split()[:1] == ["docker"]:
                                hits.append(first.value)
                    elif isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                        if a0.value.split()[:1] == ["docker"]:
                            hits.append(a0.value)
            if owner == "shutil" and attr == "which":
                if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "docker":
                    hits.append("shutil.which('docker')")
    return hits


def assert_suite_needs_no_docker() -> None:
    for f in sorted(TESTS_DIR.glob("*.py")):
        hits = _docker_cli_hits(f)
        assert not hits, f"{f.name} invokes the docker CLI: {hits}"


# ------------------------------------------------------------ image-vs-lock


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_packages(lock: dict) -> dict[str, dict]:
    pkgs: dict[str, dict] = {}
    for p in lock.get("package", []):
        pkgs.setdefault(normalize(p["name"]), p)
    return pkgs


def runtime_closure(pkgs: dict[str, dict]) -> set[str]:
    closure: set[str] = set()
    stack = ["turbochickens"]
    while stack:
        name = stack.pop()
        if name in closure:
            continue
        pkg = pkgs.get(name)
        assert pkg is not None, f"uv.lock has no pin for runtime dependency {name!r}"
        closure.add(name)
        stack.extend(normalize(d["name"]) for d in pkg.get("dependencies", []))
    return closure


def assert_image_lock(pyproject: dict, lock: dict) -> None:
    """The image installs the project; its deps must equal the pinned lock graph."""
    reqs = [Requirement(s) for s in pyproject["project"]["dependencies"]]
    assert lock.get("requires-python") == pyproject["project"]["requires-python"], (
        "uv.lock requires-python drifted from pyproject.toml"
    )
    pkgs = lock_packages(lock)
    root = pkgs.get("turbochickens")
    assert root is not None, "uv.lock has no turbochickens root package"
    meta = {
        (normalize(r["name"]), r.get("specifier", ""))
        for r in root.get("metadata", {}).get("requires-dist", [])
    }
    declared = {(normalize(r.name), str(r.specifier)) for r in reqs}
    assert meta == declared, (
        "uv.lock root requires-dist drifted from pyproject.toml dependencies "
        f"(lock: {sorted(meta)}, pyproject: {sorted(declared)})"
    )
    closure = runtime_closure(pkgs)
    for req in reqs:
        name = normalize(req.name)
        assert name in closure, f"declared dependency {req.name} missing from the lock closure"
        pin = pkgs[name]
        assert Version(pin["version"]) in req.specifier, (
            f"{req.name} is pinned at {pin['version']} which violates {req.specifier}"
        )
    for name in closure:
        pkg = pkgs[name]
        if pkg.get("source", {}).get("editable") is not None:
            continue  # the project itself is built from source
        hashes = ([pkg["sdist"]["hash"]] if pkg.get("sdist") else []) + [
            w["hash"] for w in pkg.get("wheels", [])
        ]
        assert hashes, f"{name} is pinned in uv.lock without any hash"


def load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def load_lock() -> dict:
    return tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))


# ------------------------------------------------------------------- positive


def test_workflow_files_exist_and_parse_as_valid_yaml():
    for path in workflow_paths():
        doc = load_workflow(path)
        assert "jobs" in doc, f"{path.name} declares no jobs"


def test_ci_triggers_on_push_and_pull_request():
    for doc in ci_docs():
        assert {"push", "pull_request"} <= triggers(doc)


def test_matrix_covers_python_3_12_and_3_13_with_frozen_sync_then_full_suite():
    for doc in ci_docs():
        for jid, job in matrix_jobs_of(doc):
            assert_test_job(job, jid)


def test_separate_audit_job_runs_dependency_audit_and_image_lock_diff():
    for doc in ci_docs():
        assert_audit_job(doc)


def test_all_actions_pinned_by_commit_sha():
    for doc in ci_docs():
        assert_actions_sha_pinned(doc)


def test_suite_needs_no_docker_daemon():
    assert_suite_needs_no_docker()
    for doc in ci_docs():
        for jid, job in matrix_jobs_of(doc):
            assert "services" not in job
            assert not any("docker" in c.lower() for c in run_commands(job))


def test_image_dependencies_match_the_pinned_lock():
    assert_image_lock(load_pyproject(), load_lock())


def test_lock_pins_are_hash_pinned_across_the_runtime_closure():
    pkgs = lock_packages(load_lock())
    for name in runtime_closure(pkgs):
        pkg = pkgs[name]
        if pkg.get("source", {}).get("editable") is not None:
            continue
        assert pkg.get("sdist") or pkg.get("wheels"), f"{name} has neither sdist nor wheels"


# ------------------------------------------------------------------- negative


def _ci_doc() -> dict:
    return copy.deepcopy(ci_docs()[0])


def test_contract_rejects_missing_pull_request_trigger():
    doc = _ci_doc()
    key, raw = on_raw(doc)
    del raw["pull_request"]
    with pytest.raises(AssertionError, match="push and pull_request"):
        assert_ci_contract(doc)


def test_contract_rejects_matrix_without_python_3_13():
    doc = _ci_doc()
    for _jid, job in matrix_jobs_of(doc):
        job["strategy"]["matrix"]["python-version"] = ["3.12"]
    with pytest.raises(AssertionError, match="3.12 and 3.13"):
        assert_ci_contract(doc)


def test_contract_rejects_non_frozen_sync():
    doc = _ci_doc()
    for _jid, job in matrix_jobs_of(doc):
        job["steps"] = [
            {**s, "run": s["run"].replace("uv sync --frozen", "uv sync")} if "run" in s else s
            for s in job["steps"]
        ]
    with pytest.raises(AssertionError, match="uv sync --frozen"):
        assert_ci_contract(doc)


def test_contract_rejects_subset_pytest_run():
    doc = _ci_doc()
    for _jid, job in matrix_jobs_of(doc):
        job["steps"] = [
            {**s, "run": ".venv/bin/python -m pytest -q tests/test_daemon.py"}
            if "run" in s and "pytest" in s["run"]
            else s
            for s in job["steps"]
        ]
    with pytest.raises(AssertionError, match="FULL suite"):
        assert_ci_contract(doc)


def test_contract_rejects_docker_service_on_the_test_job():
    doc = _ci_doc()
    for _jid, job in matrix_jobs_of(doc):
        job["services"] = {"docker": {"image": "docker:latest"}}
    with pytest.raises(AssertionError, match="Docker services"):
        assert_ci_contract(doc)


def test_contract_rejects_missing_audit_job():
    doc = _ci_doc()
    test_jids = {jid for jid, _ in matrix_jobs_of(doc)}
    for jid in list(doc["jobs"]):
        if jid not in test_jids:
            del doc["jobs"][jid]
    with pytest.raises(AssertionError, match="separate"):
        assert_ci_contract(doc)


def test_contract_rejects_floating_action_tag():
    doc = _ci_doc()
    for _jid, job in doc["jobs"].items():
        for step in job.get("steps", []):
            if "uses" in step:
                step["uses"] = step["uses"].split("@")[0] + "@v7"
    with pytest.raises(AssertionError, match="SHA"):
        assert_ci_contract(doc)


def test_lock_diff_rejects_pin_violating_the_declared_specifier():
    lock = copy.deepcopy(load_lock())
    for p in lock["package"]:
        if p["name"] == "aiohttp":
            p["version"] = "3.9.0"  # violates pyproject's aiohttp>=3.10
    with pytest.raises(AssertionError, match="violates"):
        assert_image_lock(load_pyproject(), lock)


def test_lock_diff_rejects_missing_pin():
    lock = copy.deepcopy(load_lock())
    lock["package"] = [p for p in lock["package"] if p["name"] != "croniter"]
    with pytest.raises(AssertionError, match="no pin"):
        assert_image_lock(load_pyproject(), lock)


def test_lock_diff_rejects_stale_root_metadata():
    lock = copy.deepcopy(load_lock())
    for p in lock["package"]:
        if p["name"] == "turbochickens":
            p["metadata"]["requires-dist"] = [
                r for r in p["metadata"]["requires-dist"] if r["name"] != "typer"
            ]
    with pytest.raises(AssertionError, match="drifted"):
        assert_image_lock(load_pyproject(), lock)


def test_lock_diff_rejects_unhashed_pin():
    lock = copy.deepcopy(load_lock())
    for p in lock["package"]:
        if p["name"] == "yarl":
            p.pop("sdist", None)
            p["wheels"] = []
    with pytest.raises(AssertionError, match="without any hash"):
        assert_image_lock(load_pyproject(), lock)


def test_specifier_seam_actually_bites():
    # guard the guard: the specifier comparison used above must reject a bad pin
    assert Version("3.9.0") not in SpecifierSet(">=3.10")
    assert Version("3.14.5") in SpecifierSet(">=3.10")
