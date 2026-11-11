"""Strict v4 import. Never routes scores through telemetry or generic CO₂ aliases."""
from __future__ import annotations

import hashlib
import json
import math
from io import BytesIO

from openpyxl import load_workbook

from app.config import settings
from app.core.co2_corrosion import LIMITATIONS, digest

REQUIRED = {"№ скважины", "T_C", "P_bar", "V_1975_median", "V_A1_median", "V_A2_median"}
NUMERIC = {"T_C", "P_atm", "P_bar", "Q_m3d", "D_mm", "v_mps", "pH_measured", "pH_used",
           "pH_estimated", "HCO3_mgL", "GF", "KVCH", "TDS", "Cl", "pCO2_low", "pCO2_mid", "pCO2_high",
           "V_1975_median", "V_A1_median", "V_A2_median", "S_CO2_1975", "S_CO2_A1", "S_CO2_A2", "S_CO2_final"}
ALLOWED = REQUIRED | NUMERIC | {"Пласт", "Класс_1975", "Класс_A1", "Класс_A2", "Класс_final",
                               "validation_status", "flags_anomaly", "excel_row"}


def parse(content: bytes, filename: str, synthetic: bool) -> dict:
    if filename.lower().endswith(".json"):
        rows = json.loads(content.decode("utf-8-sig"))
        if not isinstance(rows, list):
            raise ValueError("JSON должен содержать массив расчётных строк v4.")
        sheet = "calculation_rows"
    elif filename.lower().endswith(".xlsx"):
        book = load_workbook(BytesIO(content), read_only=True, data_only=True)
        try:
            matches = [s for s in book if s.title.strip().endswith("Расчёт")]
            if len(matches) != 1:
                raise ValueError("Нужен один лист «Расчёт» с точными колонками v4.")
            sheet = matches[0].title
            iterator = matches[0].iter_rows(values_only=True)
            header = list(next(iterator, ()))
            if not REQUIRED <= set(header) or len([x for x in header if x]) != len(set(x for x in header if x)):
                raise ValueError("Нет обязательных колонок v4 либо заголовки повторяются.")
            rows = []
            for excel_row, values in enumerate(iterator, 2):
                if len(rows) >= settings.max_excel_rows:
                    raise ValueError("Превышен предел строк импорта.")
                if any(v is not None for v in values):
                    rows.append(dict(zip(header, values)) | {"excel_row": excel_row})
        finally:
            book.close()
    else:
        raise ValueError("Для CO₂ используйте calculation_rows.json или книгу v4 .xlsx.")
    if not rows or len(rows) > settings.max_excel_rows:
        raise ValueError("Пустой файл либо превышен предел строк импорта.")
    source_hash = hashlib.sha256(content).hexdigest()
    assessments, errors, seen = [], [], set()
    for number, raw in enumerate(rows, 2):
        try:
            if not isinstance(raw, dict) or not REQUIRED <= raw.keys():
                raise ValueError("Нет обязательных колонок v4.")
            row = {k: v for k, v in raw.items() if k in ALLOWED}
            well = row["№ скважины"]
            if isinstance(well, bool) or not isinstance(well, (str, int, float)) or not str(well).strip():
                raise ValueError("Нет корректного ID скважины.")
            if isinstance(well, float):
                if not math.isfinite(well) or not well.is_integer():
                    raise ValueError("Некорректный числовой ID.")
                well = int(well)
            well = str(well).strip()
            if len(well) > 128 or well in seen:
                raise ValueError("ID слишком длинный либо повторяется в файле.")
            for key in NUMERIC:
                value = row.get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                    raise ValueError(f"{key}: требуется конечное число.")
            for key in REQUIRED - {"№ скважины"}:
                if row.get(key) is None:
                    raise ValueError(f"{key}: нет значения.")
            if row["T_C"] <= -273.15 or row["P_bar"] <= 0:
                raise ValueError("Недопустимая температура или давление.")
            if any(row.get(k, 0) is not None and row.get(k, 0) < 0 for k in NUMERIC - {"T_C"}):
                raise ValueError("Отрицательное значение скорости, давления или состава.")
            source_row = row.get("excel_row", number)
            if isinstance(source_row, bool) or not isinstance(source_row, int) or source_row < 2:
                raise ValueError("Некорректный номер исходной строки.")
            seen.add(well)
            flags = list(LIMITATIONS) + [
                "Импорт внешнего расчёта v4: A1 имеет противоречие знака поправки скорости; полный A2 не воспроизведён.",
                "Тип исходного давления и дата состояния среды не подтверждены."]
            if synthetic:
                flags.append("Синтетические демонстрационные данные.")
            if row.get("pH_estimated"):
                flags.append("pH подставлен во внешнем расчёте.")
            flags.extend(str(row[k]) for k in ("validation_status", "flags_anomaly") if row.get(k))
            assessments.append({"well_id": well, "state_at": None, "model_version": "co2-v4-import-1",
                "calculation_mode": "imported", "synthetic": synthetic, "source_hash": source_hash,
                "source_row": source_row, "source_sheet": sheet, "source_name": filename[:256],
                "input_hash": digest(row), "inputs": row, "scenarios": [],
                "imported_results": {k: row[k] for k in row if k.startswith(("V_", "S_CO2_", "Класс_"))},
                "quality_flags": flags, "applicability_status": "external_unvalidated"})
        except (ValueError, TypeError) as exc:
            errors.append({"row": number, "message": str(exc)})
    return {"source_hash": source_hash, "rows_received": len(rows), "valid_rows": len(assessments),
            "errors": errors, "assessments": assessments}
