const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appSource = fs.readFileSync('control-panel/static/app.js', 'utf8');
const html = fs.readFileSync('control-panel/static/index.html', 'utf8');
const css = fs.readFileSync('control-panel/static/styles.css', 'utf8');
const source = appSource + '\nthis.__ui = { state, normalizeToken, isConfigDirty, dirtyKeys, currentSettings, lock, setBusy, renderComponents, checkReleases, startOperation, renderOperation, operationSucceeded, refreshStatus, renderReleases, runComponent, runAction, installRelease, discardEdits, toast, fieldMarkup, optionLabel, updateConfigStatus, renderBand, renderUpdates, requireReauth, CONFIRMS, describeOperation, setNotice, showFieldErrors, renderConfiguration, selectTab };';

const nodes = new Map();
const makeNode = () => ({
  value: '', checked: false, disabled: false, textContent: '', hidden: false, className: '', innerHTML: '', dataset: {}, open: false,
  classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
  addEventListener() {}, setAttribute() {}, removeAttribute() {}, closest: () => null, focus() {}, scrollIntoView() {},
  querySelector() { return null; }, querySelectorAll() { return []; }, isConnected: true,
});
const node = (id) => nodes.get(id) || nodes.set(id, makeNode()).get(id);
const fields = {};
const formControls = [];
const clearBoxes = [];
const dialogTitles = [];
let dialogAnswer = 'ok';
const dialog = node('#confirm-dialog');
dialog.showModal = function showModal() {
  dialogTitles.push({ title: node('#confirm-title').textContent, intro: node('#confirm-intro').textContent, items: node('#confirm-items').innerHTML, ok: node('#confirm-ok').textContent });
  this.open = true;
  this.returnValue = dialogAnswer;
  this.open = false;
  if (this.onclose) this.onclose();
};
dialog.close = function close(value) { this.returnValue = value; };
let fetchImpl = async () => ({ ok: true, json: async () => ({ configuration: context.__ui.state.config }) });
const context = {
  console, setTimeout: () => 0, clearTimeout() {},
  sessionStorage: { values: {}, getItem(key) { return this.values[key] || ''; }, setItem(key, value) { this.values[key] = value; }, removeItem(key) { delete this.values[key]; } },
  window: { addEventListener() {}, confirm: () => { throw new Error('window.confirm must not be used'); }, location: { reload() {} } },
  document: {
    hidden: false, activeElement: { dataset: {} }, addEventListener() {},
    querySelector: (selector) => node(selector),
    querySelectorAll: (selector) => selector.includes('#config-form') ? formControls : selector.includes('data-clear-secret') ? clearBoxes : [],
  },
  URL, Date,
  fetch: (...args) => fetchImpl(...args),
};
vm.createContext(context);
vm.runInContext(source, context);
const ui = context.__ui;
const tick = () => new Promise((resolve) => setImmediate(resolve));
const ok = (payload) => ({ ok: true, json: async () => payload });
const control = (name, value, extra = {}) => { const item = { name, value, type: 'text', disabled: false, dataset: { readOnly: 'false' }, ...extra }; formControls.push(item); return item; };

// ---- static structure -------------------------------------------------------
assert.doesNotMatch(appSource, /\bconfirm\s*\(/, 'no window.confirm (or any confirm call) is left in app.js');
assert.doesNotMatch(appSource, /\balert\s*\(|\bprompt\s*\(/, 'no blocking native dialogs');
assert.equal((html.match(/data-action="apply"/g) || []).length, 1, 'exactly one Apply control');
assert.doesNotMatch(html, /Apply configuration|Apply now/, 'the duplicate Apply card and old label are gone');
assert.doesNotMatch(html, /Install &amp; start|Run preflight|Create backup|Your boat computer,/, 'old hero is gone');
assert.match(html, /Reinstall &amp; start/); assert.match(html, /Check setup/);
['Home', 'Settings', 'Updates', 'Maintenance'].forEach((name) => assert.match(html, new RegExp(`role="tab"[^>]*>${name}`)));
assert.equal((html.match(/role="tab"/g) || []).length, 4);
assert.match(html, /<dialog id="confirm-dialog"/);
assert.match(html, /How do I reach this panel\?/); assert.match(html, /ssh -L 8780:127\.0\.0\.1:8780/); assert.match(html, /id="show-token"/);
assert.match(html, /Contacts github\.com only when you press it/);
assert.match(html, /id="banner" aria-live="polite"/);
assert.doesNotMatch(html, /\bstyle="/, 'CSP forbids inline styles');
assert.doesNotMatch(html + appSource, /https?:\/\/(?!127\.0\.0\.1|github\.com)/, 'no external requests');
assert.doesNotMatch(css, /@import|url\(\s*["']?https?:/, 'no external stylesheet or font requests');
assert.match(css, /prefers-color-scheme: dark/); assert.match(css, /IBM Plex Mono/);
assert.doesNotMatch(appSource, /style="/, 'generated markup has no inline styles');
assert.doesNotMatch(appSource, /SignalK|Started:|restart vesselstack-control-panel\.service/, 'no stale prose');

// ---- token parsing ----------------------------------------------------------
assert.equal(ui.normalizeToken('CONTROL_PANEL_TOKEN=abc123'), 'abc123');
assert.equal(ui.normalizeToken('  CONTROL_PANEL_TOKEN = abc123 \n'), 'abc123');
assert.equal(ui.normalizeToken("CONTROL_PANEL_TOKEN='abc 123'"), 'abc 123');
assert.equal(ui.normalizeToken('export CONTROL_PANEL_TOKEN="abc"'), 'abc');
assert.equal(ui.normalizeToken(' abc=def '), 'abc=def');
assert.equal(ui.normalizeToken('CONTROL_PANEL_TOKEN='), '');

// ---- dirty tracking, switches and secrets -----------------------------------
ui.state.config = { fields: [
  { key: 'TOKEN', kind: 'secret', read_only: false, choices: [] },
  { key: 'MODE', kind: 'select', choices: ['existing', 'docker'], read_only: false },
  { key: 'ENABLED', kind: 'boolean', choices: [], read_only: false },
], values: { TOKEN: { configured: true } } };
const tokenBox = control('TOKEN', 'replacement'); const modeBox = control('MODE', 'existing'); const enabledBox = control('ENABLED', 'on', { type: 'checkbox', checked: false, dataset: { readOnly: 'false', bool: 'true' } });
assert.deepEqual(ui.dirtyKeys(), ['TOKEN'], 'replacing a configured secret is dirty');
tokenBox.value = ''; assert.equal(ui.isConfigDirty(), false, 'blank secret preserves its value, default select and switch are clean');
enabledBox.checked = true; assert.deepEqual(ui.dirtyKeys(), ['ENABLED'], 'turning a switch on is a change');
assert.equal(ui.currentSettings().ENABLED, 'true'); enabledBox.checked = false; assert.equal(ui.currentSettings().ENABLED, 'false', 'an unchecked switch is sent as false, not dropped');
clearBoxes.push({ checked: true, dataset: { clearSecret: 'TOKEN' } });
assert.equal(ui.isConfigDirty(), true, 'clearing a configured secret is dirty'); clearBoxes.length = 0;

formControls.length = 0;
ui.state.busy = false;
control('BOAT_NAME', 'My Boat');
ui.state.config = { fields: [{ key: 'BOAT_NAME', kind: 'text', read_only: false }], values: { BOAT_NAME: 'Original' } };
ui.setBusy(true);
assert.equal(ui.isConfigDirty(), true, 'disabled busy form still reports unsaved edits');
assert.equal(node('#sb-state').textContent, 'Unsaved changes (1)');
assert.equal(node('#sb-note').textContent, 'Save first to apply');
assert.equal(node('#sb-apply').disabled, true, 'Apply is off while edits are unsaved');
ui.setBusy(false);
assert.equal(node('#sb-save').disabled, false); assert.equal(node('#sb-discard').disabled, false);
formControls[0].value = 'Original';
ui.updateConfigStatus();
assert.equal(node('#sb-state').textContent, 'All changes saved');
assert.equal(node('#sb-note').textContent, 'Saved settings are ready to apply');
assert.equal(node('#sb-apply').disabled, false, 'Apply is available once nothing is unsaved');

// ---- dialogs replace window.confirm -------------------------------------------
(async () => {
  const posts = [];
  fetchImpl = async (path, options = {}) => {
    if (options.method === 'POST') { posts.push({ path, body: JSON.parse(options.body) }); return ok({ id: 1, label: 'x', state: 'running', output: [] }); }
    return ok({ components: [], operation: { state: 'idle' }, installed_version: '0.6.0', running_version: '0.6.0' });
  };
  ui.state.token = 'test-token';
  ui.state.components = [{ id: 'grafana', label: 'Grafana', status: 'running' }, { id: 'signalk', label: 'Signal K', status: 'running' }];
  ui.state.installedVersion = '0.6.0';
  const askedFor = async (run) => { dialogTitles.length = 0; dialogAnswer = 'cancel'; await run(); await tick(); return dialogTitles.slice(); };
  for (const [action, title] of [['stop', 'Stop all services?'], ['restart', 'Restart all services?'], ['start', 'Start all services?'], ['install', 'Reinstall & start?'], ['apply', 'Apply saved settings?'], ['restart-panel', 'Restart the Control Panel?']]) {
    const asked = await askedFor(() => ui.runAction(action));
    assert.equal(asked.length, 1, `${action} asks first`);
    assert.equal(asked[0].title, title);
    assert.equal(posts.length, 0, `cancelling ${action} starts nothing`);
  }
  assert.match((await askedFor(() => ui.runAction('stop')))[0].items, /Home Assistant and Grafana[\s\S]*Signal K[\s\S]*Boat Chat[\s\S]*Displays/, 'Stop all lists what goes dark');
  assert.match((await askedFor(() => ui.runAction('install')))[0].items, /Live data pauses[\s\S]*Unsaved edits[\s\S]*not touched/, 'Reinstall lists its effects');
  const reviewed = await askedFor(() => ui.runAction('update', { source_directory: '/var/lib/vesselstack/releases/x' }));
  assert.match(reviewed[0].title, /update from this folder/); assert.match(reviewed[0].intro, /\/var\/lib\/vesselstack\/releases\/x/);
  for (const [verb, pattern] of [['stop', /^Stop Signal K\?$/], ['restart', /^Restart Signal K\?$/], ['remove', /^Disable Signal K\?$/]]) {
    const asked = await askedFor(() => ui.runComponent('signalk', verb));
    assert.equal(asked.length, 1, `component ${verb} asks first`); assert.match(asked[0].title, pattern);
  }
  assert.match((await askedFor(() => ui.runComponent('signalk', 'remove')))[0].intro, /stay off, including after a restart or Start all/);
  ui.state.releases = [{ id: 9, tag: 'v0.6.1', version: '0.6.1', name: 'Fixes', html_url: 'https://github.com/jclima/VesselStack/releases/tag/v0.6.1' }];
  const install = await askedFor(() => ui.installRelease(9));
  assert.equal(install[0].title, 'Back up & install 0.6.1?'); assert.equal(posts.length, 0);
  // Edits are never discarded and the panel never locks without asking.
  formControls[0].value = 'Edited'; ui.state.config = { fields: [{ key: 'BOAT_NAME', kind: 'text', read_only: false }], values: { BOAT_NAME: 'Original' } };
  assert.equal((await askedFor(() => ui.discardEdits()))[0].title, 'Discard your edits?');
  const locked = await askedFor(() => ui.lock());
  assert.equal(locked[0].title, 'Lock the panel?'); assert.equal(ui.state.token, 'test-token', 'cancelling the lock keeps the session');
  formControls[0].value = 'Original';
  // Starting everything, restarting one component and the safe actions.
  for (const action of ['backup', 'preflight', 'bootstrap-history']) {
    ui.state.busy = false; ui.state.pendingOperation = null;
    assert.equal((await askedFor(() => ui.runAction(action))).length, 0, `${action} is not interrupting and needs no dialog`);
    assert.equal(posts.at(-1).body.action, action);
  }
  dialogAnswer = 'ok';
  ui.state.busy = false; ui.state.pendingOperation = null; posts.length = 0;
  await ui.runAction('stop'); await tick();
  assert.deepEqual(posts[0], { path: '/api/action', body: { action: 'stop' } }, 'confirming Stop all posts the same action id as before');
  ui.state.busy = false; ui.state.pendingOperation = null;
  await ui.runAction('install'); await tick();
  assert.equal(posts.at(-1).body.action, 'install', 'Reinstall & start keeps its install action id');
  ui.state.busy = false; ui.state.pendingOperation = null;
  await ui.runAction('preflight'); await tick();
  assert.equal(posts.at(-1).body.action, 'preflight', 'Check setup keeps its preflight action id');
  ui.state.busy = false; ui.state.pendingOperation = null;
  await ui.runComponent('grafana', 'remove'); await tick();
  assert.deepEqual(posts.at(-1), { path: '/api/component', body: { component: 'grafana', action: 'remove' } });
  assert.equal(Object.keys(ui.CONFIRMS).sort().join(), ['apply', 'install', 'restart', 'restart-panel', 'start', 'stop'].join());
  ui.state.pendingOperation = null; ui.state.busy = false;

  // ---- components: names, statuses, menu, configure -----------------------
  const comps = [
    { id: 'grafana', label: 'Grafana', description: 'Charts', section: 'Services & ports', manageable: true, enabled: true, state: 'running', status: 'running' },
    { id: 'heimdall', label: 'Heimdall', description: 'Start page', section: 'Services & ports', manageable: true, enabled: true, state: 'exited', status: 'stopped' },
    { id: 'telegram', label: 'Telegram alerts', description: 'Alerts', section: 'Boat Chat', manageable: false, enabled: false, state: 'active', status: 'disabled', still_running: true },
    { id: 'socketcan', label: 'SocketCAN', description: 'CAN', section: 'Signal K & NMEA 2000', manageable: false, enabled: false, state: 'inactive', status: 'disabled' },
    { id: 'ais-catcher', label: 'AIS-catcher', description: 'AIS', section: 'AIS receiver', manageable: false, enabled: true, state: 'unknown', status: 'unknown' },
    { id: 'control-panel', label: 'Control Panel', status: 'running' },
  ];
  ui.renderComponents(comps, true);
  const grid = node('#component-grid').innerHTML;
  assert.match(grid, /<h3>AIS-catcher<\/h3>/); assert.match(grid, /<h3>Telegram alerts<\/h3>/); assert.match(grid, /<h3>SocketCAN<\/h3>/);
  assert.doesNotMatch(grid, /Control Panel<\/h3>/, 'the panel does not list itself');
  assert.doesNotMatch(grid, /Active|Available|Ais Catcher|Socketcan/, 'only Running, Stopped and Disabled words, real names');
  assert.match(grid, /status running">Running/); assert.match(grid, /status stopped">Stopped/); assert.match(grid, /status disabled">Disabled/); assert.match(grid, /status unknown">Unknown/);
  assert.match(grid, /data-configure-section="Services &amp; ports"/, 'Configure link where there are settings');
  assert.match(grid, /Turn on in Settings/, 'a config-driven disabled component points at Settings');
  assert.match(grid, /Still running although it is turned off/);
  assert.doesNotMatch(grid, /data-verb="remove"[^>]*>Remove/, 'no Remove button, only the menu');
  assert.match(grid, /aria-label="More actions for Grafana"/);
  assert.doesNotMatch(grid, /aria-label="More actions for (Telegram|SocketCAN|AIS)/, 'only core services can be disabled');
  assert.match(node('#component-note').textContent, /^5 components: 1 running, 1 stopped, 2 disabled, 1 unknown/, 'count note matches what is shown');
  assert.match(node('#band').innerHTML, /1 of 3 stopped/);
  ui.state.menuOpen = 'grafana'; ui.renderComponents(comps, true);
  assert.match(node('#component-grid').innerHTML, /removes its container and marks it disabled/, 'the menu explains what Disable does');
  ui.state.menuOpen = null;
  ui.renderComponents([{ id: 'grafana', label: 'Grafana', manageable: true, enabled: false, state: 'running', status: 'disabled', still_running: true }], true);
  assert.match(node('#component-grid').innerHTML, /Retry disabling/, 'disabled but still running core component offers a retry');
  ui.renderComponents([{ id: 'grafana', label: 'Grafana', manageable: true, enabled: false, state: 'stopped', status: 'disabled' }], true);
  assert.doesNotMatch(node('#component-grid').innerHTML, /Retry disabling/); assert.match(node('#component-grid').innerHTML, />Enable</);
  ui.renderComponents([{ id: 'socketcan', label: 'SocketCAN', manageable: false, enabled: true, state: 'active', status: 'running' }], true);
  assert.doesNotMatch(node('#component-grid').innerHTML, /Add component|Remove|Retry removal|data-verb="add"/, 'hardware never offers membership actions');
  ui.renderComponents([{ id: 'a', label: 'A', status: 'running' }, { id: 'b', label: 'B', status: 'running' }], true);
  assert.match(node('#band').innerHTML, /All systems running/);

  // ---- result banner -----------------------------------------------------------
  ui.state.components = [{ id: 'grafana', label: 'Grafana', status: 'running' }];
  ui.state.bootId = 'boot'; ui.state.status = { backup: { path: '/media/backup', mount: 'mounted', state: 'ok', last_backup_at: Math.floor(Date.now() / 1000) - 60 } };
  const now = Math.floor(Date.now() / 1000);
  ui.renderOperation({ id: 3, label: 'backup', state: 'failed', started_at: now - 5, finished_at: now, output: ['ctl: running backup', 'ERROR: backup mount /media/backup is not mounted'], summary: { reason: 'backup mount /media/backup is not mounted' } });
  assert.match(node('#banner').innerHTML, /role="alert"/); assert.match(node('#banner').innerHTML, /Backup failed<\/strong> — backup mount \/media\/backup is not mounted\. Plug in or mount the backup drive and try again\./);
  assert.match(node('#banner').innerHTML, /<details class="disc"><summary>Show details<\/summary><pre class="log"[^>]*>ctl: running backup\nERROR/, 'details hold the operation log');
  assert.doesNotMatch(node('#banner').innerHTML, /Update failed/, 'no update wording for a backup');
  assert.match(node('#banner').innerHTML, /aria-label="Dismiss message"/);
  ui.renderOperation({ id: 4, label: 'backup', state: 'succeeded', started_at: now, finished_at: now, output: ['x'], summary: { saved_path: '/media/backup/vesselstack-1.tar.gz' } });
  assert.match(node('#banner').innerHTML, /Backup finished [^<]* &#10003;<\/strong> — saved to <span class="mono">\/media\/backup\/vesselstack-1\.tar\.gz<\/span>/);
  assert.doesNotMatch(node('#banner').innerHTML, /restart vesselstack-control-panel/, 'no routine restart advice on success');
  ui.renderOperation({ id: 5, label: 'stop grafana', state: 'running', started_at: now, output: [] });
  assert.match(node('#banner').innerHTML, /Stopping Grafana…/); assert.equal(ui.state.busy, true);
  ui.renderOperation({ id: 5, label: 'stop grafana', state: 'succeeded', started_at: now, finished_at: now, output: [] });
  assert.match(node('#banner').innerHTML, /Grafana stopped [^<]*&#10003;/); assert.equal(ui.state.busy, false);
  ui.renderOperation({ id: 6, label: 'update-release', state: 'succeeded', phase: 'complete', target_version: '0.6.1', running_version: '0.6.1', started_at: now, finished_at: now, output: [] });
  assert.match(node('#banner').innerHTML, /Installed 0\.6\.1/);
  ui.renderOperation({ id: 7, label: 'update-release', state: 'succeeded', phase: 'installing', target_version: '0.6.1', running_version: '0.6.0', started_at: now, output: [] });
  assert.match(node('#banner').innerHTML, /waiting for the Control Panel to confirm/); assert.equal(ui.state.busy, true, 'unverified update success keeps controls locked');
  ui.state.busy = false; ui.state.operationRunning = false; ui.state.pendingOperation = null;
  ui.renderOperation({ id: 3, label: 'backup', state: 'failed', started_at: now - 5, finished_at: now, output: ['e'], summary: { reason: 'boom' } });
  assert.match(node('#banner').innerHTML, /Backup failed/);
  context.document.querySelector('#banner').innerHTML = node('#banner').innerHTML;
  ui.state.dismissed.add(`boot:3:${now - 5}:backup`);
  ui.renderOperation({ id: 3, label: 'backup', state: 'failed', started_at: now - 5, finished_at: now, output: ['e'], summary: { reason: 'boom' } });
  assert.equal(node('#banner').innerHTML, '', 'a dismissed result stays dismissed across polls');
  ui.renderOperation({ id: 1, label: 'backup', state: 'succeeded', started_at: now - 7200, finished_at: now - 7100, output: [] });
  assert.equal(node('#banner').innerHTML, '', 'an old result from an earlier session is not shown');
  assert.match(ui.describeOperation({ label: 'remove grafana' }).done, /disabled/);
  assert.equal(ui.operationSucceeded({ label: 'backup', state: 'succeeded', output: [] }), true);
  assert.equal(ui.operationSucceeded({ label: 'restart grafana', state: 'succeeded' }), true);

  // ---- field markup ----------------------------------------------------------
  assert.equal(ui.optionLabel('BOAT_UNITS', 'us_customary'), 'US customary');
  assert.equal(ui.optionLabel('BOAT_CHAT_FALLBACK_PROVIDER', ''), 'None');
  const selectMarkup = ui.fieldMarkup({ key: 'SIGNALK_MODE', label: 'Signal K mode', kind: 'select', choices: ['docker', 'existing'], description: 'Help <b>text</b>', read_only: false }, 'existing');
  assert.match(selectMarkup, /<option value="docker" >Docker \(recommended\)<\/option>/, 'select options show human labels but keep stored values');
  assert.match(selectMarkup, /aria-describedby="setting-SIGNALK_MODE-err setting-SIGNALK_MODE-help"/); assert.match(selectMarkup, /Help &lt;b&gt;text&lt;\/b&gt;/);
  assert.match(selectMarkup, /id="setting-SIGNALK_MODE-err" role="alert"/, 'room for an inline error next to the field');
  const switchMarkup = ui.fieldMarkup({ key: 'SOCKETCAN_ENABLE', label: 'Read the NMEA 2000 network', kind: 'boolean', description: 'Turn on after testing', read_only: false }, 'true');
  assert.match(switchMarkup, /type="checkbox"[^>]*data-bool="true"[^>]*checked/); assert.match(switchMarkup, /<strong>Read the NMEA 2000 network/);
  const secretMarkup = ui.fieldMarkup({ key: 'OPENAI_API_KEY', label: 'OpenAI API key', kind: 'secret', description: 'x', read_only: false }, { configured: true });
  assert.match(secretMarkup, /Saved · leave blank to keep/); assert.doesNotMatch(secretMarkup, /value="[^"]+"[^>]*name="OPENAI/);
  assert.doesNotMatch(selectMarkup + switchMarkup + secretMarkup, /`/, 'no literal backticks');

  // ---- toasts --------------------------------------------------------------
  ui.toast('Something failed', true);
  assert.match(node('#toast').className, /error/);
  assert.equal(node('#toast-close').hidden, false, 'error toast offers a dismiss button');
  ui.toast('Fine'); assert.equal(node('#toast-close').hidden, true);
  assert.match(css, /#toast button\{min-height:44px/, 'toast Dismiss is a 44px target');
  assert.match(css, /filters button\{[^}]*padding/); assert.doesNotMatch(css, /\.filters button\{[^}]*min-height:(?!44)/);

  // ---- updates --------------------------------------------------------------
  ui.state.busy = false; ui.state.installedVersion = '0.6.0'; ui.state.releaseChecked = false; ui.state.releases = [];
  ui.renderUpdates();
  assert.match(node('#release-result').innerHTML, /Not checked yet\. Nothing is contacted/);
  assert.equal(node('#update-badge').hidden, true);
  ui.renderReleases({ channel: 'stable', installed_version: '0.6.0', running_version: '0.6.0', releases: [] });
  assert.match(node('#release-result').innerHTML, /You're up to date on 0\.6\.0/); assert.equal(node('#update-badge').hidden, true);
  assert.match(node('#band').innerHTML, /Up to date on 0\.6\.0/);
  ui.renderReleases({ channel: 'stable', installed_version: '0.6.0', running_version: '0.6.0', releases: [
    { id: 42601, tag: 'v0.6.1', version: '0.6.1', name: 'Fixes', html_url: 'https://github.com/jclima/VesselStack/releases/tag/v0.6.1' },
    { id: 1, tag: 'v0.5.0', version: '0.5.0', name: 'Old', html_url: 'https://github.com/jclima/VesselStack/releases/tag/v0.5.0' },
    { id: 2, tag: 'v9', version: '9.0.0', name: 'Bad link', html_url: 'https://evil.example/releases/tag/v9' }] });
  assert.match(node('#release-result').innerHTML, /0\.6\.1 is available/); assert.match(node('#release-result').innerHTML, /Back up &amp; install 0\.6\.1/);
  assert.doesNotMatch(node('#release-result').innerHTML, /0\.5\.0|9\.0\.0|evil\.example/, 'older and unreviewable releases are never offered');
  assert.equal(node('#update-badge').hidden, false); assert.equal(node('#update-badge').textContent, '1');
  assert.match(node('#band').innerHTML, /0\.6\.1 available/); assert.match(node('#band').innerHTML, /Go to Updates/);

  fetchImpl = (path) => { posts.push({ path }); return Promise.resolve(ok({ channel: 'stable', installed_version: '1.0.0', running_version: '1.0.0', releases: [{ id: 2, tag: 'v2', version: '2.0.0', name: 'Stable', html_url: 'https://github.com/example/repo/releases/tag/v2' }] })); };
  ui.state.token = 'async-token'; node('#include-lab').checked = true;
  let resolveLab;
  fetchImpl = (path) => path.includes('channel=lab') ? new Promise((resolve) => { resolveLab = resolve; }) : Promise.resolve(ok({ channel: 'stable', installed_version: '1.0.0', running_version: '1.0.0', releases: [{ id: 2, tag: 'v2', version: '2.0.0', name: 'Stable', html_url: 'https://github.com/example/repo/releases/tag/v2' }] }));
  const labRequest = ui.checkReleases();
  node('#include-lab').checked = false;
  const stableRequest = ui.checkReleases();
  await stableRequest;
  resolveLab(ok({ channel: 'lab', releases: [{ id: 1, tag: 'lab', version: '9.0.0-lab', name: 'Lab', html_url: 'https://github.com/example/repo/releases/tag/lab' }] }));
  await labRequest;
  assert.equal(ui.state.releases[0].id, 2, 'out-of-order catalog response cannot replace the newest channel');
  fetchImpl = () => Promise.reject(new Error('offline'));
  await ui.checkReleases();
  assert.match(node('#release-result').innerHTML, /Couldn't check for updates<\/strong> — offline/); assert.equal(ui.state.releaseChecked, false);

  // ---- operations, reconciliation and connection states -------------------------
  ui.state.token = 'async-token'; ui.state.busy = false; ui.state.operationRunning = false; ui.state.pendingOperation = null;
  fetchImpl = (path, options = {}) => options.method === 'POST'
    ? Promise.reject(new Error('network drop'))
    : Promise.resolve(ok({ components: [], operation: { state: 'idle' } }));
  await ui.startOperation({ action: 'update-release', release_id: 2, channel: 'stable' });
  await Promise.resolve(); await Promise.resolve();
  assert.equal(ui.state.busy, true, 'lost POST response keeps controls busy while status reconciles');
  ui.renderOperation({ state: 'idle' });
  assert.match(ui.state.notices.opstatus.text, /status is unavailable/i, 'uncertain admission shows recovery guidance');
  ui.state.pendingOperation = { baselineId: 7, operationId: null };
  ui.renderOperation({ id: 7, label: 'backup', state: 'succeeded', output: [] });
  assert.equal(ui.state.busy, true, 'old terminal operation cannot satisfy a lost new request');
  ui.renderOperation({ id: 8, label: 'backup', state: 'succeeded', output: [] });
  assert.equal(ui.state.busy, false, 'new terminal operation releases the pending request');
  ui.state.token = 'async-token'; ui.state.busy = false; ui.state.pendingOperation = null;
  fetchImpl = (path, options = {}) => options.method === 'POST'
    ? Promise.reject(Object.assign(new Error('invalid release'), { status: 400 }))
    : Promise.resolve(ok({ components: [], operation: { state: 'idle' } }));
  await ui.startOperation({ action: 'update-release', release_id: 2, channel: 'stable' });
  await Promise.resolve(); await Promise.resolve();
  assert.equal(ui.state.busy, false, 'known HTTP 400 allows retry after clearing the rejected request');
  fetchImpl = (path, options = {}) => options.method === 'POST'
    ? Promise.reject(Object.assign(new Error('another operation is running'), { status: 409 }))
    : Promise.resolve(ok({ components: [], operation: { id: 55, label: 'backup', state: 'running' } }));
  await ui.startOperation({ action: 'backup' });
  await tick();
  assert.equal(ui.state.busy, true, 'HTTP 409 reconciles an already-running operation');

  fetchImpl = () => Promise.resolve({ ok: false, status: 404, statusText: 'Not Found', json: async () => ({ error: 'missing status' }) });
  ui.state.token = 'async-token'; ui.state.pendingOperation = null; ui.state.busy = false; ui.state.reconnectTimer = null; ui.state.reconnectAttempts = 0;
  await ui.refreshStatus();
  assert.equal(ui.state.busy, true, 'status 404 keeps controls locked');
  assert.equal(ui.state.reconnectTimer, 0, 'status 404 schedules bounded retry without recursive hot loop');
  assert.equal(node('#connection-label').textContent, 'Disconnected', 'a dead backend is Disconnected, not Locked');
  assert.equal(node('#retry').hidden, false, 'Retry now is offered');
  assert.match(ui.state.notices.offline.text, /Can't reach the boat computer/); assert.doesNotMatch(JSON.stringify(ui.state.notices), /Unlock button|Use Unlock/);
  ui.state.bootId = null;
  fetchImpl = () => Promise.resolve(ok({ components: [{ id: 'grafana', label: 'Grafana', status: 'running' }], operation: { state: 'idle' }, installed_version: '0.6.0', running_version: '0.6.0', boot_id: 'b1', boat_name: 'Example Vessel', backup: { path: '/media/backup', mount: 'mounted', state: 'ok', last_backup_at: Math.floor(Date.now() / 1000) }, restart_needed: false }));
  await ui.refreshStatus();
  assert.equal(node('#connection-label').textContent, 'Connected'); assert.equal(node('#retry').hidden, true); assert.equal(ui.state.notices.offline, undefined);
  assert.equal(node('#boat-name').textContent, 'Example Vessel'); assert.equal(node('#version-chip').textContent, '0.6.0');
  assert.match(node('#band').innerHTML, /Last backup: today/); assert.match(node('#band').innerHTML, /Backup drive OK/);
  assert.equal(node('#band').innerHTML.includes('Back up now'), true);
  ui.state.status = { backup: { path: '/media/backup', mount: 'not_mounted', state: 'unknown', last_backup_at: null, message: 'x' } }; ui.renderBand();
  assert.match(node('#band').innerHTML, /Last backup: unknown/); assert.match(node('#band').innerHTML, /Backup drive not mounted/);
  // A restarted panel with unsaved edits is information, not an error.
  ui.state.bootId = 'b1'; formControls.length = 0; control('BOAT_NAME', 'Edited'); ui.state.config = { fields: [{ key: 'BOAT_NAME', kind: 'text', read_only: false }], values: { BOAT_NAME: 'Original' } };
  fetchImpl = () => Promise.resolve(ok({ components: [], operation: { state: 'idle' }, boot_id: 'b2' }));
  await ui.refreshStatus();
  assert.equal(ui.state.notices.restarted.kind, 'info'); assert.match(node('#notice').innerHTML, /class="banner info"/); assert.doesNotMatch(node('#notice').innerHTML, /banner fail/);
  // An idle poll that finds the token rotated tells the user plainly.
  fetchImpl = () => Promise.resolve({ ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({ error: 'nope' }) });
  await ui.refreshStatus();
  assert.equal(node('#login-error').textContent, 'Your session ended. Unlock again. Your unsaved edits are kept.');
  assert.equal(ui.state.token, '');
  // Inline per-field errors come back from the server and are placed next to the field.
  const invalidField = { classList: { add() { this.added = true; }, contains: () => false, remove() {} }, querySelector: () => errorNode };
  const errorNode = { textContent: '' };
  node('#setting-BOAT_TIMEZONE').closest = () => invalidField; node('#setting-BOAT_TIMEZONE').closest = () => invalidField;
  ui.showFieldErrors({ BOAT_TIMEZONE: 'Timezone not recognised. Use a name like Europe/Lisbon' });
  assert.equal(invalidField.classList.added, true); assert.equal(errorNode.textContent, 'Timezone not recognised. Use a name like Europe/Lisbon');

  ui.state.token = 'async-token'; ui.state.config = null; formControls.length = 0;
  ui.lock(true);
  assert.equal(ui.state.token, '', 'lock clears token before late async work can restore state');
  assert.equal(context.sessionStorage.values.vesselstack_control_token, undefined, 'lock clears the session token');
  assert.equal(node('#app').hidden, true, 'lock hides the app and secrets');
  assert.equal(node('#connection-label').textContent, 'Locked');
  console.log('control-panel UI regression checks passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
