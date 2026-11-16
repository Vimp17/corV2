// One persistent draft and pending request per well; redraws keep the actual form nodes.
const co2Forms = new Map();
const co2Cards = new Map();
const CO2_INPUTS = {
  temperature_c: 'Температура, °C',
  pressure_abs_bar: 'Абсолютное давление, бар',
  co2_mole_fractions: 'Доли CO₂ (молярные)',
  composition_source: 'Происхождение состава',
  source: 'Источник значений',
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
    el('h3', { text: `${a.calculation_mode === 'imported' ? 'Импорт v4' : 'Расчёт'} · ${a.model_version}` }),
    el('div', {
      class: 'hint',
      text: `${a.synthetic ? 'Синтетические данные · ' : ''}Дата состояния: ${a.state_at || 'неизвестна'} · Сохранено: ${a.created_at || a.calculated_at || '—'}`,
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
            ['CO₂, %', 'pCO₂, бар', 'Скорость, мм/год'].map((t) => el('th', { text: t })),
          ),
        ),
        el(
          'tbody',
          {},
          a.scenarios.map((s) =>
            el(
              'tr',
              {},
              [s.co2_mole_fraction * 100, s.pco2_bar, s.rate_mm_year].map((v) => el('td', { text: ruNum(v) })),
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
  box.append(
    el(
      'ul',
      { class: 'hint' },
      (a.quality_flags || []).map((t) => el('li', { text: t })),
    ),
  );
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
              : { measured: 'измеренный', scenario: 'сценарии' }[value] || String(value ?? '—'),
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
          { source: a.source_name, hash: a.source_hash || a.input_hash, row: a.source_row, inputs: a.inputs },
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
  const fields = el('div', { class: 'form-grid' });
  form.append(fields);
  const errors = [];
  const input = (label, props, hint, parent = fields) => {
    const errorId = `co2-error-${co2Forms.size}-${errors.length}`;
    const node = el('input', { required: true, 'aria-label': label, 'aria-describedby': `${errorId} ${errorId}-hint`, ...props });
    const error = el('span', { id: errorId, class: 'field-error', role: 'alert' });
    const help = el('span', { id: `${errorId}-hint`, class: 'hint', text: hint });
    errors.push(error);
    parent.append(el('label', { class: 'field' }, label + (props.required === false ? ' (необязательно)' : ' *'), node,
      help, error));
    return { node, error, hint: help };
  };
  const well = wellId ? null : input('Скважина', { maxlength: 128 }, 'ID скважины из рейтинга. Для нового ID подтвердите создание ниже.');
  const temp = input('Температура, °C', { type: 'number', step: 'any', placeholder: 'Например, 60' }, 'Температура в точке расчёта, в градусах Цельсия.');
  const pressure = input('Абсолютное давление, бар', { type: 'number', step: 'any', placeholder: 'Например, 50' }, 'Введите абсолютное давление, а не избыточное; единица — бар.');
  const scenarios = el('input', { type: 'radio', name: `composition-${wellId}`, checked: true });
  const measured = el('input', { type: 'radio', name: `composition-${wellId}` });
  const composition = el('fieldset', { class: 'co2-composition' },
    el('legend', { text: 'Какую долю CO₂ использовать' }),
    el('div', { class: 'co2-choices' },
      el('label', { class: 'check' }, scenarios, ' Сценарии CO₂'),
      el('label', { class: 'check' }, measured, ' Измеренная доля CO₂')));
  form.append(composition);
  const fractions = input('Доля CO₂, %', { value: '0.5;1.5;3', inputmode: 'decimal' }, '', composition);
  const modeHint = () => { fractions.hint.textContent = measured.checked
    ? 'Одно измеренное значение от 0 до 100 %, например 1,5.'
    : 'От 1 до 10 долей от 0 до 100 % через точку с запятой. Например: 0,5; 1,5; 3. Начальные значения — учебные сценарии.'; };
  modeHint();
  let scenarioDraft = fractions.node.value,
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
  const options = el('details', { class: 'co2-options' }, el('summary', { text: 'Дополнительно — необязательно' }));
  const metadata = el('div', { class: 'form-grid' });
  options.append(metadata);
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
    if (!valid) return;
    const submittedWell = wellId || well.node.value.trim();
    button.disabled = true;
    try {
      const result = await api('/api/v1/co2/calculate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          well_id: submittedWell,
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
function renderCo2(detail) {
  const box = $('wellCo2');
  if (!box) return;
  box.hidden = !detail.co2_enabled;
  if (box.hidden) return;
  let card = co2Cards.get(detail.well_id);
  if (!card) {
    const history = el('div', { class: 'co2-history' });
    const root = el(
      'div',
      {},
      el('h3', { text: 'CO₂-коррозия · мм/год' }),
      el('div', { class: 'hint', text: 'Дополнительный сценарный расчёт. Индекс и мероприятие рейтинга сохраняются.' }),
      el('details', { class: 'box' }, el('summary', { text: 'Новый расчёт' }), co2Form(detail.well_id)),
      history,
    );
    card = { root, history };
    co2Cards.set(detail.well_id, card);
  }
  box.replaceChildren(card.root);
  renderCo2History(detail.co2_assessments || [], detail.well_id);
}
function initCo2() {
  const box = $('co2Feature');
  box.append(
    el('h2', { class: 'section', id: 'co2Title', text: 'CO₂-коррозия' }),
    el('p', { class: 'sub', text: 'Расчёт скорости в мм/год и импорт результатов v4. История — в карточке скважины.' }),
    el('details', { class: 'box' }, el('summary', { text: 'Новый расчёт для скважины' }), co2Form()),
  );
  const picker = el('input', { type: 'file', accept: '.json,.xlsx', 'aria-label': 'Файл CO₂ v4', hidden: true });
  const filename = el('span', { id: 'co2Filename', class: 'hint', role: 'status', 'aria-live': 'polite', text: 'Файл не выбран' });
  const pick = el('button', { class: 'btn secondary', type: 'button', text: 'Выбрать файл', onClick: () => picker.click() });
  const synthetic = el('input', { type: 'checkbox', checked: true }),
    create = el('input', { type: 'checkbox' });
  const preview = el('button', { class: 'btn secondary', type: 'button', text: 'Проверить файл v4' });
  const confirm = el('button', { class: 'btn', type: 'button', text: 'Импортировать проверенный файл', hidden: true });
  const output = el('div', { 'aria-live': 'polite' });
  let checkedFile = null;
  const invalidate = () => {
    checkedFile = null;
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
      output.textContent = 'Выберите файл v4.';
      return;
    }
    if (commit && checkedFile !== file) return;
    preview.disabled = confirm.disabled = true;
    try {
      const body = new FormData();
      body.append('file', file);
      body.append('synthetic', synthetic.checked);
      body.append('confirm', commit);
      body.append('create_wells', create.checked);
      const report = await api('/api/v1/co2/import', { method: 'POST', body });
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
      preview.disabled = confirm.disabled = false;
    }
  }
  preview.addEventListener('click', () => upload(false));
  confirm.addEventListener('click', () => upload(true));
  box.append(
    el(
      'div',
      { class: 'box' },
      el('h3', { text: 'Импорт CO₂ v4' }),
      el('p', { class: 'hint', text: 'Для импорта нужен один JSON- или XLSX-файл v4. Сначала проверьте его, затем подтвердите запись. Для ручного расчёта выше файл не нужен.' }),
      el('div', { class: 'co2-file-picker' }, picker, pick, filename),
      el('label', { class: 'check' }, synthetic, ' Синтетические данные'),
      el('p', { class: 'hint', text: 'Отмечено для учебных файлов. Снимите отметку, если файл содержит реальные данные.' }),
      el('label', { class: 'check' }, create, ' Создать отсутствующие скважины'),
      el('p', { class: 'hint', text: 'Нужно только для новых ID. Их список появится после проверки файла.' }),
      preview,
      confirm,
      output,
    ),
  );
}
