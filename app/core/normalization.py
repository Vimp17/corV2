from __future__ import annotations

from datetime import datetime, timezone

from app.core.quality import AUXILIARY_RANGES, MEASUREMENTS, RANGE_LIMITS
from app.schemas import NormalizationReport, TelemetryPoint


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_records(records: list[TelemetryPoint], max_forward_fill_days: int) -> tuple[list[dict], NormalizationReport]:
    indexed = list(enumerate(records))
    ordered = sorted(indexed, key=lambda pair: (_as_utc(pair[1].timestamp), pair[0]))
    output: list[dict] = []
    last_valid: dict[str, tuple[datetime, float]] = {}
    imputed = 0

    for original_index, point in ordered:
        raw = point.model_dump(mode="json", exclude_none=False)
        timestamp = _as_utc(point.timestamp)
        normalized: dict = {
            "source_record_index": original_index,
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "raw": raw,
            "calculated": {},
            "imputed_fields": [],
            "invalid_fields": [],
        }
        for field in MEASUREMENTS:
            value = getattr(point, field)
            valid = value is not None and RANGE_LIMITS[field][0] <= value <= RANGE_LIMITS[field][1]
            if valid:
                normalized["calculated"][field] = float(value)
                last_valid[field] = (timestamp, float(value))
            else:
                normalized["calculated"][field] = None
                if value is not None:
                    normalized["invalid_fields"].append(field)
                previous = last_valid.get(field)
                if previous and (timestamp - previous[0]).total_seconds() <= max_forward_fill_days * 86400:
                    normalized["calculated"][field] = previous[1]
                    normalized["imputed_fields"].append(field)
                    imputed += 1
        for field, (low, high) in AUXILIARY_RANGES.items():
            value = getattr(point, field)
            valid = value is not None and low <= value <= high
            if valid:
                normalized["calculated"][field] = float(value)
                last_valid[field] = (timestamp, float(value))
            else:
                normalized["calculated"][field] = None
                if value is not None:
                    normalized["invalid_fields"].append(field)
                previous = last_valid.get(field)
                if previous and (timestamp - previous[0]).total_seconds() <= max_forward_fill_days * 86400:
                    normalized["calculated"][field] = previous[1]
                    normalized["imputed_fields"].append(field)
                    imputed += 1
        normalized["calculated"]["water_compatibility_issue"] = point.water_compatibility_issue

        # Preserve supplied values; derive missing load/loss as separate calculated features.
        load_origin = (
            "forward_fill" if "corrosion_load" in normalized["imputed_fields"] else
            "observed" if normalized["calculated"]["corrosion_load"] is not None else "unavailable"
        )
        if normalized["calculated"]["corrosion_load"] is None:
            calc = normalized["calculated"]
            source_fields = ("water_cut_pct", "co2_pct", "chlorides_mg_l", "inhibitor_efficiency", "injection_deviation_pct")
            if all(calc.get(field) is not None for field in source_fields) and point.water_compatibility_issue is not None:
                water = calc["water_cut_pct"] / 100.0
                co2 = max(0.0, min(calc["co2_pct"] / 5.0, 2.0))
                chlorides = max(0.0, min(calc["chlorides_mg_l"] / 50000.0, 2.0))
                protection = max(0.05, min(1.15 - calc["inhibitor_efficiency"], 1.15))
                technology = max(0.0, min(calc["injection_deviation_pct"] / 30.0, 2.0))
                load = (0.12 + 0.28 * water + 0.26 * co2 + 0.20 * chlorides
                        + 0.28 * protection + 0.16 * technology
                        + 0.22 * bool(point.water_compatibility_issue))
                calc["corrosion_load"] = max(0.02, min(load, 3.0))
                load_origin = "derived_v3_demo_formula"
        loss_origin = (
            "forward_fill" if "metal_loss_mm" in normalized["imputed_fields"] else
            "observed" if normalized["calculated"]["metal_loss_mm"] is not None else "unavailable"
        )
        if normalized["calculated"]["metal_loss_mm"] is None:
            initial = normalized["calculated"].get("initial_wall_thickness_mm")
            wall = normalized["calculated"].get("wall_thickness_mm")
            if initial is not None and wall is not None:
                normalized["calculated"]["metal_loss_mm"] = max(0.0, initial - wall)
                loss_origin = "derived_from_wall_thickness"
        normalized["feature_sources"] = {
            "corrosion_load": load_origin,
            "metal_loss_mm": loss_origin,
            "initial_wall_thickness_mm": (
                "forward_fill" if "initial_wall_thickness_mm" in normalized["imputed_fields"] else
                "observed" if normalized["calculated"]["initial_wall_thickness_mm"] is not None else "unavailable"
            ),
        }
        output.append(normalized)

    report = NormalizationReport(
        records_total=len(records),
        records_sorted=ordered == indexed,
        raw_values_preserved=True,
        calculated_values_added=True,
        imputed_values=imputed,
        forward_fill_limit_days=max_forward_fill_days,
        note=("Raw значения сохранены отдельно. Значения вне DQ-диапазона не используются; "
              "короткие пропуски ограниченно заполняются. Расчёт corrosion_load использует "
              "демонстрационную формулу notebook и помечен в provenance."),
    )
    return output, report
