#!/usr/bin/env python3
"""Compare evaluation versions with confidence intervals using only stdlib."""

import argparse
import json
import math
from itertools import combinations
from pathlib import Path
from statistics import NormalDist
from typing import Any

from _jsonio import load_json


def wilson_interval(successes: int, total: int, confidence: float = 0.95) -> tuple[float, float]:
    if total <= 0:
        raise ValueError("total must be positive")
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    proportion = successes / total
    denominator = 1 + z**2 / total
    centre = (proportion + z**2 / (2 * total)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / total + z**2 / (4 * total**2)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def difference_interval(
    successes_a: int,
    total_a: int,
    successes_b: int,
    total_b: int,
    confidence: float = 0.95,
) -> tuple[float, float, float]:
    if total_a <= 0 or total_b <= 0:
        raise ValueError("sample sizes must be positive")
    proportion_a = successes_a / total_a
    proportion_b = successes_b / total_b
    difference = proportion_b - proportion_a
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    standard_error = math.sqrt(
        proportion_a * (1 - proportion_a) / total_a + proportion_b * (1 - proportion_b) / total_b
    )
    return difference, difference - z * standard_error, difference + z * standard_error


def _verify_evidence_index(item: dict[str, Any]) -> str:
    """Check a metrics record's index and return its recomputed digest.

    Imported here rather than at module scope so this tool keeps working as a
    plain stdlib script: ``collect_metrics`` imports the Pydantic contracts to
    replay a run, which a comparison of already-collected metrics does not need
    unless an index is actually present.
    """
    from collect_metrics import evidence_index_sha256, verify_evidence_index

    verify_evidence_index(item)
    return evidence_index_sha256(item)


def _index_summary(item: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce a metrics record to the evidence index a comparison may publish.

    A comparison output is smaller than the metrics it reads, so it carries the
    digest and the run ids behind VTCR rather than the whole run list. The digest
    is what lets a reviewer check that the compared file still describes the same
    run set. A record without an index reports ``None``: it is not upgraded into
    one, and ``main`` refuses to publish a comparison built from such a record.
    """
    records = item.get("runs")
    if not isinstance(records, list) or not records:
        return None
    return {
        "run_count": len(records),
        "evidence_index_sha256": item.get("evidence_index_sha256"),
        "recomputed_evidence_index_sha256": _verify_evidence_index(item),
        "vtcr_contributors": list(item.get("metric_contributors", {}).get("vtcr", [])),
    }


def compare(metrics: list[dict[str, Any]], labels: list[str]) -> dict[str, Any]:
    if len(metrics) != len(labels):
        raise ValueError("metrics and labels must have the same length")
    versions = []
    counts = {}
    for label, item in zip(labels, metrics, strict=True):
        total = int(item["run_count"])
        if total < 1:
            raise ValueError(f"{label}: run_count must be at least 1")
        rate = float(item["vtcr"])
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"{label}: vtcr must be between 0 and 1, got {rate!r}")
        # `vtcr * run_count` has to be a whole count. Rounding it fabricated a
        # numerator that never existed and then reported an interval from it
        # (issue #415), so an inconsistent metrics file is refused instead.
        implied = rate * total
        successes = round(implied)
        if abs(implied - successes) > 1e-9:
            raise ValueError(f"{label}: vtcr {rate!r} of {total} runs is not a whole success count")
        lower, upper = wilson_interval(successes, total)
        counts[label] = (successes, total)
        versions.append(
            {
                "label": label,
                "successes": successes,
                "total": total,
                "vtcr": successes / total,
                "confidence_interval_95": [lower, upper],
                "evidence_index": _index_summary(item),
            }
        )

    # Every pairwise comparison is one hypothesis in one family. Three versions
    # make three comparisons, so a 95% interval per comparison leaves the chance
    # of at least one false "significant" well above 5%. Bonferroni is the
    # conservative choice and, more importantly, the one whose adjustment a
    # reader can check from the numbers in the report (issue #415).
    pairs = list(combinations(labels, 2))
    family_size = len(pairs)
    alpha = 0.05
    adjusted_alpha = alpha / family_size if family_size else alpha
    pairwise = []
    for label_a, label_b in pairs:
        difference, lower, upper = difference_interval(
            *counts[label_a], *counts[label_b], confidence=1.0 - adjusted_alpha
        )
        pairwise.append(
            {
                "from": label_a,
                "to": label_b,
                "vtcr_difference": difference,
                "confidence_interval": [lower, upper],
                "confidence_level": 1.0 - adjusted_alpha,
                "statistically_significant": lower > 0 or upper < 0,
            }
        )
    return {
        "method": (
            "Wilson score for per-version rates; unpooled normal difference for pairwise "
            "comparisons with a Bonferroni correction across the family"
        ),
        "multiplicity": {
            "family": "all pairwise version comparisons in this report",
            "family_size": family_size,
            "alpha": alpha,
            "adjusted_alpha": adjusted_alpha,
            "correction": "bonferroni",
        },
        "versions": versions,
        "pairwise": pairwise,
        "evidence_indexes_match": all(item["evidence_index"] is not None for item in versions)
        and len({item["evidence_index"]["evidence_index_sha256"] for item in versions}) == 1,
        "every_version_has_an_evidence_index": all(item["evidence_index"] is not None for item in versions),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Workbench-1 evaluation metrics")
    parser.add_argument("--metrics", nargs="+", type=Path, required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    labels = [label.strip() for label in args.labels.split(",") if label.strip()]
    metrics = [load_json(path) for path in args.metrics]
    # A comparison is a published artifact, so every input must still explain
    # its own total before the comparison is written.
    for item in metrics:
        _verify_evidence_index(item)
    payload = compare(metrics, labels)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
