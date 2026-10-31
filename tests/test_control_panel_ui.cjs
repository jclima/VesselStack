const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('control-panel/static/app.js', 'utf8') + '\nthis.__ui = { state, statusClass, isConfigDirty, currentSettings, saveConfiguration };';
const nodes = new Map();
const node = (id) => nodes.get(id) || nodes.set(id, { value: '', checked: false, disabled: false, textContent: '', hidden: false, className: '', addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; } }).get(id);
const fields = {};
const context = {
  console, setTimeout: () => 0, clearTimeout() {}, confirm: () => true,
  localStorage: { getItem: () => '', setItem() {}, removeItem() {} },
  window: { addEventListener() {} },
  document: { querySelector: (selector) => node(selector), querySelectorAll: () => [] },
  FormData: class { constructor() {} entries() { return Object.entries(fields); } },
  fetch: async () => ({ ok: true, json: async () => ({ configuration: context.__ui.state.config }) }),
};
vm.createContext(context);
vm.runInContext(source, context);
const ui = context.__ui;

assert.equal(ui.statusClass('inactive'), '');
assert.equal(ui.statusClass('running'), 'active');
ui.state.token = 'test';
ui.state.config = { fields: [
  { key: 'TOKEN', kind: 'secret', read_only: false, choices: [] },
  { key: 'MODE', kind: 'select', choices: ['existing', 'docker'], read_only: false },
  { key: 'ENABLED', kind: 'boolean', choices: [], read_only: false },
], values: { TOKEN: { configured: true } } };
fields.TOKEN = 'replacement'; fields.MODE = 'existing'; fields.ENABLED = 'false';
assert.equal(ui.isConfigDirty(), true, 'replacing a configured secret is dirty');
delete fields.TOKEN; assert.equal(ui.isConfigDirty(), false, 'blank secret preserves its value');
fields.TOKEN = null; node('[data-clear-secret]').checked = true;
assert.equal(ui.isConfigDirty(), true, 'clearing a configured secret is dirty');
delete fields.TOKEN; delete fields.MODE; delete fields.ENABLED;
assert.equal(ui.isConfigDirty(), false, 'displayed select and boolean defaults are clean');
console.log('control-panel UI regression checks passed');
