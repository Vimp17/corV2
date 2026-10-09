from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.core.quality import AUXILIARY_RANGES, RANGE_LIMITS
from app.schemas import ExternalSourceStatus, SkippedAnalysis, TelemetryPoint

SIGNAL_FIELDS = (
    "water_cut_pct", "co2_pct", "chlorides_mg_l", "inhibitor_efficiency",
    "injection_deviation_pct", "corrosion_rate_mm_year", "wall_thickness_mm",
    "initial_wall_thickness_mm", "metal_loss_mm", "corrosion_load",
)
FIELD_LABELS = {
    "water_cut_pct": "обводнённость, %", "co2_pct": "содержание CO₂, %",
    "chlorides_mg_l": "хлориды, мг/л", "inhibitor_efficiency": "эффективность ингибитора",
    "injection_deviation_pct": "отклонение закачки, %",
    "corrosion_rate_mm_year": "скорость коррозии, мм/год",
    "wall_thickness_mm": "толщина стенки, мм", "initial_wall_thickness_mm": "начальная толщина стенки, мм",
    "metal_loss_mm": "потеря металла, мм", "corrosion_load": "коррозионная нагрузка",
}


def source_status_contract(rows: list[dict]) -> list[ExternalSourceStatus]:
    return [ExternalSourceStatus(
        source_id=row["source_id"], status=row["status"], retrieved_at=row["retrieved_at"],
        record_count=row["record_count"], message=row["message"],
    ) for row in rows]


def assess_readiness(well_id: str, records: list[TelemetryPoint], source_rows: list[dict],
                     horizon_days: int | None = None) -> SkippedAnalysis | None:
    """Return a SkippedAnalysis when the data cannot support a model run, else None.

    Blocking: too few unique measurements, too few recent observations for a trend, too few
    current signals, or timestamps beyond the allowed future skew. Staleness is not blocking.
    """
    timestamps = {
        (point.timestamp.replace(tzinfo=timezone.utc) if point.timestamp.tzinfo is None
         else point.timestamp.astimezone(timezone.utc)).isoformat()
        for point in records
    }
    def as_utc(point: TelemetryPoint) -> datetime:
        return (point.timestamp.replace(tzinfo=timezone.utc) if point.timestamp.tzinfo is None
                else point.timestamp.astimezone(timezone.utc))

    latest_point = max(records, key=as_utc) if records else None
    latest_utc = as_utc(latest_point) if latest_point else None
    recent_window_start = latest_utc - timedelta(days=settings.trend_window_days) if latest_utc else None
    recent_timestamps = {stamp for stamp in (
        point.timestamp.replace(tzinfo=timezone.utc) if point.timestamp.tzinfo is None
        else point.timestamp.astimezone(timezone.utc) for point in records
    ) if recent_window_start is not None and recent_window_start <= stamp <= latest_utc}
    current_window_start = latest_utc - timedelta(days=settings.max_forward_fill_days) if latest_utc else None
    current_signals: set[str] = set()
    if latest_utc is not None and current_window_start is not None:
        for point in records:
            timestamp = as_utc(point)
            if not current_window_start <= timestamp <= latest_utc:
                continue
            values = point.model_dump()
            for field in SIGNAL_FIELDS:
                value = values.get(field)
                bounds = RANGE_LIMITS.get(field) or AUXILIARY_RANGES.get(field)
                if value is not None and bounds and bounds[0] <= value <= bounds[1]:
                    current_signals.add(field)
    current_signal_count = len(current_signals)
    missing: list[str] = []
    requested_data: list[str] = []

    if len(timestamps) < settings.min_telemetry_records:
        missing.append(f"Есть {len(timestamps)} уникальных измерений; нужно не менее {settings.min_telemetry_records}.")
        requested_data.append(f"Передайте не менее {settings.min_telemetry_records} измерений с разными датами и временем.")
    if len(recent_timestamps) < settings.min_trend_observations:
        missing.append(f"За последние {settings.trend_window_days} дней есть {len(recent_timestamps)} измерений; нужно не менее {settings.min_trend_observations} для оценки динамики.")
        requested_data.append(f"Предоставьте не менее {settings.min_trend_observations} измерений в скользящем окне {settings.trend_window_days} дней.")
    if current_signal_count < settings.min_current_signals:
        missing.append(f"За последние {settings.max_forward_fill_days} дней заполнено {current_signal_count} прогнозных показателя; нужно не менее {settings.min_current_signals}.")
        candidates = ", ".join(FIELD_LABELS[field] for field in SIGNAL_FIELDS)
        requested_data.append(f"Добавьте как минимум {settings.min_current_signals} показателя из списка: {candidates}.")
    # Stale telemetry is deliberately NOT a blocking condition: hiding a well's last known
    # risk because its feed stopped would be worse than showing it. The pipeline marks such
    # results as stale (data_freshness / data_update_required) instead. Timestamps far in
    # the future indicate a clock or timezone error and do block the analysis.
    if latest_utc is None:
        missing.append("Нет временной отметки телеметрии.")
    else:
        ahead_hours = (latest_utc - datetime.now(timezone.utc)).total_seconds() / 3600
        if ahead_hours > settings.max_future_skew_hours:
            missing.append(
                f"Последнее измерение датировано будущим временем (+{ahead_hours:.1f} ч.; "
                f"допустимо до {settings.max_future_skew_hours:g} ч.); проверьте часы и часовой пояс источника."
            )
            requested_data.append("Исправьте дату/часовой пояс последнего измерения и повторите загрузку.")

    if not missing:
        return None
    bad_sources = [row for row in source_rows if row["status"] in {"error", "unavailable", "not_configured"}]
    for row in bad_sources:
        missing.append(f"Источник {row['source_id']}: {row['status']} — {row.get('message') or 'нет подробностей'}")
        if row["status"] == "not_configured":
            requested_data.append(f"Настройте API-адрес источника {row['source_id']} или загрузите его Excel по соответствующему профилю.")
        else:
            requested_data.append(f"Проверьте доступность API/выгрузки источника {row['source_id']} и повторите синхронизацию.")
    message = (f"Анализ скважины {well_id} пропущен: данных пока недостаточно или источник недоступен. "
               "Модели не запускались. Исправьте перечисленные пункты и повторите синхронизацию.")
    return SkippedAnalysis(
        well_id=well_id, horizon_days=horizon_days,
        telemetry_records=len(records), unique_timestamps=len(timestamps),
        current_signal_count=current_signal_count, missing_items=missing,
        source_statuses=source_status_contract(source_rows), operator_message=message,
        requested_data=list(dict.fromkeys(requested_data)),
    )
