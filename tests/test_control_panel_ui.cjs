const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appSource = fs.readFileSync('control-panel/static/app.js', 'utf8');
const html = fs.readFileSync('control-panel/static/index.html', 'utf8');
const css = fs.readFileSync('control-panel/static/styles.css', 'utf8');
const source = appSource + '\nthis.__ui = { state, normalizeToken, isConfigDirty, dirtyKeys, currentSettings, lock, setBusy, renderComponents, checkReleases, startOperation, renderOperation, operationSucceeded, refreshStatus, renderReleases, runComponent, runAction, installRelease, discardEdits, toast, fieldMarkup, optionLabel, updateConfigStatus, renderBand, renderUpdates, requireReauth, CONFIRMS, describeOperation, askBackupChoice, confirmWithBackup, backupOptions, setNotice, showFieldErrors, renderConfiguration, selectTab, firmwareChip, readinessItems, previewModel, newDraft, draftDirty, draftProblems, titleProblem, addCheck, addRowToDraft, removeItem, removePage, addPage, moveWithin, savePayload, draftShow, geometryFor, otaTargetProblem, serialProblem, uploadRequest, isNewOtaTarget, parseStages, applyBlock, defaultOtaTarget, defaultMethod, deployPanel, saveDraft, startEditing, stopEditing, applyToScreen, runDisplayAction, onDisplaysInput, openApplyDialog, renderHistory, loadHistory, restoreBackup, formatStamp, pageEditor, saveBar, rowAddControls, entryById, displaysDirty, layoutChoices, previewSvg, themeVars, itemInfo, liveRowsByPath, liveValue, rowBadges, rowValueText, filterCatalogRows, groupCatalogRows, loadDisplays, renderDisplays, renderDataBrowser, displayCard, resetDisplays, onDisplaysClick, h, sv };';
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
const backupDialog = node('#backup-choice-dialog');
const backupShown = [];
let backupDriver = null;
backupDialog.showModal = function showModal() {
  this.open = true; this.returnValue = '';
  backupShown.push({ title: node('#backup-choice-title').textContent, intro: node('#backup-choice-intro').textContent, options: node('#backup-choice-options').innerHTML, notes: node('#backup-choice-notes').innerHTML, ok: node('#backup-choice-ok').textContent, okClass: node('#backup-choice-ok').className });
  setImmediate(async () => { if (backupDriver) await backupDriver(); else backupDialog.close('cancel'); });
};
backupDialog.close = function close(value) { this.returnValue = value; this.open = false; if (this.onclose) this.onclose(); };
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
assert.match(html, /<dialog id="backup-choice-dialog"/); assert.match(html, /id="backup-choice-ack"/);
assert.match(html, /I understand there is no backup|backup-choice-ack-text/);
assert.match(appSource, /I understand there is no backup to restore if this update fails/);
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
  // ---- backup choice: the normal dialog when the backup works, a choice dialog when it does not ----------
  {
    const pre = (extra = {}) => ({ path: '/media/backup-drive/backups', mount: 'not_mounted', default_ok: false, fstab_mount: false, drives: [], refused: [], mounted_elsewhere: [],
      system_disk_once: { offered: true, reason: '', target: '/var/backups/vesselstack' }, skip: { offered: true }, ...extra });
    const drive = { uuid: 'AB12-CD34', label: 'BACKUP <b>', size: '58G', fstype: 'ext4', device: '/dev/sda1' };
    const originalFetch = fetchImpl;
    let preflights = [];
    const configPosts = [];
    const useBackupFetch = () => {
      fetchImpl = async (path, options = {}) => {
        if (path === '/api/backup-preflight') { if (preflights.length === 0) throw new Error('offline'); const next = preflights.length > 1 ? preflights.shift() : preflights[0]; if (next instanceof Error) throw next; return ok(next); }
        if (path === '/api/config' && options.method === 'POST') { configPosts.push(JSON.parse(options.body)); return ok({ configuration: ui.state.config }); }
        if (options.method === 'POST') { posts.push({ path, body: JSON.parse(options.body) }); return ok({ id: 1, label: 'x', state: 'running', output: [] }); }
        return ok({ components: [], operation: { state: 'idle' }, installed_version: '0.6.0', running_version: '0.6.0' });
      };
    };
    const reset = () => { ui.state.busy = false; ui.state.pendingOperation = null; posts.length = 0; backupShown.length = 0; dialogTitles.length = 0; configPosts.length = 0; node('#backup-choice-ack').checked = false; node('#backup-choice-path').value = ''; };
    const pick = (value) => node('#backup-choice-options').onchange({ target: { value } });
    useBackupFetch();

    // The backup works: today's confirmation, nothing else, and no backup field in the request.
    reset(); preflights = [pre({ default_ok: true, mount: 'mounted' })]; dialogAnswer = 'ok';
    await ui.installRelease(9); await tick();
    assert.equal(backupShown.length, 0, 'a working backup never shows the choice dialog');
    assert.equal(dialogTitles[0].title, 'Back up & install 0.6.1?');
    assert.deepEqual(posts[0].body, { action: 'update-release', release_id: 9, channel: ui.state.releaseChannel }, 'the default request is unchanged');
    // A failed lookup falls back to the same confirmation (the server still requires a mounted backup).
    reset(); preflights = [];
    await ui.installRelease(9); await tick();
    assert.equal(backupShown.length, 0); assert.equal(dialogTitles[0].title, 'Back up & install 0.6.1?');
    assert.equal(Object.hasOwn(posts[0].body, 'backup'), false);

    // Not mounted: the choice dialog lists drives, refused and already-mounted ones, and escapes names.
    const listing = pre({ fstab_mount: true, drives: [drive],
      refused: [{ device: '/dev/sdb1', label: 'CAMERA', size: '29G', fstype: 'vfat', reason: 'vfat is not supported for backups' }],
      mounted_elsewhere: [{ device: '/dev/sde1', label: '', size: '16G', fstype: 'ext4', mountpoint: '/media/pi/USBSTICK' }] });
    reset(); preflights = [listing]; backupDriver = async () => { backupDialog.close('cancel'); };
    await ui.installRelease(9); await tick();
    assert.equal(dialogTitles.length, 0, 'the usual confirmation is replaced by the choice dialog'); assert.equal(backupShown.length, 1); assert.equal(posts.length, 0, 'cancel starts nothing');
    const shown = backupShown[0];
    assert.equal(shown.title, 'Back up & install 0.6.1?'); assert.match(shown.intro, /not mounted \(\/media\/backup-drive\)/);
    assert.match(shown.options, /Mount the backup drive now/); assert.match(shown.options, /Mount &quot;BACKUP &lt;b&gt;&quot; \(58G, ext4\)/); assert.doesNotMatch(shown.options, /<b>/, 'drive names are escaped');
    assert.match(shown.options, /check again/); assert.match(shown.options, /system disk this once \(not recommended\)/); assert.match(shown.options, /different backup folder/); assert.match(shown.options, /Install without a backup/);
    assert.match(shown.options, /value="fstab" checked/, 'the first safe choice is preselected, never the skip');
    assert.match(shown.notes, /\/dev\/sdb1 29G &quot;CAMERA&quot; vfat: vfat is not supported/); assert.match(shown.notes, /already mounted at \/media\/pi\/USBSTICK/);

    // Each choice posts exactly its backup object.
    const choose = async (value, action = 'update-release') => {
      reset(); preflights = [listing];
      backupDriver = async () => { pick(value); await node('#backup-choice-ok').onclick(); };
      if (action === 'update') await ui.runAction('update', { source_directory: '/var/lib/vesselstack/releases/x' }); else await ui.installRelease(9);
      await tick();
      return posts.at(-1)?.body;
    };
    assert.deepEqual((await choose('fstab')).backup, { mode: 'mount_fstab' });
    assert.deepEqual((await choose('drive:AB12-CD34')).backup, { mode: 'mount_drive', uuid: 'AB12-CD34' });
    assert.deepEqual((await choose('system')).backup, { mode: 'system_disk_once', confirm: 'system-disk-once' });
    const folderUpdate = await choose('drive:AB12-CD34', 'update');
    assert.deepEqual(folderUpdate, { action: 'update', source_directory: '/var/lib/vesselstack/releases/x', backup: { mode: 'mount_drive', uuid: 'AB12-CD34' } }, 'update from a folder offers the same choices');

    // Skipping needs the checkbox and uses the danger button.
    reset(); preflights = [listing]; let blocked;
    backupDriver = async () => {
      pick('skip');
      assert.equal(node('#backup-choice-skip-ack').hidden, false); assert.equal(node('#backup-choice-ok').disabled, true, 'the button is off until the box is ticked');
      assert.equal(node('#backup-choice-ok').className, 'danger solid'); assert.equal(node('#backup-choice-ok').textContent, 'Install without a backup');
      assert.equal(node('#backup-choice-ack-text').textContent, 'I understand there is no backup to restore if this update fails');
      await node('#backup-choice-ok').onclick(); blocked = backupDialog.open;
      node('#backup-choice-ack').checked = true; node('#backup-choice-ack').onchange();
      assert.equal(node('#backup-choice-ok').disabled, false);
      await node('#backup-choice-ok').onclick();
    };
    await ui.installRelease(9); await tick();
    assert.equal(blocked, true, 'pressing the disabled button does nothing');
    assert.deepEqual(posts.at(-1).body.backup, { mode: 'skip', confirm: 'install-without-backup' });
    pick('fstab'); assert.equal(node('#backup-choice-skip-ack').hidden, true, 'the checkbox only shows for the skip choice');

    // Turned off by the lock: neither the system disk nor the skip is offered, and the reason is shown.
    reset(); preflights = [pre({ drives: [drive], system_disk_once: { offered: false, reason: 'Turned off: VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false is set in the configuration.', target: '' }, skip: { offered: false } })];
    backupDriver = async () => { backupDialog.close('cancel'); };
    await ui.installRelease(9); await tick();
    assert.doesNotMatch(backupShown[0].options, /system disk this once|Install without a backup/); assert.match(backupShown[0].notes, /ALLOW_UNMOUNTED=false/);

    // "Check again": a drive that turned up means the normal backup, so no backup field is sent.
    reset(); preflights = [pre(), pre({ default_ok: true, mount: 'mounted' })];
    backupDriver = async () => { pick('recheck'); await node('#backup-choice-ok').onclick(); };
    await ui.installRelease(9); await tick();
    assert.equal(Object.hasOwn(posts.at(-1).body, 'backup'), false);
    // ... and a drive that is still missing keeps the dialog open with a refreshed list.
    reset(); preflights = [pre(), pre({ drives: [drive] })];
    backupDriver = async () => { pick('recheck'); await node('#backup-choice-ok').onclick(); assert.match(node('#backup-choice-options').innerHTML, /AB12-CD34/); backupDialog.close('cancel'); };
    await ui.installRelease(9); await tick();
    assert.equal(posts.length, 0);

    // A different folder is saved through the settings call, then the check runs again.
    reset(); preflights = [pre(), pre({ default_ok: true, mount: 'mounted', path: '/media/other/backups' })];
    backupDriver = async () => { pick('folder'); assert.equal(node('#backup-choice-ok').disabled, true, 'an empty folder cannot be saved'); node('#backup-choice-path').value = '/media/other/backups'; node('#backup-choice-path').oninput(); await node('#backup-choice-ok').onclick(); };
    await ui.installRelease(9); await tick();
    assert.deepEqual(configPosts, [{ settings: { VESSELSTACK_BACKUP: '/media/other/backups' } }]);
    assert.equal(Object.hasOwn(posts.at(-1).body, 'backup'), false);
    backupDriver = null; dialogAnswer = 'cancel'; preflights = [];
    fetchImpl = originalFetch;
    reset();
  }
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
    { id: 'homepage', label: 'Home page', description: 'Start page', section: 'Services & ports', manageable: true, enabled: true, state: 'exited', status: 'stopped' },
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

  assert.match(ui.describeOperation({ label: 'update-release', target_version: '0.6.2', backup_skipped: true }).running, /without a backup/);
  assert.match(ui.describeOperation({ label: 'update-release', target_version: '0.6.2', backup_mode: 'skip' }).done, /no backup was made/);
  assert.doesNotMatch(JSON.stringify(ui.describeOperation({ label: 'update-release', target_version: '0.6.2', backup_skipped: true })), /backed up first/, 'a skipped backup is never described as made');
  assert.match(ui.describeOperation({ label: 'update', backup_skipped: true }).running, /without a backup/);
  assert.match(ui.describeOperation({ label: 'update-release', target_version: '0.6.2' }).running, /Backing up, then installing 0\.6\.2/);

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
  const ready = (extra = {}) => ({ displays_enabled: true, secrets_readable: true, mqtt_password_stored: true, wifi_stored: true, data_bind_non_loopback: true, data_bind_hint: 'HINT-TEXT', docker_available: true, esphome_image_present: true, toolchain_cache_present: true, free_disk_bytes: 5e10, min_free_bytes_first_build: 3221225472, min_free_bytes_warm_build: 1073741824, firmware: { status: 'current', built_at_utc: 'T1', esphome_version: 'x', reasons: [], credentials_checked: true, has_previous: false }, last_flash: { at: 'T2', method: 'serial', ok: true, built_at_utc: 'T1', ota_target: null, credentials_changed: false }, ...extra });
  const chip = (extra) => ui.firmwareChip(ready(extra));
  deq([chip().state, chip().label], ['on-screen', 'On screen']);
  assert.equal(chip({ last_flash: null }).label, 'Never flashed');
  assert.equal(chip({ last_flash: { ok: false, built_at_utc: 'T1' } }).state, 'flash-failed');
  assert.equal(chip({ last_flash: { ok: true, built_at_utc: 'OLD' } }).label, 'Changes not on screen', 'a flash of an older build is not on screen');
  assert.equal(chip({ last_flash: { ok: true, built_at_utc: 'OLD', matches_settings: true } }).label, 'On screen', 'what the screen runs matches the settings (e.g. after a rollback)');
  assert.equal(chip({ firmware: { status: 'outdated', built_at_utc: 'NEW' }, last_flash: { ok: true, built_at_utc: 'OLD', matches_settings: true } }).label, 'On screen');
  assert.equal(chip({ last_flash: { ok: true, built_at_utc: 'T1', matches_settings: false } }).label, 'Changes not on screen');
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
  assert.ok(hostileCard.find((el) => el.tagName === 'button' && el.attrs['data-theme-id']).every((el) => 'disabled' in el.attrs), 'theme swatches are read-only until Edit is pressed');
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
  assert.match(rootText, /Edit theme and pages/); assert.match(rootText, /Apply to screen/);
  const browserText = allText(node('#catalog-root'));
  assert.match(browserText, /SOG/); assert.match(browserText, /6\.4 kn/); assert.match(browserText, /Used on Helm/); assert.match(browserText, /Uncatalogued/);
  assert.equal(calls.length, 3); await ui.loadDisplays(false); assert.equal(calls.length, 3, 'a fresh tab is not reloaded');
  ui.state.displays.query = 'zzz'; ui.renderDataBrowser(); assert.match(allText(node('#catalog-root')), /Nothing matches/);
  // Preview toggles and page choice go through the delegated handler.
  const clickOn = (attrs) => ui.onDisplaysClick({ target: { closest: () => ({ dataset: attrs, id: attrs.id || '' }) } });
  clickOn({ previewMode: 'night' }); assert.equal(ui.state.displays.mode, 'night');
  clickOn({ previewSeverity: 'alarm' }); assert.equal(ui.state.displays.severity, 'alarm');
  clickOn({ previewPage: '2', display: 'helm' }); assert.equal(ui.state.displays.page.helm, 2);
  // ---- Displays tab: editing, adding data, saving and applying ------------------------------------------------
  {
    const REV = 'a'.repeat(64);
    const entry = () => JSON.parse(JSON.stringify({ ...fixture, readiness: ready(), revision: REV }));
    const row = (extra) => ({ kind: 'instrument', row_id: 'stw', label: 'STW', path: 'navigation.speedThroughWater', group: 'Navigation', convert: 'm/s->kn', unit: 'kn', precision: 1, layouts: ['single', 'grid-2x2', 'status-grid'], roles: [], proposed_id: 'stw', id_stem: 'stw', configured_as: null, used_on: [], template: false,
      definition: { path: 'navigation.speedThroughWater', label: 'STW', convert: 'm/s->kn', unit: 'kn', precision: 1, stale_after_s: 10 }, ...extra });
    const note = (extra) => ({ kind: 'notification', row_id: 'smoke', label: 'Smoke', path: 'notifications.smoke', group: 'Safety', layouts: ['status-grid', 'electrical'], roles: [], proposed_id: 'smoke', id_stem: 'smoke', configured_as: null, used_on: [], template: false,
      definition: { path: 'notifications.smoke', label: 'Smoke', alarm: false }, ...extra });

    // Draft and dirty tracking.
    const e1 = entry(); const draft = ui.newDraft(e1);
    assert.equal(ui.draftDirty(e1, draft), false); assert.equal(ui.draftDirty(e1, null), false);
    deq(draft.pages.map((p) => p.layout), ['grid-2x2', 'status-grid', 'tank-bars', 'gauge-arc', 'electrical']);
    draft.theme = 'ocean'; assert.equal(ui.draftDirty(e1, draft), true); draft.theme = 'classic'; assert.equal(ui.draftDirty(e1, draft), false, 'putting it back is clean again');
    draft.pages[0].title = 'Nav2'; assert.equal(ui.draftDirty(e1, draft), true); draft.pages[0].title = 'Nav'; assert.equal(ui.draftDirty(e1, draft), false);
    draft.pages[0].items.reverse(); assert.equal(ui.draftDirty(e1, draft), true); draft.pages[0].items.reverse();
    draft.pages[0].items.push('x'); ui.removeItem(draft, 0, 4); assert.equal(ui.draftDirty(e1, draft), false);
    assert.equal(e1.display.pages[0].items.length, 4, 'editing the draft never touches the loaded display');

    // The preview follows the draft: theme, and pages whose geometry is known.
    const themed = ui.draftShow(e1, { ...draft, theme: 'ocean' });
    assert.equal(ui.previewModel(themed, 0, themesJson, { theme: 'ocean' }).vars['--pv-tile'], themesJson.themes.ocean.day.tile);
    assert.equal(ui.geometryFor(e1, { layout: 'grid-2x2', items: ['a'] }, 0).layout, 'grid-2x2', 'a grid keeps its cell boxes whatever the number of values');
    assert.equal(ui.geometryFor(e1, { layout: 'status-grid', items: ['a', 'b'] }, 7), null, 'a layout with a new number of values has no geometry until it is saved');
    deq(ui.layoutChoices(e1), ['single', 'grid-2x2', 'status-grid', 'tank-bars', 'gauge-arc', 'electrical']);
    deq(ui.layoutChoices({ ...e1, board: { layouts: ['single', 'grid-2x2'] } }), ['single', 'grid-2x2'], 'only layouts of the board');

    // Titles.
    assert.match(ui.titleProblem(''), /name/); assert.match(ui.titleProblem('x'.repeat(17)), /16/); assert.match(ui.titleProblem('a\u0007b'), /ordinary/); assert.equal(ui.titleProblem('Nav 2'), '');

    // Add-to-page compatibility rules, with a reason for every refusal.
    const d1 = ui.newDraft(e1); d1.selected = 0;
    assert.match(ui.addCheck(e1, d1, row()).reason, /full \(4 values\)/, 'capacity');
    ui.removeItem(d1, 0, 3); assert.equal(ui.addCheck(e1, d1, row()).ok, true);
    assert.match(ui.addCheck(e1, d1, row({ configured_as: 'sog' })).reason, /Already on this page/);
    d1.selected = 2; assert.match(ui.addCheck(e1, d1, note()).reason, /Cannot be shown on a Tank bars page/);
    assert.match(ui.addCheck(e1, d1, row()).reason, /minimum and maximum/, 'a value with no range cannot go on a bar');
    ui.removeItem(d1, 2, 2); assert.equal(ui.addCheck(e1, d1, row({ layouts: ['tank-bars'], min: 0, max: 100 })).ok, true);
    d1.selected = 4; assert.match(ui.addCheck(e1, d1, row({ layouts: ['electrical'], roles: ['voltage'] })).reason, /full/); assert.match(ui.addCheck(e1, d1, row()).reason, /Cannot be shown on a Electrical page/);
    ui.removeItem(d1, 4, 4); ui.removeItem(d1, 4, 3);
    assert.match(ui.addCheck(e1, d1, row({ layouts: ['electrical'], roles: ['voltage'] })).reason, /next slot is “shore”/, 'electrical slots keep their roles');
    assert.equal(ui.addCheck(e1, d1, note()).ok, true, 'a notification fits the shore slot');
    assert.equal(ui.addCheck(e1, d1, row({ layouts: ['electrical'], roles: ['shore'] })).ok, true);
    assert.match(ui.addCheck({ ...e1, layouts: {} }, d1, row()).reason, /cannot show/);
    assert.match(ui.addCheck(e1, { pages: [], selected: 0 }, row()).reason, /Choose a page/);

    // Adding: ids, definitions, opt-in alarms and ranges.
    const d2 = ui.newDraft(e1); d2.selected = 1; ui.removeItem(d2, 1, 8);
    const plain = ui.addRowToDraft(e1, d2, row({ layouts: ['status-grid'], suggested_thresholds: { warn_above: 10 } }));
    assert.equal(plain.ok, true); assert.equal(plain.id, 'stw'); assert.equal(d2.pages[1].items.length, 9, 'the status page had room for one more');
    deq(Object.keys(d2.newInstruments), ['stw']); assert.equal(d2.newInstruments.stw.thresholds, undefined, 'suggested limits are OFF unless opted in');
    ui.removeItem(d2, 1, d2.pages[1].items.length - 1);
    assert.equal(Object.keys(d2.newInstruments).length, 0, 'removing the only use of a new item forgets its definition');
    const withAlarm = ui.addRowToDraft(e1, d2, row({ layouts: ['status-grid'], suggested_thresholds: { warn_above: 10 } }), { alarm: true });
    deq(d2.newInstruments[withAlarm.id].thresholds, { warn_above: 10 });
    const dn = ui.newDraft(e1); dn.selected = 1; ui.removeItem(dn, 1, 8);
    ui.addRowToDraft(e1, dn, note()); assert.equal(dn.newNotifications.smoke.alarm, false, 'a new notification does not count in the alarm banner by default');
    const dn0 = ui.newDraft(e1); dn0.selected = 1; ui.removeItem(dn0, 1, 8);
    const noAlarmKey = note(); delete noAlarmKey.definition.alarm; ui.addRowToDraft(e1, dn0, noAlarmKey);
    assert.strictEqual(dn0.newNotifications.smoke.alarm, false, 'alarm is explicitly false when the definition has no alarm key and the opt-in is off');
    const dn3 = ui.newDraft(e1); dn3.selected = 0; ui.removeItem(dn3, 0, 3);
    const hot = ui.addRowToDraft(e1, dn3, row({ layouts: ['single', 'grid-2x2'], suggested_thresholds: { warn_above: 10 } }));
    assert.equal(dn3.newInstruments[hot.id].thresholds, undefined, 'no thresholds are added when the opt-in is off');
    const dn2 = ui.newDraft(e1); dn2.selected = 1; ui.removeItem(dn2, 1, 8);
    ui.addRowToDraft(e1, dn2, note(), { alarm: true }); assert.equal(dn2.newNotifications.smoke.alarm, true, 'the opt-in maps to alarm:true');
    // An id that is already taken gets a suffix; an item added twice is one item.
    const d3 = ui.newDraft(e1); d3.selected = 0; ui.removeItem(d3, 0, 3); ui.removeItem(d3, 0, 2);
    const clash = ui.addRowToDraft(e1, d3, row({ proposed_id: 'sog', layouts: ['grid-2x2'] }));
    assert.equal(clash.id, 'sog_2', 'never reuses an existing id');
    d3.selected = 3; const again = ui.addRowToDraft(e1, d3, row({ proposed_id: 'sog', layouts: ['gauge-arc'], min: 0, max: 30 }));
    assert.equal(again.ok, false, 'gauge page is full'); d3.selected = 1; ui.removeItem(d3, 1, 8);
    assert.equal(ui.addRowToDraft(e1, d3, row({ proposed_id: 'sog', layouts: ['grid-2x2', 'status-grid'] })).id, 'sog_2', 'the same row on a second page reuses the item');
    assert.equal(Object.keys(d3.newInstruments).length, 1);
    // Ranges for bars and arcs.
    const d4 = ui.newDraft(e1); d4.selected = 2; ui.removeItem(d4, 2, 2);
    const tank = row({ row_id: 'oil', label: 'Oil', layouts: ['tank-bars'], min: 0, max: 100, definition: { path: 'tanks.oil', label: 'Oil', min: 0, max: 100 }, proposed_id: 'oil' });
    assert.match(ui.addRowToDraft(e1, d4, tank, { min: '10', max: '5' }).error, /minimum must be/);
    assert.match(ui.addRowToDraft(e1, d4, tank, { min: 'x', max: '5' }).error, /minimum must be/);
    const ranged = ui.addRowToDraft(e1, d4, tank, { min: '0', max: '250' });
    assert.equal(d4.newInstruments[ranged.id].max, 250); assert.equal(d4.newInstruments[ranged.id].min, 0);
    // Template rows: the user names the instance; the id and path follow.
    const tpl = row({ row_id: 'eng', label: 'Engine temp', template: true, path: 'propulsion.*.temperature', path_template: 'propulsion.*.temperature', label_template: 'Engine * temp', id_stem: 'engine_temp', proposed_id: 'engine_temp', layouts: ['grid-2x2'], definition: { path: 'propulsion.*.temperature', label: 'Engine temp', convert: 'K->C', unit: '°C', precision: 0, stale_after_s: 30 } });
    const d5 = ui.newDraft(e1); d5.selected = 0; ui.removeItem(d5, 0, 3);
    assert.match(ui.addRowToDraft(e1, d5, tpl, {}).error, /Type which one/); assert.match(ui.addRowToDraft(e1, d5, tpl, { instance: 'a.b' }).error, /Type which one/); assert.match(ui.addRowToDraft(e1, d5, tpl, { instance: '../x' }).error, /Type which one/);
    const port = ui.addRowToDraft(e1, d5, tpl, { instance: 'port' });
    assert.equal(port.id, 'engine_temp_port'); assert.equal(d5.newInstruments.engine_temp_port.path, 'propulsion.port.temperature'); assert.equal(d5.newInstruments.engine_temp_port.label, 'Engine port temp');

    // Page operations.
    const d6 = ui.newDraft(e1);
    while (d6.pages.length < 8) assert.equal(ui.addPage(e1, d6), true);
    assert.equal(ui.addPage(e1, d6), false, 'at most 8 pages'); assert.equal(d6.pages[7].layout, 'grid-2x2'); assert.equal(d6.selected, 7);
    assert.ok(ui.draftProblems(e1, d6).some((p) => /Page 6: add at least 1 value/.test(p.text)), 'an empty page blocks saving');
    const d7 = ui.newDraft({ ...e1, display: { ...e1.display, pages: [e1.display.pages[0]] } });
    assert.equal(ui.removePage(d7, 0), false, 'a display keeps at least one page');
    const d8 = ui.newDraft(e1); assert.equal(ui.moveWithin(d8.pages[0].items, 0, -1), false); assert.equal(ui.moveWithin(d8.pages[0].items, 0, 1), true); assert.equal(d8.pages[0].items[0], 'depth');
    const d9 = ui.newDraft(e1); d9.pages[0].layout = 'single'; d9.pages[1].title = 'x'.repeat(20);
    const issues = ui.draftProblems(e1, d9).map((p) => p.text).join('|');
    assert.match(issues, /holds 1, but it has 4/); assert.match(issues, /at most 16/);
    d9.pages[2].layout = 'grid-2x2'; d9.pages[0].layout = 'grid-2x2'; d9.pages[1].title = 'S'; d9.pages[2].items = ['sog'];
    d9.pages[3].layout = 'tank-bars'; d9.pages[3].items = ['sog'];
    assert.match(ui.draftProblems(e1, d9).map((p) => p.text).join('|'), /“SOG” needs a minimum and maximum/);

    // The save request: exact shape, nothing unrequested.
    const d10 = ui.newDraft(e1);
    deq(ui.savePayload(e1, d10), { id: 'helm', revision: REV, pages: e1.display.pages.map((p) => ({ title: p.title, layout: p.layout, items: p.items })) });
    d10.theme = 'daylight'; d10.pages[0].title = ' Wind '; d10.selected = 0; ui.removeItem(d10, 0, 3);
    ui.addRowToDraft(e1, d10, row({ layouts: ['grid-2x2'] }), { alarm: true });
    const payload = ui.savePayload(e1, d10);
    deq(Object.keys(payload).sort(), ['id', 'new_instruments', 'pages', 'revision', 'theme']);
    assert.equal(payload.theme, 'daylight'); assert.equal(payload.pages[0].title, 'Wind'); deq(payload.new_instruments.stw, { path: 'navigation.speedThroughWater', label: 'STW', convert: 'm/s->kn', unit: 'kn', precision: 1, stale_after_s: 10 });
    assert.equal(payload.new_notifications, undefined);

    // Upload targets, client side.
    ['192.0.2.50', 'vs-helm.local', 'boat-helm', '2001:db8::1', 'helm.boat.example.'].forEach((value) => assert.equal(ui.otaTargetProblem(value), '', value));
    ['', '-x', '--force', 'a b', ' 192.0.2.50', 'http://192.0.2.50', '192.0.2.50:80', 'a/b', '..', 'x..y', 'a_b', '999.1.1.1', 'bootsel', 'COM3', '12345', 'a'.repeat(300), 'host\n'].forEach((value) => assert.notEqual(ui.otaTargetProblem(value), '', JSON.stringify(value)));
    const ports = [{ path: '/dev/serial/by-id/usb-X-if00', device: '/dev/ttyACM0' }];
    assert.equal(ui.serialProblem('/dev/serial/by-id/usb-X-if00', ports), ''); assert.notEqual(ui.serialProblem('/dev/ttyUSB1', ports), ''); assert.notEqual(ui.serialProblem('', ports), ''); assert.notEqual(ui.serialProblem('x', []), '');
    deq(ui.uploadRequest(e1, 'ota', '192.0.2.50', ports), { body: { id: 'helm', method: 'ota', target: '192.0.2.50' } });
    assert.ok(ui.uploadRequest(e1, 'ota', '-x', ports).error); assert.ok(ui.uploadRequest(e1, 'telnet', 'x', ports).error);
    assert.equal(ui.defaultOtaTarget(e1), 'vs-helm.local'); assert.equal(ui.defaultOtaTarget({ ...e1, readiness: ready({ last_flash: { ok: true, method: 'ota', ota_target: '192.0.2.77' } }) }), '192.0.2.77');
    assert.equal(ui.defaultMethod(e1), 'serial', 'the first flash is by USB'); assert.equal(ui.defaultMethod({ ...e1, readiness: ready({ last_flash: { ok: true, method: 'ota' } }) }), 'ota'); assert.equal(ui.defaultMethod({ ...e1, readiness: ready({ last_flash: null }) }), 'serial');
    // Apply is blocked with a reason and the command to fix it.
    assert.equal(ui.applyBlock(e1), null);
    assert.equal(ui.applyBlock({ ...e1, readiness: ready({ wifi_stored: false }) }).command, 'sudo vesselstackctl display wifi');
    assert.equal(ui.applyBlock({ ...e1, readiness: ready({ mqtt_password_stored: false }) }).command, 'sudo vesselstackctl display rotate helm');
    assert.match(ui.applyBlock({ ...e1, readiness: ready({ docker_available: false }) }).reason, /Docker/);
    assert.match(ui.applyBlock({ ...e1, readiness: ready({ free_disk_bytes: 1e8 }) }).reason, /free disk space/);
    assert.equal(ui.applyBlock({ ...e1, readiness: ready({ esphome_image_present: false }) }), null, 'a missing builder image is downloaded by the build, not a blocker');

    // Progress stepper from STAGE lines.
    const stepStates = (lines, op, kind) => ui.parseStages(lines, op, kind).steps.map((s) => `${s.key}:${s.status}`).join(' ');
    assert.equal(stepStates([], 'running'), 'render:active compile:todo upload:todo done:todo');
    assert.equal(stepStates(['STAGE: render', 'x', 'STAGE: compile', 'log'], 'running'), 'render:done compile:active upload:todo done:todo');
    assert.equal(stepStates(['STAGE: render', 'STAGE: compile', 'STAGE: upload'], 'failed'), 'render:done compile:done upload:failed done:todo');
    assert.equal(stepStates(['STAGE: render', 'STAGE: compile', 'STAGE: upload', 'STAGE: done'], 'succeeded'), 'render:done compile:done upload:done done:done');
    assert.equal(stepStates(['STAGE: render'], 'succeeded'), 'render:done compile:done upload:done done:done');
    const skipped = ui.parseStages(['STAGE: render', 'STAGE: compile', 'note: the stored firmware already matches the current settings and credentials; skipping the compile', 'STAGE: upload'], 'running');
    assert.equal(skipped.skippedCompile, true); assert.equal(skipped.steps[1].label, 'Firmware was already up to date');
    assert.equal(stepStates(['STAGE: upload'], 'running', 'rollback'), 'upload:active done:todo');
    assert.equal(stepStates(['STAGE:  upload  junk'], 'running'), 'render:active compile:todo upload:todo done:todo', 'only exact STAGE lines count');

    // ---- the DOM: edit mode, save bar, data list controls ----
    const posts2 = [];
    const base = { ...entry(), id: 'helm' };
    const st = ui.state.displays;
    st.mode = 'day'; st.severity = 'plain'; st.list = [base]; st.themes = themesJson; st.loaded = true; st.editing = null; st.drafts = {}; st.page = {}; st.saveErrors = {}; st.conflict = {}; st.rowOpts = {}; st.rowErrors = {}; st.query = '';
    st.catalog = { schema: 1, live: { requested: true, ok: true }, groups: ['Navigation'], rows: [row({ layouts: ['grid-2x2', 'status-grid', 'single'] }), note(), row({ row_id: 'tpl', label: 'Engine temp', template: true, path: 'propulsion.*.temperature', path_template: 'propulsion.*.temperature', label_template: 'Engine * temp', id_stem: 'engine_temp', proposed_id: 'engine_temp', layouts: ['grid-2x2'], suggested_thresholds: { warn_above: 90 } })] };
    ui.state.operationRunning = false; ui.state.operation = null;
    const readOnly = allText(ui.displayCard(base));
    assert.match(readOnly, /Edit theme and pages/); assert.doesNotMatch(readOnly, /Discard/);
    ui.renderDisplays(); ui.renderDataBrowser();
    assert.doesNotMatch(allText(node('#catalog-root')), /Add to page/, 'no Add buttons until a display is being edited');
    ui.runDisplayAction({ act: 'edit', display: 'helm' });
    assert.equal(st.editing, 'helm'); assert.equal(ui.displaysDirty(), false);
    let cardEl = node('#displays-root').children[0];
    const buttons = (root, predicate) => root.find((el) => el.tagName === 'button' && predicate(el.attrs));
    const bar = () => node('#displays-root').children[0].find((el) => el.attrs && el.attrs['data-savebar'] === 'helm')[0];
    let save = buttons(bar(), (a) => a['data-act'] === 'save')[0]; let apply = buttons(bar(), (a) => a['data-act'] === 'apply')[0]; let discard = buttons(bar(), (a) => a['data-act'] === 'discard')[0];
    assert.ok('disabled' in save.attrs && 'disabled' in discard.attrs, 'Save and Discard are off while nothing changed'); assert.ok(!('disabled' in apply.attrs), 'Apply to screen is available');
    assert.ok(buttons(node('#displays-root').children[0], (a) => a['data-theme-id']).every((b) => !('disabled' in b.attrs)), 'swatches are selectable in edit mode');
    assert.ok(buttons(node('#displays-root').children[0], (a) => /^Move .* up$/.test(a['aria-label'] || '')).length > 0, 'reorder buttons carry accessible names');
    assert.ok(buttons(node('#displays-root').children[0], (a) => /^Remove SOG from page 1$/.test(a['aria-label'] || '')).length === 1);
    // Choosing a theme and typing a name makes the draft dirty and the preview follows.
    ui.runDisplayAction({ act: 'theme', display: 'helm', themeId: 'ocean' });
    assert.equal(st.drafts.helm.theme, 'ocean'); assert.equal(ui.displaysDirty(), true);
    assert.match(allText(node('#displays-root')), /Ocean theme/);
    const frame = node('#displays-root').children[0].find((el) => el.attrs && el.attrs.class && el.attrs.class.startsWith('preview-frame'))[0];
    assert.equal(frame.styleVars['--pv-tile'], themesJson.themes.ocean.day.tile, 'the preview shows the draft theme');
    save = buttons(bar(), (a) => a['data-act'] === 'save')[0]; assert.ok(!('disabled' in save.attrs), 'Save is on once something changed');
    ui.onDisplaysInput({ target: { dataset: { pageTitle: '0', display: 'helm' }, value: 'Wind', type: 'text' } });
    assert.equal(st.drafts.helm.pages[0].title, 'Wind');
    // Data rows: disabled with a reason when the page is full, enabled once there is room.
    ui.renderDataBrowser();
    const addButtons = () => node('#catalog-root').find((el) => el.tagName === 'button' && el.attrs['data-act'] === 'add-row');
    assert.equal(addButtons().length, 3);
    assert.ok(addButtons().every((b) => 'disabled' in b.attrs), 'page 1 is full'); assert.match(allText(node('#catalog-root')), /This page is full/);
    assert.match(allText(node('#catalog-root')), /Adding to Helm/);
    ui.runDisplayAction({ act: 'item-remove', display: 'helm', page: '0', item: '3' });
    assert.ok(!('disabled' in addButtons()[0].attrs), 'room for the first row');
    assert.match(node('#catalog-root').find((el) => el.tagName === 'button' && el.attrs['data-row-id'] === 'smoke')[0].attrs['aria-label'], /^Add Smoke to page 1$/);
    assert.ok('disabled' in addButtons().find((b) => b.attrs['data-row-id'] === 'smoke').attrs && /Cannot be shown on a 2 × 2 grid page/.test(allText(node('#catalog-root'))), 'a notification cannot go on a grid');
    assert.equal(node('#catalog-root').find((el) => el.tagName === 'input' && el.attrs['data-row-opt'] === 'instance').length, 1, 'a template row asks which one');
    assert.equal(node('#catalog-root').find((el) => el.tagName === 'input' && el.attrs['data-row-opt'] === 'alarm').length, 2, 'the alarm opt-in is offered for a row with suggested limits and for a notification');
    assert.match(allText(node('#catalog-root')), /Count in alarm banner \(uses the suggested limits\)/);
    // Add a template row without naming it, then with.
    ui.runDisplayAction({ act: 'add-row', rowId: 'tpl' });
    assert.match(st.rowErrors.tpl, /Type which one/); assert.match(allText(node('#catalog-root')), /Type which one/);
    ui.onDisplaysInput({ target: { dataset: { rowOpt: 'instance', rowId: 'tpl' }, value: 'port', type: 'text' } });
    ui.onDisplaysInput({ target: { dataset: { rowOpt: 'alarm', rowId: 'tpl' }, checked: true, type: 'checkbox' } });
    ui.runDisplayAction({ act: 'add-row', rowId: 'tpl' });
    deq(st.drafts.helm.pages[0].items, ['sog', 'depth', 'house_v', 'engine_temp_port']);
    deq(st.drafts.helm.newInstruments.engine_temp_port.thresholds, { warn_above: 90 });
    assert.equal(st.rowOpts.tpl, undefined, 'the options are used up');
    // Save: success path posts exactly the payload, leaves edit mode and reloads.
    ui.state.token = 'tok-save';
    const sent = [];
    fetchImpl = async (path, options = {}) => {
      if (options.method === 'POST') { sent.push([path, JSON.parse(options.body), options.headers['X-VesselStack-Token']]); return ok({ ok: true, changed: true }); }
      return ok(routes[path]);
    };
    routes['/api/displays'] = { schema: 1, displays: [base] };
    await ui.saveDraft(base);
    assert.equal(sent.length, 1); assert.equal(sent[0][0], '/api/displays/save'); assert.equal(sent[0][2], 'tok-save');
    deq(Object.keys(sent[0][1]).sort(), ['id', 'new_instruments', 'pages', 'revision', 'theme']); assert.equal(sent[0][1].pages[0].title, 'Wind'); assert.equal(sent[0][1].theme, 'ocean');
    assert.equal(st.editing, null, 'a saved draft leaves edit mode'); assert.equal(st.saving, false);
    // Save: a changed revision (409 code 3) keeps the edits and offers a reload; validation errors are listed.
    ui.runDisplayAction({ act: 'edit', display: 'helm' }); ui.runDisplayAction({ act: 'theme', display: 'helm', themeId: 'daylight' });
    fetchImpl = async (path, options = {}) => options.method === 'POST' ? ({ ok: false, status: 409, statusText: 'Conflict', json: async () => ({ error: 'changed', errors: ['changed'], code: 3 }) }) : ok(routes[path]);
    await ui.saveDraft(base);
    assert.equal(st.conflict.helm, true); assert.equal(st.editing, 'helm', 'the edits are kept'); assert.match(allText(node('#displays-root')), /changed somewhere else/); assert.match(allText(node('#displays-root')), /Reload the latest settings and drop my edits/);
    fetchImpl = async (path, options = {}) => options.method === 'POST' ? ({ ok: false, status: 400, statusText: 'Bad', json: async () => ({ error: 'one', errors: ['pages[0].title: too long', 'second problem'], code: 2 }) }) : ok(routes[path]);
    await ui.saveDraft(base);
    assert.equal(st.conflict.helm, false); assert.match(allText(node('#displays-root')), /pages\[0\]\.title: too long/); assert.match(allText(node('#displays-root')), /second problem/);
    fetchImpl = async (path, options = {}) => options.method === 'POST' ? ({ ok: false, status: 409, statusText: 'Conflict', json: async () => ({ error: 'Another operation is running' }) }) : ok(routes[path]);
    await ui.saveDraft(base);
    assert.equal(st.conflict.helm, false, 'a busy server is not a revision conflict'); assert.match(allText(node('#displays-root')), /Another operation is running/);
    ui.runDisplayAction({ act: 'reload', display: 'helm' }); assert.equal(st.editing, null); assert.equal(st.drafts.helm, undefined);
    // An invalid draft is never sent.
    ui.runDisplayAction({ act: 'edit', display: 'helm' }); st.drafts.helm.pages[0].title = '';
    sent.length = 0; fetchImpl = async (path, options = {}) => { sent.push(path); return ok({}); };
    await ui.saveDraft(base); assert.equal(sent.length, 0); assert.match(allText(node('#displays-root')), /Give the page a name/);
    ui.runDisplayAction({ act: 'discard', display: 'helm' }); assert.equal(st.editing, null);

    // ---- Apply: dialog result, deploy request, operation tracking ----
    nodes.set('#apply-body', new FakeEl('div'));
    const applyDialog = node('#apply-dialog');
    let dialogSeen = null;
    applyDialog.showModal = function showModal() {
      const dlg = ui.state.displays.applyDialog;
      dialogSeen = { method: dlg.method, text: allText(node('#apply-body')), title: node('#apply-title').textContent };
      dlg.host.value = dlgInput.host; dlg.port.value = dlgInput.port;
      node('#apply-ok').onclick();
      if (this.onclose) this.onclose();
    };
    applyDialog.close = function close(value) { this.returnValue = value; };
    let dlgInput = { host: '192.0.2.50', port: '/dev/serial/by-id/usb-X-if00' };
    const calls2 = [];
    const opState = { id: 41, label: 'display deploy helm', state: 'running', output: [], started_at: 1, finished_at: null };
    fetchImpl = async (path, options = {}) => {
      calls2.push([path, options.method || 'GET', options.body ? JSON.parse(options.body) : null]);
      if (path === '/api/displays/serial-ports') return ok({ schema: 1, ports });
      if (options.method === 'POST') return ok(opState);
      if (path === '/api/status') return ok({ components: [], operation: opState, installed_version: '0.6.0', running_version: '0.6.0', boot_id: 'b' });
      return ok(routes[path]);
    };
    ui.state.token = 'tok-apply'; ui.state.busy = false; ui.state.pendingOperation = null;
    // First flash: USB is the default, Wi-Fi shown as unverified, every note present.
    await ui.applyToScreen('helm');
    assert.equal(dialogSeen.method, 'serial'); assert.match(dialogSeen.text, /By USB cable/); assert.match(dialogSeen.text, /Over Wi-Fi/); assert.match(dialogSeen.text, /Wi-Fi updates are not yet verified on hardware/); assert.match(dialogSeen.text, /first time a new display is set up it must be sent by USB cable/);
    assert.match(dialogSeen.text, /3 to 6 minutes/); assert.match(dialogSeen.text, /backups, updates and settings changes are paused/);
    const post = calls2.find(([path, method]) => method === 'POST');
    assert.equal(post[0], '/api/displays/deploy'); deq(post[2], { id: 'helm', method: 'serial', target: '/dev/serial/by-id/usb-X-if00' });
    // Over Wi-Fi after a Wi-Fi flash: host prefilled from the last target; a bad host is refused inside the dialog.
    base.readiness = ready({ toolchain_cache_present: false, last_flash: { ok: true, method: 'ota', ota_target: '192.0.2.77', built_at_utc: 'T1' } });
    await tick(); await tick(); await tick();
    dlgInput = { host: '-x', port: '' }; calls2.length = 0; ui.state.pendingOperation = null; ui.state.busy = false;
    applyDialog.showModal = function showModal() {
      const dlg = ui.state.displays.applyDialog;
      assert.equal(dlg.method, 'ota'); assert.equal(dlg.host.attrs.value, '192.0.2.77', 'prefilled from the last Wi-Fi target');
      assert.match(allText(node('#apply-body')), /10 to 15 minutes the first time/); assert.match(allText(node('#apply-body')), /needs about 3\.0 GB of free space; this computer has 47 GB/);
      dlg.host.value = '-x'; node('#apply-ok').onclick();
      assert.match(dlg.error.textContent, /IP address or host name/, 'a bad host is explained and the dialog stays open');
      assert.equal(this.returnValue, '', 'it did not close');
      dlg.host.value = '192.0.2.77'; node('#apply-ok').onclick();
      if (this.onclose) this.onclose();
    };
    await ui.applyToScreen('helm');
    const post2 = calls2.find(([path, method]) => method === 'POST');
    deq(post2[2], { id: 'helm', method: 'ota', target: '192.0.2.77' });
    // Blocked Apply: no dialog, a reason.
    base.readiness = ready({ wifi_stored: false }); calls2.length = 0; dialogSeen = null; ui.state.busy = false;
    await ui.applyToScreen('helm'); assert.equal(dialogSeen, null); assert.equal(calls2.length, 0);
    assert.match(node('#toast-text').textContent, /Wi-Fi name and password/);
    // Rollback uses its own endpoint.
    await tick(); await tick(); await tick();
    base.readiness = ready({ last_flash: { ok: true, method: 'ota', ota_target: '192.0.2.77', built_at_utc: 'T1' }, firmware: { status: 'current', built_at_utc: 'T1', has_previous: true, rollback_possible: true } }); calls2.length = 0; ui.state.pendingOperation = null; ui.state.busy = false;
    applyDialog.showModal = function showModal() { const dlg = ui.state.displays.applyDialog; assert.equal(dlg.kind, 'rollback'); assert.match(node('#apply-title').textContent, /previous firmware/); dlg.host.value = '192.0.2.77'; dlg.port.value = ''; node('#apply-ok').onclick(); if (this.onclose) this.onclose(); };
    await ui.runDisplayAction({ act: 'rollback', display: 'helm' }); await tick(); await tick();
    assert.equal(calls2.find(([path, method]) => method === 'POST')[0], '/api/displays/rollback');
    assert.match(allText(ui.displayCard(base)), /Put previous firmware back/);

    // OTA target confirmation: the image holds the Wi-Fi password, so a new address needs an explicit tick.
    assert.equal(ui.isNewOtaTarget(base, '192.0.2.77'), false); assert.equal(ui.isNewOtaTarget(base, ' 192.0.2.77 '), false); assert.equal(ui.isNewOtaTarget(base, '192.0.2.99'), true);
    assert.equal(ui.isNewOtaTarget({ readiness: ready() }, '192.0.2.77'), true, 'no recorded target: every address is new');
    deq(ui.uploadRequest(base, 'ota', '192.0.2.99', ports, true).body, { id: 'helm', method: 'ota', target: '192.0.2.99', confirm_new_target: true });
    deq(ui.uploadRequest(base, 'ota', '192.0.2.77', ports, false).body, { id: 'helm', method: 'ota', target: '192.0.2.77' });
    for (const kind of ['deploy', 'rollback']) {
      if (kind === 'rollback') base.readiness = ready({ last_flash: { ok: true, method: 'ota', ota_target: '192.0.2.77', built_at_utc: 'T1' }, firmware: { status: 'current', built_at_utc: 'T1', has_previous: true, rollback_possible: true } });
      else base.readiness = ready({ last_flash: { ok: true, method: 'ota', ota_target: '192.0.2.77', built_at_utc: 'T1' } });
      await tick(); await tick(); await tick();
      calls2.length = 0; ui.state.pendingOperation = null; ui.state.busy = false;
      let seen = null;
      applyDialog.showModal = function showModal() {
        const dlg = ui.state.displays.applyDialog;
        dlg.host.value = '192.0.2.99'; dlg.sync();
        seen = { hidden: dlg.confirmRow.hidden, disabled: node('#apply-ok').disabled, text: allText(node('#apply-body')) };
        node('#apply-ok').onclick();
        seen.closedEarly = this.returnValue === 'ok'; seen.error = dlg.error.textContent;
        dlg.confirmBox.checked = true; dlg.sync(); seen.enabled = node('#apply-ok').disabled === false;
        node('#apply-ok').onclick();
        if (this.onclose) this.onclose();
      };
      await (kind === 'rollback' ? ui.runDisplayAction({ act: 'rollback', display: 'helm' }) : ui.applyToScreen('helm')); await tick(); await tick();
      assert.equal(seen.hidden, false, kind); assert.equal(seen.disabled, true, kind + ': Start stays disabled until ticked');
      assert.match(seen.text, /Send firmware, which contains this boat's Wi-Fi password, to 192\.0\.2\.99/); assert.equal(seen.closedEarly, false); assert.match(seen.error, /Tick the box/); assert.equal(seen.enabled, true);
      const sent = calls2.find(([path, method]) => method === 'POST');
      deq(sent[2], { id: 'helm', method: 'ota', target: '192.0.2.99', confirm_new_target: true });
    }

    // The progress panel is built from the operation output: stepper, details, and the exact remedy on failure.
    const hostileOutput = ['STAGE: render', 'STAGE: compile', '<img src=x onerror=alert(1)>', 'STAGE: upload'];
    ui.state.operation = { id: 7, label: 'display deploy helm', state: 'running', output: hostileOutput, started_at: 1 };
    let panel = ui.deployPanel(base);
    assert.equal(panel.find((el) => el.tagName === 'li' && el.attrs.class === 'step active').length, 1); assert.match(allText(panel), /Sending it to the screen/); assert.match(allText(panel), /Backups, updates and settings changes are paused/);
    assert.ok(allText(panel).includes('<img src=x onerror=alert(1)>')); assert.equal(panel.find((el) => el.tagName === 'img').length, 0, 'tool output is text only');
    ui.state.operation = { id: 7, label: 'display deploy helm', state: 'failed', output: hostileOutput, started_at: 1, finished_at: Math.floor(Date.now() / 1000), summary: { reason: 'the upload failed (exit status 1).', fix: 'Check that the display is powered on.' } };
    panel = ui.deployPanel(base); const failedText = allText(panel);
    assert.match(failedText, /did not finish/); assert.match(failedText, /To fix: Check that the display is powered on\./); assert.equal(panel.find((el) => el.tagName === 'li' && el.attrs.class === 'step failed').length, 1);
    ui.state.operation = { id: 8, label: 'display deploy helm', state: 'succeeded', output: ['STAGE: done'], started_at: 1, finished_at: Math.floor(Date.now() / 1000) };
    assert.match(allText(ui.deployPanel(base)), /Sent\. The screen restarts/);
    ui.state.operation = { id: 9, label: 'display deploy aux', state: 'running', output: [], started_at: 1 };
    assert.equal(allText(ui.deployPanel(base)), '', 'another display\'s operation is not shown here');
    ui.state.operation = { id: 10, label: 'backup', state: 'running', output: [], started_at: 1 };
    assert.equal(allText(ui.deployPanel(base)), '');
    assert.equal(ui.describeOperation({ label: 'display deploy helm' }).running, 'Putting the settings on Helm\'s screen…');
    assert.match(ui.describeOperation({ label: 'display rollback helm' }).done, /previous firmware was put back on Helm/);
    // While an operation runs, saving is off and says why.
    ui.state.operationRunning = true; st.editing = null; ui.runDisplayAction({ act: 'edit', display: 'helm' }); ui.runDisplayAction({ act: 'theme', display: 'helm', themeId: 'daylight' });
    const busyBar = node('#displays-root').children[0].find((el) => el.attrs && el.attrs['data-savebar'] === 'helm')[0];
    assert.ok(buttons(busyBar, (a) => a['data-act'] === 'save').every((b) => 'disabled' in b.attrs)); assert.match(allText(busyBar), /Another operation is running/);
    ui.state.operationRunning = false; ui.runDisplayAction({ act: 'discard', display: 'helm' });

    // ---- History and restore ----
    ui.formatStamp && assert.equal(ui.formatStamp('20260101T120000Z'), '2026-01-01 12:00 UTC');
    const historyCalls = [];
    fetchImpl = async (path, options = {}) => { historyCalls.push([path, options.method || 'GET', options.body ? JSON.parse(options.body) : null]); if (path === '/api/displays/history') return ok({ schema: 1, backups: [{ stamp: '20260101T120000Z', size: 900, revision: 'r' }, { stamp: '20251231T080000Z', size: 880, revision: 'q' }] }); if (options.method === 'POST') return ok({ ok: true, revision: 'new' }); return ok(routes[path]); };
    ui.state.token = 'tok-hist'; routes['/api/displays'] = { schema: 1, displays: [{ ...base, readiness: ready({ firmware: { status: 'current', built_at_utc: 'T1', rollback_possible: true } }) }] };
    nodes.set('#history-root', new FakeEl('div'));
    await ui.loadHistory();
    const histText = allText(node('#history-root')); assert.match(histText, /2026-01-01 12:00 UTC/); assert.match(histText, /2025-12-31 08:00 UTC/);
    const restoreButtons = node('#history-root').find((el) => el.tagName === 'button' && el.attrs['data-act'] === 'restore'); assert.equal(restoreButtons.length, 2);
    assert.match(restoreButtons[0].attrs['aria-label'], /Restore the settings from 2026-01-01 12:00 UTC/);
    historyCalls.length = 0; dialogAnswer = 'cancel';
    await ui.restoreBackup('20260101T120000Z'); assert.equal(historyCalls.filter(([, m]) => m === 'POST').length, 0, 'cancel restores nothing');
    dialogAnswer = 'ok'; ui.state.displays.list = [{ ...base, revision: REV }];
    await ui.restoreBackup('20260101T120000Z');
    deq(historyCalls.find(([, m]) => m === 'POST'), ['/api/displays/restore', 'POST', { stamp: '20260101T120000Z', revision: REV }]);
    assert.match(dialogTitles[dialogTitles.length - 1].title, /Restore these settings/);
    assert.match(allText(node('#history-root')), /settings were restored/); assert.match(allText(node('#history-root')), /Put previous firmware back on Helm/, 'after a restore, rolling the firmware back is offered');
    dialogAnswer = 'ok';
    // Restore is off while there are unsaved edits.
    ui.state.displays.list = [base]; ui.runDisplayAction({ act: 'edit', display: 'helm' }); ui.runDisplayAction({ act: 'theme', display: 'helm', themeId: 'daylight' });
    ui.renderHistory(); assert.ok(node('#history-root').find((el) => el.tagName === 'button' && el.attrs['data-act'] === 'restore').every((b) => 'disabled' in b.attrs)); assert.match(allText(node('#history-root')), /Save or discard your edits first/);
    ui.runDisplayAction({ act: 'discard', display: 'helm' });

    // Nothing the editor builds uses inline styles or raw HTML.
    ui.runDisplayAction({ act: 'edit', display: 'helm' });
    assert.equal(node('#displays-root').find((el) => el.attrs && 'style' in el.attrs).length, 0);
    assert.doesNotMatch(appSource, /innerHTML\s*=\s*[^;]*(draft|row\.|entry\.)/, 'editor data never goes through innerHTML');
    ui.runDisplayAction({ act: 'discard', display: 'helm' });
    ui.state.operation = null;
  }

  routes['/api/displays'] = { schema: 1, displays: [shown, { id: 'aux', name: 'Aux', board: 'b', error: 'could not be read' }] };
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
