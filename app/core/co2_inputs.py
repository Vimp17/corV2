"""Bounded raw-input import. Units are explicit; no inferred pH, pressure type or flow."""
from __future__ import annotations

import csv
import hashlib
import json
from io import BytesIO, StringIO
from zipfile import ZipFile

from openpyxl import load_workbook
from pydantic import ValidationError

from app.config import settings
from app.core.co2_corrosion import calculate
from app.core.excel_cells import as_datetime, number
from app.core.norsok import ModelConfigurationError, load_model
from app.schemas import CO2Calculation

HEADERS = {
    'well_id': 'Скважина', 'temperature_c': 'Температура, °C',
    'pressure_abs_bar': 'Абсолютное давление, бар', 'co2_pct': 'CO₂, % мол.',
    'ph_in_situ': 'pH при рабочих условиях', 'wall_shear_pa': 'Касательное напряжение, Па',
    'material': 'Материал', 'water_wetting': 'Контакт воды с металлом',
    'water_cut_pct': 'Обводнённость, %', 'h2s_partial_bar': 'Парциальное давление H₂S, бар',
    'organic_acids_ppm': 'Органические кислоты, ppm', 'state_at': 'Дата состояния', 'source': 'Источник',
    'co2_mole_fraction': 'Доля CO₂, моль/моль',
}
REQUIRED = {'well_id', 'temperature_c', 'pressure_abs_bar', 'ph_in_situ', 'wall_shear_pa', 'material', 'water_wetting'}
NUMERIC = {'temperature_c', 'pressure_abs_bar', 'co2_pct', 'co2_mole_fraction', 'ph_in_situ',
           'wall_shear_pa', 'water_cut_pct', 'h2s_partial_bar', 'organic_acids_ppm'}
ALIASES = {label.casefold(): key for key, label in HEADERS.items()} | {key: key for key in HEADERS}


def _keys(header) -> list[str]:
    keys = [ALIASES.get(str(key).strip().casefold(), '') for key in header]
    if len(keys) > 32 or not all(keys) or len(set(keys)) != len(keys):
        raise ValueError('Неизвестные или повторяющиеся колонки. Используйте шаблон исходных данных NORSOK.')
    if not REQUIRED <= set(keys) or len({'co2_pct', 'co2_mole_fraction'} & set(keys)) != 1:
        raise ValueError('Нужны обязательные колонки и одна единица состава: CO₂, % мол. либо доля моль/моль.')
    return keys


def _table(iterator):
    header = list(next(iterator, ()))
    while header and header[-1] is None:
        header.pop()
    keys = _keys(header)
    rows = []
    for index, values in enumerate(iterator, 2):
        if index > settings.max_excel_rows + 1:
            raise ValueError('Превышен предел строк импорта.')
        if not any(v is not None and v != '' for v in values):
            continue
        if any(v is not None and v != '' for v in values[len(keys):]):
            raise ValueError(f'Строка {index}: есть значения без заголовка.')
        rows.append((index, dict(zip(keys, values))))
    return rows


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('JSON содержит повторяющийся ключ: ' + key)
        result[key] = value
    return result


def _read(content: bytes, filename: str):
    extension = filename.rsplit('.', 1)[-1].lower()
    if extension == 'json':
        rows = json.loads(content.decode('utf-8-sig'), object_pairs_hook=_json_object)
        if not isinstance(rows, list):
            raise ValueError('JSON должен содержать массив исходных строк.')
        return 'inputs', list(enumerate(rows, 2))
    if extension == 'csv':
        text = content.decode('utf-8-sig')
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=';,\t')
        except csv.Error as exc:
            raise ValueError('CSV: нужен UTF-8 и разделитель ; , или табуляция.') from exc
        return 'inputs', _table(csv.reader(StringIO(text), dialect))
    if extension != 'xlsx':
        raise ValueError('Поддерживаются исходные данные .xlsx, .csv (UTF-8) и .json.')
    with ZipFile(BytesIO(content)) as archive:
        if sum(info.file_size for info in archive.infolist()) > 150 * 1024 * 1024:
            raise ValueError('Слишком большой распакованный XLSX.')
    book = load_workbook(BytesIO(content), read_only=True, data_only=False)
    try:
        matches = []
        for sheet in book:
            if sheet.max_column and sheet.max_column > 32:
                continue
            try:
                _keys(next(sheet.iter_rows(max_row=1, values_only=True), ()))
                matches.append(sheet)
            except ValueError:
                continue
        if len(matches) != 1:
            raise ValueError('Нужен один лист с заголовками шаблона NORSOK. Листы-инструкции допустимы.')
        sheet = matches[0]
        return sheet.title, _table(sheet.iter_rows(max_row=settings.max_excel_rows + 2, max_col=32, values_only=True))
    finally:
        book.close()


def parse(content: bytes, filename: str, synthetic: bool, model: dict | None = None) -> dict:
    if len(content) > settings.max_excel_bytes:
        raise ValueError('Файл превышает предел размера.')
    model = model or load_model()
    sheet, rows = _read(content, filename)
    if not rows or len(rows) > settings.max_excel_rows:
        raise ValueError('Файл пуст либо превышен предел строк импорта.')
    source_hash = hashlib.sha256(content).hexdigest()
    assessments, errors, seen = [], [], set()
    for index, raw in rows:
        try:
            if not isinstance(raw, dict):
                raise ValueError('Требуется объект с именованными колонками.')
            keys = _keys(raw)
            row = {key: value for key, value in zip(keys, raw.values()) if value is not None and value != ''}
            for key in NUMERIC & row.keys():
                value = number(row[key])
                if value is None:
                    raise ValueError(f'{HEADERS[key]}: требуется конечное число, без формул и единиц в ячейке.')
                row[key] = value
            well = row.get('well_id')
            if isinstance(well, bool) or not isinstance(well, (str, int, float)):
                raise ValueError('Укажите ID скважины.')
            if isinstance(well, float):
                if not well.is_integer():
                    raise ValueError('Числовой ID должен быть целым; ведущие нули сохраняются только в текстовой ячейке.')
                well = int(well)
            row['well_id'] = str(well).strip()
            if row.get('state_at') is not None:
                row['state_at'] = as_datetime(row['state_at'])
            for key, mapping in (('material', {'углеродистая сталь': 'carbon_steel'}),
                                 ('water_wetting', {'подтверждён': 'water_wet', 'подтвержден': 'water_wet'})):
                if isinstance(row.get(key), str):
                    row[key] = mapping.get(row[key].strip().casefold(), row[key].strip())
            fraction = row.pop('co2_pct') / 100 if 'co2_pct' in row else row.pop('co2_mole_fraction', None)
            row.update(co2_mole_fractions=[fraction], composition_source='measured', synthetic=synthetic,
                       source=row.get('source') or filename[:256])
            request = CO2Calculation(**row)
            identity = (request.well_id, request.state_at)
            if identity in seen:
                raise ValueError('Повтор скважины и даты состояния в файле; оставьте одну строку для состояния.')
            result = calculate(request, model)
            result.update(source_hash=source_hash, source_row=index, source_sheet=sheet, source_name=filename[:256])
            assessments.append(result)
            seen.add(identity)
        except ModelConfigurationError:
            raise
        except (ValueError, TypeError, OverflowError) as exc:
            if isinstance(exc, ValidationError):
                message = '; '.join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            else:
                message = str(exc)
            errors.append({'row': index, 'message': message})
    return dict(source_hash=source_hash, configuration_hash=model['configuration_hash'],
                model_title=model['title'], rows_received=len(rows), valid_rows=len(assessments),
                errors=errors, assessments=assessments)


def template_csv() -> str:
    """Excel-readable UTF-8 CSV with headers only; no synthetic values to import as real."""
    output = StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow([HEADERS[key] for key in HEADERS if key != 'co2_mole_fraction'])
    return '\ufeff' + output.getvalue()
