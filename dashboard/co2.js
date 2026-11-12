const co2Forms = new Map();
const co2Cards = new Map();
    // CO₂ scenario calculation and strict v4 import.
    function co2Label(key) {
      const labels = { V_1975_median: 'Базовая формула v4, мм/год', V_A1_median: 'A1 v4, мм/год', V_A2_median: 'A2 v4, мм/год',
        S_CO2_1975: 'Базовый балл v4', S_CO2_A1: 'Балл A1 v4', S_CO2_A2: 'Балл A2 v4', S_CO2_final: 'Итоговый балл v4',
        'Класс_1975': 'Базовый класс v4', 'Класс_A1': 'Класс A1 v4', 'Класс_A2': 'Класс A2 v4', 'Класс_final': 'Итоговый класс v4' };
      return labels[key] || key;
    }
    function co2Result(a) {
      const box = el('div', { class: 'box' },
        el('h3', { text: `${a.calculation_mode === 'imported' ? 'Импорт v4' : 'Расчёт'} · ${a.model_version}` }),
        el('div', { class: 'hint', text: `${a.synthetic ? 'Синтетические данные · ' : ''}Дата состояния: ${a.state_at || 'неизвестна'} · Сохранено: ${a.created_at || a.calculated_at || '—'}` }));
      if (a.scenarios?.length) box.append(el('table', { class: 'rules' },
        el('thead', {}, el('tr', {}, ['CO₂, %', 'pCO₂, бар', 'Скорость, мм/год'].map(t => el('th', { text: t })))),
        el('tbody', {}, a.scenarios.map(s => el('tr', {}, [s.co2_mole_fraction * 100, s.pco2_bar, s.rate_mm_year].map(v => el('td', { text: ruNum(v) })))))));
      const imported = Object.entries(a.imported_results || {});
      if (imported.length) box.append(el('dl', { class: 'dl' }, imported.flatMap(([k,v]) => [el('dt', { text: co2Label(k) }), el('dd', { text: typeof v === 'number' ? ruNum(v) : v })])));
      box.append(el('ul', { class: 'hint' }, (a.quality_flags || []).map(t => el('li', { text: t }))));
      const inputs = el('details', {}, el('summary', { text: 'Исходные значения и происхождение' }),
        el('pre', { style: 'white-space:pre-wrap;overflow-wrap:anywhere', text: JSON.stringify({ source: a.source_name, hash: a.source_hash || a.input_hash, row: a.source_row, inputs: a.inputs }, null, 2) }));
      box.append(inputs);
      return box;
    }
    function co2Form(wellId = '') {
      if (co2Forms.has(wellId)) return co2Forms.get(wellId);
      const form = el('form', { class: 'box co2-form' });
      const input = (label, props) => {
        const node = el('input', { required: true, ...props });
        form.append(el('label', { style: 'display:block;margin:8px 0' }, label + ' ', node)); return node;
      };
      const well = input('Скважина', { value: wellId, maxlength: 128, readonly: !!wellId });
      const temp = input('Температура, °C', { type: 'number', step: 'any' });
      const pressure = input('Абсолютное давление, бар', { type: 'number', min: '0.000001', step: 'any' });
      const fractions = input('Сценарии CO₂, % (через ;)', { value: '0.5;1.5;3' });
      const source = input('Источник входных значений', { value: 'Ручной ввод' });
      const date = input('Дата состояния (если известна)', { type: 'datetime-local', required: false });
      const synthetic = el('input', { type: 'checkbox' });
      form.append(el('label', {}, synthetic, ' Синтетические данные'));
      const measured = el('input', { type: 'checkbox' });
      form.append(el('label', { style: 'display:block' }, measured, ' Измеренная доля CO₂ (одно значение)'));
      const create = el('input', { type: 'checkbox' });
      if (!wellId) form.append(el('label', { style: 'display:block' }, create, ' Создать скважину, если её ещё нет'));
      const button = el('button', { class: 'btn', type: 'submit', text: 'Рассчитать CO₂' });
      const output = el('div', { 'aria-live': 'polite' });
      form.append(button, output);
      form.addEventListener('submit', async event => {
        event.preventDefault(); button.disabled = true;
        try {
          if (fractions.value.split(';').some(v => !v.trim())) throw new Error('Укажите доли CO₂ без пустых значений.');
          const values = fractions.value.split(';').map(v => Number(v.trim().replace(',', '.')) / 100);
          const result = await api('/api/v1/co2/calculate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
            well_id: well.value.trim(), temperature_c: Number(temp.value), pressure_abs_bar: Number(pressure.value),
            co2_mole_fractions: values, composition_source: measured.checked ? 'measured' : 'scenario',
            source: source.value, state_at: date.value ? new Date(date.value).toISOString() : null,
            synthetic: synthetic.checked, create_well: create.checked }) });
          output.replaceChildren(co2Result(result.assessment), el('div', { class: 'hint', text: result.duplicates ? 'Такой расчёт уже сохранён.' : 'Расчёт сохранён.' }));
          if (wellId && state.selected === wellId) { const history = await api(`/api/v1/co2/wells/${encodeURIComponent(wellId)}`); if (state.selected === wellId) renderCo2History(history.assessments, wellId); }
          await refresh({ keepNotice: true });
        } catch (error) { output.replaceChildren(el('div', { class: 'hint', text: error.message })); }
        finally { button.disabled = false; }
      });
      co2Forms.set(wellId, form);
      return form;
    }
    function renderCo2History(assessments, wellId = state.selected) {
      const box = co2Cards.get(wellId)?.history; if (!box) return;
      box.replaceChildren(...(assessments.length ? assessments.map(co2Result) : [el('div', { class: 'hint', text: 'Расчётов CO₂ пока нет.' })]));
    }
    function renderCo2(detail) {
      const box = $('wellCo2'); if (!box) return;
      box.hidden = !detail.co2_enabled;
      if (box.hidden) return;
      let card = co2Cards.get(detail.well_id);
      if (!card) {
        const history = el('div', { class: 'co2-history' });
        const root = el('div', {}, el('h3', { text: 'CO₂-коррозия · мм/год' }),
          el('div', { class: 'hint', text: 'Дополнительный сценарный расчёт. Индекс и мероприятие рейтинга сохраняются.' }),
          el('details', {}, el('summary', { text: 'Новый расчёт' }), co2Form(detail.well_id)), history);
        card = {root, history}; co2Cards.set(detail.well_id, card);
      }
      box.replaceChildren(card.root);
      renderCo2History(detail.co2_assessments || [], detail.well_id);
    }
    function initCo2() {
      const box = $('co2Feature');
      box.append(el('h2', { text: 'CO₂-коррозия' }), el('p', { class: 'sub', text: 'Расчёт сценарной скорости в мм/год и импорт результатов v4. Полная история — в карточке скважины.' }),
        el('details', {}, el('summary', { text: 'Новый расчёт для скважины' }), co2Form()));
      const picker = el('input', { type: 'file', accept: '.json,.xlsx' });
      const synthetic = el('input', { type: 'checkbox', checked: true });
      const create = el('input', { type: 'checkbox' });
      const preview = el('button', { class: 'btn secondary', type: 'button', text: 'Проверить файл v4' });
      const confirm = el('button', { class: 'btn', type: 'button', text: 'Импортировать проверенный файл', hidden: true });
      const output = el('div', { 'aria-live': 'polite' });
      let checkedFile = null;
      const invalidate = () => { checkedFile = null; confirm.hidden = true; };
      picker.addEventListener('change', invalidate); synthetic.addEventListener('change', invalidate);
      async function upload(commit) {
        const file = picker.files[0]; if (!file) { output.textContent = 'Выберите файл v4.'; return; }
        if (commit && checkedFile !== file) return;
        preview.disabled = confirm.disabled = true;
        try {
          const body = new FormData(); body.append('file', file); body.append('synthetic', synthetic.checked);
          body.append('confirm', commit); body.append('create_wells', create.checked);
          const report = await api('/api/v1/co2/import', { method: 'POST', body });
          output.replaceChildren(el('p', { text: `Строк: ${report.rows_received}; корректных: ${report.valid_rows}; ошибок: ${report.errors.length}; новых скважин: ${report.unknown_wells.length}.` }),
            el('pre', { style: 'white-space:pre-wrap', text: report.errors.slice(0,20).map(e => `Строка ${e.row}: ${e.message}`).join('\n') }));
          if (commit) { invalidate(); output.append(el('p', { text: `Сохранено: ${report.stored}; повторов: ${report.duplicates}.` })); await refresh({ keepNotice: true }); }
          else { checkedFile = file; confirm.hidden = !!report.errors.length; }
        } catch (error) { invalidate(); output.textContent = error.message; }
        finally { preview.disabled = confirm.disabled = false; }
      }
      preview.addEventListener('click', () => upload(false)); confirm.addEventListener('click', () => upload(true));
      box.append(el('div', { class: 'box' }, el('h3', { text: 'Импорт CO₂ v4 (отдельно от документов)' }), picker,
        el('label', { style: 'display:block' }, synthetic, ' Синтетические данные'),
        el('label', { style: 'display:block' }, create, ' Создать отсутствующие скважины'), preview, confirm, output));
    }


