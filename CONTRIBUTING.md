# Contributing to TurboChickens

How to work on this repo: setup, tests, the spec-ticket workflow, and the house rules that keep the farm coherent.

## Setup

```sh
uv sync                 # .venv with turbochickens + the dev group (pytest)
.venv/bin/tc --help     # the CLI
```

Python >=3.12. The daemon and the CLI come from the same package — `tc` is the `[project.scripts]` entry point in `pyproject.toml`, coopd is `tc daemon start`.

## Run the tests

```sh
.venv/bin/python -m pytest -q
```

The suite is loopback-only and needs no Docker daemon. It currently stands at 742 tests. CI runs the same command on every push and pull request (`.github/workflows/ci.yml`, Python 3.12 and 3.13), plus an audit job that runs `uv lock --check`, `uv audit`, and the image-vs-lock diff — keep both green.

## Work in spec tickets

Changes land as spec tickets, one commit per ticket:

- A ticket is a file `specs/NN-<slug>.md` with a "What to build" paragraph, a "Blocked by" line, a "Status" line, and a checkbox acceptance list.
- Commit subjects are imperative and scoped to the ticket. One commit per ticket — no squash-and-smear.
- Never add a `Co-Authored-By: Mistral Vibe` trailer. That is a repo rule: the address maps to a GitHub contributor, and forging it misattributes the work.
- Update `STATUS.md` at every spec boundary.

## Version rule

Agents may bump only the `0.0.X` patch part of `version` in `pyproject.toml`. Minor and major bumps are human decisions.

## Adding a dependency

Add the requirement to `pyproject.toml` (`dependencies` for runtime, the `dev` group for tooling), then:

```sh
uv lock
uv sync
```

CI asserts the lock matches `pyproject.toml` (`uv lock --check`) and audits it (`uv audit`), and the coop container installs only from the hash-pinned `uv.lock` — a stale lock fails both.

## Naming: the lexicon comes first

Every operator-facing noun goes through [docs/lexicon.md](docs/lexicon.md). If a term is missing, add it there first, then use it. Machine interfaces — command names, op names, settings keys, audit events — stay byte-exact; the lexicon governs prose, not machine strings. Retired terms stay retired — see the retired table in the lexicon.

## Design system

GUI work follows [specs/00-design-system.md](specs/00-design-system.md) (the settled system) and `specs/33-design-system-baseline/` (variant A, light, is the default grade; variant B is the dark grade; the voice card there governs copy). Docs and media use the same variant-A tokens; working pagecast packs live under `specs/33-design-system-baseline/media-pack/` and render with `pagecast build . --no-encode` + `pagecast stills .`.

## CI

Push a branch or open a PR: `.github/workflows/ci.yml` runs the full suite on Python 3.12 and 3.13 plus the dependency audit and image-vs-lock job. The suite must pass before merge.
