"""Indicators of a well the models and the rating work with: name, unit and valid range in one place.

The normalisation (range checks), the readiness of the models, the НТЦ rules, the math model, the
data gaps and the AI agent take the names and units from here. Numbers are shown the Russian way
(fmt): a decimal comma, a space between thousands, no exponent.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite, log10


@dataclass(frozen=True)
class Indicator:
    name: str              # in a sentence: «CO₂», «обводнённость»
    unit: str              # «%», «мг/л», «мм/год», «мм»; "" for a fraction
    low: float             # physical sanity bounds: a value outside them is not used
    high: float
    caption: str = ""      # name with the unit, when it is not «name, unit»

    @property
    def label(self) -> str:
        return self.caption or (f"{self.name}, {self.unit}" if self.unit else self.name)

    def show(self, value: float) -> str:
        """The value with its unit: «5,63 %», «23 990 мг/л», «0,82»."""
        return f"{fmt(value)} {self.unit}" if self.unit else fmt(value)


# Sanity bounds inherited from the V3.0 notebook: data-quality bounds, not corrosion intervention
# limits. The first seven are measured routinely (MAIN: the readiness of the models looks at them).
INDICATORS: dict[str, Indicator] = {
    "water_cut_pct": Indicator("обводнённость", "%", 0.0, 100.0),
    "co2_pct": Indicator("CO₂", "%", 0.0, 8.0),
    "chlorides_mg_l": Indicator("хлориды", "мг/л", 0.0, 80000.0),
    "inhibitor_efficiency": Indicator("эффективность ингибитора", "", 0.0, 1.2, "эффективность ингибитора (доля)"),
    "injection_deviation_pct": Indicator("отклонение закачки ингибитора", "%", 0.0, 60.0),
    "corrosion_rate_mm_year": Indicator("скорость коррозии", "мм/год", 0.0, 1.5),
    "wall_thickness_mm": Indicator("толщина стенки", "мм", 0.8, 14.0),
    "initial_wall_thickness_mm": Indicator("начальная толщина стенки", "мм", 0.8, 14.0),
    "metal_loss_mm": Indicator("потеря металла", "мм", 0.0, 14.0),
    "corrosion_load": Indicator("коррозионная нагрузка", "", 0.02, 3.0),
}
MAIN = tuple(INDICATORS)[:7]


def fmt(value: float, digits: int = 3) -> str:
    """A number the Russian way with `digits` significant digits: 5,63; 23 990; 0,012; −0,0000474."""
    if not isfinite(value):
        return "—"
    if value == 0:
        return "0"
    decimals = max(0, digits - 1 - floor(log10(abs(value))))
    text = f"{abs(value):,.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return ("−" if value < 0 else "") + text.replace(",", " ").replace(".", ",")


def plural(count: int, one: str, few: str, many: str) -> str:
    """«1 замер», «3 замера», «8 замеров»."""
    if 11 <= count % 100 <= 14:
        return many
    return one if count % 10 == 1 else few if 2 <= count % 10 <= 4 else many
