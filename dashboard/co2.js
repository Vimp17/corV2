// One persistent draft and pending request per well; redraws keep the actual form nodes.
const co2Forms = new Map();
const co2Cards = new Map();
const CO2_INPUTS = {
  co2_pct: 'CO₂, % мол.',
  temperature_c: 'Температура, °C',
  pressure_abs_bar: 'Абсолютное давление, бар',
  co2_mole_fractions: 'Доли CO₂ (молярные)',
  composition_source: 'Происхождение состава',
  source: 'Источник значений',
  ph_in_situ: 'pH при рабочих условиях', wall_shear_pa: 'Касательное напряжение, Па',
  water_cut_pct: 'Обводнённость, %', material: 'Материал', water_wetting: 'Контакт воды с металлом',
  h2s_partial_bar: 'Парциальное давление H₂S, бар', organic_acids_ppm: 'Органические кислоты, ppm',
  T_C: 'Температура, °C',
  P_bar: 'Давление v4, бар',
  P_atm: 'Исходное давление v4, атм',
  Q_m3d: 'Расход, м³/сут',
  D_mm: 'Диаметр, мм',
  v_mps: 'Скорость потока, м/с',
  pH_measured: 'Измеренный pH',
  pH_used: 'Использованный pH',
  pH_estimated: 'pH оценён',
  HCO3_mgL: 'Гидрокарбонаты, мг/л',
  GF: 'Газовый фактор',
  KVCH: 'КВЧ',
  TDS: 'Минерализация',
  Cl: 'Хлориды',
  pCO2_low: 'Нижнее pCO₂, бар',
  pCO2_mid: 'Среднее pCO₂, бар',
  pCO2_high: 'Верхнее pCO₂, бар',
};
function co2Label(key) {
  return (
    {
      V_1975_median: 'Базовая формула v4, мм/год',
      V_A1_median: 'A1 v4, мм/год',
      V_A2_median: 'A2 v4, мм/год',
      S_CO2_1975: 'Базовый балл v4',
      S_CO2_A1: 'Балл A1 v4',
      S_CO2_A2: 'Балл A2 v4',
      S_CO2_final: 'Итоговый балл v4',
      Класс_1975: 'Базовый класс v4',
      Класс_A1: 'Класс A1 v4',
      Класс_A2: 'Класс A2 v4',
      Класс_final: 'Итоговый класс v4',
    }[key] || key
  );
}
function co2Result(a) {
  const box = el(
    'div',
    { class: 'box' },
    el('h3', { text: `${a.automatic ? 'Автоматический расчёт' : a.calculation_mode === 'imported' ? 'Импорт v4' : 'Расчёт'} · ${a.model_title || a.model_version || 'NORSOK'}` }),
    el('div', {
      class: 'hint',
      text: `${a.synthetic ? 'Синтетические данные · ' : ''}Дата состояния: ${a.state_at || 'смотрите даты исходных параметров'} · ${a.automatic ? 'Рассчитано' : 'Сохранено'}: ${a.created_at || a.calculated_at || '—'}`,
    }),
  );
  if (a.scenarios?.length)
    box.append(
      el(
        'table',
        { class: 'rules' },
        el(
          'thead',
          {},
          el(
            'tr',
            {},
            ['CO₂, %', 'pCO₂, бар', ...(a.configuration_hash ? ['Фугитивность, бар'] : []), 'Скорость, мм/год'].map((t) => el('th', { text: t })),
          ),
        ),
        el(
          'tbody',
          {},
          a.scenarios.map((s) =>
            el(
              'tr',
              {},
              [s.co2_mole_fraction * 100, s.pco2_bar, ...(a.configuration_hash ? [s.fugacity_bar] : []), s.rate_mm_year].map((v) => el('td', { text: ruNum(v) })),
            ),
          ),
        ),
      ),
    );
  const imported = Object.entries(a.imported_results || {});
  if (imported.length)
    box.append(
      el(
        'dl',
        { class: 'dl' },
        imported.flatMap(([k, v]) => [
          el('dt', { text: co2Label(k) }),
          el('dd', { text: typeof v === 'number' ? ruNum(v) : v }),
        ]),
      ),
    );
  if (a.automatic) {
    const status = a.automatic_status === 'calculated' ? 'Результат обновляется автоматически из данных карточки.'
      : a.automatic_status === 'not_applicable' ? 'Данные вне области NORSOK. Численная скорость не определена.'
      : 'Проверьте администраторскую конфигурацию расчёта.';
    box.append(el('p', { class: 'hint', text: status }));
    const provenance = Object.entries(a.input_provenance || {});
    box.append(el('p', { class: 'hint', text: `Из источников: ${provenance.filter(([,v]) => v.kind === 'source').length}; подстановок администратора: ${(a.defaults_used || []).length}. Подстановки являются допущениями проекта.` }));
    const values = el('table', { class: 'rules' },
      el('thead', {}, el('tr', {}, ['Параметр', 'Использовано', 'Происхождение'].map(text => el('th', { text })))),
      el('tbody', {}, provenance.map(([key, item]) => el('tr', {},
        el('td', { text: CO2_INPUTS[key] || key }),
        el('td', { text: typeof item.value === 'number' ? ruNum(item.value) : ({carbon_steel:'углеродистая сталь',water_wet:'контакт с водой'}[item.value] || item.value) }),
        el('td', { text: item.kind === 'default' ? `Подстановка администратора. ${item.reason || ''}` : `${item.source_id || 'Источник карточки'} · ${item.date || 'дата неизвестна'} · ${item.label || ''}` })))));
    box.append(el('details', {}, el('summary', { text: 'Использованные параметры и подстановки' }), values));
  }
  const warnings = el('ul', { class: 'hint' }, (a.quality_flags || []).map(text => el('li', { text })));
  box.append(a.automatic ? el('details', { open: a.automatic_status !== 'calculated' },
    el('summary', { text: 'Ограничения и допущения' }), warnings) : warnings);
  const inputs = el(
    'details',
    {},
    el('summary', { text: 'Исходные значения и происхождение' }),
    el(
      'dl',
      { class: 'dl' },
      Object.entries(a.inputs || {})
        .filter(([key]) => CO2_INPUTS[key])
        .flatMap(([key, value]) => [
          el('dt', { text: CO2_INPUTS[key] }),
          el('dd', {
            text: Array.isArray(value)
              ? value.map(ruNum).join('; ')
              : { measured: 'измеренный', scenario: 'сценарии', carbon_steel: 'углеродистая сталь', water_wet: 'подтверждён', unknown: 'неизвестно', not_water_wet: 'не подтверждён', other: 'другой материал' }[value] || String(value ?? '—'),
          }),
        ]),
    ),
    el('p', {
      class: 'hint',
      text: `Источник: ${a.source_name || a.inputs?.source || 'не указан'}${a.source_row ? ' · строка ' + a.source_row : ''}`,
    }),
  );
  inputs.append(
    el(
      'details',
      {},
      el('summary', { text: 'Технические данные: JSON и хеш' }),
      el('pre', {
        style: 'white-space:pre-wrap;overflow-wrap:anywhere',
        text: JSON.stringify(
          { version: a.model_version, configuration_hash: a.configuration_hash, model: a.model_snapshot, source: a.source_name, hash: a.source_hash || a.input_hash, row: a.source_row, inputs: a.inputs },
          null,
          2,
        ),
      }),
    ),
  );
  box.append(inputs);
  return box;
}
function co2Form(wellId = '') {
  if (co2Forms.has(wellId)) return co2Forms.get(wellId);
  const form = el('form', { class: 'co2-form', novalidate: true });
  form.append(el('p', { class: 'hint', text: 'Обязательные поля отмечены *. Дополнительные сведения можно не заполнять.' }));
  if (wellId) form.append(el('p', { class: 'hint', text: `Скважина: ${wellId} · выбрана в карточке` }));
  const method = el('select', { 'aria-label': 'Методика CO₂' },
    el('option', { value: 'active', text: 'NORSOK M-506:2005 — основная методика' }),
    el('option', { value: 'co2-v4-base-1', text: 'Базовая формула v4 — сравнение без pH и сдвига' }));
  method.value = 'active';
  form.append(el('label', { class: 'field' }, 'Методика CO₂', method));
  const fields = el('div', { class: 'form-grid' });
  form.append(fields);
  const errors = [];
  const input = (label, props, hint, parent = fields) => {
    const errorId = `co2-error-${co2Forms.size}-${errors.length}`;
    const node = el('input', { required: true, 'aria-label': label, 'aria-describedby': `${errorId} ${errorId}-hint`, ...props });
    const error = el('span', { id: errorId, class: 'field-error', role: 'alert' });
    const help = el('span', { id: `${errorId}-hint`, class: 'hint', text: hint });
    errors.push(error);
    const text = label + (props.required === false ? ' (необязательно)' : ' *');
    parent.append(props.type === 'checkbox'
      ? el('label', { class: 'field co2-confirm' }, node, el('span', { text }), help, error)
      : el('label', { class: 'field' }, text, node, help, error));
    return { node, error, hint: help };
  };
  const well = wellId ? null : input('Скважина', { maxlength: 128 }, 'ID скважины из рейтинга. Для нового ID подтвердите создание ниже.');
  const temp = input('Температура, °C', { type: 'number', step: 'any', placeholder: 'Например, 60' }, 'Температура в точке расчёта, в градусах Цельсия.');
  const pressure = input('Абсолютное давление, бар', { type: 'number', step: 'any', placeholder: 'Например, 50' }, 'Введите абсолютное давление, а не избыточное; единица — бар.');
  const scenarios = el('input', { type: 'radio', name: `composition-${wellId}` });
  const measured = el('input', { type: 'radio', name: `composition-${wellId}`, checked: true });
  const composition = el('fieldset', { class: 'co2-composition' },
    el('legend', { text: 'Какую долю CO₂ использовать' }),
    el('div', { class: 'co2-choices' },
      el('label', { class: 'check' }, scenarios, ' Сценарии CO₂'),
      el('label', { class: 'check' }, measured, ' Измеренная доля CO₂')));
  form.append(composition);
  const fractions = input('Доля CO₂, %', { placeholder: 'Например, 1.5', inputmode: 'decimal' }, '', composition);
  const modeHint = () => { fractions.hint.textContent = measured.checked
    ? 'Одно измеренное значение от 0 до 100 %, например 1,5.'
    : 'От 1 до 10 долей от 0 до 100 % через точку с запятой. Например: 0,5; 1,5; 3.'; };
  modeHint();
  let scenarioDraft = '',
    measuredDraft = '';
  const changeMode = () => {
    if (measured.checked) {
      scenarioDraft = fractions.node.value;
      fractions.node.value = measuredDraft;
      fractions.node.placeholder = 'Одно значение, например 1.5';
    } else {
      measuredDraft = fractions.node.value;
      fractions.node.value = scenarioDraft;
      fractions.node.placeholder = 'Например 0.5;1.5;3';
    }
    fractions.error.textContent = '';
    modeHint();
  };
  measured.addEventListener('change', changeMode);
  scenarios.addEventListener('change', changeMode);
  const norsok = el('fieldset', { class: 'co2-composition' }, el('legend', { text: 'Исходные данные NORSOK' }));
  const norsokFields = el('div', { class: 'form-grid' });
  norsok.append(el('p', { class: 'hint', text: 'Нужен pH водной фазы при рабочей температуре и давлении. Лабораторный pH при комнатных условиях требует отдельного пересчёта. Касательное напряжение получают из измерений или проверенного гидравлического расчёта.' }), norsokFields);
  const ph = input('pH при рабочих условиях', { type: 'number', step: 'any', min: 3.5, max: 6.5 }, 'Обязательно: 3,5–6,5. Значение не подставляется автоматически.', norsokFields);
  const shear = input('Касательное напряжение, Па', { type: 'number', step: 'any', min: 1, max: 150 }, 'Обязательно: напряжение на стенке 1–150 Па. Это не скорость потока в м/с.', norsokFields);
  const water = input('Обводнённость, %', { type: 'number', step: 'any', min: 0, max: 100, required: false }, 'Необязательно: 0–100 %. Сохраняется как характеристика среды; линейного множителя скорости по % воды в этой методике нет.', norsokFields);
  const steel = input('Подтверждаю углеродистую сталь', { type: 'checkbox' }, 'Для других материалов эта методика неприменима.', norsok);
  const wet = input('Подтверждаю контакт воды с металлом', { type: 'checkbox' }, 'Обводнённость сама по себе не подтверждает смачивание стенки водой.', norsok);
  form.append(norsok);
  const methodHint = el('p', { class: 'hint' });
  form.append(methodHint);
  const changeMethod = () => {
    norsok.hidden = method.value !== 'active';
    methodHint.textContent = norsok.hidden ? 'Сравнительная формула v4: учитывает только температуру и pCO₂. pH, поток и защита в ней не учитываются.' : 'NORSOK: температура 5–150 °C, абсолютное давление 1–1000 бар, фугитивность CO₂ 0,1–10 бар. Скорость без поправки на ингибитор и гликоль; не является остаточным ресурсом.';
  };
  method.addEventListener('change', changeMethod); changeMethod();
  const options = el('details', { class: 'co2-options' }, el('summary', { text: 'Дополнительно — необязательно' }));
  const metadata = el('div', { class: 'form-grid' });
  options.append(metadata);
  const h2s = input('Парциальное давление H₂S, бар', { type: 'number', step: 'any', min: 0, required: false }, 'Для NORSOK: оставьте пустым, если неизвестно. Ноль означает подтверждённое отсутствие. Свыше 0,05 бар расчёт MVP заблокирован.', metadata);
  const acids = input('Органические кислоты, ppm', { type: 'number', step: 'any', min: 0, required: false }, 'Для NORSOK: неизвестное содержание оставьте пустым. Более 100 ppm при pCO₂ < 0,5 бар требует другой оценки.', metadata);
  const source = input('Источник входных значений', { required: false, placeholder: 'Ручной ввод', maxlength: 256 }, 'Можно указать пробу или документ. Если оставить пустым, сохранится «Ручной ввод».', metadata);
  const date = input('Дата состояния', { type: 'datetime-local', required: false }, 'Если дата измерений неизвестна, оставьте пустым.', metadata);
  const synthetic = el('input', { type: 'checkbox' });
  options.append(el('label', { class: 'check' }, synthetic, ' Синтетические данные'),
    el('p', { class: 'hint', text: 'Отметьте для учебных значений. Для реальных измерений оставьте выключенным.' }));
  form.append(options);
  const create = el('input', { type: 'checkbox' });
  if (!wellId) form.append(el('label', { class: 'check' }, create, ' Создать скважину, если её ещё нет'),
    el('p', { class: 'hint', text: 'Отметьте только при расчёте для нового ID. Для скважины из рейтинга это не требуется.' }));
  const button = el('button', { class: 'btn', type: 'submit', text: 'Рассчитать CO₂' });
  const output = el('div', { 'aria-live': 'polite' });
  form.append(button, output);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (button.disabled) return;
    errors.forEach((e) => {
      e.textContent = '';
    });
    output.replaceChildren();
    const fail = (field, text) => {
      field.error.textContent = text;
      return false;
    };
    let valid = true;
    if (!wellId && (!well.node.value.trim() || well.node.value.trim().length > 128))
      valid = fail(well, 'Укажите ID скважины: 1–128 символов.');
    if (!temp.node.value.trim() || !Number.isFinite(Number(temp.node.value)) || Number(temp.node.value) <= -273.15)
      valid = fail(temp, 'Температура должна быть выше −273,15 °C.');
    if (
      !pressure.node.value.trim() ||
      !Number.isFinite(Number(pressure.node.value)) ||
      Number(pressure.node.value) <= 0
    )
      valid = fail(pressure, 'Укажите абсолютное давление больше 0 бар.');
    const parts = fractions.node.value.split(';');
    const values = parts.map((v) => Number(v.trim().replace(',', '.')) / 100);
    if (
      parts.some((v) => !v.trim()) ||
      values.some((v) => !Number.isFinite(v) || v < 0 || v > 1) ||
      values.length > 10 ||
      new Set(values).size !== values.length
    )
      valid = fail(
        fractions,
        'Укажите от 1 до 10 разных долей от 0 до 100 %. Разделитель сценариев — точка с запятой.',
      );
    if (measured.checked && values.length !== 1)
      valid = fail(fractions, 'Для измеренной доли требуется одно значение, например 1.5.');
    if (date.node.value && !Number.isFinite(new Date(date.node.value).valueOf()))
      valid = fail(date, 'Проверьте дату и время состояния.');
    const isNorsok = method.value === 'active';
    const inRange = (field, low, high, optional = false) => {
      const text = field.node.value.trim();
      if (optional && !text) return;
      const value = Number(text);
      if (!text || !Number.isFinite(value) || value < low || value > high)
        valid = fail(field, `Укажите значение от ${low} до ${high}.`);
    };
    if (isNorsok) {
      inRange(water, 0, 100, true);
      for (const field of [ph, shear]) if (!field.node.value.trim() || !Number.isFinite(Number(field.node.value))) valid = fail(field, 'Укажите конечное число. Диапазон проверяется по активной методике на сервере.');
      inRange(h2s, 0, .05, true); inRange(acids, 0, Number.MAX_VALUE, true);
      if (!steel.node.checked) valid = fail(steel, 'Подтвердите материал для NORSOK.');
      if (!wet.node.checked) valid = fail(wet, 'Подтвердите контакт воды со стенкой.');
    }
    if (!valid) return;
    const submittedWell = wellId || well.node.value.trim();
    button.disabled = true;
    try {
      const result = await api('/api/v1/co2/calculate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          well_id: submittedWell, model_id: method.value,
          ...(isNorsok ? {
            ph_in_situ: Number(ph.node.value), wall_shear_pa: Number(shear.node.value),
            water_cut_pct: water.node.value.trim() ? Number(water.node.value) : null,
            material: 'carbon_steel', water_wetting: 'water_wet',
            h2s_partial_bar: h2s.node.value.trim() ? Number(h2s.node.value) : null,
            organic_acids_ppm: acids.node.value.trim() ? Number(acids.node.value) : null,
          } : {}),
          temperature_c: Number(temp.node.value),
          pressure_abs_bar: Number(pressure.node.value),
          co2_mole_fractions: values,
          composition_source: measured.checked ? 'measured' : 'scenario',
          source: source.node.value.trim() || 'Ручной ввод',
          state_at: date.node.value ? new Date(date.node.value).toISOString() : null,
          synthetic: synthetic.checked,
          create_well: create.checked,
        }),
      });
      output.replaceChildren(
        el('p', { class: 'hint', text: result.duplicates ? 'Такой расчёт уже сохранён.' : 'Расчёт сохранён.' }),
      );
      if (!wellId) output.append(co2Result(result.assessment));
      if (wellId && state.selected === wellId) {
        const history = await api(`/api/v1/co2/wells/${encodeURIComponent(wellId)}`);
        if (state.selected === wellId) renderCo2History(history.assessments, wellId);
      }
      await refresh({ keepNotice: true });
      if (!wellId)
        output.append(el('button', {
          class: 'btn secondary', type: 'button', text: `Открыть скважину ${submittedWell}`,
          onClick: () => openWell(submittedWell),
        }));
    } catch (error) {
      output.replaceChildren(el('p', { class: 'hint warn', role: 'alert', text: error.message }));
    } finally {
      button.disabled = false;
    }
  });
  co2Forms.set(wellId, form);
  return form;
}
function renderCo2History(assessments, wellId = state.selected) {
  const box = co2Cards.get(wellId)?.history;
  if (!box) return;
  box.replaceChildren(
    ...(assessments.length
      ? assessments.map(co2Result)
      : [el('div', { class: 'hint', text: 'Расчётов CO₂ пока нет.' })]),
  );
}
function updateCo2Automatic(wellId, assessment) {
  const card = co2Cards.get(wellId);
  if (!card || !assessment) return;
  const identity = JSON.stringify([assessment.automatic_status, assessment.input_hash,
    assessment.defaults_hash, assessment.configuration_hash, assessment.scenarios, assessment.quality_flags]);
  if (identity === card.automaticIdentity) return;
  card.current.replaceChildren(co2Result(assessment));
  card.automaticIdentity = identity;
}
function renderCo2(detail) {
  const box = $('wellCo2');
  if (!box) return;
  box.hidden = !detail.co2_enabled;
  if (box.hidden) return;
  let card = co2Cards.get(detail.well_id);
  if (!card) {
    const history = el('div', { class: 'co2-history' });
    const current = el('div', { class: 'co2-current', 'aria-live': 'polite' });
    const root = el(
      'div',
      {},
      el('h3', { text: 'CO₂-коррозия · мм/год' }),
      el('div', { class: 'hint', text: 'Автоматически по данным карточки. Недостающие параметры берутся из администраторских настроек. Индекс рейтинга рассчитывается отдельно.' }),
      current,
      el('details', {}, el('summary', { text: 'Сохранённые ручные и внешние расчёты' }), history),
    );
    card = { root, history, current };
    co2Cards.set(detail.well_id, card);
  }
  box.replaceChildren(card.root);
  updateCo2Automatic(detail.well_id, detail.co2_automatic);
  renderCo2History(detail.co2_assessments || [], detail.well_id);
}
function initCo2() {
  const box = $('co2Feature');
  box.append(
    el('h2', { class: 'section', id: 'co2Title', text: 'CO₂-коррозия' }),
    el('p', { class: 'sub', text: 'В карточке каждой скважины уже доступен автоматический расчёт NORSOK. Исходные данные обновляются из источников, отсутствующие параметры — из настроек администратора. Ниже доступны отдельные сравнительные расчёты и импорт.' }),
    el('details', { class: 'box' }, el('summary', { text: 'Ручной сценарий для сравнения' }), co2Form()),
  );
  const modelInfo = el('p', { class: 'hint', role: 'status', text: 'Проверяем активную методику…' });
  box.append(modelInfo);
  api('/api/v1/co2/model').then(model => {
    modelInfo.textContent = `Активная методика: ${model.title} · редакция конфигурации ${model.revision} · калибровка ×${model.rate_multiplier}.`;
    const defaults = model.automatic_defaults;
    if (defaults) modelInfo.append(el('details', {}, el('summary', { text: 'Подстановки администратора — только просмотр' }),
      el('p', { text: defaults.description }), el('dl', { class: 'dl' }, Object.entries(defaults.values).flatMap(([key,value]) => [
        el('dt', { text: CO2_INPUTS[key] || key }), el('dd', { text: typeof value === 'number' ? ruNum(value) : ({carbon_steel:'углеродистая сталь',water_wet:'допущен контакт с водой'}[value] || value) })
      ]))));
  }).catch(error => { modelInfo.textContent = error.message; });
  box.append(co2Upload(true), el('details', { class: 'box' }, el('summary', { text: 'Архивные внешние расчёты v4' }), co2Upload(false)));
}
function co2Upload(raw) {
  const box = el('div', { class: 'box', id: raw ? 'co2RawUpload' : 'co2V4Upload' });
  const picker = el('input', { type: 'file', accept: raw ? '.json,.xlsx,.csv' : '.json,.xlsx', 'aria-label': raw ? 'Исходные данные NORSOK' : 'Файл CO₂ v4', hidden: true });
  const filename = el('span', { id: raw ? 'co2RawFilename' : 'co2Filename', class: 'hint', role: 'status', 'aria-live': 'polite', text: 'Файл не выбран' });
  const pick = el('button', { class: 'btn secondary', type: 'button', text: raw ? 'Выбрать исходные данные' : 'Выбрать файл', onClick: () => picker.click() });
  const synthetic = el('input', { type: 'checkbox', checked: !raw }),
    create = el('input', { type: 'checkbox' });
  const preview = el('button', { class: 'btn secondary', type: 'button', text: raw ? 'Проверить и рассчитать файл' : 'Проверить файл v4' });
  const confirm = el('button', { class: 'btn', type: 'button', text: raw ? 'Сохранить проверенные расчёты' : 'Импортировать проверенный файл', hidden: true });
  const output = el('div', { 'aria-live': 'polite' });
  let checkedFile = null, modelHash = '', generation = 0;
  const invalidate = () => {
    checkedFile = null; generation++;
    confirm.hidden = true;
  };
  picker.addEventListener('change', () => {
    filename.textContent = picker.files[0]?.name || 'Файл не выбран';
    invalidate();
    output.replaceChildren();
  });
  synthetic.addEventListener('change', invalidate);
  async function upload(commit) {
    const file = picker.files[0];
    if (!file) {
      output.textContent = raw ? 'Выберите файл исходных данных.' : 'Выберите файл v4.';
      return;
    }
    if (commit && checkedFile !== file) return;
    const requestedGeneration = generation;
    preview.disabled = confirm.disabled = picker.disabled = synthetic.disabled = create.disabled = true;
    try {
      const body = new FormData();
      body.append('file', file);
      body.append('synthetic', synthetic.checked);
      body.append('confirm', commit);
      body.append('create_wells', create.checked);
      if (raw) body.append('configuration_hash', modelHash);
      const report = await api(raw ? '/api/v1/co2/calculate-file' : '/api/v1/co2/import', { method: 'POST', body });
      if (requestedGeneration !== generation) return;
      modelHash = report.configuration_hash || '';
      output.replaceChildren(
        el('p', {
          text: `Строк: ${report.rows_received}; корректных: ${report.valid_rows}; ошибок: ${report.errors.length}; новых скважин: ${report.unknown_wells.length}.`,
        }),
        el('p', { class: 'hint', text: report.verification }),
        el(
          'ul',
          { class: 'hint warn' },
          report.errors.slice(0, 20).map((e) => el('li', { text: `Строка ${e.row}: ${e.message}` })),
        ),
      );
      if (raw && report.preview?.length) output.append(el('p', { class: 'hint', text: 'Предварительные скорости (мм/год): ' + report.preview.map(a => `${a.well_id}: ${a.scenarios.map(s => ruNum(s.rate_mm_year)).join('; ')}`).join(' · ') }));
      if (report.warning_count) {
        const unique = [...new Set(report.warnings.map((w) => w.message))];
        output.append(
          el(
            'details',
            { open: true },
            el('summary', {
              text: `Предупреждений: ${report.warning_count} (показаны типы из первых ${report.warnings.length})`,
            }),
            el(
              'ul',
              { class: 'hint warn' },
              unique.map((message) => el('li', { text: message })),
            ),
          ),
        );
      }
      if (report.unknown_wells.length)
        output.append(
          el('p', {
            class: 'hint',
            text: `Новые скважины: ${report.unknown_wells.slice(0, 20).join(', ')}${report.unknown_wells.length > 20 ? '…' : ''}. Для записи подтвердите создание.`,
          }),
        );
      if (commit) {
        invalidate();
        output.append(el('p', { text: `Сохранено: ${report.stored}; повторов: ${report.duplicates}.` }));
        await refresh({ keepNotice: true });
      } else {
        checkedFile = file;
        confirm.hidden = !!report.errors.length;
      }
    } catch (error) {
      invalidate();
      output.textContent = error.message;
    } finally {
      preview.disabled = confirm.disabled = picker.disabled = synthetic.disabled = create.disabled = false;
    }
  }
  preview.addEventListener('click', () => upload(false));
  confirm.addEventListener('click', () => upload(true));
  box.append(
    el(
      'div',
      {},
      el('h3', { text: raw ? 'Рассчитать по файлу исходных данных' : 'Импорт CO₂ v4' }),
      el('p', { class: 'hint', text: raw ? 'Загрузите один лист XLSX, CSV в UTF-8 или JSON по шаблону. Нужны температура, абсолютное давление, измеренная доля CO₂, pH при рабочих условиях, касательное напряжение, материал и подтверждение контакта с водой. Пропуски не заполняются автоматически. При ошибке любой строки файл целиком не записывается.' : 'Внешние результаты v4 сохраняются отдельно и не пересчитываются по NORSOK. Сначала проверьте JSON или XLSX, затем подтвердите запись.' }),
      el('div', { class: 'co2-file-picker' }, picker, pick, filename),
      el('label', { class: 'check' }, synthetic, ' Синтетические данные'),
      el('p', { class: 'hint', text: raw ? 'Отметьте только для учебного набора. Шаблон содержит только заголовки; заполните строки своими измерениями.' : 'Отмечено для учебных файлов. Снимите отметку, если файл содержит реальные данные.' }),
      el('label', { class: 'check' }, create, raw ? ' Создать отсутствующие скважины из исходных данных' : ' Создать отсутствующие скважины'),
      el('p', { class: 'hint', text: 'Нужно только для новых ID. Их список появится после проверки файла.' }),
      preview,
      confirm,
      output,
    ),
  );
  if (raw) box.prepend(el('button', { class: 'btn secondary', type: 'button', text: 'Скачать шаблон для Excel (CSV)', onClick: async () => {
    try {
      const response = await fetch('/api/v1/co2/input-template', { headers: headers() });
      if (!response.ok) throw new Error('Не удалось скачать шаблон. Проверьте доступ к API.');
      const url = URL.createObjectURL(await response.blob());
      const link = el('a', { href: url, download: 'norsok-inputs.csv' });
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { output.textContent = error.message; }
  }}));
  return box;
}
