from __future__ import annotations

from datetime import datetime, timezone

from app.schemas import DataQualityReport, QualityIssue, TelemetryPoint


# Physical sanity bounds inherited from the V3.0 demonstration notebook.
# These are DQ bounds, not corrosion intervention limits.
RANGE_LIMITS: dict[str, tuple[float, float]] = {
    "water_cut_pct": (0.0, 100.0),
    "co2_pct": (0.0, 8.0),
    "chlorides_mg_l": (0.0, 80000.0),
    "inhibitor_efficiency": (0.0, 1.2),
    "injection_deviation_pct": (0.0, 60.0),
    "corrosion_rate_mm_year": (0.0, 1.5),
    "wall_thickness_mm": (0.8, 14.0),
}
MEASUREMENTS = tuple(RANGE_LIMITS)
AUXILIARY_RANGES: dict[str, tuple[float, float]] = {
    "initial_wall_thickness_mm": (0.8, 14.0),
    "metal_loss_mm": (0.0, 14.0),
    "corrosion_load": (0.02, 3.0),
}


def validate_records(records: list[TelemetryPoint]) -> DataQualityReport:
    issues: list[QualityIssue] = []
    missing_values = range_violations = duplicate_timestamps = timezone_missing = frozen_runs = 0
    seen: set[str] = set()
    prior = None

    for index, record in enumerate(records):
        timestamp = record.timestamp
        normalized_timestamp = (timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None
                                else timestamp.astimezone(timezone.utc))
        timestamp_key = normalized_timestamp.isoformat()
        if timestamp.tzinfo is None:
            timezone_missing += 1
            issues.append(QualityIssue(
                record_index=index, code="timestamp_timezone_missing", field="timestamp",
                value=timestamp_key, message="Временная зона не задана; при нормализации будет принято UTC.",
            ))
        if normalized_timestamp > datetime.now(timezone.utc):
            issues.append(QualityIssue(
                record_index=index, code="future_timestamp", field="timestamp", value=timestamp_key,
                message="Timestamp позже текущего времени UTC.",
            ))
        if prior is not None:
            try:
                left = prior if prior.tzinfo else prior.replace(tzinfo=timezone.utc)
                right = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
                if right < left:
                    issues.append(QualityIssue(
                        record_index=index, code="timestamp_out_of_order", field="timestamp",
                        value=timestamp_key, message="Запись поступила не в хронологическом порядке.",
                    ))
            except TypeError:
                pass
        prior = timestamp
        if timestamp_key in seen:
            duplicate_timestamps += 1
            issues.append(QualityIssue(
                record_index=index, code="duplicate_timestamp", field="timestamp", value=timestamp_key,
                message="Для скважины есть несколько наблюдений с одной отметкой времени.",
            ))
        seen.add(timestamp_key)

        missing_fields = []
        for field in MEASUREMENTS:
            value = getattr(record, field)
            if value is None:
                missing_values += 1
                missing_fields.append(field)
                continue
            low, high = RANGE_LIMITS[field]
            if not low <= value <= high:
                range_violations += 1
                issues.append(QualityIssue(
                    record_index=index, code="range_violation", field=field, value=value,
                    message=f"Значение вне физического диапазона DQ [{low:g}, {high:g}].",
                ))
        if missing_fields:
            issues.append(QualityIssue(
                record_index=index, code="missing_values", value=missing_fields,
                message="В записи отсутствуют некоторые телеметрические поля.",
            ))
        for field, (low, high) in AUXILIARY_RANGES.items():
            value = getattr(record, field)
            if value is not None and not low <= value <= high:
                range_violations += 1
                issues.append(QualityIssue(
                    record_index=index, code="range_violation", field=field, value=value,
                    message=f"Значение вне демонстрационного DQ-диапазона [{low:g}, {high:g}].",
                ))
    # A run is a potential frozen sensor only when 5+ adjacent submitted records
    # carry the same non-null value. This is a warning, not a deletion rule.
    for field in MEASUREMENTS:
        last_value = object()
        run: list[int] = []
        runs: list[list[int]] = []
        for index, record in enumerate(records):
            value = getattr(record, field)
            if value is not None and value == last_value:
                run.append(index)
            else:
                if len(run) >= 5:
                    runs.append(run)
                run = [index] if value is not None else []
                last_value = value
        if len(run) >= 5:
            runs.append(run)
        for frozen_run in runs:
            frozen_runs += 1
            for index in frozen_run:
                issues.append(QualityIssue(
                    record_index=index, code="possible_frozen_sensor", field=field,
                    value=len(frozen_run), message="Одинаковое значение повторяется в 5+ соседних записях.",
                ))

    affected = len({item.record_index for item in issues})
    return DataQualityReport(
        status="WARN" if issues else "PASS",
        records_total=len(records),
        records_with_issues=affected,
        missing_values=missing_values,
        range_violations=range_violations,
        duplicate_timestamps=duplicate_timestamps,
        timezone_missing_timestamps=timezone_missing,
        frozen_runs=frozen_runs,
        issues=issues,
    )
