"""Read-only automatic NORSOK assessment from card sources and admin project defaults."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.core.co2_corrosion import calculate, digest
from app.core.co2_inputs import HEADERS
from app.core.excel_cells import header_key, number
from app.core.norsok import ModelConfigurationError, load_model
from app.schemas import CO2Calculation

FIELDS = ('temperature_c', 'pressure_abs_bar', 'co2_pct', 'ph_in_situ', 'wall_shear_pa',
          'water_cut_pct', 'material', 'water_wetting')
OPTIONAL = ('h2s_partial_bar', 'organic_acids_ppm')
ALIASES = {header_key(label): key for key, label in HEADERS.items() if key in FIELDS + OPTIONAL}
ALIASES.update({header_key(key): key for key in FIELDS + OPTIONAL})
ALIASES.update({header_key('T_C'): 'temperature_c', header_key('Температура на устье, °C'): 'temperature_c'})


class _Defaults(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    max_age_days: int = Field(ge=1, le=3650, strict=True)
    registry_pressure_basis: Literal['unknown', 'absolute', 'gauge']
    allow_lab_ph_as_in_situ: bool = Field(strict=True)
    values: dict


def _request(well_id, values):
    inputs = {key: value for key, value in values.items() if key != 'co2_pct'}
    inputs['co2_mole_fractions'] = [values['co2_pct'] / 100]
    return CO2Calculation(well_id=well_id, **inputs)


def load_defaults(path: Path | None = None, model: dict | None = None) -> dict:
    """No HTTP/UI mutation path; administrators own the file and environment overrides."""
    try:
        raw = json.loads(Path(path or settings.co2_defaults_path).read_text(encoding='utf-8-sig'))
        for key in FIELDS + OPTIONAL:
            value = os.getenv('MAI_CO2_DEFAULT_' + key.upper())
            if value is not None:
                raw['values'][key] = value if key in ('material', 'water_wetting') else json.loads(value)
        for key in ('max_age_days', 'allow_lab_ph_as_in_situ', 'registry_pressure_basis'):
            value = os.getenv('MAI_CO2_' + key.upper())
            if value is not None:
                raw[key] = value if key == 'registry_pressure_basis' else json.loads(value)
        policy = _Defaults.model_validate(raw).model_dump()
        if not set(FIELDS) <= policy['values'].keys() or set(policy['values']) - set(FIELDS + OPTIONAL):
            raise ValueError('Неверный список параметров подстановки.')
        if isinstance(policy['values']['co2_pct'], bool):
            raise ValueError('CO₂ должен быть числом.')
        calculate(_request('DEFAULT-CHECK', policy['values']), model or load_model())
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        raise ModelConfigurationError('Неверные администраторские подстановки CO₂; проверьте config/co2_defaults.json и MAI_CO2_DEFAULT_*.') from exc
    policy['configuration_hash'] = digest(policy)
    return policy


def _moment(value):
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def assess(well_id: str, records: list, payloads: dict, *, defaults: dict | None = None,
           model: dict | None = None, now: datetime | None = None) -> dict:
    model = model or load_model()
    defaults = defaults or load_defaults(model=model)
    now = now or datetime.now(timezone.utc)
    candidates, reasons = {}, {}

    def offer(key, value, when, source, label, assumed_basis=False):
        if value is None or value == '' or value == 'unknown':
            return
        if isinstance(value, str):
            translations = {'углеродистая сталь': 'carbon_steel', 'подтверждён': 'water_wet',
                            'подтвержден': 'water_wet', 'не подтверждён': 'not_water_wet'}
            value = translations.get(value.strip().casefold(), value) if key in ('material', 'water_wetting') else value
        when = _moment(when)
        if when and (now-when > timedelta(days=defaults['max_age_days']) or
                     when-now > timedelta(hours=settings.max_future_skew_hours)):
            reasons[key] = 'Исходное значение устарело или датировано будущим временем.'
            return
        invalid = None
        if key not in ('material', 'water_wetting'):
            parsed = number(value)
            if parsed is None:
                invalid = str(value)[:100]
            value = parsed
        item = {'kind': 'source', 'value': value, 'source_id': source,
                'date': when.isoformat() if when else None, 'label': str(label), 'assumed_basis': assumed_basis}
        if invalid is not None:
            item['invalid_value'] = invalid
        if key not in candidates or (item['date'] or '') > (candidates[key]['date'] or ''):
            candidates[key] = item

    def named(values, when, source):
        for label, value in values.items():
            key = ALIASES.get(header_key(label))
            if key:
                offer(key, value, when, source, label)
            elif header_key(label) in (header_key('co2_mole_fraction'), header_key(HEADERS['co2_mole_fraction'])):
                parsed = number(value)
                offer('co2_pct', parsed*100 if parsed is not None else value, when, source, label)
            elif header_key(label) in ('ph', 'phводы', 'phmeasured', 'phused'):
                if defaults['allow_lab_ph_as_in_situ']:
                    offer('ph_in_situ', value, when, source, label, assumed_basis=True)
                else:
                    reasons['ph_in_situ'] = 'pH в карточке указан без подтверждения рабочих условий.'
            elif header_key(label) in ('pbar', 'давлениеатм', 'давлениебар'):
                reasons['pressure_abs_bar'] = 'Тип или единицы исходного давления не подтверждены.'

    for point in sorted(records, key=lambda p: _moment(p.timestamp)):
        source = str(getattr(point, 'source_id', '') or '')
        for key in FIELDS + OPTIONAL:
            offer(key, getattr(point, key, None), point.timestamp,
                  getattr(point, 'field_sources', {}).get(key, source), key)
        for label, value in (point.extra_measurements or {}).items():
            named({label: value}, point.timestamp, getattr(point, 'extra_sources', {}).get(label, source))
    for source, payload in payloads.items():
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get('passport'), dict):
            passport = payload['passport']
            named(passport, passport.get('passport_date'), source)
        for row in reversed(payload.get('rows') or []):
            if not isinstance(row, dict):
                continue
            labels = payload.get('labels') or {}
            named({labels.get(key, key): value for key, value in row.items()}, payload.get('date'), source)
            if 'wellhead_temp_c' in row:
                offer('temperature_c', row['wellhead_temp_c'], payload.get('date'), source, 'Температура на устье, °C')
            if row.get('wellhead_pressure_atm') is not None:
                basis = defaults['registry_pressure_basis']
                if basis == 'unknown':
                    reasons['pressure_abs_bar'] = 'В реестре давление в атм без типа; используется настроечное абсолютное давление.'
                else:
                    parsed = number(row['wellhead_pressure_atm'])
                    pressure = parsed*1.01325 + (1.01325 if basis == 'gauge' else 0) if parsed is not None else row['wellhead_pressure_atm']
                    offer('pressure_abs_bar', pressure, payload.get('date'), source, f'Давление из реестра, атм ({basis}) → абсолютные бар')

    provenance, values = {}, {}
    for key in FIELDS + OPTIONAL:
        item = candidates.get(key)
        if item is None and key in defaults['values']:
            item = {'kind': 'default', 'value': defaults['values'][key], 'date': None,
                    'source_id': None, 'reason': reasons.get(key, 'В карточке нет подходящего значения.'),
                    'label': 'Администраторское допущение проекта'}
        if item is not None:
            provenance[key] = item
            values[key] = item['value']
    used = [key for key, item in provenance.items() if item['kind'] == 'default']
    common = {'well_id': well_id, 'automatic': True, 'calculation_mode': 'automatic',
              'defaults_used': used, 'defaults_hash': defaults['configuration_hash'],
              'defaults_snapshot': defaults, 'input_provenance': provenance,
              'model_title': model['title'], 'model_snapshot': model,
              'model_version': f"{model['model_id']}-r{model['revision']}-{model['configuration_hash'][:12]}",
              'configuration_hash': model['configuration_hash'], 'calculated_at': now.isoformat()}
    try:
        invalid = [HEADERS[key] for key, item in provenance.items() if 'invalid_value' in item]
        if invalid:
            raise ValueError('Некорректные исходные значения: ' + ', '.join(invalid))
        request = _request(well_id, values).model_copy(update={
            'composition_source': 'scenario' if 'co2_pct' in used else 'measured',
            'source': 'Автоматически из карточки и администраторских подстановок'})
        result = calculate(request, model)
        result.update(common, automatic_status='calculated')
        if used:
            result['applicability_status'] = 'screening_with_project_defaults'
            result['quality_flags'].append('Использованы администраторские допущения проекта: ' +
                                          ', '.join(f"{HEADERS[key]} = {provenance[key]['value']}" for key in used) + '. Это не измерения и не нормативы NORSOK.')
        if provenance['ph_in_situ'].get('assumed_basis'):
            result['quality_flags'].append('Администратор разрешил использовать лабораторный pH как рабочий; это дополнительное допущение.')
        result['input_hash'] = digest({'calculation': result['input_hash'], 'defaults': defaults['configuration_hash'],
                                       'provenance': provenance})
        return result
    except ModelConfigurationError:
        raise
    except (ValueError, TypeError, ArithmeticError) as exc:
        return {**common, 'automatic_status': 'not_applicable', 'scenarios': [], 'inputs': values,
                'state_at': None,
                'synthetic': False, 'imported_results': {}, 'quality_flags': [str(exc)]}


def for_wells(well_ids) -> dict[str, dict]:
    from app.db.co2 import automatic_inputs
    ids = list(dict.fromkeys(well_ids))
    if not ids:
        return {}
    try:
        model = load_model()
        defaults = load_defaults(model=model)
    except ModelConfigurationError as exc:
        return {well: {'well_id': well, 'automatic': True, 'automatic_status': 'configuration_error',
                       'calculation_mode': 'automatic', 'scenarios': [], 'quality_flags': [str(exc)]} for well in ids}
    data = automatic_inputs(ids)
    now = datetime.now(timezone.utc)
    results = {}
    for well in ids:
        try:
            results[well] = assess(well, **data[well], defaults=defaults, model=model, now=now)
        except ModelConfigurationError as exc:
            results[well] = {'well_id': well, 'automatic': True, 'automatic_status': 'configuration_error',
                             'calculation_mode': 'automatic', 'scenarios': [], 'quality_flags': [str(exc)]}
    return results
