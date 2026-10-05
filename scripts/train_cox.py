#!/usr/bin/env python
"""Fit one static Cox snapshot model and export the API's JSON artifact."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from lifelines import CoxPHFitter


DEFAULT_FEATURES = [
    "water_cut_pct", "co2_pct", "chlorides_mg_l", "inhibitor_efficiency",
    "injection_deviation_pct", "corrosion_rate_mm_year", "wall_thickness_mm",
    "corrosion_load",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="CSV with one observation per well")
    parser.add_argument("--output", required=True, type=Path, help="Destination cox_model.json")
    parser.add_argument("--duration-col", default="duration_days")
    parser.add_argument("--event-col", default="event_observed")
    parser.add_argument("--features", default=",".join(DEFAULT_FEATURES),
                        help="Comma-separated numeric feature columns")
    parser.add_argument("--model-version", default="1.0")
    parser.add_argument("--penalizer", type=float, default=0.1)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    features = [name.strip() for name in args.features.split(",") if name.strip()]
    if not features:
        raise SystemExit("At least one feature must be provided")
    if not args.input.is_file():
        raise SystemExit(f"Input CSV not found: {args.input}")
    frame = pd.read_csv(args.input)
    required = [args.duration_col, args.event_col, *features]
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise SystemExit(f"Missing CSV columns: {', '.join(missing)}")
    data = frame[required].copy()
    for column in required:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.replace([float("inf"), float("-inf")], float("nan"))
    data = data.dropna(subset=[args.duration_col, args.event_col])
    data = data.loc[(data[args.duration_col] > 0) & data[args.event_col].isin([0, 1])].copy()
    data[args.event_col] = data[args.event_col].astype(int)
    medians = data[features].median().fillna(0.0)
    data[features] = data[features].fillna(medians)
    features = [name for name in features if data[name].nunique(dropna=False) > 1]
    if not features:
        raise SystemExit("No non-constant feature remains after cleaning")
    if len(data) < 10 or data[args.event_col].nunique() < 2 or data[args.event_col].sum() < 2:
        raise SystemExit("Need >=10 rows and at least two events and two censorings to fit this Cox model")

    fit = data[[args.duration_col, args.event_col, *features]]
    model = CoxPHFitter(penalizer=args.penalizer)
    model.fit(fit, duration_col=args.duration_col, event_col=args.event_col)
    baseline = model.baseline_survival_.iloc[:, 0]
    coefficients = {str(name): float(value) for name, value in model.params_.items()}
    means = {name: float(fit[name].mean()) for name in features}
    medians = {name: float(medians[name]) for name in features}
    try:
        training_c_index = float(model.concordance_index_)
        if not math.isfinite(training_c_index):
            training_c_index = None
    except (ArithmeticError, ValueError):
        training_c_index = None
    artifact = {
        "model": "cox",
        "model_version": args.model_version,
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "features": features,
        "medians": medians,
        "means": means,
        "coefficients": coefficients,
        "baseline_survival": [
            {"time_days": float(time_days), "survival": float(survival)}
            for time_days, survival in baseline.items()
        ],
        "training_rows": int(len(fit)),
        "training_events": int(fit[args.event_col].sum()),
        "concordance_index_train_only": training_c_index,
        "warning": "Train-only metric is not evidence of production performance. Validate on later, held-out wells.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output = args.output.resolve()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=args.output.parent,
                                     delete=False, suffix=".tmp") as handle:
        json.dump(artifact, handle, ensure_ascii=False, allow_nan=False, indent=2)
        temp_path = Path(handle.name)
    os.replace(temp_path, args.output)
    print(f"Saved Cox artifact: {args.output}")
    print(f"Rows={len(fit)}, events={int(fit[args.event_col].sum())}, features={len(features)}")


if __name__ == "__main__":
    main()
