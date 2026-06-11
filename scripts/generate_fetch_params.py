#!/usr/bin/env python3
"""Generate fetch_params YAML for a config from district codes × month range.

Usage:
    python generate_fetch_params.py --districts reference/seoul_district_codes.csv \
        --start 202001 --end 202412 --output configs/seoul_apartment_rent.yaml \
        --template configs/templates/seoul_apt_rent_template.yaml
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import yaml


def generate_params(district_codes: list[str], start_ym: str, end_ym: str) -> list[dict[str, str]]:
    """Generate LAWD_CD × DEAL_YMD param combinations."""
    months = _month_range(start_ym, end_ym)
    params: list[dict[str, str]] = []
    for code in district_codes:
        for ym in months:
            params.append({"LAWD_CD": code, "DEAL_YMD": ym})
    return params


def _validate_yyyymm(value: str, *, label: str) -> tuple[int, int]:
    """Validate YYYYMM format and return (year, month)."""
    if len(value) != 6 or not value.isdigit():
        raise ValueError(f"{label} must be exactly 6 digits in YYYYMM format, got: {value!r}")
    year, month = int(value[:4]), int(value[4:6])
    if month < 1 or month > 12:
        raise ValueError(f"{label} has invalid month {month:02d} (must be 01-12): {value!r}")
    return year, month


def _month_range(start: str, end: str) -> list[str]:
    """Generate YYYYMM strings from start to end inclusive."""
    sy, sm = _validate_yyyymm(start, label="start")
    ey, em = _validate_yyyymm(end, label="end")
    months: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        months.append(f"{y}{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def load_district_codes(csv_path: str) -> list[str]:
    """Load district codes from CSV."""
    codes: list[str] = []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            codes.append(row["district_code"])
    return codes


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate fetch_params for a dataset config")
    parser.add_argument("--districts", required=True, help="Path to district codes CSV")
    parser.add_argument("--start", required=True, help="Start YYYYMM (e.g. 202001)")
    parser.add_argument("--end", required=True, help="End YYYYMM (e.g. 202412)")
    parser.add_argument(
        "--template", required=True, help="Path to config template YAML (without fetch_params)"
    )
    parser.add_argument("--output", required=True, help="Output config YAML path")
    args = parser.parse_args()

    codes = load_district_codes(args.districts)
    if not codes:
        print(f"error: no district codes found in {args.districts}", file=sys.stderr)
        sys.exit(1)

    params = generate_params(codes, args.start, args.end)

    template_path = Path(args.template)
    with open(template_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config["source"]["fetch_params"] = params

    total = len(params)
    months_count = total // len(codes) if codes else 0
    header_comment = (
        f"# Auto-generated config: {len(codes)} districts × "
        f"{months_count} months = {total} API calls\n"
        f"# Generated from template: {args.template}\n\n"
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header_comment)
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"Generated {output_path} ({total} fetch_params)")


if __name__ == "__main__":
    main()
