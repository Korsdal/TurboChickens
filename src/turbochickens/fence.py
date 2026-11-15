"""The Fence — the one shared "safe relative ref" containment rule (spec 32, G1).

Every place that turns a spec-supplied *ref* into a host path must pass through
this module, so the rule exists exactly once:

- **lexical rule** (:func:`safe_ref`): a ref is a relative POSIX path — no
  absolute path, no backslash, no NUL byte, no empty/``.``/``..`` segment;
- **containment rule** (:func:`resolve_contained`): the resolved target must
  stay under the *resolved* base — symlink-safe, so a link pointing out of the
  base resolves outside and is refused.

Applied by the Egg spec loader for ``input.prompt_file``, non-nestbox
``input.attachments``, and ``output.schema_file`` at Candling, and defensively
again by the Hatchery before any file is copied or read at provisioning. The
Beak's workspace path lock (:mod:`turbochickens.beak`) delegates here too — it
was the reference implementation.

Refusals speak farm per ``docs/lexicon.md``: the **Fox** refuses at the
**fence**, and the refusal is a field-named error matching the existing
contract (``<dotted.field>: the fox refuses …``). Machine interfaces are
untouched — this module only guards which host paths a ref may name.
"""

from __future__ import annotations

from pathlib import Path


class UnsafeRefError(Exception):
    """A ref that crosses the fence. Carries the dotted ``field`` name so the
    refusal stays field-named like every other spec error."""

    def __init__(self, field: str, ref: object, detail: str, area: str = "the egg's own directory") -> None:
        self.field = field
        self.ref = ref
        self.detail = detail
        self.area = area
        self.fox = fox_refusal(ref, detail, area)  # refusal copy without the field prefix
        super().__init__(f"{field}: {self.fox}")


def safe_ref(ref: object) -> str | None:
    """Normalize one ref: relative, POSIX, no empty/'.'/'..' segments, no
    backslash or NUL, not absolute. Returns the normalized relative path, or
    None when the ref tries to leave its base lexically."""
    if not isinstance(ref, str) or not ref or ref != ref.strip():
        return None
    if ref.startswith("/") or "\\" in ref or "\x00" in ref:
        return None
    parts = ref.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return "/".join(parts)


def resolve_contained(base: Path, ref: str) -> Path | None:
    """Resolve ``base / ref`` and require the result to stay inside ``base``.
    The ref must first pass :func:`safe_ref`, and the resolved target must
    additionally stay inside the resolved base — symlink-safe: a link pointing
    out of the base resolves outside and is refused. Returns the resolved
    path, or None on any escape."""
    if safe_ref(ref) != ref:
        return None
    base_resolved = base.resolve()
    target = (base_resolved / ref).resolve()
    if target == base_resolved or base_resolved in target.parents:
        return target
    return None


def fox_refusal(ref: object, detail: str, area: str = "the egg's own directory") -> str:
    """Operator-facing refusal copy, Fox vocabulary per ``docs/lexicon.md``:
    the Fox refuses at the fence, and the fence holds."""
    return f"the fox refuses {ref!r} — {detail}; the fence holds: it must stay inside {area}"


def containment_detail(base: Path, ref: object) -> str | None:
    """Why ``ref`` crosses the fence under ``base`` — the Fox's reason — or
    None when the ref is fully contained (lexically and after resolving)."""
    if safe_ref(ref) is None:
        return (
            "not a safe relative ref — relative only, with no absolute, empty, '.', or '..'"
            " segments, no backslash, no NUL"
        )
    assert isinstance(ref, str)
    if resolve_contained(base, ref) is None:
        return "it resolves outside the base (a symlink or alias points away)"
    return None


def contained_ref(base: Path, ref: object, field: str, area: str = "the egg's own directory") -> Path:
    """The full check in one call: returns the resolved target path when the
    ref is safe and contained under ``base``; raises :class:`UnsafeRefError`
    (field-named, Fox copy) otherwise."""
    detail = containment_detail(base, ref)
    if detail is not None:
        raise UnsafeRefError(field, ref, detail, area)
    resolved = resolve_contained(base, ref)
    assert resolved is not None  # containment_detail passed, so this resolves
    return resolved
