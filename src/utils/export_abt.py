"""Export the anti-leakage ABT view from PostgreSQL to Parquet or CSV for Colab/ML."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

from src.config import DB_CONFIG

DEFAULT_OUTPUT = Path("data/abt_readmission_features.parquet")
ABT_QUERY = "SELECT * FROM analytics.abt_readmission_features"


def export_abt(output_path: Path = DEFAULT_OUTPUT, file_format: str = "parquet") -> Path:
    """Query `analytics.abt_readmission_features` and write it to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(DB_CONFIG.sqlalchemy_url)

    try:
        with engine.connect() as conn:
            df = pd.read_sql(ABT_QUERY, conn, coerce_float=True)
    finally:
        engine.dispose()

    if file_format == "parquet":
        df.to_parquet(output_path, index=False)
    elif file_format == "csv":
        df.to_csv(output_path, index=False)
    else:
        raise ValueError(f"Unsupported format: {file_format}")

    print(f"Exported {len(df)} rows and {len(df.columns)} columns to {output_path}")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export analytics.abt_readmission_features to Parquet or CSV."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination file path (default: data/abt_readmission_features.parquet).",
    )
    parser.add_argument(
        "--format",
        choices=("parquet", "csv"),
        default="parquet",
        help="Output file format (default: parquet).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    export_abt(output_path=args.output, file_format=args.format)


if __name__ == "__main__":
    main()
