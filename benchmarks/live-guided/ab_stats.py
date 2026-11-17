"""Small statistics for the A/B results. Stdlib only.

Pass rates use the Wilson score interval. The difference of two independent
proportions uses Newcombe's hybrid score interval (method 10), which behaves at
0% and 100% where the Wald interval does not. ``fisher_two_sided`` is the exact
two-sided p-value for a 2x2 table, by summing hypergeometric probabilities no
larger than the observed one.
"""

from __future__ import annotations

import math
import statistics

Z95 = 1.959963984540054


def wilson(passed: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for ``passed`` successes in ``n`` trials."""
    if n <= 0:
        return (float("nan"), float("nan"))
    if not 0 <= passed <= n:
        raise ValueError("passed must be between 0 and n")
    p = passed / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def newcombe_diff(passed1: int, n1: int, passed2: int, n2: int, z: float = Z95) -> tuple[float, float, float]:
    """Difference ``p1 - p2`` with Newcombe's hybrid score interval: ``(diff, low, high)``."""
    if n1 <= 0 or n2 <= 0:
        return (float("nan"), float("nan"), float("nan"))
    p1, p2 = passed1 / n1, passed2 / n2
    l1, u1 = wilson(passed1, n1, z)
    l2, u2 = wilson(passed2, n2, z)
    diff = p1 - p2
    low = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    high = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return (diff, max(-1.0, low), min(1.0, high))


def fisher_two_sided(a: int, b: int, c: int, d: int) -> float:
    """Exact two-sided p for the table [[a, b], [c, d]] (rows are arms, columns pass/fail)."""
    row1, row2, col1 = a + b, c + d, a + c
    total = row1 + row2
    if total == 0:
        return float("nan")

    def prob(x: int) -> float:
        return math.comb(row1, x) * math.comb(row2, col1 - x) / math.comb(total, col1)

    observed = prob(a)
    low, high = max(0, col1 - row2), min(row1, col1)
    return min(1.0, sum(p for p in (prob(x) for x in range(low, high + 1)) if p <= observed * (1 + 1e-9)))


def describe(values: list[float]) -> dict:
    """Count, mean, median, min, max and sum; empty input gives Nones."""
    if not values:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None, "sum": None}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "min": min(values),
        "max": max(values),
        "sum": round(sum(values), 3),
    }


def _r(x: float) -> float:
    return round(x, 4)


def arm_summary(runs: list[dict]) -> dict:
    """Pass rate with Wilson 95% interval, plus calls, tokens and wall time."""
    n = len(runs)
    passed = sum(1 for run in runs if run["passed"])
    low, high = wilson(passed, n)
    provider_errors = sum(1 for run in runs if run.get("provider_error"))
    return {
        "runs": n,
        "passed": passed,
        "pass_rate": _r(passed / n) if n else None,
        "wilson95": [_r(low), _r(high)] if n else None,
        "provider_error_runs": provider_errors,
        "api_calls": describe([run["api_calls"] for run in runs if run.get("api_calls") is not None]),
        "total_tokens": describe([run["total_tokens"] for run in runs if run.get("total_tokens") is not None]),
        "wall_s": describe([run["wall_s"] for run in runs if run.get("wall_s") is not None]),
        "target_rejections": describe([run["target_rejections"] for run in runs if run.get("target_rejections") is not None]),
    }


def compare(guided: list[dict], unguided: list[dict]) -> dict:
    """Guided minus unguided pass rate with Newcombe 95% interval and Fisher's exact p."""
    g_pass = sum(1 for run in guided if run["passed"])
    u_pass = sum(1 for run in unguided if run["passed"])
    g_n, u_n = len(guided), len(unguided)
    if not g_n or not u_n:
        return {"difference": None}
    diff, low, high = newcombe_diff(g_pass, g_n, u_pass, u_n)
    return {
        "difference": _r(diff),
        "newcombe95": [_r(low), _r(high)],
        "fisher_exact_p": round(fisher_two_sided(g_pass, g_n - g_pass, u_pass, u_n - u_pass), 4),
    }


def reading(guided: list[dict], unguided: list[dict]) -> str:
    """A neutral one-line reading of the comparison. Never says 'superior'."""
    if not guided or not unguided:
        return "no comparison: an arm has no runs"
    g_pass = sum(1 for run in guided if run["passed"])
    u_pass = sum(1 for run in unguided if run["passed"])
    if g_pass == len(guided) and u_pass == len(unguided):
        return "ceiling: both arms passed every run, so this task set does not discriminate the arms"
    if g_pass == 0 and u_pass == 0:
        return "floor: neither arm passed a run, so this task set does not discriminate the arms"
    diff, low, high = newcombe_diff(g_pass, len(guided), u_pass, len(unguided))
    if low <= 0 <= high:
        direction = "higher" if diff > 0 else ("lower" if diff < 0 else "equal")
        return f"no detectable difference: the guided pass rate is {direction} but the 95% interval for the difference includes 0"
    if diff > 0:
        return "guided pass rate higher, 95% interval for the difference excludes 0; applies to this task set only"
    return "guided pass rate lower, 95% interval for the difference excludes 0"
