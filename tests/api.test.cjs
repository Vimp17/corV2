const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function client(fetch) {
  const context = { fetch, AbortController, setTimeout: () => 1, clearTimeout: () => { context.cleared++; }, cleared: 0,
    $: () => ({ value: '' }) };
  vm.createContext(context);
  const html = fs.readFileSync('dashboard/index.html', 'utf8');
  vm.runInContext(html.slice(html.indexOf('    const headers ='), html.indexOf('    // A refused key:')), context);
  return context;
}
test('API timeout includes reading the body', async () => {
  let finish;
  const c = client(async () => ({ ok: true, json: () => new Promise(resolve => { finish = resolve; }) }));
  const request = c.api('/qa');
  await new Promise(setImmediate);
  assert.equal(c.cleared, 0);
  finish({}); await request;
  assert.equal(c.cleared, 1);
});
test('HTML or broken JSON cannot silently become a successful empty response', async () => {
  const c = client(async () => ({ status: 200, ok: true, json: async () => { throw new SyntaxError('Unexpected HTML'); } }));
  await assert.rejects(c.api('/qa'), /JSON/);
});
