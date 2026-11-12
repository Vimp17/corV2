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
  const form = el('form', { class: 'box co2-form', novalidate: true });
  const errors = [];
  const input = (label, props) => {
    const errorId = `co2-error-${co2Forms.size}-${errors.length}`;
    const node = el('input', { required: true, 'aria-label': label, 'aria-describedby': errorId, ...props });
    const error = el('span', { id: errorId, class: 'field-error', role: 'alert' });
    errors.push(error);
    form.append(el('label', { style: 'display:block;margin:10px 0' }, label + ' ', node, error));
    return { node, error };
  };
  const well = input('Скважина', { value: wellId, maxlength: 128, readonly: !!wellId });
  const temp = input('Температура, °C', { type: 'number', step: 'any' });
  const pressure = input('Абсолютное давление, бар', { type: 'number', step: 'any' });
  const scenarios = el('input', { type: 'radio', name: `composition-${wellId}`, checked: true });
  const measured = el('input', { type: 'radio', name: `composition-${wellId}` });
  form.append(
    el(
      'div',
      {},
      el('label', {}, scenarios, ' Сценарии CO₂'),
      el('label', { style: 'margin-left:16px' }, measured, ' Измеренная доля CO₂'),
    ),
  );
  const fractions = input('Доля CO₂, % (сценарии через ;)', { value: '0.5;1.5;3', inputmode: 'decimal' });
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
  };
  measured.addEventListener('change', changeMode);
  scenarios.addEventListener('change', changeMode);
  const source = input('Источник входных значений', { value: 'Ручной ввод', maxlength: 256 });
  const date = input('Дата состояния (если известна)', { type: 'datetime-local', required: false });
  const synthetic = el('input', { type: 'checkbox' });
  form.append(el('label', {}, synthetic, ' Синтетические данные'));
  const create = el('input', { type: 'checkbox' });
  if (!wellId) form.append(el('label', { style: 'display:block' }, create, ' Создать скважину, если её ещё нет'));
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
    if (!well.node.value.trim() || well.node.value.trim().length > 128)
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
    if (!source.node.value.trim()) valid = fail(source, 'Укажите источник входных значений.');
    if (date.node.value && !Number.isFinite(new Date(date.node.value).valueOf()))
      valid = fail(date, 'Проверьте дату и время состояния.');
    if (!valid) return;
    const submittedWell = well.node.value.trim();
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
          source: source.node.value.trim(),
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
      el('details', {}, el('summary', { text: 'Новый расчёт' }), co2Form(detail.well_id)),
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
    el('h2', { text: 'CO₂-коррозия' }),
    el('p', { class: 'sub', text: 'Расчёт скорости в мм/год и импорт результатов v4. История — в карточке скважины.' }),
    el('details', {}, el('summary', { text: 'Новый расчёт для скважины' }), co2Form()),
  );
  const picker = el('input', { type: 'file', accept: '.json,.xlsx', 'aria-label': 'Файл CO₂ v4' });
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
  picker.addEventListener('change', invalidate);
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
      picker,
      el('label', { style: 'display:block' }, synthetic, ' Синтетические данные'),
      el('label', { style: 'display:block' }, create, ' Создать отсутствующие скважины'),
      preview,
      confirm,
      output,
    ),
  );
}
