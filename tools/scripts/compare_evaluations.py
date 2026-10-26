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
        successes = round(float(item["vtcr"]) * total)
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

    pairwise = []
    for label_a, label_b in combinations(labels, 2):
        difference, lower, upper = difference_interval(*counts[label_a], *counts[label_b])
        pairwise.append(
            {
                "from": label_a,
                "to": label_b,
                "vtcr_difference": difference,
                "confidence_interval_95": [lower, upper],
                "statistically_significant": lower > 0 or upper < 0,
            }
        )
    return {
        "method": "Wilson score and unpooled normal difference, 95% confidence",
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
