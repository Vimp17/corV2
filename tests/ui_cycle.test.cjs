const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function setup(result) {
  const notices = [],
    button = { disabled: false };
  const c = {
    state: { rating: { settings: {} }, busy: false },
    $: () => button,
    notice: (s, t) => notices.push([s, t]),
    refresh: async () => {},
    setTimeout: (f) => f(),
    Date,
    api: async () => ({ job_id: 'QA-1' }),
  };
  const html = fs.readFileSync('dashboard/index.html', 'utf8');
  vm.createContext(c);
  vm.runInContext(
    html.slice(
      html.indexOf('    async function waitForJob'),
      html.indexOf('    // ---------------------------------------------------------------- documents'),
    ),
    c,
  );
  if (result) c.waitForJob = async () => result;
  return { c, notices };
}
test('empty daily cycle reports no computation', async () => {
  const { c, notices } = setup({
    status: 'needs_configuration',
    message: 'Список скважин пуст.',
    source_failures: [],
    alerts: [],
  });
  await c.runNow();
  const [message, type] = notices.at(-1);
  assert.doesNotMatch(message, /Рейтинг пересчитан/);
  assert.equal(type, 'warning');
});
test('source or computation failure is visible after a cycle', async () => {
  const { c, notices } = setup({
    status: 'completed',
    message: 'Обработано 1.',
    source_failures: [{ source_id: 'DARS', well_id: 'A', message: 'HTTP 401' }],
    alerts: [],
  });
  await c.runNow();
  const [message, type] = notices.at(-1);
  assert.equal(type, 'warning');
  assert.match(message, /401/);
  assert.match(message, /DARS/);
});
test('job wait is bounded and retains its identifier', async () => {
  const { c } = setup();
  let time = 0,
    calls = 0;
  c.Date = { now: () => (time += 200000) };
  c.api = async () => (++calls === 1 ? { status: 'running' } : { status: 'completed', result: {} });
  await assert.rejects(c.waitForJob('QA-1'), /QA-1/);
  assert.ok(calls <= 1);
});
test('a removed job does not prevent starting the next cycle', async () => {
  const { c } = setup(); c.state.monitorJob = 'MISSING';
  c.api = async () => { throw Object.assign(new Error('Нет задачи'), { status: 404 }); };
  await assert.rejects(c.waitForJob('MISSING'), /MISSING/);
  assert.equal(c.state.monitorJob, null);
});
