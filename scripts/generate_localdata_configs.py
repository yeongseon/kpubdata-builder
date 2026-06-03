#!/usr/bin/env python3
"""Generate HuggingFace publishing configs for localdata datasets in bulk.

Usage:
    python generate_localdata_configs.py --output-dir scripts/configs/localdata/
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

# Representative localdata datasets grouped by category.
# Each entry: (dataset_id, hf_slug, title_en, title_ko)
LOCALDATA_DATASETS: list[tuple[str, str, str, str]] = [
    # Food & Beverage
    ("general_restaurant", "localdata-general-restaurant", "General Restaurant", "일반음식점"),
    ("rest_cafe", "localdata-rest-cafe", "Cafe/Bakery", "휴게음식점"),
    ("bakery", "localdata-bakery", "Bakery", "제과점"),
    # Healthcare
    ("hospital", "localdata-hospital", "Hospital", "병원"),
    ("clinic", "localdata-clinic", "Clinic", "의원"),
    ("pharmacy", "localdata-pharmacy", "Pharmacy", "약국"),
    ("dental_clinic", "localdata-dental-clinic", "Dental Clinic", "치과의원"),
    # Accommodation & Tourism
    (  # noqa: E501
        "tourist_accommodation",
        "localdata-tourist-accommodation",
        "Tourist Accommodation",
        "관광숙박업",
    ),
    (  # noqa: E501
        "general_accommodation",
        "localdata-general-accommodation",
        "General Accommodation",
        "일반숙박업",
    ),
    # Sports & Leisure
    ("fitness_center", "localdata-fitness-center", "Fitness Center", "체력단련장"),
    ("swimming_pool", "localdata-swimming-pool", "Swimming Pool", "수영장"),
    # Animals
    ("animal_hospital", "localdata-animal-hospital", "Animal Hospital", "동물병원"),
    ("pet_grooming", "localdata-pet-grooming", "Pet Grooming", "동물미용업"),
    # Education
    ("private_academy", "localdata-private-academy", "Private Academy", "학원"),
    # Retail
    ("convenience_store", "localdata-convenience-store", "Convenience Store", "편의점"),
]


def generate_config(
    dataset_id: str,
    hf_slug: str,
    title_en: str,
    title_ko: str,
    template_path: Path,
) -> dict[str, object]:
    """Generate a config dict from the common template."""
    with open(template_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config["source"]["dataset"] = dataset_id
    config["output"]["staging_dir"] = f"./staging/{hf_slug}"
    config["output"]["hf_repo"] = f"kpubdata/{hf_slug}"
    config["card"]["title"] = f"Korea {title_en} Business Licenses ({title_ko})"
    config["card"]["description"] = (
        f"Business license data for {title_en.lower()} ({title_ko}) "
        f"establishments in Korea.\n\n"
        f"Data sourced from Korea's Local Government Data (지방행정인허가 데이터)\n"
        f"via the localdata.go.kr API.\n"
    )

    return config


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate localdata HF configs in bulk")
    parser.add_argument(
        "--template",
        default="scripts/configs/templates/localdata_template.yaml",
        help="Path to common template",
    )
    parser.add_argument(
        "--output-dir",
        default="scripts/configs/localdata",
        help="Output directory for generated configs",
    )
    args = parser.parse_args()

    template_path = Path(args.template)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for dataset_id, hf_slug, title_en, title_ko in LOCALDATA_DATASETS:
        config = generate_config(dataset_id, hf_slug, title_en, title_ko, template_path)
        output_path = output_dir / f"{dataset_id}.yaml"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# {title_ko} ({title_en}) — Auto-generated localdata config\n")
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"  Generated: {output_path}")

    print(f"\nTotal: {len(LOCALDATA_DATASETS)} configs generated in {output_dir}/")


if __name__ == "__main__":
    main()
