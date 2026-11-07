const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appSource = fs.readFileSync('control-panel/static/app.js', 'utf8');
const html = fs.readFileSync('control-panel/static/index.html', 'utf8');
const css = fs.readFileSync('control-panel/static/styles.css', 'utf8');
const source = appSource + '\nthis.__ui = { state, normalizeToken, isConfigDirty, dirtyKeys, currentSettings, lock, setBusy, renderComponents, checkReleases, startOperation, renderOperation, operationSucceeded, refreshStatus, renderReleases, runComponent, runAction, installRelease, discardEdits, toast, fieldMarkup, optionLabel, updateConfigStatus, renderBand, renderUpdates, requireReauth, CONFIRMS, describeOperation, setNotice, showFieldErrors, renderConfiguration, selectTab, firmwareChip, readinessItems, previewModel, previewSvg, themeVars, itemInfo, liveRowsByPath, liveValue, rowBadges, rowValueText, filterCatalogRows, groupCatalogRows, loadDisplays, renderDisplays, renderDataBrowser, displayCard, resetDisplays, onDisplaysClick, h, sv };';

const nodes = new Map();
// A small element tree, enough to build and read back the Displays tab without a browser.
class FakeEl {
  constructor(tag, ns) { this.tagName = tag; this.namespaceURI = ns || null; this.nodeType = 1; this.attrs = {}; this.children = []; this.styleVars = {}; this.listeners = {}; this._text = ''; }
  get dataset() { const out = {}; Object.entries(this.attrs).forEach(([key, value]) => { if (key.startsWith('data-')) out[key.slice(5).replace(/-(\w)/g, (_m, ch) => ch.toUpperCase())] = value; }); return out; }
  get style() { return { setProperty: (name, value) => { this.styleVars[name] = value; } }; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  append(...items) { items.forEach((item) => this.children.push(item)); }
  replaceWith() {}
  closest() { return null; }
  find(predicate, out = []) { if (predicate(this)) out.push(this); this.children.forEach((child) => child.find && child.find(predicate, out)); return out; }
}
class FakeText { constructor(text) { this.nodeType = 3; this.data = String(text); } get textContent() { return this.data; } }
const allText = (element) => element.textContent;
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
    createElement: (tag) => new FakeEl(tag), createElementNS: (ns, tag) => new FakeEl(tag, ns), createTextNode: (text) => new FakeText(text),
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
['Home', 'Settings', 'Displays', 'Updates', 'Maintenance'].forEach((name) => assert.match(html, new RegExp(`role="tab"[^>]*>${name}`)));
assert.equal((html.match(/role="tab"/g) || []).length, 5);
assert.equal((html.match(/id="displays"/g) || []).length, 1, 'one Displays panel');
assert.doesNotMatch(html, /\sstyle=/, 'no inline style attributes (CSP style-src self)');
assert.match(html, /<dialog id="confirm-dialog"/);
assert.match(html, /How do I reach this panel\?/); assert.match(html, /ssh -L 8780:127\.0\.0\.1:8780/); assert.match(html, /id="show-token"/);
assert.match(html, /Contacts github\.com only when you press it/);
assert.match(html, /id="banner" aria-live="polite"/);
assert.doesNotMatch(html, /\bstyle="/, 'CSP forbids inline styles');
// The SVG namespace identifier (never fetched) is the one allowed non-local URL.
assert.doesNotMatch(html + appSource, /https?:\/\/(?!127\.0\.0\.1|github\.com|www\.w3\.org\/2000\/svg")/, 'no external requests');
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
  ui.renderOperation({ id: 5, label: 'backup', state: 'failed', started_at: now - 5, finished_at: now, output: ['x'], summary: { reason: 'backup mount /media/backup is not mounted', fix: 'mount the backup drive at /media/backup and run again, or pass <b>--allow-unmounted</b>. sudo env X=1 vesselstackctl update <i>' } });
  assert.match(node('#banner').innerHTML, /<div class="fix"><strong>To fix:<\/strong> mount the backup drive at \/media\/backup and run again, or pass &lt;b&gt;--allow-unmounted&lt;\/b&gt;\. <span class="mono">sudo env X=1 vesselstackctl update &lt;i&gt;<\/span><\/div>/, 'the fix is shown escaped with the command in monospace');
  assert.doesNotMatch(node('#banner').innerHTML, /<b>|<i>/, 'no injected markup');
  ui.renderOperation({ id: 4, label: 'backup', state: 'succeeded', started_at: now, finished_at: now, output: ['x'], summary: { saved_path: '/media/backup/vesselstack-1.tar.gz' } });
  assert.match(node('#banner').innerHTML, /Backup finished [^<]*<\/strong> — saved to <span class="mono">\/media\/backup\/vesselstack-1\.tar\.gz<\/span>/);
  assert.doesNotMatch(node('#banner').innerHTML, /restart vesselstack-control-panel/, 'no routine restart advice on success');
  ui.renderOperation({ id: 5, label: 'stop grafana', state: 'running', started_at: now, output: [] });
  assert.match(node('#banner').innerHTML, /Stopping Grafana…/); assert.equal(ui.state.busy, true);
  ui.renderOperation({ id: 5, label: 'stop grafana', state: 'succeeded', started_at: now, finished_at: now, output: [] });
  assert.match(node('#banner').innerHTML, /Grafana stopped [^<]*</); assert.equal(ui.state.busy, false);
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
  // Numeric settings render as text inputs so a non-numeric saved value is shown as stored and is not an edit.
  const lengthMarkup = ui.fieldMarkup({ key: 'BOAT_LOA_M', label: 'Length overall (m)', kind: 'number', description: 'x', read_only: false }, '12m');
  assert.match(lengthMarkup, /type="text"/); assert.match(lengthMarkup, /inputmode="decimal"/); assert.match(lengthMarkup, /value="12m"/); assert.doesNotMatch(lengthMarkup, /type="number"/);
  assert.match(ui.fieldMarkup({ key: 'CONTROL_PANEL_PORT', label: 'Port', kind: 'port', description: 'x', read_only: false }, '8780'), /type="text" [^>]*value="8780"/);
  formControls.length = 0; control('BOAT_LOA_M', '12m'); ui.state.config = { fields: [{ key: 'BOAT_LOA_M', kind: 'number', read_only: false }], values: { BOAT_LOA_M: '12m' } };
  assert.deepEqual(ui.dirtyKeys(), [], 'an untouched non-numeric saved length is not counted as edited');
  formControls.length = 0; control('BOAT_LOA_M', '13');
  assert.deepEqual(ui.dirtyKeys(), ['BOAT_LOA_M'], 'changing the value is an edit');
  formControls.length = 0; ui.state.config = null;

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

  {
  // ---- Displays tab: pure helpers and DOM builders -------------------------------------
  const deq = (actual, expected) => assert.deepEqual(JSON.parse(JSON.stringify(actual)), JSON.parse(JSON.stringify(expected))); // the app runs in another realm
  const fixture = JSON.parse(fs.readFileSync('tests/fixtures/displays/panel_show.json', 'utf8'));
  const themesJson = JSON.parse(fs.readFileSync('displays/themes.json', 'utf8'));
  const ready = (extra = {}) => ({ displays_enabled: true, secrets_readable: true, mqtt_password_stored: true, wifi_stored: true, data_bind_non_loopback: true, data_bind_hint: 'HINT-TEXT', docker_available: true, esphome_image_present: true, toolchain_cache_present: true, free_disk_bytes: 5e10, min_free_bytes_first_build: 3221225472, min_free_bytes_warm_build: 1073741824, firmware: { status: 'current', built_at_utc: 'T1', esphome_version: 'x', reasons: [], credentials_checked: true, has_previous: false }, last_flash: { at: 'T2', method: 'serial', ok: true, firmware_built_at: 'T1' }, ...extra });
  const chip = (extra) => ui.firmwareChip(ready(extra));
  deq([chip().state, chip().label], ['on-screen', 'On screen']);
  assert.equal(chip({ last_flash: null }).label, 'Never flashed');
  assert.equal(chip({ last_flash: { ok: false, firmware_built_at: 'T1', error: 'x' } }).state, 'flash-failed');
  assert.equal(chip({ last_flash: { ok: true, firmware_built_at: 'OLD' } }).label, 'Changes not on screen', 'a flash of an older build is not on screen');
  assert.equal(chip({ firmware: { status: 'outdated', built_at_utc: 'T1' } }).label, 'Changes not on screen');
  assert.equal(chip({ firmware: { status: 'none', built_at_utc: null }, last_flash: null }).label, 'Not built');
  assert.equal(chip({ firmware: { status: 'unknown' } }).label, 'Unknown');
  assert.equal(ui.firmwareChip(null).state, 'unknown');

  const todo = (items) => items.filter((item) => item.state === 'todo').map((item) => item.key);
  deq(todo(ui.readinessItems('helm', ready())), []);
  const loop = ui.readinessItems('helm', ready({ data_bind_non_loopback: false })).find((item) => item.key === 'network');
  assert.equal(loop.state, 'todo'); assert.equal(loop.hint, 'HINT-TEXT'); assert.equal(loop.command, 'sudo vesselstackctl lan all --yes');
  const noWifi = ui.readinessItems('helm', ready({ wifi_stored: false })).find((item) => item.key === 'wifi');
  assert.equal(noWifi.command, 'sudo vesselstackctl display wifi');
  assert.equal(ui.readinessItems('helm', ready({ mqtt_password_stored: false })).find((item) => item.key === 'mqtt').command, 'sudo vesselstackctl display rotate helm');
  deq(todo(ui.readinessItems('helm', ready({ displays_enabled: false }))), ['enabled']);
  assert.ok(ui.readinessItems('helm', ready({ secrets_readable: false })).filter((item) => ['mqtt', 'wifi'].includes(item.key)).every((item) => item.state === 'unknown'));
  assert.ok(todo(ui.readinessItems('helm', ready({ docker_available: false }))).includes('docker'));
  assert.ok(todo(ui.readinessItems('helm', ready({ free_disk_bytes: 1e8 }))).includes('disk'));
  assert.ok(!todo(ui.readinessItems('helm', ready({ free_disk_bytes: 2e9, toolchain_cache_present: true }))).includes('disk'), 'a warm build needs less space');
  assert.ok(todo(ui.readinessItems('helm', ready({ free_disk_bytes: 2e9, toolchain_cache_present: false }))).includes('disk'));

  // Preview model: the boxes come straight from `show --json`; colours come from the theme.
  deq(fixture.preview.map((item) => item.layout), ['grid-2x2', 'status-grid', 'tank-bars', 'gauge-arc', 'electrical']);
  const live = ui.liveRowsByPath({ rows: [{ path: fixture.instruments.sog.path, live_state: 'live', value: 6.44, convert: fixture.instruments.sog.convert }, { path: fixture.instruments.depth.path, live_state: 'live', value: 9, convert: 'other-conversion' }, { path: 'x.y', live_state: 'no_live_data' }] });
  const grid = ui.previewModel(fixture, 0, themesJson, { mode: 'day', severity: 'plain' }, live);
  assert.equal(grid.round, true); deq(grid.cells.map((cell) => cell.box), fixture.preview[0].cells);
  deq(grid.header, fixture.preview[0].header); assert.equal(grid.footer.pages, 5);
  assert.equal(grid.cells[0].label, 'SOG'); assert.equal(grid.cells[0].value, '6.4', 'live value uses the item precision');
  assert.equal(grid.cells[1].value, '—', 'a value converted differently is not shown as this item');
  assert.equal(grid.cells[2].value, '—', 'no live data gives a placeholder');
  assert.equal(ui.previewModel(fixture, 0, themesJson, {}, null).cells[0].value, '—');
  const status = ui.previewModel(fixture, 1, themesJson, {});
  assert.equal(status.cells.length, 9); assert.equal(status.cells[0].cellKind, 'status'); assert.equal(status.cells[0].valueSize, 30, 'three-column status grid uses the small font');
  assert.equal(ui.previewModel(fixture, 2, themesJson, {}).cells[0].cellKind, 'bar');
  const gauge = ui.previewModel(fixture, 3, themesJson, {}); assert.equal(gauge.arc.sweep, 270); assert.equal(gauge.cells[0].cellKind, 'gauge');
  const elec = ui.previewModel(fixture, 4, themesJson, {}); assert.equal(elec.cells.length, 0); assert.equal(elec.rows.length, 5); assert.equal(elec.rows[0].role, 'voltage'); deq(elec.textBox, fixture.preview[4].text_box);
  assert.equal(ui.previewModel(fixture, 9, themesJson, {}), null, 'an unknown page has no preview');
  assert.equal(ui.previewModel(fixture, 0, null, {}), null, 'no theme data, no preview');
  const classic = themesJson.themes.classic;
  const day = ui.themeVars(classic, 'day', 'plain'); const night = ui.themeVars(classic, 'night', 'plain');
  assert.equal(day['--pv-page'], classic.day.page_bg); assert.equal(night['--pv-page'], classic.night.page_bg); assert.notEqual(ui.themeVars(themesJson.themes.daylight, 'day', 'plain')['--pv-page'], ui.themeVars(themesJson.themes.daylight, 'night', 'plain')['--pv-page'], 'night repaints a light theme');
  assert.equal(ui.themeVars(classic, 'day', 'alarm')['--pv-tile'], classic.sev.day.alarm[0]);
  assert.equal(ui.themeVars(classic, 'day', 'stale')['--pv-tile'], classic.sev.day.unknown[0], 'stale uses the unknown colours');
  assert.equal(ui.themeVars(classic, 'day', 'warn')['--pv-gauge'], classic.gauge.day.warn);
  assert.equal(ui.themeVars(themesJson.themes['high-contrast'], 'day', 'plain')['--pv-border'] !== 'transparent', true, 'a themed tile border shows');
  assert.equal(ui.themeVars(classic, 'day', 'plain')['--pv-border'], 'transparent');
  const ocean = ui.previewModel(fixture, 0, themesJson, { theme: 'ocean' });
  assert.equal(ocean.vars['--pv-tile'], themesJson.themes.ocean.day.tile);
  const svg = ui.previewSvg(grid);
  assert.equal(svg.attrs.viewBox, '0 0 480 480'); assert.match(svg.attrs['aria-label'], /Approximate preview/);
  assert.equal(svg.find((el) => el.tagName === 'text').length > 0, true);
  assert.equal(svg.find((el) => el.tagName === 'clipPath').length, 1, 'a round screen is clipped to a circle');
  assert.equal(svg.find((el) => el.attrs && 'style' in el.attrs).length, 0, 'no inline style attributes');
  [3, 4, 2, 1].forEach((index) => assert.ok(ui.previewSvg(ui.previewModel(fixture, index, themesJson, { mode: 'night', severity: 'alarm' }, live)).find((el) => el.tagName === 'path' || el.tagName === 'rect').length > 0));

  // Badges, value text, search and grouping.
  const names = (id) => ({ helm: 'Helm' }[id] || id);
  deq(ui.rowBadges({ live_state: 'live', group: 'Navigation', used_on: ['helm'] }, names).map((b) => b.text), ['Live', 'Used on Helm']);
  deq(ui.rowBadges({ live_state: 'no_live_data', group: 'Navigation', used_on: [] }).map((b) => b.key), ['nolive']);
  deq(ui.rowBadges({ live_state: 'unknown', group: 'Navigation', used_on: [] }), [], 'live data was not asked for: no live badge');
  deq(ui.rowBadges({ live_state: 'live', group: 'Uncatalogued', row_id: 'unc:a.b', used_on: [] }).map((b) => b.key), ['live', 'uncatalogued']);
  assert.equal(ui.rowValueText({ live_state: 'live', value: 6.4, unit: 'kn' }), '6.4 kn'); assert.equal(ui.rowValueText({ live_state: 'no_live_data', value: 6.4, unit: 'kn' }), '');
  const rows = [{ label: 'SOG', group: 'Navigation', path: 'navigation.speedOverGround' }, { label: 'House', group: 'Electrical', path: 'electrical.batteries.house.voltage' }, { label: 'Depth', group: 'Navigation', path: 'environment.depth' }];
  assert.equal(ui.filterCatalogRows(rows, 'sog').length, 1); assert.equal(ui.filterCatalogRows(rows, 'ELECTRICAL').length, 1); assert.equal(ui.filterCatalogRows(rows, 'depth')[0].label, 'Depth'); assert.equal(ui.filterCatalogRows(rows, '  ').length, 3);
  deq(ui.groupCatalogRows(rows, ['Electrical', 'Navigation', 'Empty']).map((g) => [g.name, g.rows.length]), [['Electrical', 1], ['Navigation', 2]]);

  // Text from the boat configuration is only ever text: a hostile label creates no elements.
  const hostile = JSON.parse(JSON.stringify(fixture)); hostile.instruments.sog.label = '<img src=x onerror=alert(1)>'; hostile.display.name = '"><script>x</script>';
  hostile.readiness = ready({ data_bind_non_loopback: false, data_bind_hint: '<b>hint</b>' }); hostile.display.pages[0].title = '<i>t</i>';
  const hostileCard = ui.displayCard(hostile);
  const cardText = allText(hostileCard);
  assert.ok(cardText.includes('<img src=x onerror=alert(1)>') && cardText.includes('<b>hint</b>') && cardText.includes('"><script>x</script>'), 'hostile text is shown literally');
  assert.equal(hostileCard.find((el) => ['img', 'script', 'b'].includes(el.tagName) || (el.tagName === 'i' && el.textContent === 't')).length, 0, 'and never becomes markup');
  assert.equal(hostileCard.find((el) => el.attrs && ('style' in el.attrs || 'onerror' in el.attrs)).length, 0);
  assert.ok(hostileCard.find((el) => el.tagName === 'button' && el.attrs['data-theme-id']).every((el) => 'disabled' in el.attrs), 'theme swatches are read-only in this version');
  const current = hostileCard.find((el) => el.attrs && el.attrs['data-theme-id'] === 'classic')[0];
  assert.match(current.attrs['aria-label'], /Classic theme, current/); assert.equal(current.attrs['aria-pressed'], 'true');
  deq(hostileCard.find((el) => el.tagName === 'button' && el.attrs['data-theme-id']).length, 4);

  // Loading: fixed endpoints, rendering from the responses, session and lock handling.
  ['#displays-root', '#displays-status', '#catalog-root', '#catalog-note', '#displays-updated'].forEach((id) => nodes.set(id, new FakeEl('div')));
  const calls = [];
  const shown = { ...fixture, readiness: ready({ firmware: { status: 'outdated', built_at_utc: 'T1' } }), revision: 'r' };
  const catalogPayload = { schema: 1, live: { requested: true, ok: true }, groups: ['Navigation', 'Uncatalogued'], rows: [{ label: 'SOG', group: 'Navigation', path: 'navigation.speedOverGround', live_state: 'live', value: 6.4, unit: 'kn', used_on: ['helm'], convert: 'm/s->kn', row_id: 'sog' }, { label: 'Odd', group: 'Uncatalogued', row_id: 'unc:odd', path: 'odd.path', live_state: 'live', used_on: [] }] };
  const routes = { '/api/displays': { schema: 1, displays: [shown, { id: 'aux', name: 'Aux', board: 'b', error: 'could not be read' }] }, '/api/displays/themes': themesJson, '/api/displays/catalog?live=1': catalogPayload };
  fetchImpl = async (path, options) => { calls.push([path, options.headers['X-VesselStack-Token']]); return ok(routes[path]); };
  ui.state.token = 'tok'; ui.resetDisplays();
  await ui.loadDisplays(true);
  deq(calls, [['/api/displays', 'tok'], ['/api/displays/themes', 'tok'], ['/api/displays/catalog?live=1', 'tok']]);
  assert.equal(ui.state.displays.loaded, true); assert.equal(ui.state.displays.list.length, 2);
  const rootText = allText(node('#displays-root'));
  assert.match(rootText, /Helm/); assert.match(rootText, /Changes not on screen/); assert.match(rootText, /could not be read/); assert.match(rootText, /Approximate preview/);
  assert.match(rootText, /sudo vesselstackctl display build helm/);
  const browserText = allText(node('#catalog-root'));
  assert.match(browserText, /SOG/); assert.match(browserText, /6\.4 kn/); assert.match(browserText, /Used on Helm/); assert.match(browserText, /Uncatalogued/);
  assert.equal(calls.length, 3); await ui.loadDisplays(false); assert.equal(calls.length, 3, 'a fresh tab is not reloaded');
  ui.state.displays.query = 'zzz'; ui.renderDataBrowser(); assert.match(allText(node('#catalog-root')), /Nothing matches/);
  // Preview toggles and page choice go through the delegated handler.
  const clickOn = (attrs) => ui.onDisplaysClick({ target: { closest: () => ({ dataset: attrs, id: attrs.id || '' }) } });
  clickOn({ previewMode: 'night' }); assert.equal(ui.state.displays.mode, 'night');
  clickOn({ previewSeverity: 'alarm' }); assert.equal(ui.state.displays.severity, 'alarm');
  clickOn({ previewPage: '2', display: 'helm' }); assert.equal(ui.state.displays.page.helm, 2);
  // A failing catalog does not hide the displays; a failing list shows one plain message.
  fetchImpl = async (path) => path === '/api/displays/catalog?live=1' ? ({ ok: false, status: 504, statusText: 'x', json: async () => ({ error: 'too long' }) }) : ok(routes[path]);
  await ui.loadDisplays(true); assert.equal(ui.state.displays.list.length, 2); assert.match(ui.state.displays.error, /data list could not be loaded: too long/);
  fetchImpl = async () => ({ ok: false, status: 502, statusText: 'Bad Gateway', json: async () => ({ error: 'The display tool is not available on this boat computer.' }) });
  await ui.loadDisplays(true); assert.match(ui.state.displays.error, /Couldn't load the displays: The display tool is not available/);
  assert.match(allText(node('#displays-status')), /not available/);
  // A response that arrives after Lock is dropped, and Lock forgets everything.
  const releases = []; fetchImpl = (path) => new Promise((resolve) => { releases.push(() => resolve(ok(routes[path]))); });
  ui.state.token = 'tok2'; ui.resetDisplays();
  const pending = ui.loadDisplays(true);
  ui.lock(true); releases.forEach((release) => release()); await pending; await tick();
  assert.equal(ui.state.displays.list.length, 0); assert.equal(ui.state.displays.loaded, false); assert.equal(allText(node('#displays-root')), '');
  fetchImpl = async () => { throw new Error('no network for the tab test'); };
  ui.state.token = 'tok3'; ui.selectTab('displays'); await tick();
  assert.equal(ui.state.displays.loading, false);
  ui.lock(true);
  }
  console.log('control-panel UI regression checks passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
