const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('control-panel/static/app.js', 'utf8') + '\nthis.__ui = { state, statusClass, isConfigDirty, currentSettings, lock, setBusy, renderComponents };';
const nodes = new Map();
const node = (id) => nodes.get(id) || nodes.set(id, { value: '', checked: false, disabled: false, textContent: '', hidden: false, className: '', innerHTML: '', classList: { toggle() {} }, addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; } }).get(id);
const fields = {};
const formControls = [];
let confirmCalls = 0;
const context = {
  console, setTimeout: () => 0, clearTimeout() {}, confirm: () => { confirmCalls += 1; return true; },
  sessionStorage: { values: {}, getItem(key) { return this.values[key] || ''; }, setItem(key, value) { this.values[key] = value; }, removeItem(key) { delete this.values[key]; } },
  window: { addEventListener() {}, confirm: () => { confirmCalls += 1; return true; } },
  document: { querySelector: (selector) => node(selector), querySelectorAll: (selector) => selector.includes('#config-form') ? formControls : [] },
  FormData: class { constructor() {} entries() { return Object.entries(fields).filter(([key]) => !formControls.some((control) => control.name === key && control.disabled)); } },
  fetch: async () => ({ ok: true, json: async () => ({ configuration: context.__ui.state.config }) }),
};
vm.createContext(context);
vm.runInContext(source, context);
const ui = context.__ui;

assert.equal(ui.statusClass('inactive'), '');
assert.equal(ui.statusClass('running'), 'active');
assert.equal(ui.statusClass('inactive (dead)'), '');
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

ui.state.busy = false;
formControls.push({ name: 'BOAT_NAME', value: 'My Boat', type: 'text', disabled: false, dataset: { readOnly: 'false' } });
ui.state.config = { fields: [{ key: 'BOAT_NAME', kind: 'text', read_only: false }], values: { BOAT_NAME: 'Original' } };
fields.BOAT_NAME = 'My Boat';
ui.setBusy(true);
assert.equal(ui.isConfigDirty(), true, 'disabled busy form still reports unsaved edits');
ui.lock();
assert.equal(confirmCalls, 1, 'locking with disabled unsaved edits still confirms');
ui.state.busy = false;
ui.renderComponents([{ id: 'grafana', label: 'Grafana', manageable: true, enabled: false, state: 'running', kind: 'container' }]);
assert.match(node('#component-grid').innerHTML, /Retry removal/, 'excluded but running core component offers retry removal');
ui.renderComponents([{ id: 'grafana', label: 'Grafana', manageable: true, enabled: false, state: 'stopped', kind: 'container' }]);
assert.doesNotMatch(node('#component-grid').innerHTML, /Retry removal/, 'stopped excluded core component does not offer retry removal');
ui.renderComponents([{ id: 'socketcan', label: 'SocketCAN', manageable: false, enabled: true, state: 'active', kind: 'systemd' }]);
assert.doesNotMatch(node('#component-grid').innerHTML, /Add component|Remove|Retry removal/, 'non-manageable hardware never offers component membership actions');

ui.state.token = 'session-token'; ui.state.config = null; ui.lock();
assert.equal(context.sessionStorage.values.vesselstack_control_token, undefined, 'lock clears the session token');
assert.equal(node('#app').hidden, true, 'lock hides the app and secrets');
ui.state.token = 'test'; ui.state.config = { fields: [], values: {} }; ui.setBusy(true);
assert.equal(node('#refresh').disabled, false, 'refresh remains available while busy');
assert.equal(node('#save-config').disabled, true, 'save is disabled while busy');
console.log('control-panel UI regression checks passed');
