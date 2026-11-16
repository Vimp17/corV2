// Compact automatic CO2 point calculation in the well card.
const co2Cards = new Map();
const CO2_INPUTS = {
  temperature_c: 'Температура, °C', pressure_abs_bar: 'Абсолютное давление, бар',
  co2_pct: 'CO₂, % мол.', ph_in_situ: 'pH при рабочих условиях',
  wall_shear_pa: 'Касательное напряжение, Па', water_cut_pct: 'Обводнённость, %',
  material: 'Материал', water_wetting: 'Контакт воды с металлом',
  h2s_partial_bar: 'Парциальное давление H₂S, бар', organic_acids_ppm: 'Органические кислоты, ppm',
};
function co2Value(value) {
  if (typeof value === 'number') return ruNum(value);
  return {carbon_steel:'углеродистая сталь',water_wet:'есть',not_water_wet:'нет',
    unknown:'неизвестно',other:'другой материал'}[value] || String(value ?? '—');
}
function updateCo2Automatic(wellId, assessment) {
  const card = co2Cards.get(wellId);
  if (!card || !assessment) return;
  const identity = JSON.stringify([assessment.automatic_status, assessment.input_provenance, assessment.scenarios]);
  if (identity === card.identity) return;
  const scenarios = assessment.scenarios || [];
  card.summary.textContent = 'CO₂-коррозия: ' + (scenarios.length
    ? scenarios.map(s => `${ruNum(s.rate_mm_year)} мм/год`).join('; ')
    : 'расчёт недоступен');
  const inputs = Object.entries(assessment.input_provenance || {});
  const values = el('table', { class:'rules' },
    el('thead', {}, el('tr', {}, ['Параметр','Использовано','Источник'].map(text => el('th',{text})))),
    el('tbody', {}, inputs.map(([key,item]) => el('tr', {},
      el('td',{text:CO2_INPUTS[key] || key}), el('td',{text:co2Value(item.value)}),
      el('td',{text:item.kind === 'default' ? 'Настройки' : item.source_id || 'Карточка скважины'})))));
  const result = el('table', { class:'rules' },
    el('thead', {}, el('tr', {}, ['CO₂, %','pCO₂, бар','Скорость, мм/год'].map(text => el('th',{text})))),
    el('tbody', {}, scenarios.map(s => el('tr', {},
      [s.co2_mole_fraction*100,s.pco2_bar,s.rate_mm_year].map(value => el('td',{text:ruNum(value)}))))));
  card.content.replaceChildren(values, result);
  card.identity = identity;
}
function renderCo2(detail) {
  const box = $('wellCo2');
  if (!box) return;
  box.hidden = !detail.co2_enabled;
  if (box.hidden) return;
  let card = co2Cards.get(detail.well_id);
  if (!card) {
    const summary = el('summary',{text:'CO₂-коррозия: расчёт недоступен'});
    const content = el('div',{class:'co2-current'});
    const root = el('details',{class:'rules-box'},summary,content);
    card = {root,summary,content};
    co2Cards.set(detail.well_id,card);
  }
  box.replaceChildren(card.root);
  updateCo2Automatic(detail.well_id,detail.co2_automatic);
}
