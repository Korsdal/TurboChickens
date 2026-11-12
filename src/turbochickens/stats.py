"""Per-job usage statistics (spec 08) — the pure seam.

Token usage is measured telemetry, never a budget: each run records its
tokens in/out in the manifest, and these functions roll a job's finished runs
up into the `job_usage_stats` row — n, mean/p50/p95 tokens in+out, and the
expected cost per run derived from `providers.toml` pricing (computed by the
caller, which owns the provider registry).
"""

from __future__ import annotations

import math
from typing import Any


def percentile(sorted_vals: list[float], p: float) -> float | None:
    """Percentile with linear interpolation (numpy 'linear' method) over an
    already-sorted list. Returns None for an empty list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(sorted_vals[int(k)])
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def rollup_usage(usages: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up per-run usage dicts (``{"tokens_in": …, "tokens_out": …, …}``)
    into the stats row fields: n plus mean/p50/p95 of tokens in, out, and
    total (in+out). `tokens_p95` is the p95 of the per-run total."""
    pairs = [
        (int(u.get("tokens_in") or 0), int(u.get("tokens_out") or 0))
        for u in usages
        if isinstance(u, dict)
    ]
    ins = sorted(p[0] for p in pairs)
    outs = sorted(p[1] for p in pairs)
    totals = sorted(a + b for a, b in pairs)
    return {
        "n_runs": len(pairs),
        "tokens_in_mean": _mean(ins),
        "tokens_in_p50": percentile(ins, 50),
        "tokens_in_p95": percentile(ins, 95),
        "tokens_out_mean": _mean(outs),
        "tokens_out_p50": percentile(outs, 50),
        "tokens_out_p95": percentile(outs, 95),
        "tokens_p95": percentile(totals, 95),
    }
