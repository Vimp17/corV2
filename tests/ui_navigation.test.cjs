const { test } = require('node:test'),
  assert = require('node:assert/strict'),
  fs = require('node:fs'),
  vm = require('node:vm');
const html = fs.readFileSync('dashboard/index.html', 'utf8');
function navigation(answer = false) {
  let confirmations = 0,
    redraws = 0;
  const nodes = {};
  const c = {
    state: {
      rating: { wells: [{ well_id: 'A' }, { well_id: 'B' }] },
      drawerOpen: true,
      selected: 'A',
      cardDirty: true,
      opener: null,
    },
    $: (id) =>
      (nodes[id] ||= {
        classList: { add() {}, remove() {} },
        setAttribute() {},
        removeAttribute() {},
        focus() {},
        scrollTop: 0,
      }),
    location: { pathname: '/dashboard', search: '', hash: '' },
    history: { replaceState() {} },
    document: { activeElement: null, contains: () => false },
    renderCard() {
      redraws++;
    },
    renderTable() {},
    notice() {},
    confirm() {
      confirmations++;
      return answer;
    },
  };
  vm.createContext(c);
  vm.runInContext(html.slice(html.indexOf('    function setHash('), html.indexOf('    function cardOrder(')), c);
  return { c, counts: () => ({ confirmations, redraws }) };
}
test('declining close retains dirty form', () => {
  const { c, counts } = navigation();
  c.closeWell();
  assert.equal(c.state.drawerOpen, true);
  assert.equal(c.state.cardDirty, true);
  assert.equal(counts().confirmations, 1);
});
test('accepting close confirms first', () => {
  const { c, counts } = navigation(true);
  c.closeWell();
  assert.equal(c.state.drawerOpen, false);
  assert.equal(counts().confirmations, 1);
});
test('same well click preserves form', () => {
  const { c, counts } = navigation();
  c.openWell('A');
  assert.equal(c.state.cardDirty, true);
  assert.equal(counts().redraws, 0);
});
test('declining switch keeps the well', () => {
  const { c } = navigation();
  c.openWell('B');
  assert.equal(c.state.selected, 'A');
  assert.equal(c.state.cardDirty, true);
});
test('late error cannot overwrite selected well', async () => {
  const tasks = {},
    box = {
      replaceChildren(...parts) {
        this.parts = parts;
      },
    };
  const c = {
    state: { selected: 'A' },
    api: (path) =>
      new Promise((resolve, reject) => {
        tasks[path] = { resolve, reject };
      }),
    $: () => box,
    el: (t, a) => a.text,
    renderAiBox: (d) => box.replaceChildren('AI ' + d.well_id),
  };
  for (const n of [
    'renderRules',
    'renderMath',
    'renderCo2',
    'renderWellDocs',
    'renderExtraMeasurements',
    'renderRegistry',
  ])
    c[n] = () => {};
  vm.createContext(c);
  vm.runInContext(html.slice(html.indexOf('    let detailRequest ='), html.indexOf('    function renderAiBox(')), c);
  const a = c.loadWellExtras('A');
  c.state.selected = 'B';
  const b = c.loadWellExtras('B');
  tasks['/api/v1/rating/wells/B'].resolve({ well_id: 'B' });
  await b;
  tasks['/api/v1/rating/wells/A'].reject(new Error('Old A'));
  await a;
  assert.deepEqual(box.parts, ['AI B']);
});
test('delete-all confirms retained versions even when failed', async () => {
  let confirms = 0,
    deletes = 0;
  const roots = {
    docClearMenu: {
      replaceChildren(...n) {
        this.children = n;
      },
      querySelector() {
        return { focus() {} };
      },
    },
    docClear: { setAttribute() {} },
  };
  const c = {
    state: {
      docs: {
        counts: { failed: 1 },
        documents: [{ status: 'failed', has_accepted_data: true, loaded: { wells: ['A'] } }],
      },
    },
    $: (id) => roots[id],
    el: (tag, attrs, ...children) => ({ tag, ...attrs, children }),
    confirm: () => {
      confirms++;
      return false;
    },
    api: async () => {
      deletes++;
      return { deleted: 1, wells: [] };
    },
    notice() {},
    loadDocuments: async () => {},
    refresh: async () => {},
  };
  vm.createContext(c);
  vm.runInContext(
    html.slice(html.indexOf('    function openClearMenu('), html.indexOf('    async function uploadDocuments(')),
    c,
  );
  c.openClearMenu();
  await roots.docClearMenu.children.filter((n) => n.tag === 'button')[1].onClick();
  assert.equal(confirms, 1);
  assert.equal(deletes, 0);
});
