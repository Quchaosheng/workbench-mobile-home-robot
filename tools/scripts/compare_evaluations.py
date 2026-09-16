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
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Workbench-1 evaluation metrics")
    parser.add_argument("--metrics", nargs="+", type=Path, required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    labels = [label.strip() for label in args.labels.split(",") if label.strip()]
    payload = compare(
        [load_json(path) for path in args.metrics],
        labels,
    )
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
