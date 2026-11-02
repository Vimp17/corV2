from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WellRequest(APIModel):
    well_id: str = Field(min_length=1, max_length=128)

    @field_validator("well_id")
    @classmethod
    def trim_well_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("well_id cannot be blank")
        return value


class TelemetryPoint(BaseModel):
    """Canonical telemetry units follow the V3.0 notebook column names."""

    model_config = ConfigDict(extra="allow")

    timestamp: datetime
    water_cut_pct: float | None = None
    co2_pct: float | None = None
    chlorides_mg_l: float | None = None
    inhibitor_efficiency: float | None = None
    injection_deviation_pct: float | None = None
    corrosion_rate_mm_year: float | None = None
    wall_thickness_mm: float | None = None
    initial_wall_thickness_mm: float | None = None
    metal_loss_mm: float | None = None
    corrosion_load: float | None = None
    water_compatibility_issue: bool | None = None
    # Passport and treatment fields used by the well rating (DARS: field, status; ХИМИК:
    # tonnage and НОО of the inhibitor treatment). They are not risk-model features.
    field: str | None = Field(default=None, max_length=256)
    in_operation: bool | None = None
    tonnage_t: float | None = Field(default=None, ge=0, le=10000)
    liquid_rate_m3_day: float | None = Field(default=None, ge=0, le=100000)
    noo_days: float | None = Field(default=None, ge=0, le=100000)
    # Indicators the system has no field for (pH, hardness, iron…), keyed by the column or
    # indicator name with its unit. Stored and shown in the well card and given to the AI agent;
    # they do not enter the risk models or the rating.
    extra_measurements: dict[str, float] | None = None

    @model_validator(mode="before")
    @classmethod
    def compatibility_alias(cls, value):
        if isinstance(value, dict) and "water_compatibility_issue" not in value and "incompatible_water" in value:
            value = {**value, "water_compatibility_issue": value["incompatible_water"]}
        return value

    @field_validator("extra_measurements")
    @classmethod
    def valid_extra_measurements(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if not value:
            return None
        cleaned: dict[str, float] = {}
        for name, number in list(value.items())[:30]:
            name = " ".join(str(name).split())[:100]
            if name and number is not None and math.isfinite(float(number)):
                cleaned[name] = float(number)
        return cleaned or None

    @field_validator(
        "water_cut_pct", "co2_pct", "chlorides_mg_l", "inhibitor_efficiency",
        "injection_deviation_pct", "corrosion_rate_mm_year", "wall_thickness_mm",
        "initial_wall_thickness_mm", "metal_loss_mm", "corrosion_load", "tonnage_t", "noo_days",
        "liquid_rate_m3_day",
    )
    @classmethod
    def finite_measurements(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("measurement must be a finite number")
        return value


class IngestionRequest(WellRequest):
    source_id: str = Field(min_length=1, max_length=128)
    batch_id: str | None = Field(default=None, min_length=1, max_length=128)
    records: list[TelemetryPoint] = Field(min_length=1, max_length=10000)

    @field_validator("batch_id")
    @classmethod
    def trim_batch_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("batch_id cannot be blank")
        return value

    @field_validator("source_id")
    @classmethod
    def normalize_source_id(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("source_id cannot be blank")
        return value


class WorkHistoryItem(APIModel):
    date: datetime
    work_type: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=2000)
    equipment: str | None = Field(default=None, max_length=256)
    result: str | None = Field(default=None, max_length=2000)
    # Set on demount and base-inspection acts: was corrosion found.
    corrosion_detected: bool | None = None


class FailureHistoryItem(APIModel):
    failure_date: datetime
    equipment: str | None = Field(default=None, max_length=256)
    failure_type: str = Field(min_length=1, max_length=256)
    cause: str | None = Field(default=None, max_length=2000)
    corrosion_detected: bool | None = None
    repair: str | None = Field(default=None, max_length=2000)
    downtime_days: int | None = Field(default=None, ge=0, le=36500)


class HistoryIngestionRequest(WellRequest):
    source_id: str = Field(default="ERA", min_length=1, max_length=128)
    batch_id: str | None = Field(default=None, min_length=1, max_length=128)
    work_history: list[WorkHistoryItem] = Field(default_factory=list, max_length=10000)
    failure_history: list[FailureHistoryItem] = Field(default_factory=list, max_length=10000)

    @field_validator("batch_id")
    @classmethod
    def trim_batch_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("batch_id cannot be blank")
        return value

    @field_validator("source_id")
    @classmethod
    def normalize_source_id(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("source_id cannot be blank")
        return value

    @model_validator(mode="after")
    def require_history(self):
        if not self.work_history and not self.failure_history:
            raise ValueError("at least one work or failure history record is required")
        return self


class CrewAvailability(APIModel):
    crew_id: str = Field(min_length=1, max_length=128)
    specialties: list[str] = Field(default_factory=list, max_length=100)
    region: str | None = Field(default=None, max_length=128)
    status: Literal["available", "busy", "unknown"] = "unknown"
    available_from: datetime | None = None
    available_to: datetime | None = None
    source_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_availability_window(self):
        if (self.available_from is not None and self.available_to is not None
                and self.available_to < self.available_from):
            raise ValueError("available_to must not be earlier than available_from")
        return self


class MonitoringResult(APIModel):
    status: Literal["completed", "needs_configuration"]
    started_at: datetime
    completed_at: datetime
    wells_checked: int = Field(ge=0)
    wells_scored: int = Field(ge=0)
    wells_unscored: int = Field(ge=0)
    # Wells awaiting a decision (a measure without a planned date): the AI agent drafts a proposal for each.
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    source_failures: list[dict[str, Any]] = Field(default_factory=list)
    dashboard_url: str
    message: str


# --------------------------------------------------------------------------- well rating
PromineStatus = Literal["not_created", "created", "sent"]


class RatingWellPatch(APIModel):
    """Operator edits from the dashboard."""

    plan_date: date | None = None
    promine_status: PromineStatus | None = None
    comment: str | None = Field(default=None, max_length=2000)
