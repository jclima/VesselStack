const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('control-panel/static/app.js', 'utf8') + '\nthis.__ui = { state, statusClass, isConfigDirty, currentSettings, lock, setBusy, renderComponents, checkReleases, startOperation, renderOperation, operationSucceeded, refreshStatus, renderReleases, updateReleaseControls, runComponent, runAction, toast, fieldMarkup, optionLabel, updateConfigStatus };';
const nodes = new Map();
const node = (id) => nodes.get(id) || nodes.set(id, { value: '', checked: false, disabled: false, textContent: '', hidden: false, className: '', innerHTML: '', classList: { toggle() {} }, addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; } }).get(id);
const fields = {};
const formControls = [];
let confirmCalls = 0;
const confirmMessages = [];
let fetchImpl = async () => ({ ok: true, json: async () => ({ configuration: context.__ui.state.config }) });
const context = {
  console, setTimeout: () => 0, clearTimeout() {}, confirm: () => { confirmCalls += 1; return true; },
  sessionStorage: { values: {}, getItem(key) { return this.values[key] || ''; }, setItem(key, value) { this.values[key] = value; }, removeItem(key) { delete this.values[key]; } },
  window: { addEventListener() {}, confirm: (message) => { confirmCalls += 1; confirmMessages.push(message); return true; } },
  document: { querySelector: (selector) => node(selector), querySelectorAll: (selector) => selector.includes('#config-form') ? formControls : [] },
  FormData: class { constructor() {} entries() { return Object.entries(fields).filter(([key]) => !formControls.some((control) => control.name === key && control.disabled)); } },
  URL,
  fetch: (...args) => fetchImpl(...args),
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
ui.state.busy = false; ui.state.installedVersion = '0.5.4';
ui.renderReleases({ channel: 'stable', installed_version: '0.5.4', running_version: '0.5.4', releases: [{ id: 42600, tag: 'v0.6.0', version: '0.6.0', name: 'Stable', html_url: 'https://github.com/jclima/VesselStack/releases/tag/v0.6.0' }] });
node('#release-select').value = '42600'; ui.updateReleaseControls();
assert.equal(node('#install-release').textContent, 'Back up & install 0.6.0', 'eligible selection labels install target');
assert.equal(node('#install-release').disabled, false, 'eligible selection enables install');
ui.renderReleases({ channel: 'stable', installed_version: '0.6.0', running_version: '0.6.0', releases: [] });
assert.equal(node('#install-release').disabled, true, 'no eligible release keeps install disabled');
ui.setBusy(false); assert.equal(node('#install-release').disabled, true, 'setBusy cannot enable install without candidate');
assert.equal(ui.operationSucceeded({ label: 'backup', state: 'succeeded', output: [] }), true, 'legacy ordinary success without phase is terminal');
assert.equal(ui.operationSucceeded({ label: 'restart grafana', state: 'succeeded' }), true, 'component restart is not panel restart verification');

// Copy and accessibility polish (pre-0.6.0).
assert.equal(ui.optionLabel('BOAT_UNITS', 'us_customary'), 'US customary');
assert.equal(ui.optionLabel('SIGNALK_MODE', 'docker'), 'Docker (built-in)');
assert.equal(ui.optionLabel('BOAT_CHAT_FALLBACK_PROVIDER', ''), 'None');
const selectMarkup = ui.fieldMarkup({ key: 'SIGNALK_MODE', label: 'Signal K mode', kind: 'select', choices: ['docker', 'existing'], description: 'Help <b>text</b>', read_only: false }, 'existing');
assert.match(selectMarkup, /<option value="docker" >Docker \(built-in\)<\/option>/, 'select options show human labels but keep stored values');
assert.match(selectMarkup, /aria-describedby="setting-SIGNALK_MODE-help"/, 'help text is tied to its control');
assert.match(selectMarkup, /Help &lt;b&gt;text&lt;\/b&gt;/, 'help text is escaped');
ui.state.components = [{ id: 'grafana', label: 'Grafana' }, { id: 'signalk', label: 'Signalk' }];
confirmMessages.length = 0;
ui.runComponent('signalk', 'restart');
assert.equal(confirmMessages[0], 'Restart Signal K? Live instrument data will pause briefly.', 'component restart confirms with its human label');
ui.runComponent('grafana', 'start');
assert.equal(confirmMessages[1], 'Start Grafana? Live instrument data will pause briefly.');
ui.runAction('restart');
assert.match(confirmMessages[2], /Live instrument data will pause briefly\./);
ui.toast('Something failed', true);
assert.match(node('#toast').className, /error/);
assert.equal(node('#toast-close').hidden, false, 'error toast offers a dismiss button and has no auto-hide timer');
ui.toast('Fine');
assert.equal(node('#toast-close').hidden, true);
ui.state.config = { fields: [], values: {} };
ui.state.rollbackPath = '/opt/backups/vesselstack.env.1';
ui.updateConfigStatus();
assert.match(node('#config-status').textContent, /All changes saved\..*\/opt\/backups\/vesselstack\.env\.1/, 'rollback path stays visible in the status line');
const source_ = fs.readFileSync('control-panel/static/app.js', 'utf8');
assert.doesNotMatch(source_, /SignalK|Started:/, 'prose uses Signal K and friendly operation text');

(async () => {
  ui.state.token = 'async-token'; ui.state.busy = false; ui.state.operationRunning = false;
  node('#include-lab').checked = true;
  let resolveLab;
  fetchImpl = (path) => path.includes('channel=lab') ? new Promise((resolve) => { resolveLab = resolve; }) : Promise.resolve({ ok: true, json: async () => ({ channel: 'stable', installed_version: '1.0.0', running_version: '1.0.0', releases: [{ id: 2, tag: 'v2', version: '2.0.0', name: 'Stable', html_url: 'https://github.com/example/repo/releases/tag/v2' }] }) });
  const labRequest = ui.checkReleases();
  node('#include-lab').checked = false;
  const stableRequest = ui.checkReleases();
  await stableRequest;
  resolveLab({ ok: true, json: async () => ({ channel: 'lab', releases: [{ id: 1, tag: 'lab', version: '9.0.0-lab', name: 'Lab', html_url: 'https://github.com/example/repo/releases/tag/lab' }] }) });
  await labRequest;
  assert.equal(ui.state.releases[0].id, 2, 'out-of-order catalog response cannot replace the newest channel');

  let statusCalls = 0;
  fetchImpl = (path, options = {}) => options.method === 'POST'
    ? Promise.reject(new Error('network drop'))
    : Promise.resolve({ ok: true, json: async () => ({ components: [], operation: { state: 'idle' } }) });
  await ui.startOperation({ action: 'update-release', release_id: 2, channel: 'stable' });
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(ui.state.busy, true, 'lost POST response keeps controls busy while status reconciles');
  ui.renderOperation({ state: 'idle' });
  assert.match(node('#operation-progress').textContent, /status is unavailable|retry status/i, 'uncertain admission shows recovery guidance');
  ui.state.pendingOperation = { baselineId: 7, operationId: null };
  ui.renderOperation({ id: 7, label: 'backup', state: 'succeeded', output: [] });
  assert.equal(ui.state.busy, true, 'old terminal operation cannot satisfy a lost new request');
  ui.renderOperation({ id: 8, label: 'backup', state: 'succeeded', output: [] });
  assert.equal(ui.state.busy, false, 'new terminal operation releases the pending request');
  ui.state.token = 'async-token'; ui.state.busy = false; ui.state.pendingOperation = null;
  fetchImpl = (path, options = {}) => options.method === 'POST'
    ? Promise.reject(Object.assign(new Error('invalid release'), { status: 400 }))
    : Promise.resolve({ ok: true, json: async () => ({ components: [], operation: { state: 'idle' } }) });
  await ui.startOperation({ action: 'update-release', release_id: 2, channel: 'stable' });
  await Promise.resolve(); await Promise.resolve();
  assert.equal(ui.state.busy, false, 'known HTTP 400 allows retry after clearing the rejected request');
  fetchImpl = (path, options = {}) => options.method === 'POST'
    ? Promise.reject(Object.assign(new Error('another operation is running'), { status: 409 }))
    : Promise.resolve({ ok: true, json: async () => ({ components: [], operation: { id: 55, label: 'backup', state: 'running' } }) });
  await ui.startOperation({ action: 'backup' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.state.busy, true, 'HTTP 409 reconciles an already-running operation');
  fetchImpl = () => Promise.resolve({ ok: false, status: 404, statusText: 'Not Found', json: async () => ({ error: 'missing status' }) });
  ui.state.token = 'async-token'; ui.state.pendingOperation = null; ui.state.busy = false; ui.state.reconnectTimer = null;
  await ui.refreshStatus();
  assert.equal(ui.state.busy, true, 'status 404 keeps controls locked');
  assert.equal(ui.state.reconnectTimer, 0, 'status 404 schedules bounded retry without recursive hot loop');
  statusCalls += 1;
  ui.lock();
  assert.equal(ui.state.token, '', 'lock clears token before late async work can restore state');
  console.log('control-panel UI regression checks passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
