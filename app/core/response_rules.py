"""Validated rules shared by the Excel importer and the proposal policy."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator

from app.core.indicators import INDICATORS, MAIN

Status = Literal["draft", "review", "approved", "retired", "test"]
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config/response_rules.json"
FIELDS = {f"measurements.{key}": INDICATORS[key] for key in MAIN}


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Reviewed(Record):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    revision: int = Field(strict=True, ge=1)
    status: Status = "draft"
    reviewer: str | None = None
    reviewed_on: date | None = None
    basis: str | None = None

    @model_validator(mode="after")
    def approval(self):
        if self.status == "approved" and not (self.reviewer and self.reviewed_on and self.basis):
            raise ValueError("Для approved нужны reviewer, reviewed_on и basis (документ и пункт)")
        return self


class Condition(Record):
    field: str
    op: Literal["eq", "gt", "gte", "lt", "lte", "is_missing"]
    value: StrictInt | StrictFloat | None = None
    max_age_days: int | None = Field(default=None, strict=True, ge=1)

    @model_validator(mode="after")
    def operand(self):
        if self.field not in FIELDS:
            raise ValueError("Неизвестный показатель")
        if self.op == "is_missing":
            if self.value is not None:
                raise ValueError("У is_missing нет значения для сравнения")
        elif self.value is not None:
            indicator = FIELDS[self.field]
            if not indicator.low <= self.value <= indicator.high:
                raise ValueError(f"{indicator.name}: порог вне диапазона данных {indicator.low}–{indicator.high}")
        return self


class Conditions(Record):
    all: list[Condition] = Field(default_factory=list)
    any: list[Condition] = Field(default_factory=list)

    @model_validator(mode="after")
    def nonempty(self):
        if not (self.all or self.any):
            raise ValueError("Нужно хотя бы одно условие")
        return self


class Rule(Reviewed):
    conditions: Conditions
    action: str = Field(min_length=1)
    explanation: str = Field(min_length=1)

    @model_validator(mode="after")
    def complete_conditions(self):
        if self.status in {"approved", "test"}:
            for condition in [*self.conditions.all, *self.conditions.any]:
                if condition.op != "is_missing" and condition.value is None:
                    raise ValueError("Перед согласованием заполните все пороги")
                if condition.max_age_days is None:
                    raise ValueError("Перед согласованием задайте допустимую давность данных")
        return self


class RuleBook(Record):
    schema_version: Literal[2]
    revision: int = Field(strict=True, ge=1)
    rules: list[Rule]

    @model_validator(mode="after")
    def unique_ids(self):
        if len({item.id for item in self.rules}) != len(self.rules):
            raise ValueError("Идентификаторы мероприятий должны быть уникальны")
        return self


def load_rulebook(path: Path = DEFAULT_PATH) -> RuleBook:
    return RuleBook.model_validate_json(Path(path).read_text(encoding="utf-8"))
