"""Generate synthetic ДАРС, ХИМИК and ЭРА ремонта workbooks for the demo and manual testing.

    python scripts/build_sample_data.py                 # data ending today
    python scripts/build_sample_data.py --end-date 2026-10-14 --out data

All three files describe the same wells (DEMO-001 … DEMO-014 on three fields), so after
uploading them (ДАРС and ХИМИК first, then ЭРА ремонта) the rating has every column filled:
field, status, equipment, sampling, environment, tonnage and НОО, demount and base-inspection
acts, category and the НТЦ score. The values are tuned so the measures cover every range:
3 × planned repair package (index 86–100), 3 × engineer decision (64–79), 3 × shock dosing
(43–57), 3 × no measures, and DEMO-013 with too little telemetry (rating without model scores).
DEMO-014 has a corrosive environment but confirmed protection (coated tubing, inhibitor 0.90,
corrosion rate 0.06 mm/yr): its index drops from 71 (engineer decision) to 43 (shock dosing).
Data older than MAI_MAX_TELEMETRY_AGE_HOURS is flagged as stale, so regenerate the files with a
recent --end-date before a demo. Every value is synthetic and describes no real equipment.
"""
from __future__ import annotations

import argparse
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

DAYS = 30            # daily ДАРС telemetry
LAB_STEP = 5         # ХИМИК sample every 5 days (forward fill covers 7)
INITIAL_WALL = 12.0

# Baseline per well. Values outside the risk thresholds are kept clearly below them, so the
# rule points (and the index = points / 14 × 100) are exactly the ones stated in the comment.
# ДАРС and ХИМИК rows have different times, so the 7-day persistence rules do not add points.
#   ДАРС: water cut %, injection deviation %, corrosion rate mm/yr, wall mm
#   ХИМИК: CO₂ %, chlorides mg/l, inhibitor efficiency, incompatible waters, tonnage t, НОО days
WELLS = {
    # ПКР: CO₂ critical 7 + corrosion rate critical 7 = 14 -> 100
    "DEMO-001": dict(field="Северное", wc=68, inj=6, rate=0.65, wall=10.6, co2=5.6, cl=24000, inh=0.82,
                     compat=False, tonnage=3.0, noo=38),
    # ПКР: inhibitor critical 7 + injection high 4 + wall warning 2 = 13 -> 93
    "DEMO-002": dict(field="Озёрное", wc=61, inj=21, rate=0.25, wall=7.5, co2=2.1, cl=21000, inh=0.35,
                     compat=False, tonnage=2.5, noo=52),
    # ПКР: injection high 4 + inhibitor low 4 + chlorides high 4 = 12 -> 86
    "DEMO-003": dict(field="Восточное", wc=58, inj=22, rate=0.22, wall=10.9, co2=2.3, cl=34000, inh=0.62,
                     compat=False, tonnage=2.0, noo=61),
    # РИ: corrosion rate critical 7 + CO₂ high 4 = 11 -> 79
    "DEMO-004": dict(field="Северное", wc=63, inj=7, rate=0.62, wall=10.2, co2=3.5, cl=22000, inh=0.80,
                     compat=False, tonnage=1.5, noo=95),
    # РИ: water cut high 4 + chlorides high 4 + wall warning 2 = 10 -> 71
    "DEMO-005": dict(field="Озёрное", wc=81, inj=4, rate=0.20, wall=7.6, co2=2.2, cl=33000, inh=0.84,
                     compat=False, tonnage=1.0, noo=110),
    # РИ: injection critical 7 + wall warning 2 = 9 -> 64; stopped 9 days ago
    "DEMO-006": dict(field="Восточное", wc=55, inj=34, rate=0.18, wall=7.7, co2=1.9, cl=17000, inh=0.85,
                     compat=False, tonnage=1.0, noo=125, stopped_days=9),
    # УД: corrosion rate high 4 + injection high 4 = 8 -> 57
    "DEMO-009": dict(field="Восточное", wc=49, inj=20, rate=0.42, wall=10.4, co2=1.7, cl=15000, inh=0.86,
                     compat=False, tonnage=0.0, noo=190),
    # УД: inhibitor critical 7 -> 50
    "DEMO-007": dict(field="Северное", wc=52, inj=5, rate=0.19, wall=10.8, co2=1.8, cl=16000, inh=0.35,
                     compat=False, tonnage=0.5, noo=130),
    # УД: CO₂ high 4 + wall warning 2 = 6 -> 43
    "DEMO-008": dict(field="Озёрное", wc=57, inj=6, rate=0.15, wall=7.8, co2=3.4, cl=18000, inh=0.83,
                     compat=False, tonnage=0.5, noo=160),
    # Мероприятия не требуются: water cut high 4 -> 29
    "DEMO-010": dict(field="Северное", wc=78, inj=4, rate=0.14, wall=11.1, co2=1.6, cl=14000, inh=0.88,
                     compat=False, tonnage=0.0, noo=240),
    # Мероприятия не требуются: 0 -> 0; last laboratory sample 45 days ago (no sampling)
    "DEMO-011": dict(field="Озёрное", wc=47, inj=3, rate=0.11, wall=11.4, co2=1.4, cl=12000, inh=0.90,
                     compat=False, tonnage=0.0, noo=300, lab_offset=45),
    # Мероприятия не требуются: wall warning 2 -> 14; no ХИМИК samples, no ЭРА history
    "DEMO-012": dict(field="Восточное", wc=44, inj=3, rate=0.12, wall=7.9, co2=None, cl=None, inh=None,
                     compat=None, tonnage=None, noo=None, no_lab=True),
    # Only 3 days of telemetry: the models are skipped, the rating shows the ЭРА evidence
    # Protected: CO₂ high 4 + chlorides high 4 + wall warning 2 = 10 -> 71; effective protection
    # counts the 8 environment points × 0.5 -> 6 -> 43
    "DEMO-014": dict(field="Восточное", wc=60, inj=4, rate=0.06, wall=7.6, co2=3.6, cl=32000, inh=0.90,
                     compat=False, tonnage=2.0, noo=210),
    "DEMO-013": dict(field="Северное", wc=50, inj=4, rate=0.13, wall=11.0, co2=None, cl=None, inh=None,
                     compat=None, tonnage=None, noo=None, no_lab=True, dars_days=3),
}
WELLS = dict(sorted(WELLS.items()))

EQUIPMENT = {
    "DEMO-001": ("УЭЦН-5-80, НКТ-73", "УЭЦН-5-80, НКТ-73 (с покрытием)"),
    "DEMO-002": ("УЭЦН-5А-125, НКТ-73", "УЭЦН-5А-125, НКТ-73"),
    "DEMO-003": ("УЭЦН-5-45, НКТ-60", "УЭЦН-5-60, НКТ-60"),
    "DEMO-004": ("УЭЦН-5-60, НКТ-73", None),
    "DEMO-005": ("УЭЦН-5-80, НКТ-73", None),
    "DEMO-006": ("УШГН-57, НКТ-73", None),
    "DEMO-007": ("УЭЦН-5-45, НКТ-60", "УЭЦН-5-45, НКТ-60"),
    "DEMO-008": ("УЭЦН-5А-125, НКТ-73", None),
    "DEMO-009": ("УЭЦН-5-60, НКТ-73", "УЭЦН-5-60, НКТ-73 (с покрытием)"),
    "DEMO-010": ("УШГН-44, НКТ-60", "УШГН-44, НКТ-60"),
    "DEMO-011": ("УЭЦН-5-80, НКТ-73", None),
    "DEMO-013": ("УЭЦН-5-45, НКТ-60", None),
    "DEMO-014": ("УЭЦН-5-80, НКТ-73 с покрытием", None),
}

HEAD_FONT = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="0E2A47")
REQUIRED_FILL = PatternFill("solid", fgColor="0F766E")


def wobble(value: float, i: int, seed: int, amplitude: float = 0.004) -> float:
    """Deterministic small variation without a trend."""
    return value * (1 + amplitude * math.sin(i * 1.7 + seed))


def write(path: Path, sheet_name: str, headers: list[str], rows: list[list], required: set[str]) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = sheet_name
    sheet.append(headers)
    for column, header in enumerate(headers, start=1):
        cell = sheet.cell(1, column)
        cell.font = HEAD_FONT
        cell.fill = REQUIRED_FILL if header in required else HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(column)].width = max(12, min(30, len(header) + 4))
    for row in rows:
        sheet.append(row)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, datetime):
                cell.number_format = "DD.MM.YYYY HH:MM"
    sheet.freeze_panes = "A2"
    sheet.row_dimensions[1].height = 32
    book.save(path)


def dars_rows(end: date) -> list[list]:
    rows = []
    for seed, (well, spec) in enumerate(WELLS.items()):
        days = spec.get("dars_days", DAYS)
        for i in range(days):
            day = end - timedelta(days=days - 1 - i)
            stopped = spec.get("stopped_days") and (end - day).days < spec["stopped_days"]
            rows.append([
                well, spec["field"], datetime.combine(day, time(6, 0)),
                "Не в работе" if stopped else "В работе",
                round(wobble(spec["wc"], i, seed), 2), round(wobble(spec["inj"], i, seed + 1), 2),
                round(wobble(spec["rate"], i, seed + 2), 3), round(wobble(spec["wall"], i, seed + 3, 0.001), 3),
                INITIAL_WALL,
            ])
    return rows


def khimik_rows(end: date) -> list[list]:
    rows = []
    for seed, (well, spec) in enumerate(WELLS.items()):
        if spec.get("no_lab"):
            continue
        last = end - timedelta(days=spec.get("lab_offset", 0))
        for k, offset in enumerate(range(DAYS - 2, -1, -LAB_STEP)):
            day = last - timedelta(days=offset)
            rows.append([
                well, datetime.combine(day, time(10, 30)),
                round(wobble(spec["co2"], k, seed, 0.006), 3), round(wobble(spec["cl"], k, seed + 1, 0.006), -1),
                round(wobble(spec["inh"], k, seed + 2, 0.004), 3),
                "Да" if spec["compat"] else "Нет", spec["tonnage"], spec["noo"],
            ])
    return rows


def era_rows(end: date) -> list[list]:
    def event(well, days_ago, kind, work_type, equipment, result=None, corrosion=None, reason=None,
              failure_type=None, cause=None, repair=None, downtime=None):
        return [well, datetime.combine(end - timedelta(days=days_ago), time(9, 0)), kind, work_type, reason,
                equipment, result, failure_type, cause,
                None if corrosion is None else ("Да" if corrosion else "Нет"), repair, downtime]

    rows = []
    for well, (first, second) in EQUIPMENT.items():
        rows.append(event(well, 240, "Работа", "Спуск оборудования", first, "Оборудование спущено",
                          reason="Ввод после ремонта"))
        rows.append(event(well, 60, "Работа", "Плановая диагностика", first, "Замечаний нет",
                          reason="График диагностики"))
    # Demount and base-inspection acts with the corrosion finding, then a new run.
    acts = {
        "DEMO-001": (True, True, "Язвенная коррозия НКТ, сквозные поражения"),
        "DEMO-002": (True, None, None),
        "DEMO-003": (True, False, "Коррозионных поражений нет, износ рабочих органов"),
        "DEMO-007": (False, False, "Коррозии нет"),
        "DEMO-009": (True, True, "Общая коррозия корпуса насоса"),
    }
    for well, (demount_corrosion, base_corrosion, base_result) in acts.items():
        rows.append(event(well, 150, "Работа", "Демонтаж ГНО", EQUIPMENT[well][0],
                          "Следы коррозии на НКТ" if demount_corrosion else "Видимых следов коррозии нет",
                          demount_corrosion, reason="Снижение подачи"))
        if base_corrosion is not None:
            rows.append(event(well, 142, "Работа", "Разбор на базе", EQUIPMENT[well][0], base_result,
                              base_corrosion, reason="Акт разбора"))
        rows.append(event(well, 135, "Работа", "Спуск оборудования", EQUIPMENT[well][1],
                          "Оборудование спущено", reason="После демонтажа"))
    # A demount older than the 365-day window is not counted in the rating.
    rows.append(event("DEMO-010", 400, "Работа", "Демонтаж ГНО", EQUIPMENT["DEMO-010"][0],
                      "Следы коррозии на НКТ", True, reason="Снижение подачи"))
    rows.append(event("DEMO-010", 395, "Работа", "Спуск оборудования", EQUIPMENT["DEMO-010"][1],
                      "Оборудование спущено", reason="После демонтажа"))
    # Failures.
    rows.append(event("DEMO-001", 152, "Отказ", None, "НКТ", corrosion=True, failure_type="Негерметичность НКТ",
                      cause="Коррозия", repair="Замена НКТ", downtime=4))
    rows.append(event("DEMO-004", 90, "Отказ", None, "ЭЦН", corrosion=False, failure_type="Снижение изоляции",
                      cause="Износ кабеля", repair="Замена кабельной линии", downtime=3))
    rows.sort(key=lambda row: (row[0], row[1]))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today(),
                        help="last day of the data, YYYY-MM-DD (default: today)")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    write(args.out / "sample_dars.xlsx", "ДАРС",
          ["Номер скважины", "Месторождение", "Дата измерения", "Статус", "Обводнённость, %",
           "Отклонение закачки, %", "Скорость коррозии, мм/год", "Толщина стенки, мм",
           "Начальная толщина стенки, мм"],
          dars_rows(args.end_date), {"Номер скважины", "Дата измерения"})
    write(args.out / "sample_khimik.xlsx", "ХИМИК",
          ["Номер скважины", "Дата отбора проб", "CO₂, %", "Хлориды, мг/л", "Эффективность ингибитора",
           "Несовместимость вод", "Тоннаж обработки, т", "НОО, сут"],
          khimik_rows(args.end_date), {"Номер скважины", "Дата отбора проб"})
    write(args.out / "sample_era.xlsx", "ЭРА ремонта",
          ["Номер скважины", "Дата события", "Тип записи", "Вид работ", "Причина", "Оборудование",
           "Результат", "Тип отказа", "Причина отказа", "Обнаружена коррозия", "Ремонт", "Простой, дни"],
          era_rows(args.end_date), {"Номер скважины", "Дата события", "Тип записи"})
    print(f"Saved sample_dars.xlsx, sample_khimik.xlsx, sample_era.xlsx to {args.out} "
          f"(data up to {args.end_date:%d.%m.%Y})")


if __name__ == "__main__":
    main()
