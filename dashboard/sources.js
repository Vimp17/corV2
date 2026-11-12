// Connection cards keep their inputs and diagnostic output while statuses are refreshed.
const sourceCards = new Map();
let sourcesRequest = 0;
async function loadSources() {
  const request = ++sourcesRequest;
  try {
    const data = await api('/api/v1/sources');
    if (request !== sourcesRequest) return;
    const cards = data.sources.map((source) => {
      let card = sourceCards.get(source.profile_id);
      if (!card) {
        const status = el('div', { class: 'hint' });
        const well = el('input', {
          maxlength: 128,
          value: state.selected || state.rating?.wells[0]?.well_id || '',
          placeholder: 'ID скважины',
        });
        const output = el('div', { class: 'hint', 'aria-live': 'polite' });
        const button = el('button', { class: 'btn secondary small', type: 'button', text: 'Проверить без импорта' });
        const root = el(
          'div',
          { class: 'box' },
          el('h3', { text: source.title }),
          status,
          el('label', { style: 'display:block;margin-top:12px' }, 'Проверить скважину ', well),
          button,
          output,
        );
        button.addEventListener('click', async () => {
          if (!well.value.trim()) {
            output.textContent = 'Укажите ID скважины.';
            well.focus();
            return;
          }
          button.disabled = true;
          output.textContent = 'Проверяем ответ источника; данные в БД не записываются…';
          try {
            const report = await api(`/api/v1/sources/${encodeURIComponent(source.profile_id)}/check`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ well_id: well.value.trim() }),
            });
            output.className = `hint ${report.status === 'ok' ? 'ok' : 'warn'}`;
            output.replaceChildren(el('p', { text: report.message }));
            if (report.record_count)
              output.append(
                el('p', {
                  text: `Записей: ${report.record_count}. ${report.sheet_name ? 'Лист: ' + report.sheet_name : 'Формат JSON'}.`,
                }),
              );
            if (Object.keys(report.source_columns || {}).length)
              output.append(
                el(
                  'details',
                  {},
                  el('summary', { text: 'Найденные колонки' }),
                  el(
                    'ul',
                    {},
                    Object.entries(report.source_columns).map(([field, name]) =>
                      el('li', { text: `${name} → ${field}` }),
                    ),
                  ),
                ),
              );
          } catch (error) {
            output.className = 'hint warn';
            output.textContent = error.message;
          } finally {
            button.disabled = false;
          }
        });
        card = { root, status, well, button, output };
        sourceCards.set(source.profile_id, card);
      }
      card.button.hidden = !['api', 'adapter'].includes(source.mode);
      card.well.parentElement.hidden = card.button.hidden;
      const last = source.last_attempt;
      const names = {
        ok: 'успешно',
        partial: 'частично',
        warning: 'с предупреждениями',
        error: 'ошибка',
        unavailable: 'данных нет',
        not_configured: 'не настроен',
      };
      const modes = {
        upload: 'Загрузка файлами / push API',
        api: 'URL задан · подключение требует проверки',
        adapter: 'Адаптер установлен · подключение требует проверки',
        adapter_missing: 'Ошибка: адаптер не установлен',
      };
      card.status.replaceChildren(
        ...[
          el('p', { class: source.mode === 'adapter_missing' ? 'warn' : '', text: modes[source.mode] }),
          el('p', {
            text: last
              ? `Последняя попытка: ${fmtTime(last.retrieved_at)} · ${last.well_id} · ${names[last.status] || last.status} · записей ${last.record_count ?? '—'}`
              : 'Загрузок ещё не было.',
          }),
          last
            ? el('p', {
                text: `${{ manual_upload: 'Файл', document: 'Документ', api_refresh: 'Запрос API', api_push: 'Push API', adapter: 'Адаптер' }[last.origin] || last.origin}${last.message ? ': ' + last.message : ''}`,
              })
            : null,
          el('p', {
            text:
              source.kind === 'json_schedule'
                ? 'Ожидается JSON расписания бригад.'
                : 'Ожидается XLSX с заголовками профиля источника.',
          }),
        ].filter(Boolean),
      );
      return card.root;
    });
    $('sourceCards').replaceChildren(...cards);
    $('buildVersion').textContent =
      `Версия ${data.version}${data.build.git_commit ? ' · сборка ' + data.build.git_commit.slice(0, 12) : ' · запуск из исходников'}${data.build.built_at ? ' · ' + fmtTime(data.build.built_at) : ''}. В начальном списке скважин: ${data.configured_well_count}. Последняя попытка одного источника не подтверждает полноту данных по всему фонду.`;
  } catch (error) {
    if (request !== sourcesRequest) return;
    if (!sourceCards.size)
      $('sourceCards').replaceChildren(el('p', { class: 'hint warn', text: `${error.message}${keyHint(error)}` }));
    else $('buildVersion').textContent = `Состояние источников не обновлено: ${error.message} Показаны предыдущие сведения.`;
  }
}
