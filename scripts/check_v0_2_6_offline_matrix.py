#!/usr/bin/env python3
"""Validate the offline v0.2.6 regression matrix without any browser access."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX_JSON = ROOT / "outputs" / "v0.2.6_offline_regression_matrix_20260626_121500.json"


def main() -> int:
    data = json.loads(MATRIX_JSON.read_text(encoding="utf-8"))
    matrix = data.get("matrix")
    minimal = data.get("minimal_sample_requirements")
    errors: list[str] = []

    if not isinstance(matrix, list) or not matrix:
        errors.append("matrix is missing")
    else:
        for idx, row in enumerate(matrix):
            if not isinstance(row, dict):
                errors.append(f"row {idx} is not an object")
                continue
            for key in ("lane", "positive_anchors", "negative_terms", "expected_identification", "expected_advice_boundary"):
                if key not in row or not row[key]:
                    errors.append(f"row {idx} missing {key}")
            if not isinstance(row.get("positive_anchors"), list) or len(row["positive_anchors"]) < 4:
                errors.append(f"row {idx} positive_anchors too short")
            if not isinstance(row.get("negative_terms"), list) or len(row["negative_terms"]) < 4:
                errors.append(f"row {idx} negative_terms too short")
            if not isinstance(row.get("expected_profile_keys"), list) or not row["expected_profile_keys"]:
                errors.append(f"row {idx} missing expected_profile_keys")

    if not isinstance(minimal, list) or len(minimal) < 4:
        errors.append("minimal_sample_requirements incomplete")

    lanes = {row.get("lane") for row in matrix or [] if isinstance(row, dict)}
    expected_lanes = {
        "微导纳米 / 双采购岗",
        "微导纳米 / 机械工程师",
        "鹏新旭 / PQE 专家",
        "ACDC 服务器电源研发总监",
        "电源 / 硬件",
    }
    missing = sorted(expected_lanes - lanes)
    if missing:
        errors.append("missing lanes: " + ", ".join(missing))

    if errors:
        print("v0.2.6 离线回归矩阵校验未通过:")
        for item in errors:
            print(f"- {item}")
        return 1

    print(f"v0.2.6 离线回归矩阵校验通过: {len(matrix)} 条矩阵记录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

