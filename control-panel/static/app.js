const state = {
  token: sessionStorage.getItem("vesselstack_control_token") || "",
  config: null,
  poller: null,
  components: [],
  status: null,
  componentFilter: "all",
  componentQuery: "",
  menuOpen: null,
  componentSignature: "",
  busy: false,
  saving: false,
  session: 0,
  operationRunning: false,
  pendingOperation: null,
  lastOperationId: null,
  operation: null,
  watching: new Set(),
  dismissed: new Set(),
  notices: {},
  releaseEpoch: 0,
  releaseChannel: "stable",
  releases: [],
  releaseChecked: false,
  releaseError: "",
  bootId: null,
  reconnectTimer: null,
  reconnectAttempts: 0,
  reauthDirty: false,
  releaseCheckPending: false,
  installedVersion: "unknown",
  runningVersion: "unknown",
  releaseCheckedAt: "",
  rollbackPath: "",
  openSections: new Set(),
};

const IDLE_POLL_MS = 15000;
const BUSY_POLL_MS = 1200;
const RECENT_RESULT_SECONDS = 900;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function api(path, options = {}) {
  const headers = { "X-VesselStack-Token": state.token, ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `${response.status} ${response.statusText}`);
    error.status = response.status;
    if (payload.fields && typeof payload.fields === "object") error.fields = payload.fields;
    throw error;
  }
  return payload;
}

// A pasted "CONTROL_PANEL_TOKEN=..." line works as well as the bare token.
function normalizeToken(value) {
  let text = String(value ?? "").trim().replace(/^(export\s+)?CONTROL_PANEL_TOKEN\s*=\s*/, "").trim();
  if (text.length >= 2 && text[0] === text[text.length - 1] && "'\"".includes(text[0])) text = text.slice(1, -1).trim();
  return text;
}

const OPTION_LABELS = {
  BOAT_UNITS: { us_customary: "US customary", metric: "Metric" },
  SIGNALK_MODE: { docker: "Docker (recommended)", existing: "Existing server", native: "Native (on this Pi)" },
  PROVIDER: { "": "None", local: "Local (built-in)", codex_cli: "Codex CLI", claude_cli: "Claude CLI", openai: "OpenAI", vercel: "Vercel AI Gateway", bedrock: "Amazon Bedrock", google: "Google Gemini", ollama: "Ollama", openai_compatible: "OpenAI-compatible API" },
};
const optionLabel = (key, choice) => {
  const table = OPTION_LABELS[key] || (key.startsWith("BOAT_CHAT_") && key.endsWith("PROVIDER") ? OPTION_LABELS.PROVIDER : null);
  return (table && table[choice]) || choice || "None";
};

// ---------------------------------------------------------------- messages

function toast(message, isError = false) {
  const node = $("#toast");
  $("#toast-text").textContent = message;
  node.className = isError ? "show error" : "show";
  $("#toast-close").hidden = !isError;
  clearTimeout(node.timer);
  // Errors stay until dismissed; confirmations fade on their own.
  if (!isError) node.timer = setTimeout(() => { node.className = ""; }, 4200);
}

function setNotice(id, text, kind = "info") {
  if (text) state.notices[id] = { text, kind };
  else delete state.notices[id];
  const host = $("#notice");
  host.innerHTML = Object.entries(state.notices).map(([key, item]) => `<div class="banner ${item.kind}" role="status"><span class="icon" aria-hidden="true">i</span><div class="msg">${escapeHtml(item.text)}</div><button type="button" class="x" data-dismiss-notice="${escapeHtml(key)}" aria-label="Dismiss message">&#10005;</button></div>`).join("");
}

function setConnection(mode) {
  $("#connection-dot").className = mode === "connected" ? "dot online" : mode === "disconnected" ? "dot offline" : "dot";
  $("#connection-label").textContent = mode === "connected" ? "Connected" : mode === "disconnected" ? "Disconnected" : "Locked";
  $("#retry").hidden = mode !== "disconnected";
}

// ---------------------------------------------------------------- dialogs

const LIVE_DATA_NOTE = "Live instrument data pauses briefly.";
const CONFIRMS = {
  stop: { title: "Stop all services?", intro: "These will go dark:", items: ["Instruments in Home Assistant and Grafana", "Signal K (no boat data comes in)", "Boat Chat and Telegram alerts", "Displays"], note: "The Control Panel stays available. Press Start all to bring them back.", ok: "Stop all", danger: true },
  restart: { title: "Restart all services?", intro: "Every service stops and starts again. Live data pauses for under a minute.", ok: "Restart all" },
  start: { title: "Start all services?", intro: `Starts every enabled service that is stopped. ${LIVE_DATA_NOTE}`, ok: "Start all" },
  install: { title: "Reinstall & start?", intro: "This re-applies your saved settings and restarts every service.", items: ["Live data pauses for about a minute", "Unsaved edits on the Settings page are not included", "Your data and backups are not touched"], ok: "Reinstall & start" },
  apply: { title: "Apply saved settings?", intro: "Writes your saved settings into VesselStack's service files. Running services keep running; changes take effect the next time each service restarts.", items: ["The displays bridge, if it is running, restarts now", "Other services are not restarted: restart a service from Home, or use Restart all, to use the new values", "Services that are stopped stay stopped"], ok: "Apply" },
  "restart-panel": { title: "Restart the Control Panel?", intro: "This page will disconnect for a few seconds and reconnect by itself. Boat services are not affected.", ok: "Restart panel" },
};
const updateConfirm = (extra = {}) => ({
  title: "Back up & update from this folder?",
  intro: extra.source_directory ? `Folder: ${extra.source_directory}` : "A backup is taken first. Then the release in that folder is installed.",
  items: ["A backup is taken first and needs a writable backup folder", "The Control Panel and services may be interrupted", ...(isConfigDirty() ? ["Unsaved edits on the Settings page are kept but not applied"] : [])],
  ok: "Back up & update",
});
const releaseConfirm = (release) => ({
  title: `Back up & install ${release.version}?`,
  intro: `A backup is taken first. Then ${release.version} is installed.`,
  items: ["Needs a writable backup folder; if the backup cannot be saved the update stops", ...(isConfigDirty() ? ["Unsaved edits on the Settings page are kept but not applied"] : [])],
  note: "The Control Panel may restart. This page reconnects by itself, usually within a minute.",
  ok: "Back up & install",
});
const componentConfirm = (verb, name) => ({
  stop: { title: `Stop ${name}?`, intro: `${name} turns off and anything that depends on it stops working until you start it again. ${LIVE_DATA_NOTE}`, ok: "Stop", danger: true },
  restart: { title: `Restart ${name}?`, intro: `${name} stops and starts again. ${LIVE_DATA_NOTE}`, ok: "Restart" },
  remove: { title: `Disable ${name}?`, intro: `${name} will stop now and stay off, including after a restart or Start all. Anything that depends on it will stop working until you enable it again. Your saved data is kept.`, note: `Technically this removes the ${name} container and marks it disabled; Enable puts it back.`, ok: "Disable", danger: true },
}[verb]);
const LOCK_CONFIRM = { title: "Lock the panel?", intro: "You have unsaved edits on the Settings page. Locking discards them.", ok: "Lock and discard", danger: true };
const discardConfirm = (count) => ({ title: "Discard your edits?", intro: `${count} unsaved ${count === 1 ? "change" : "changes"} on this page will be reset to the saved settings.`, ok: "Discard", danger: true });

function ask(spec) {
  const dialog = $("#confirm-dialog");
  if (dialog.open) return Promise.resolve(false);
  $("#confirm-title").textContent = spec.title;
  $("#confirm-intro").textContent = spec.intro || "";
  $("#confirm-intro").hidden = !spec.intro;
  $("#confirm-items").innerHTML = (spec.items || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  $("#confirm-items").hidden = !(spec.items || []).length;
  $("#confirm-note").textContent = spec.note || "";
  $("#confirm-note").hidden = !spec.note;
  $("#confirm-ok").textContent = spec.ok;
  $("#confirm-ok").className = spec.danger ? "danger solid" : "primary";
  const opener = document.activeElement;
  return new Promise((resolve) => {
    dialog.onclose = () => {
      dialog.onclose = null;
      const target = spec.returnFocus ? $(spec.returnFocus) : opener;
      if (target && target.isConnected !== false && typeof target.focus === "function") target.focus();
      resolve(dialog.returnValue === "ok");
    };
    $("#confirm-ok").onclick = () => dialog.close("ok");
    $("#confirm-cancel").onclick = () => dialog.close("cancel");
    // Escape closes the dialog without a return value: that is Cancel.
    dialog.returnValue = "";
    dialog.showModal();
  });
}

// ---------------------------------------------------------------- session

function lock(force = false) {
  if (!force && isConfigDirty()) {
    ask(LOCK_CONFIRM).then((ok) => { if (ok) lock(true); });
    return;
  }
  clearTimeout(state.poller);
  clearTimeout(state.reconnectTimer);
  state.poller = null;
  state.token = "";
  state.session += 1;
  state.saving = false;
  state.config = null;
  state.components = [];
  state.status = null;
  state.releases = [];
  state.releaseChecked = false;
  state.releaseError = "";
  state.busy = false;
  state.operationRunning = false;
  state.pendingOperation = null;
  state.lastOperationId = null;
  state.operation = null;
  state.notices = {};
  state.bootId = null;
  state.rollbackPath = "";
  state.componentSignature = "";
  $("#toast").className = "";
  $("#config-form").innerHTML = "";
  $("#banner").innerHTML = "";
  state.bannerHtml = "";
  $("#notice").innerHTML = "";
  $("#release-result").innerHTML = "";
  resetDisplays();
  sessionStorage.removeItem("vesselstack_control_token");
  $("#app").hidden = true;
  $("#lock").hidden = true;
  $("#login").hidden = false;
  $("#version-chip").hidden = true;
  $("#boat-name").textContent = "";
  $("#token").value = "";
  $("#login-error").textContent = "";
  setConnection("locked");
}

function loginError(message) {
  $("#login-error").textContent = message;
  $("#token").setAttribute("aria-invalid", message ? "true" : "false");
  $("#login").classList.toggle("bad", Boolean(message));
}

async function unlock() {
  const session = ++state.session;
  try {
    const config = await api("/api/config");
    if (session !== state.session) return;
    state.config = config;
    sessionStorage.setItem("vesselstack_control_token", state.token);
    loginError("");
    $("#login").hidden = true;
    $("#app").hidden = false;
    $("#lock").hidden = false;
    setConnection("connected");
    if (state.reauthDirty) {
      // Keep the user's locally edited controls intact across re-auth; the
      // fetched config becomes the new save/discard baseline.
      state.reauthDirty = false;
      updateConfigStatus();
    } else {
      renderConfiguration(config);
    }
    await refreshStatus();
  } catch (error) {
    if (session !== state.session) return;
    setConnection("locked");
    // Only a rejected token is forgotten; a network problem keeps it for the retry.
    if (error.status === 401) { sessionStorage.removeItem("vesselstack_control_token"); state.token = ""; }
    loginError(error.status === 401
      ? "That token wasn't accepted. Paste only the token, or the whole CONTROL_PANEL_TOKEN=… line."
      : `Couldn't reach the Control Panel (${error.message}). Check that the SSH tunnel is still open.`);
  }
}

function requireReauth(message) {
  state.reauthDirty = isConfigDirty();
  clearTimeout(state.poller);
  clearTimeout(state.reconnectTimer);
  state.poller = null;
  state.reconnectTimer = null;
  state.token = "";
  state.session += 1;
  state.pendingOperation = null;
  state.operationRunning = false;
  state.busy = false;
  sessionStorage.removeItem("vesselstack_control_token");
  $("#login").hidden = false;
  $("#app").hidden = true;
  $("#lock").hidden = true;
  loginError("");
  $("#login-error").textContent = `${message} Unlock again.${state.reauthDirty ? " Your unsaved edits are kept." : ""}`;
  $("#token").value = "";
  setConnection("locked");
}

// ---------------------------------------------------------------- tabs

function selectTab(name) {
  $$("[data-tab]").forEach((item) => {
    const selected = item.dataset.tab === name;
    item.classList.toggle("active", selected);
    item.setAttribute("aria-selected", String(selected));
    item.tabIndex = selected ? 0 : -1;
  });
  $$(".tab-panel").forEach((panel) => { panel.hidden = panel.id !== name; });
  if (name === "settings") updateScrollSpy();
  if (name === "displays") loadDisplays(false);
}

// ---------------------------------------------------------------- components and the Home band

const STATUS_WORDS = { running: "Running", stopped: "Stopped", disabled: "Disabled", unknown: "Unknown" };
const visibleComponents = () => state.components.filter((item) => item.id !== "control-panel");

function formatWhen(seconds) {
  const when = new Date(seconds * 1000);
  const now = new Date();
  const clock = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const days = Math.round((new Date(now.getFullYear(), now.getMonth(), now.getDate()) - new Date(when.getFullYear(), when.getMonth(), when.getDate())) / 86400000);
  if (days === 0) return `today ${clock}`;
  if (days === 1) return `yesterday ${clock}`;
  return `${when.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })} ${clock}`;
}

function backupSummary() {
  const backup = state.status?.backup;
  if (!backup || !backup.path) return { tone: "warn", value: "Last backup: unknown", sub: "Backup status is not available yet." };
  if (backup.last_backup_at) {
    const sub = backup.mount === "mounted" ? "Backup drive OK"
      : backup.state === "read_only" ? "Backup folder is not writable"
      : backup.mount === "system_disk" ? "Saved on this computer's own disk, not an external drive" : "";
    return { tone: backup.state === "ok" && backup.mount !== "system_disk" ? "good" : "warn", value: `Last backup: ${formatWhen(backup.last_backup_at)}`, sub };
  }
  if (backup.mount === "not_mounted") return { tone: "warn", value: "Last backup: unknown", sub: "Backup drive not mounted, so it cannot be checked." };
  if (backup.state === "none") return { tone: "warn", value: "No backup yet", sub: backup.mount === "mounted" ? "Backup drive OK" : "Press Back up now to make the first one." };
  return { tone: "warn", value: "Last backup: unknown", sub: backup.message || "The backup folder could not be read." };
}

function softwareSummary() {
  const version = state.installedVersion;
  const available = state.releases.filter(releaseEligible);
  if (available.length) return { tone: "warn", value: `${available[0].version} available`, sub: "", html: `<button type="button" class="linklike" data-tab-link="updates">Go to Updates &rarr;</button>` };
  if (state.releaseChecked) return { tone: "good", value: `Up to date on ${version}`, sub: "Checked only when you ask" };
  return { tone: "plain", value: `Version ${version}`, sub: "Updates are checked only when you ask" };
}

function renderBand() {
  const enabled = visibleComponents().filter((item) => item.status !== "disabled");
  const stopped = enabled.filter((item) => item.status === "stopped");
  const unknown = enabled.filter((item) => item.status === "unknown");
  let services;
  if (!state.components.length) services = { tone: "plain", value: "Loading…", sub: "" };
  else if (stopped.length) services = { tone: "warn", value: `${stopped.length} of ${enabled.length} stopped`, sub: stopped.map((item) => item.label).join(", ") + (unknown.length ? ` · status unknown for ${unknown.map((item) => item.label).join(", ")}` : "") };
  else if (unknown.length) services = { tone: "warn", value: "Status unknown", sub: unknown.map((item) => item.label).join(", ") };
  else services = { tone: "good", value: "All systems running", sub: `${enabled.length} components` };
  const backup = backupSummary();
  const software = softwareSummary();
  const cell = (key, item) => `<div><span class="k">${key}</span><span class="v ${item.tone}">${escapeHtml(item.value)}</span><span class="sub">${item.html ?? escapeHtml(item.sub)}</span></div>`;
  $("#band").innerHTML = `${cell("Services", services)}${cell("Backup", backup)}${cell("Software", software)}<div class="act"><button type="button" class="primary" data-action="backup">Back up now</button></div>`;
  $("#maint-backup-last").textContent = backup.value;
  if (state.busy) setBusy(true);
}

function componentCard(component) {
  const status = component.status || "unknown";
  const id = escapeHtml(component.id);
  const name = escapeHtml(component.label || component.id);
  const verb = (action, text, extra = "") => `<button type="button" ${extra} data-component="${id}" data-verb="${action}" data-fk="${id}-${action}">${text}</button>`;
  let acts;
  if (status === "running") acts = `${verb("restart", "Restart")}${verb("stop", "Stop")}`;
  else if (status === "disabled") {
    if (component.manageable) acts = component.still_running ? verb("remove", "Retry disabling", 'class="danger"') : verb("add", "Enable");
    else acts = `<button type="button" data-configure-section="${escapeHtml(component.section || "")}" data-fk="${id}-turn-on">Turn on in Settings</button>`;
  } else acts = verb("start", "Start", 'class="primary"');
  const canMenu = component.manageable && status !== "disabled";
  const more = canMenu ? `<button type="button" class="more" aria-haspopup="true" aria-expanded="${state.menuOpen === component.id}" aria-label="More actions for ${name}" data-more="${id}" data-fk="${id}-more">&#8943;</button>` : "";
  const menu = canMenu && state.menuOpen === component.id
    ? `<div class="menu" role="group" aria-label="More actions for ${name}"><p class="stat">Disable turns ${name} off and keeps it off, including after a restart or Start all. It stops ${name}, removes its container and marks it disabled. You can enable it again any time; your saved data is kept.</p><button type="button" data-disable="${id}" data-fk="${id}-disable">Disable…</button></div>` : "";
  const configure = component.section && status !== "disabled" ? `<button type="button" class="cfg" data-configure-section="${escapeHtml(component.section)}" data-fk="${id}-configure">Configure</button>` : "";
  const detail = component.detail ? `<span class="detail">${escapeHtml(component.detail)}</span>` : "";
  const leftover = component.still_running ? `<p class="note">Still running although it is turned off.</p>` : "";
  return `<article class="comp ${status === "disabled" ? "is-disabled" : ""}"><div class="top"><h3>${name}</h3><span class="status ${status}">${STATUS_WORDS[status] || "Unknown"}</span></div><p>${escapeHtml(component.description || "")}</p>${detail}${leftover}${configure}<div class="acts">${acts}${more}</div>${menu}</article>`;
}

function renderComponents(components, force = false) {
  state.components = components || [];
  const list = visibleComponents();
  const signature = JSON.stringify([list, state.componentFilter, state.componentQuery, state.menuOpen]);
  const counts = { all: list.length, running: 0, stopped: 0, disabled: 0, unknown: 0 };
  list.forEach((item) => { counts[item.status] = (counts[item.status] || 0) + 1; });
  $$("[data-component-filter]").forEach((button) => {
    const selected = button.dataset.componentFilter === state.componentFilter;
    button.classList?.toggle("active", selected);
    button.setAttribute?.("aria-pressed", String(selected));
    const badge = button.querySelector(".n");
    if (badge) badge.textContent = counts[button.dataset.componentFilter] ?? 0;
  });
  $("#component-note").textContent = `${counts.all} components: ${counts.running} running, ${counts.stopped} stopped, ${counts.disabled} disabled${counts.unknown ? `, ${counts.unknown} unknown` : ""}. Core services can be disabled from the ⋯ menu; the rest follow your Settings.`;
  renderBand();
  if (signature === state.componentSignature && !force) return;
  state.componentSignature = signature;
  const active = document.activeElement?.dataset?.fk;
  const query = state.componentQuery.toLowerCase();
  const filtered = list.filter((item) => (state.componentFilter === "all" || item.status === state.componentFilter) && (!query || `${item.label} ${item.description || ""}`.toLowerCase().includes(query)));
  $("#component-grid").innerHTML = filtered.length ? filtered.map(componentCard).join("") : `<div class="empty"><strong>No components match.</strong><p>Try a different search or choose All.</p></div>`;
  const focusKey = state.pendingFocus || active;
  state.pendingFocus = null;
  if (focusKey) $(`[data-fk="${focusKey}"]`)?.focus();
  if (state.busy) setBusy(true);
}

function jumpToConfiguration(sectionName) {
  selectTab("settings");
  const section = $$("[data-section]").find((item) => item.dataset.section === sectionName);
  if (!section) return;
  if (section.tagName === "DETAILS") section.open = true;
  section.scrollIntoView({ behavior: "smooth", block: "start" });
  section.querySelector("input:not([disabled]), select:not([disabled])")?.focus({ preventScroll: true });
}

async function runComponent(component, action) {
  const known = state.components.find((item) => item.id === component);
  const name = known ? known.label : component;
  if (["stop", "restart", "remove"].includes(action)) {
    if (!(await ask({ ...componentConfirm(action, name), returnFocus: `[data-fk="${component}-${action === "remove" ? "more" : action}"]` }))) return;
  }
  startOperation({ component, action });
}

// ---------------------------------------------------------------- operations and the result banner

const OPERATION_TEXT = {
  backup: ["Backing up… Services pause while the copy is made; this can take a few minutes.", "Backup finished", "Backup failed", "Check the details, then try again."],
  apply: ["Applying your saved settings…", "Settings applied", "Applying settings failed", "Check the details, fix the cause, then apply again."],
  install: ["Reinstalling and starting…", "Reinstall finished", "Reinstall failed", "Check the details, then try again."],
  preflight: ["Checking setup…", "Setup check finished", "Setup check found a problem", "Check the details for what to fix."],
  start: ["Starting all services…", "All services started", "Starting the services failed", "Check the details, then try again."],
  stop: ["Stopping all services…", "All services stopped", "Stopping the services failed", "Check the details, then try again."],
  restart: ["Restarting all services…", "All services restarted", "Restarting the services failed", "Check the details, then try again."],
  "bootstrap-history": ["Setting up history storage…", "History storage is set up", "History storage setup failed", "Check the details, then try again. It is safe to run again."],
  update: ["Backing up, then updating from the folder…", "Update finished", "Update failed", "Nothing further was changed once it failed; check the details before trying again."],
  "restart-panel": ["Restarting the Control Panel…", "Control Panel restarted", "Control Panel restart failed", "Check the details, then try again."],
};
const COMPONENT_VERBS = {
  start: (n) => [`Starting ${n}…`, `${n} started`, `${n} did not start`],
  stop: (n) => [`Stopping ${n}…`, `${n} stopped`, `${n} did not stop`],
  restart: (n) => [`Restarting ${n}…`, `${n} restarted`, `${n} did not restart`],
  add: (n) => [`Enabling ${n}…`, `${n} enabled`, `${n} could not be enabled`],
  remove: (n) => [`Disabling ${n}…`, `${n} disabled`, `${n} could not be disabled`],
};

function describeOperation(operation) {
  const label = String(operation.label || "");
  const component = /^(start|stop|restart|add|remove) (.+)$/.exec(label);
  if (component) {
    const known = state.components.find((item) => item.id === component[2]);
    const [running, done, failed] = COMPONENT_VERBS[component[1]](known?.label || component[2]);
    return { running, done, failed, next: "Check the details, then try again." };
  }
  if (label === "update-release") {
    const target = operation.target_version || "the update";
    return { running: `Backing up, then installing ${target}…`, done: `Installed ${target}`, failed: `Installing ${target} failed`, next: "Your boat was backed up first if the backup step finished. Check the details before trying again." };
  }
  const entry = OPERATION_TEXT[label];
  if (entry) return { running: entry[0], done: entry[1], failed: entry[2], next: entry[3] };
  return { running: "Working…", done: "Finished", failed: "Failed", next: "Check the details, then try again." };
}

const clockTime = (seconds) => new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const operationKey = (operation) => `${state.bootId}:${operation.id}:${operation.started_at || ""}:${operation.label}`;
const logText = (operation) => (operation.output || []).join("\n");

function renderBanner(operation, context) {
  const host = $("#banner");
  const text = describeOperation(operation || {});
  let html = "";
  const log = (summary) => (operation?.output?.length ? `<details class="disc"><summary>${summary}</summary><pre class="log" tabindex="0">${escapeHtml(logText(operation))}</pre></details>` : "");
  const wasOpen = host.querySelector?.("details")?.open;
  if (context.uncertain || context.active) {
    const pendingLabel = state.pendingOperation?.label;
    const shown = operation?.label && operation.state === "running" ? text : describeOperation({ label: pendingLabel || operation?.label });
    const phase = operation?.phase && operation.state === "running" ? ` (${operation.phase})` : "";
    const waiting = context.verificationRequired ? "Finishing up… waiting for the Control Panel to confirm the new version." : shown.running + phase;
    html = `<div class="banner progress" role="status"><span class="icon spin" aria-hidden="true"></span><div class="msg"><strong>${escapeHtml(waiting)}</strong>${log("Show progress")}</div></div>`;
  } else if (operation && ["succeeded", "failed"].includes(operation.state) && operation.id != null) {
    const key = operationKey(operation);
    const finished = operation.finished_at || operation.started_at;
    const recent = finished ? Date.now() / 1000 - finished < RECENT_RESULT_SECONDS : false;
    if (!state.dismissed.has(key) && (state.watching.has(key) || recent)) {
      const when = operation.finished_at ? ` ${clockTime(operation.finished_at)}` : "";
      const summary = operation.summary || {};
      if (operation.state === "succeeded") {
        let tail = "";
        if (operation.label === "backup") tail = ` — saved to <span class="mono">${escapeHtml(summary.saved_path || state.status?.backup?.path || "the backup folder")}</span>`;
        if (operation.label === "update-release") tail = " — the Control Panel confirmed the new version.";
        html = `<div class="banner ok" role="status"><span class="icon" aria-hidden="true">&#10003;</span><div class="msg"><strong>${escapeHtml(text.done)}${escapeHtml(when)}</strong>${tail}${log("Details")}</div><button type="button" class="x" id="banner-x" aria-label="Dismiss message">&#10005;</button></div>`;
      } else {
        const reason = String(summary.reason || "see the details below").replace(/[.\s]+$/, "");
        const fix = String(summary.fix || "").trim();
        const next = fix ? "" : /mount/i.test(reason) && operation.label === "backup" ? "Plug in or mount the backup drive and try again." : text.next;
        // The exact remedy the script printed; a trailing "sudo ..." command is set in monospace. Everything is escaped.
        const split = fix.match(/^(.*?)(\bsudo\s.*)$/s);
        const fixHtml = fix ? `<div class="fix"><strong>To fix:</strong> ${split ? `${escapeHtml(split[1])}<span class="mono">${escapeHtml(split[2])}</span>` : escapeHtml(fix)}</div>` : "";
        html = `<div class="banner fail" role="alert"><span class="icon" aria-hidden="true">!</span><div class="msg"><strong>${escapeHtml(text.failed)}</strong> — ${escapeHtml(reason)}.${next ? ` ${escapeHtml(next)}` : ""}${fixHtml}${log("Show details")}</div><button type="button" class="x" id="banner-x" aria-label="Dismiss message">&#10005;</button></div>`;
      }
    }
  }
  if (state.bannerHtml !== html) {
    state.bannerHtml = html;
    host.innerHTML = html;
    const details = host.querySelector?.("details");
    if (details && wasOpen) details.open = true;
  }
}

function operationSucceeded(operation) {
  if (operation?.state !== "succeeded") return false;
  // Only these exact operation labels require boot/version confirmation. The
  // legacy operation store omits phase for ordinary backup/apply/component work.
  if (!["update-release", "restart-panel"].includes(operation.label)) return true;
  return operation.phase === "complete" && Boolean(operation.target_version && operation.running_version === operation.target_version);
}

function renderOperation(operation) {
  if (!operation && state.operationRunning) {
    setBusy(true);
    setNotice("opstatus", "Operation status is unavailable. Controls stay locked; use Retry now or unlock again to recover the session.");
    return;
  }
  const current = operation || {};
  const verified = operationSucceeded(current);
  const pending = state.pendingOperation;
  const terminalForPending = pending && ["succeeded", "failed"].includes(current.state)
    && ((pending.operationId != null && current.id === pending.operationId)
      || (pending.operationId == null && current.id != null && current.id !== pending.baselineId));
  if (terminalForPending) state.pendingOperation = null;
  const label = current.state === "succeeded" && !verified ? "verification-required" : (current.state || "idle");
  const active = Boolean(state.pendingOperation || label === "running" || label === "verification-required" || (label !== "succeeded" && label !== "failed" && current.phase && current.phase !== "complete"));
  setBusy(active);
  state.operationRunning = active;
  const uncertain = Boolean(state.pendingOperation && !["running", "succeeded", "failed"].includes(current.state));
  setNotice("opstatus", uncertain ? "Operation status is unavailable. Controls stay locked; use Retry now or unlock again to recover the session." : "");
  state.operation = current;
  if (current.id != null && (active || terminalForPending)) state.watching.add(operationKey(current));
  renderBanner(current, { active, uncertain, verificationRequired: label === "verification-required" });
  if (active) {
    clearTimeout(state.poller);
    state.poller = setTimeout(refreshStatus, BUSY_POLL_MS);
  }
}

async function startOperation(payload) {
  const session = state.session;
  if (state.busy) return;
  const label = payload.component ? `${payload.action} ${payload.component}` : payload.action;
  state.pendingOperation = { baselineId: state.lastOperationId, operationId: null, label };
  setBusy(true);
  renderBanner(state.operation, { active: true, uncertain: false });
  try {
    const operation = await api(payload.component ? "/api/component" : "/api/action", { method: "POST", body: JSON.stringify(payload) });
    if (session !== state.session || !state.token) return;
    if (state.pendingOperation) state.pendingOperation.operationId = operation.id ?? null;
    renderOperation(operation);
    refreshStatus();
  } catch (error) {
    if (session !== state.session || !state.token) return;
    if (error.status === 401) { requireReauth("Your session ended."); return; }
    if (error.status >= 400 && error.status < 500) {
      state.pendingOperation = null;
      setBusy(false);
      renderBanner(state.operation, { active: false, uncertain: false });
      toast(error.message, true);
      refreshStatus();
      return;
    }
    // A POST can be accepted even when its response is lost. Reconcile status
    // before releasing controls; only a terminal status may clear busy state.
    toast(`${error.message}. Reconnecting to verify the operation…`, true);
    refreshStatus();
  }
}

async function runAction(action, extra = {}) {
  const spec = action === "update" ? updateConfirm(extra) : CONFIRMS[action];
  if (spec && !(await ask(spec))) return;
  startOperation({ action, ...extra });
}

// ---------------------------------------------------------------- status polling

function schedulePoll(delay) {
  clearTimeout(state.poller);
  state.poller = setTimeout(refreshStatus, delay);
}

async function refreshStatus() {
  const session = state.session;
  if (!state.token) return;
  if (typeof document.hidden === "boolean" && document.hidden && !state.operationRunning) { schedulePoll(IDLE_POLL_MS); return; }
  try {
    const data = await api("/api/status");
    if (session !== state.session || !state.token) return;
    if (state.bootId && data.boot_id && state.bootId !== data.boot_id) {
      if (isConfigDirty()) {
        setNotice("restarted", "The Control Panel restarted. Your unsaved edits are kept. Save or discard them, then reload this page.");
      } else {
        state.bootId = data.boot_id;
        window.location.reload();
        return;
      }
    }
    state.bootId = data.boot_id || state.bootId;
    state.reconnectAttempts = 0;
    clearTimeout(state.reconnectTimer);
    setNotice("offline", "");
    if (data.operation?.id != null) state.lastOperationId = data.operation.id;
    state.status = data;
    if (data.installed_version) state.installedVersion = data.installed_version;
    if (data.running_version) state.runningVersion = data.running_version;
    $("#version-chip").textContent = state.installedVersion;
    $("#version-chip").hidden = state.installedVersion === "unknown";
    $("#boat-name").textContent = data.boat_name || "";
    setNotice("restart-needed", data.restart_needed ? "A newer Control Panel is installed than the one running. Restart it from the Maintenance tab to use the new version." : "");
    renderUpdates();
    renderComponents(data.components);
    setConnection("connected");
    renderOperation(data.operation);
    if (!state.operationRunning) schedulePoll(IDLE_POLL_MS);
  } catch (error) {
    if (session !== state.session || !state.token) return;
    if (error.status === 401) { requireReauth("Your session ended."); return; }
    setConnection("disconnected");
    setBusy(true);
    state.reconnectAttempts += 1;
    const more = state.reconnectAttempts <= 5 ? `Retrying (${Math.min(state.reconnectAttempts, 5)} of 5)…` : "Press Retry now to try again.";
    setNotice("offline", `Can't reach the boat computer (${error.message}). Controls are paused until it answers. ${more}`);
    clearTimeout(state.reconnectTimer);
    if (state.reconnectAttempts <= 5) state.reconnectTimer = setTimeout(refreshStatus, Math.min(2000 * state.reconnectAttempts, 8000));
  }
}

// ---------------------------------------------------------------- updates

function releaseUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "github.com" && /^\/[^/]+\/[^/]+\/releases\/tag\/[^/]+$/.test(url.pathname) ? url.href : "";
  } catch (_) { return ""; }
}

function versionTuple(value) {
  const match = String(value || "").match(/^(\d+)\.(\d+)\.(\d+)/);
  return match ? match.slice(1).map(Number) : null;
}

function compareVersions(a, b) {
  const left = versionTuple(a);
  const right = versionTuple(b);
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) return left[index] - right[index];
  }
  return 0;
}

function releaseEligible(release) {
  const candidate = versionTuple(release.version);
  const installed = versionTuple(state.installedVersion);
  if (!candidate || !installed) return false;
  return compareVersions(release.version, state.installedVersion) > 0;
}

const eligibleReleases = () => state.releases.filter(releaseEligible).sort((a, b) => compareVersions(b.version, a.version));

function updateReleaseControls() {
  $("#check-releases").disabled = state.releaseCheckPending || state.busy;
  $$("[data-install]").forEach((button) => { button.disabled = state.releaseCheckPending || state.busy; });
}

function renderUpdates() {
  $("#installed-version").textContent = state.installedVersion;
  $("#running-note").textContent = state.runningVersion !== "unknown" && state.runningVersion !== state.installedVersion ? `Running ${state.runningVersion}. Restart the Control Panel to use ${state.installedVersion}.` : "Running now";
  const available = eligibleReleases();
  const badge = $("#update-badge");
  badge.hidden = !available.length;
  badge.textContent = String(available.length);
  badge.setAttribute?.("aria-label", `${available.length} update available`);
  let html = "";
  if (state.releaseCheckPending) html = `<p role="status"><span class="spin" aria-hidden="true"></span>Contacting github.com…</p>`;
  else if (state.releaseError) html = `<div class="banner fail" role="alert"><span class="icon" aria-hidden="true">!</span><div class="msg"><strong>Couldn't check for updates</strong> — ${escapeHtml(state.releaseError)}. Check the boat computer's internet connection and try again.</div></div>`;
  else if (!state.releaseChecked) html = `<p class="muted">Not checked yet. Nothing is contacted until you press the button.</p>`;
  else if (!available.length) html = `<div class="banner ok" role="status"><span class="icon" aria-hidden="true">&#10003;</span><div class="msg"><strong>You're up to date on ${escapeHtml(state.installedVersion)}</strong><span class="detail">Checked ${escapeHtml(state.releaseCheckedAt)}.</span></div></div>`;
  else {
    html = `<div class="banner info" role="status"><span class="icon" aria-hidden="true">&#8593;</span><div class="msg"><strong>${escapeHtml(available[0].version)} is available</strong> <span class="muted">(you have ${escapeHtml(state.installedVersion)})</span><span class="detail">Checked ${escapeHtml(state.releaseCheckedAt)}.</span></div></div>`;
    html += available.map((release) => `<div class="release-item"><h3>${escapeHtml(release.name)}${release.prerelease ? " (early test release)" : ""}</h3><p><a href="${releaseUrl(release.html_url)}" target="_blank" rel="noreferrer">Read the release notes on GitHub</a></p><button type="button" class="primary" data-install="${escapeHtml(release.id)}">Back up &amp; install ${escapeHtml(release.version)}</button><span class="hint">Takes a backup first, then installs. The Control Panel may restart and this page will reconnect by itself. Boat services pause briefly.</span></div>`).join("");
    if (state.releaseChannel === "lab") html += `<p class="small warn-text">Early test releases are for testing. Read the release notes before installing.</p>`;
  }
  $("#release-result").innerHTML = html;
  updateReleaseControls();
  renderBand();
}

function renderReleases(data) {
  state.installedVersion = data.installed_version || state.installedVersion;
  state.runningVersion = data.running_version || state.runningVersion;
  state.releaseCheckedAt = clockTime((data.checked_at ? new Date(data.checked_at).getTime() : Date.now()) / 1000);
  state.releases = Array.isArray(data.releases) ? data.releases.filter((release) => Number.isInteger(release.id) && release.tag && release.version && release.name && releaseUrl(release.html_url)) : [];
  state.releaseChecked = true;
  state.releaseError = "";
  renderUpdates();
}

async function checkReleases() {
  const session = state.session;
  const epoch = ++state.releaseEpoch;
  const channel = $("#include-lab").checked ? "lab" : "stable";
  state.releaseChannel = channel;
  state.releaseCheckPending = true;
  state.releaseError = "";
  renderUpdates();
  try {
    const data = await api(`/api/releases?channel=${channel}`);
    if (session !== state.session || epoch !== state.releaseEpoch || !state.token) return;
    renderReleases(data);
  } catch (error) {
    if (session === state.session && epoch === state.releaseEpoch && state.token) {
      if (error.status === 401) requireReauth("Your session ended.");
      else { state.releaseError = error.message; state.releaseChecked = false; }
    }
  } finally {
    if (session === state.session && epoch === state.releaseEpoch && state.token) {
      state.releaseCheckPending = false;
      renderUpdates();
    }
  }
}

async function installRelease(releaseId) {
  const release = state.releases.find((item) => item.id === Number(releaseId) && releaseEligible(item));
  if (!release || state.busy) return;
  if (!(await ask(releaseConfirm(release)))) return;
  startOperation({ action: "update-release", release_id: release.id, channel: state.releaseChannel });
}

// ---------------------------------------------------------------- settings

const slug = (name) => String(name).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

function fieldMarkup(spec, value) {
  const id = `setting-${spec.key}`;
  const help = spec.description ? `<span class="help" id="${id}-help">${escapeHtml(spec.description)}</span>` : "";
  const described = ` aria-describedby="${id}-err${spec.description ? ` ${id}-help` : ""}"`;
  const error = `<span class="err" id="${id}-err" role="alert"></span>`;
  const readOnly = spec.read_only ? ' disabled' : "";
  const tag = spec.read_only ? '<span class="tag">read-only</span>' : "";
  if (spec.kind === "boolean") {
    return `<div class="field wide boolean"><label class="sw"><input type="checkbox" id="${id}" name="${spec.key}" data-bool="true" data-read-only="${spec.read_only}"${described} ${value === "true" ? "checked" : ""}${readOnly}><span class="track"></span><span class="txt"><strong>${escapeHtml(spec.label)}${tag}</strong>${help}</span></label>${error}</div>`;
  }
  let input;
  if (spec.kind === "select") {
    input = `<select id="${id}" name="${spec.key}" data-read-only="${spec.read_only}"${described}${readOnly}>${spec.choices.map((choice) => `<option value="${escapeHtml(choice)}" ${choice === value ? "selected" : ""}>${escapeHtml(optionLabel(spec.key, choice))}</option>`).join("")}</select>`;
  } else {
    const type = spec.kind === "secret" ? "password" : "text";
    const shown = spec.kind === "secret" ? "" : escapeHtml(value || "");
    const configured = spec.kind === "secret" && value?.configured;
    const placeholder = spec.kind === "secret" ? (configured ? "••••••••••••" : "Not set") : "";
    // Numeric settings stay text inputs so a saved value such as "12m" is shown as stored
    // (a type=number input would display it blank and the next Save would clear it).
    const numeric = spec.kind === "number" ? 'inputmode="decimal"' : spec.kind === "port" ? 'inputmode="numeric"' : "";
    const secretAttrs = spec.kind === "secret" ? ' autocomplete="new-password"' : ' autocomplete="off"';
    input = `<input id="${id}" name="${spec.key}" type="${type}" data-read-only="${spec.read_only}"${described} value="${shown}" placeholder="${placeholder}" ${numeric}${secretAttrs} spellcheck="false"${readOnly}>`;
  }
  const saved = spec.kind === "secret" && value?.configured ? `<span class="saved">&#10003; Saved${spec.read_only ? "" : " · leave blank to keep"}</span>` : "";
  const clear = spec.kind === "secret" && value?.configured && !spec.read_only
    ? `<label class="clear-secret"><input type="checkbox" data-clear-secret="${spec.key}"> Clear stored value</label>`
    : "";
  return `<div class="field ${spec.kind}"><label for="${id}">${escapeHtml(spec.label)}${tag}</label>${input}${error}${help}${saved}${clear}</div>`;
}

function renderConfiguration(config) {
  $$("#config-form details[data-section]").forEach((item) => { if (item.open) state.openSections.add(item.dataset.section); });
  const names = (config.sections || []).map((item) => item.name);
  [...new Set(config.fields.map((item) => item.section))].forEach((name) => { if (!names.includes(name)) names.push(name); });
  const info = Object.fromEntries((config.sections || []).map((item) => [item.name, item]));
  const bySection = (name) => config.fields.filter((item) => item.section === name);
  const displaysLink = (name) => name === "Displays" ? `<p class="small"><button type="button" class="linklike" data-tab-link="displays">Choose what each display shows on the Displays tab &rarr;</button></p>` : "";
  const body = (name) => `${displaysLink(name)}${info[name]?.advanced && info[name]?.lead && bySection(name).every((item) => item.read_only) ? `<p class="small read-only-note">${escapeHtml(info[name].lead)}</p>` : ""}<div class="fields">${bySection(name).map((spec) => fieldMarkup(spec, config.values[spec.key])).join("")}</div>`;
  const common = names.filter((name) => !info[name]?.advanced && bySection(name).length);
  const advanced = names.filter((name) => info[name]?.advanced && bySection(name).length);
  const jump = `<nav class="jump" aria-label="Jump to a settings section">${common.map((name) => `<a href="#s-${slug(name)}" data-jump="${escapeHtml(name)}">${escapeHtml(name)}</a>`).join("")}${advanced.length ? `<span class="grp">Advanced</span>` : ""}${advanced.map((name) => `<a href="#s-${slug(name)}" data-jump="${escapeHtml(name)}">${escapeHtml(name)}</a>`).join("")}</nav>`;
  const commonHtml = common.map((name) => `<section class="card sect" id="s-${slug(name)}" data-section="${escapeHtml(name)}"><h2>${escapeHtml(name)}</h2><p class="lead">${escapeHtml(info[name]?.lead || "")}</p>${body(name)}</section>`).join("");
  const advancedHtml = advanced.length ? `<div class="advwrap"><h2>Advanced</h2><p>Rarely needed. Open a section only if you know you need it.</p>${advanced.map((name) => `<details class="sect" id="s-${slug(name)}" data-section="${escapeHtml(name)}" ${state.openSections.has(name) ? "open" : ""}><summary><h2>${escapeHtml(name)}</h2></summary><div class="body"><p class="small">${escapeHtml(info[name]?.lead || "")}</p>${body(name)}</div></details>`).join("")}</div>` : "";
  $("#config-form").innerHTML = `${jump}<div class="settings-body">${commonHtml}${advancedHtml}</div>`;
  $("#config-form").oninput = (event) => { clearFieldError(event.target); updateConfigStatus(); };
  $("#config-form").onchange = (event) => { clearFieldError(event.target); updateConfigStatus(); };
  updateConfigStatus();
  updateScrollSpy();
}

function clearFieldError(control) {
  const field = control?.closest?.(".field");
  if (!field || !field.classList.contains("invalid")) return;
  field.classList.remove("invalid");
  control.removeAttribute("aria-invalid");
  const message = field.querySelector(".err");
  if (message) message.textContent = "";
}

function showFieldErrors(fields) {
  let first = null;
  Object.entries(fields).forEach(([key, message]) => {
    const control = $(`#setting-${key}`);
    const field = control?.closest?.(".field");
    if (!field) return;
    field.classList.add("invalid");
    control.setAttribute("aria-invalid", "true");
    field.querySelector(".err").textContent = message;
    const details = control.closest("details");
    if (details) details.open = true;
    first ||= control;
  });
  if (first) { first.scrollIntoView({ block: "center" }); first.focus({ preventScroll: true }); }
  updateConfigStatus();
}

function updateScrollSpy() {
  const links = $$("[data-jump]");
  if (!links.length) return;
  let current = links[0];
  links.forEach((link) => {
    const target = $(`#s-${slug(link.dataset.jump)}`);
    if (target && target.getBoundingClientRect().top < 160) current = link;
  });
  links.forEach((link) => {
    link.classList.toggle("on", link === current);
    if (link === current) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  });
}

function currentSettings() {
  const settings = {};
  // Read editable controls directly: busy-state disables the form, and unchecked
  // switches are absent from FormData, so neither can be left to the browser.
  $$("#config-form input[name], #config-form select[name]").forEach((control) => {
    if (control.dataset.readOnly === "true") return;
    settings[control.name] = control.dataset.bool === "true" ? String(Boolean(control.checked)) : control.value;
  });
  $$('[data-clear-secret]').forEach((checkbox) => { if (checkbox.checked) settings[checkbox.dataset.clearSecret] = null; });
  return settings;
}

function defaultFieldValue(spec, original) {
  if (spec.kind === "secret") return "";
  if (original !== undefined && original !== null && original !== "") return original;
  if (spec.kind === "boolean") return "false";
  if (spec.kind === "select") return spec.choices?.[0] || "";
  return "";
}

function dirtyKeys() {
  if (!state.config) return [];
  const current = currentSettings();
  return state.config.fields.filter((spec) => {
    if (spec.read_only) return false;
    const original = state.config.values[spec.key];
    const next = Object.hasOwn(current, spec.key) ? current[spec.key] : defaultFieldValue(spec, original);
    if (spec.kind === "secret") {
      if (next === "") return false;
      if (next === null) return Boolean(original?.configured);
      return true;
    }
    const baseline = spec.kind === "boolean" && (original === undefined || original === null || original === "") ? "false" : spec.kind === "select" && (original === undefined || original === null || original === "") ? String(spec.choices?.[0] || "") : String(original ?? "");
    return String(next ?? "") !== baseline;
  }).map((spec) => spec.key);
}

const isConfigDirty = () => dirtyKeys().length > 0;

function updateConfigStatus() {
  const dirty = dirtyKeys();
  const count = dirty.length;
  const invalid = $$(".field.invalid").length;
  $$(".field").forEach((field) => {
    const control = field.querySelector("[name]");
    field.classList.toggle("dirty", Boolean(control) && dirty.includes(control.name));
  });
  const stateNode = $("#sb-state");
  stateNode.textContent = count ? `Unsaved changes (${count})${invalid ? ` · ${invalid} need${invalid === 1 ? "s" : ""} fixing` : ""}` : (state.config ? "All changes saved" : "No changes");
  stateNode.className = `state${invalid ? " err" : count ? " dirty" : ""}`;
  $("#sb-note").textContent = count ? "Save first to apply" : "Saved settings are ready to apply";
  $("#rollback-note").textContent = state.rollbackPath ? `Previous settings were kept at ${state.rollbackPath}` : "";
  $("#sb-apply").disabled = count > 0 || state.busy || state.saving;
  $("#sb-save").disabled = !count || state.busy || state.saving;
  $("#sb-discard").disabled = !count || state.busy || state.saving;
}

async function saveConfiguration() {
  if (!isConfigDirty()) return toast("No settings changes to save.");
  const current = currentSettings();
  const session = state.session;
  const controls = $$("#config-form input, #config-form select");
  controls.forEach((control) => { control.dataset.wasDisabled = control.disabled ? "true" : "false"; control.disabled = true; });
  state.saving = true;
  updateConfigStatus();
  const settings = {};
  state.config.fields.forEach((spec) => {
    if (spec.read_only) return;
    const original = state.config.values[spec.key];
    const next = Object.hasOwn(current, spec.key) ? current[spec.key] : defaultFieldValue(spec, original);
    if (spec.kind === "secret") {
      if (next === "") return;
      if (next === null && original?.configured) settings[spec.key] = null;
      else if (next !== null) settings[spec.key] = next;
      return;
    }
    const baseline = spec.kind === "boolean" && (original === undefined || original === null || original === "") ? "false" : spec.kind === "select" && (original === undefined || original === null || original === "") ? String(spec.choices?.[0] || "") : String(original ?? "");
    if (String(next ?? "") !== baseline) settings[spec.key] = next;
  });
  try {
    const result = await api("/api/config", { method: "POST", body: JSON.stringify({ settings }) });
    if (session !== state.session || !state.token) return;
    state.config = result.configuration;
    renderConfiguration(state.config);
    if (result.backup) state.rollbackPath = result.backup;
    updateConfigStatus();
    toast("Settings saved. Nothing has changed on the boat yet. Press Apply to put them into effect.");
  } catch (error) {
    if (session !== state.session || !state.token) return;
    if (error.status === 401) { requireReauth("Your session ended."); return; }
    controls.forEach((control) => { control.disabled = control.dataset.wasDisabled === "true"; });
    if (error.fields) {
      showFieldErrors(error.fields);
      toast(`${Object.keys(error.fields).length} ${Object.keys(error.fields).length === 1 ? "setting needs" : "settings need"} fixing before they can be saved.`, true);
    } else toast(error.message, true);
  } finally {
    if (session === state.session && state.token) {
      state.saving = false;
      updateConfigStatus();
    }
  }
}

async function discardEdits() {
  const count = dirtyKeys().length;
  if (!count || !(await ask(discardConfirm(count)))) return;
  renderConfiguration(state.config);
  toast("Edits discarded.");
}

function setBusy(busy) {
  state.busy = busy;
  state.operationRunning = busy;
  $$('[data-action], #sb-save, #sb-discard, #run-update, #check-releases, [data-install], [data-component], [data-more], [data-disable]').forEach((button) => { button.disabled = busy; });
  $$("#config-form input, #config-form select").forEach((control) => { control.disabled = busy || state.saving || control.dataset.readOnly === "true"; });
  updateReleaseControls();
  updateConfigStatus();
}

// ---------------------------------------------------------------- Displays tab (read-only in this version)
// Pure helpers first (unit-tested), then DOM builders. Every piece of data reaches the page through
// textContent / createTextNode, never innerHTML; colours and sizes go through style.setProperty on
// CSS custom properties, because the page's CSP forbids inline style attributes.

const SVG_NS = "http://www.w3.org/2000/svg";
const DISPLAYS_STALE_MS = 30000;
const LAYOUT_NAMES = { single: "One big value", "grid-2x2": "2 × 2 grid", "status-grid": "Status grid", "tank-bars": "Tank bars", "gauge-arc": "Gauge", electrical: "Electrical" };
const SEVERITIES = [["plain", "Normal"], ["ok", "OK"], ["warn", "Warning"], ["alarm", "Alarm"], ["stale", "No data"]];
const SEVERITY_KEY = { plain: "ok", ok: "ok", warn: "warn", alarm: "alarm", stale: "unknown" };
const PREVIEW_FONT = { grid: 56, single: 96, small: 20, status_lg: 48, status_sm: 30, row: 26 };
const UNCATALOGUED_GROUP = "Uncatalogued";

const freshDisplays = () => ({
  loaded: false, loading: false, error: "", loadedAt: 0, epoch: 0,
  list: [], themes: null, catalog: null, query: "",
  mode: "day", severity: "plain", page: {}, editable: false,
});
state.displays = freshDisplays();

function build(namespace, tag, props = {}, kids = []) {
  const node = namespace ? document.createElementNS(namespace, tag) : document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = String(value);
    else if (key === "vars") Object.entries(value).forEach(([name, item]) => node.style.setProperty(name, String(item)));
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  [kids].flat(Infinity).forEach((kid) => { if (kid !== undefined && kid !== null && kid !== false) node.append(kid.nodeType ? kid : document.createTextNode(String(kid))); });
  return node;
}
const h = (tag, props, ...kids) => build(null, tag, props, kids);
const sv = (tag, props, ...kids) => build(SVG_NS, tag, props, kids);

function displayName(entry) { return entry?.display?.name || entry?.name || entry?.id || ""; }
function displayId(entry) { return entry?.display?.id || entry?.id || ""; }

// Firmware chip: what is on the screen compared with what the settings say.
function firmwareChip(readiness) {
  const firmware = readiness?.firmware;
  const flash = readiness?.last_flash || null;
  const status = firmware?.status;
  if (status === "none") return { state: "not-built", label: "Not built", tone: "warn", hint: "No firmware has been built for this display yet." };
  if (status === "outdated") return { state: "outdated", label: "Changes not on screen", tone: "warn", hint: "The settings changed after the firmware was built. Rebuild and flash to show them." };
  if (status === "current") {
    if (!flash) return { state: "never-flashed", label: "Never flashed", tone: "warn", hint: "Firmware is built but has not been flashed to the display from this computer." };
    if (flash.ok !== true) return { state: "flash-failed", label: "Last flash failed", tone: "bad", hint: "The last attempt to flash this display did not finish. Try again." };
    if (flash.firmware_built_at !== firmware.built_at_utc) return { state: "outdated", label: "Changes not on screen", tone: "warn", hint: "A newer build exists than the one that was flashed. Flash again to show it." };
    return { state: "on-screen", label: "On screen", tone: "good", hint: "The firmware that was last flashed matches your settings." };
  }
  return { state: "unknown", label: "Unknown", tone: "plain", hint: "The Control Panel could not tell whether the display is up to date." };
}

const formatBytes = (value) => {
  const gb = Number(value) / 1073741824;
  return gb >= 10 ? `${Math.round(gb)} GB` : `${gb.toFixed(1)} GB`;
};

// Checklist of what must be true before a display can be built, flashed and reach the boat data.
// state: ok | todo (needs action) | unknown. `command` is shown as text to run on the boat computer.
function readinessItems(id, readiness) {
  const r = readiness || {};
  const items = [];
  const add = (key, state, label, hint = "", command = "") => items.push({ key, state, label, hint, command });
  add("enabled", r.displays_enabled ? "ok" : "todo", r.displays_enabled ? "Displays are switched on" : "Displays are switched off",
    r.displays_enabled ? "" : "Turn on “Enable instrument displays” in Settings, then Save and Apply.");
  if (r.secrets_readable === false) add("secrets", "unknown", "Display passwords could not be checked", "The Control Panel cannot read the displays secrets file, so the password and Wi-Fi checks below are unknown.");
  add("mqtt", r.secrets_readable === false ? "unknown" : r.mqtt_password_stored ? "ok" : "todo",
    r.mqtt_password_stored ? "This display has its own data password" : "This display has no data password yet",
    r.mqtt_password_stored ? "" : "Create one (the display must then be flashed with it).", r.mqtt_password_stored ? "" : `sudo vesselstackctl display rotate ${id}`);
  add("wifi", r.secrets_readable === false ? "unknown" : r.wifi_stored ? "ok" : "todo",
    r.wifi_stored ? "Wi-Fi details are stored" : "Wi-Fi details are not stored",
    r.wifi_stored ? "" : "The display needs the boat Wi-Fi name and password. They are typed on the boat computer so they never pass through this page.", r.wifi_stored ? "" : "sudo vesselstackctl display wifi");
  const reachable = r.data_bind_non_loopback === true;
  add("network", reachable ? "ok" : "todo", reachable ? "The display can reach the boat data over the network" : "The display cannot reach the boat data yet",
    reachable ? "" : (r.data_bind_hint || "The data service only listens on this computer, so a display on the network cannot connect."), reachable ? "" : "sudo vesselstackctl lan all --yes");
  if (r.docker_available === false) add("docker", "todo", "Docker is not available", "Building firmware on this computer needs Docker. You can instead export the project and build it elsewhere.", `sudo vesselstackctl display export ${id} --output FILE`);
  else if (r.docker_available === true) {
    if (r.esphome_image_present === false) add("image", "todo", "The firmware builder is not downloaded yet", "The first build downloads it, which needs an internet connection and a few minutes.");
    const need = r.toolchain_cache_present ? r.min_free_bytes_warm_build : r.min_free_bytes_first_build;
    if (Number.isFinite(r.free_disk_bytes) && Number.isFinite(need) && r.free_disk_bytes < need) add("disk", "todo", "Not enough free disk space for a build", `${formatBytes(r.free_disk_bytes)} free, ${formatBytes(need)} needed.`);
  }
  return items;
}

// What each item on a page is called and where its value comes from.
function itemInfo(show, id) {
  const inst = show?.instruments?.[id];
  if (inst) return { id, kind: "instrument", label: inst.label || id, unit: inst.unit || "", path: inst.path || "", precision: Number.isInteger(inst.precision) ? inst.precision : 0, convert: inst.convert || "" };
  const note = show?.notifications?.[id];
  if (note) return { id, kind: "notification", label: note.label || id, unit: "", path: note.path || "" };
  const ha = show?.ha?.[id];
  if (ha) return { id, kind: "ha", label: ha.label || id, unit: "", path: "" };
  return { id, kind: "missing", label: id, unit: "", path: "" };
}

const liveRowsByPath = (catalog) => {
  const map = new Map();
  (catalog?.rows || []).forEach((row) => { if (row.live_state === "live" && row.path && !map.has(row.path)) map.set(row.path, row); });
  return map;
};

// A live number is shown only when the catalog converted it the same way this item does.
function liveValue(info, live) {
  const row = info.kind === "instrument" && live ? live.get(info.path) : null;
  if (!row || row.value === undefined || row.value === null || !Number.isFinite(Number(row.value)) || row.convert !== info.convert) return "—";
  return Number(row.value).toFixed(info.precision);
}

function themeVars(theme, mode, severity) {
  const palette = theme[mode] || theme.day;
  const pair = (theme.sev?.[mode] || theme.sev?.day || {})[SEVERITY_KEY[severity] || "ok"] || [palette.tile, palette.value];
  const gauge = (theme.gauge?.[mode] || theme.gauge?.day || {})[SEVERITY_KEY[severity] || "ok"] || palette.value;
  const plain = severity === "plain";
  const border = mode === "day" ? theme.shape?.tile_border : palette.page_lo;
  const sevPair = plain ? ((theme.sev?.[mode] || theme.sev?.day || {}).ok || pair) : pair;
  return {
    "--pv-page": palette.page_bg, "--pv-tile": plain ? palette.tile : pair[0],
    "--pv-tile-label": plain ? palette.label : pair[1], "--pv-tile-value": plain ? palette.value : pair[1],
    "--pv-sev-bg": sevPair[0], "--pv-sev-fg": sevPair[1], "--pv-label": palette.label, "--pv-value": palette.value,
    "--pv-clock": palette.clock, "--pv-title": palette.title, "--pv-dot-ok": palette.dot_ok, "--pv-dot-bad": palette.dot_bad, "--pv-dot-unknown": palette.dot_unknown,
    "--pv-hi": palette.page_hi, "--pv-lo": palette.page_lo, "--pv-track": palette.track, "--pv-gauge": gauge,
    "--pv-border": (theme.shape?.tile_border_width || 0) > 0 && border ? border : "transparent",
  };
}

// Everything the preview draws, from `show --json` (geometry computed by the server) and the theme palettes.
function previewModel(show, pageIndex, themes, options = {}, live = null) {
  const page = show.display.pages[pageIndex];
  const geometry = show.preview?.[pageIndex];
  if (!page || !geometry) return null;
  const themeId = options.theme || show.theme || "classic";
  const theme = themes?.themes?.[themeId] || themes?.themes?.classic;
  if (!theme) return null;
  const mode = options.mode === "night" ? "night" : "day";
  const severity = SEVERITIES.some(([key]) => key === options.severity) ? options.severity : "plain";
  const infos = page.items.map((id) => itemInfo(show, id));
  const values = infos.map((info) => ({ ...info, value: info.kind === "instrument" ? liveValue(info, live) : "" }));
  const layout = geometry.layout;
  const model = {
    layout, pageIndex, title: page.title, themeId, mode, severity,
    screen: geometry.screen, round: geometry.screen.shape === "round", content: geometry.content, header: geometry.header || null,
    footer: show.board?.ring && show.display.pages.length > 1 ? { y: show.board.ring.footer_y, pages: show.display.pages.length } : null,
    vars: themeVars(theme, mode, severity), borderWidth: theme.shape?.tile_border_width || 0, radius: theme.shape?.tile_radius || 0,
    arc: geometry.arc || null, textBox: geometry.text_box || null, cells: [], rows: [], empty: infos.length === 0,
  };
  if (layout === "electrical") {
    const slots = show.layouts?.electrical?.slots || [];
    model.rows = values.map((item, index) => ({ ...item, role: slots[index]?.role || "" }));
    return model;
  }
  const statusFont = geometry.status_font && /lg/.test(geometry.status_font) ? PREVIEW_FONT.status_lg : PREVIEW_FONT.status_sm;
  model.cells = (geometry.cells || []).map((box, index) => {
    const item = values[index] || { label: "", value: "", unit: "", kind: "missing" };
    const kind = layout === "status-grid" ? "status" : layout === "tank-bars" ? "bar" : layout === "gauge-arc" ? "gauge" : "tile";
    const size = kind === "status" ? statusFont : layout === "single" ? PREVIEW_FONT.single : PREVIEW_FONT.grid;
    return { ...item, box, cellKind: kind, valueSize: size, labelSize: PREVIEW_FONT.small };
  });
  return model;
}

// Fit a label into a width using the average width of a condensed face (about half an em per letter).
const fitFont = (text, size, width) => Math.max(10, Math.min(size, Math.floor(width / Math.max(1, String(text).length * 0.5))));

function arcPath(box, width, start, sweep, fraction) {
  const radius = (box.width - width) / 2;
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  const point = (degrees) => [cx + radius * Math.cos(degrees * Math.PI / 180), cy + radius * Math.sin(degrees * Math.PI / 180)];
  const span = sweep * fraction;
  const [x1, y1] = point(start);
  const [x2, y2] = point(start + span);
  return `M ${x1.toFixed(1)} ${y1.toFixed(1)} A ${radius} ${radius} 0 ${span > 180 ? 1 : 0} 1 ${x2.toFixed(1)} ${y2.toFixed(1)}`;
}

let previewSerial = 0;
function previewSvg(model) {
  const { screen } = model;
  const clip = `pv-clip-${++previewSerial}`;
  const text = (props, content) => sv("text", { "text-anchor": "middle", ...props }, content);
  const body = [];
  body.push(sv("rect", { class: "pv-page", x: 0, y: 0, width: screen.width, height: screen.height }));
  const c = model.content;
  body.push(sv("rect", { class: "pv-safe", x: c.x, y: c.y, width: c.width, height: c.height }));
  if (model.header) {
    const hd = model.header;
    body.push(sv("circle", { class: "pv-dot ok", cx: screen.width / 2 - 62, cy: hd.y + hd.height / 2, r: 6 }));
    body.push(text({ class: "pv-clock", x: screen.width / 2 + 6, y: hd.y + hd.height / 2 + 9, "font-size": 28 }, "12:00"));
  }
  const tile = (box, cls) => sv("rect", { class: cls, x: box.x, y: box.y, width: box.width, height: box.height, rx: model.radius, ry: model.radius, "stroke-width": model.borderWidth });
  model.cells.forEach((cell) => {
    const { box } = cell;
    const cx = box.x + box.width / 2;
    if (cell.cellKind === "status") {
      body.push(tile(box, "pv-sev"));
      const size = fitFont(cell.label, cell.valueSize, box.width - 8);
      body.push(text({ class: "pv-sev-text", x: cx, y: box.y + box.height / 2 + size / 3, "font-size": size }, cell.label));
    } else if (cell.cellKind === "bar") {
      body.push(sv("rect", { class: "pv-track", x: box.x, y: box.y, width: box.width, height: box.height, rx: 8, ry: 8 }));
      const fill = box.height * 0.6;
      body.push(sv("rect", { class: "pv-gauge", x: box.x, y: box.y + box.height - fill, width: box.width, height: fill, rx: 8, ry: 8 }));
      body.push(text({ class: "pv-label", x: cx, y: box.y - 10, "font-size": fitFont(cell.label, PREVIEW_FONT.small, box.width + 40) }, cell.label));
      body.push(text({ class: "pv-value", x: cx, y: box.y + box.height + 30, "font-size": fitFont(cell.value, 28, box.width + 40) }, cell.value));
    } else if (cell.cellKind === "gauge") {
      body.push(text({ class: "pv-label", x: cx, y: box.y + box.height * 0.22, "font-size": cell.labelSize }, cell.label));
      body.push(text({ class: "pv-value", x: cx, y: box.y + box.height * 0.62, "font-size": fitFont(cell.value, cell.valueSize, box.width) }, cell.value));
      body.push(text({ class: "pv-label", x: cx, y: box.y + box.height * 0.86, "font-size": cell.labelSize }, cell.unit));
    } else {
      body.push(tile(box, "pv-tile"));
      body.push(text({ class: "pv-tile-label", x: cx, y: box.y + box.height * 0.26, "font-size": fitFont(cell.label, cell.labelSize, box.width - 10) }, cell.label));
      body.push(text({ class: "pv-tile-value", x: cx, y: box.y + box.height * 0.64, "font-size": fitFont(cell.value, cell.valueSize, box.width - 10) }, cell.value));
      body.push(text({ class: "pv-tile-label", x: cx, y: box.y + box.height * 0.86, "font-size": cell.labelSize }, cell.unit));
    }
  });
  if (model.arc) {
    const arc = model.arc;
    body.push(sv("path", { class: "pv-arc-track", d: arcPath(arc.box, arc.width, arc.start_angle, arc.sweep, 1), "stroke-width": arc.width }));
    body.push(sv("path", { class: "pv-arc-fill", d: arcPath(arc.box, arc.width, arc.start_angle, arc.sweep, 0.65), "stroke-width": arc.width }));
  }
  if (model.layout === "electrical" && model.textBox) {
    const box = model.textBox;
    const step = Math.min(64, box.height / Math.max(1, model.rows.length));
    const top = box.y + (box.height - step * model.rows.length) / 2;
    model.rows.forEach((row, index) => {
      const y = top + step * index + step * 0.66;
      body.push(sv("text", { class: "pv-label", x: box.x + 6, y, "font-size": PREVIEW_FONT.small }, row.label));
      body.push(sv("text", { class: "pv-value", x: box.x + box.width - 6, y, "text-anchor": "end", "font-size": PREVIEW_FONT.row }, row.kind === "instrument" ? `${row.value} ${row.unit}`.trim() : ""));
    });
  }
  if (model.footer) {
    const { pages, y } = model.footer;
    for (let index = 0; index < pages; index += 1) body.push(sv("circle", { class: index === model.pageIndex ? "pv-pagedot hi" : "pv-pagedot", cx: screen.width / 2 + (index - (pages - 1) / 2) * 22, cy: y + 14, r: 5 }));
  }
  const defs = model.round ? [sv("clipPath", { id: clip }, sv("circle", { cx: screen.width / 2, cy: screen.height / 2, r: screen.width / 2 }))] : [];
  const group = sv("g", model.round ? { "clip-path": `url(#${clip})` } : {}, body);
  return sv("svg", { class: "pv-svg", viewBox: `0 0 ${screen.width} ${screen.height}`, role: "img", "aria-label": `Approximate preview of page ${model.pageIndex + 1}, ${model.title}, ${LAYOUT_NAMES[model.layout] || model.layout}` }, defs, group);
}

// ---- data list badges and search ------------------------------------------------------------

function rowBadges(row, nameOf = (id) => id) {
  const badges = [];
  if (row.live_state === "live") badges.push({ key: "live", text: "Live", tone: "good" });
  else if (row.live_state === "no_live_data") badges.push({ key: "nolive", text: "No live data", tone: "plain" });
  if (row.group === UNCATALOGUED_GROUP || String(row.row_id || "").startsWith("unc:")) badges.push({ key: "uncatalogued", text: "Uncatalogued", tone: "warn" });
  if (Array.isArray(row.used_on) && row.used_on.length) badges.push({ key: "used", text: `Used on ${row.used_on.map(nameOf).join(", ")}`, tone: "sea" });
  return badges;
}

function rowValueText(row) {
  if (row.live_state !== "live" || row.value === undefined || row.value === null) return "";
  return `${row.value}${row.unit ? ` ${row.unit}` : ""}`;
}

function filterCatalogRows(rows, query) {
  const needle = String(query || "").trim().toLowerCase();
  if (!needle) return rows.slice();
  return rows.filter((row) => [row.label, row.group, row.path, row.id_stem].some((value) => String(value || "").toLowerCase().includes(needle)));
}

function groupCatalogRows(rows, groups) {
  const byGroup = new Map();
  rows.forEach((row) => { if (!byGroup.has(row.group)) byGroup.set(row.group, []); byGroup.get(row.group).push(row); });
  const order = [...(groups || []), ...byGroup.keys()].filter((name, index, all) => all.indexOf(name) === index && byGroup.has(name));
  return order.map((name) => ({ name, rows: byGroup.get(name) }));
}

// ---- DOM: cards -------------------------------------------------------------------------------

function themeSwatches(entry, themes) {
  const current = entry.theme || "classic";
  const ids = (entry.themes || []).map((item) => item.id);
  return h("div", { class: "swatches", role: "group", "aria-label": `Colour theme for ${displayName(entry)}` }, ids.map((id) => {
    const theme = themes?.themes?.[id];
    const description = (entry.themes.find((item) => item.id === id) || {}).description || "";
    const label = id === "high-contrast" ? "High contrast" : id.charAt(0).toUpperCase() + id.slice(1);
    const isCurrent = id === current;
    const day = theme?.day || {};
    return h("button", {
      type: "button", class: `swatch${isCurrent ? " current" : ""}`, "data-theme-id": id, "data-display": displayId(entry),
      "aria-pressed": String(isCurrent), "aria-label": `${label} theme${isCurrent ? ", current" : ""}. ${description}`, disabled: !state.displays.editable, title: description,
      vars: { "--sw-page": day.page_bg || "#000", "--sw-tile": day.tile || "#222", "--sw-value": day.value || "#fff", "--sw-label": day.label || "#aaa" },
    }, h("span", { class: "sw-chip", "aria-hidden": "true" }, h("span", { class: "sw-tile" }, h("i", { class: "sw-v" }), h("i", { class: "sw-l" }))), h("span", { class: "sw-name", text: label }), isCurrent ? h("span", { class: "sw-now", text: "Current" }) : null);
  }));
}

function checklist(entry) {
  const id = displayId(entry);
  const items = readinessItems(id, entry.readiness);
  const mark = { ok: "✓", todo: "!", unknown: "?" };
  return h("ul", { class: "checks" }, items.map((item) => h("li", { class: `check ${item.state}` },
    h("span", { class: "mark", "aria-hidden": "true", text: mark[item.state] }),
    h("div", {}, h("strong", { text: item.label }), h("span", { class: "sr", text: item.state === "ok" ? " (done)" : item.state === "todo" ? " (needs attention)" : " (unknown)" }),
      item.hint ? h("p", { class: "small", text: item.hint }) : null,
      item.command ? h("code", { text: item.command }) : null))));
}

function pageList(entry) {
  const id = displayId(entry);
  const selected = state.displays.page[id] || 0;
  return h("ol", { class: "pages" }, entry.display.pages.map((page, index) => h("li", {},
    h("button", { type: "button", class: `pagebtn${index === selected ? " on" : ""}`, "data-preview-page": String(index), "data-display": id, "aria-pressed": String(index === selected) },
      h("strong", { text: page.title }), h("span", { class: "layout", text: LAYOUT_NAMES[page.layout] || page.layout }),
      h("span", { class: "items", text: page.items.map((item) => itemInfo(entry, item).label).join(" · ") })))));
}

function previewBlock(entry) {
  const id = displayId(entry);
  const d = state.displays;
  const index = Math.min(d.page[id] || 0, entry.display.pages.length - 1);
  const model = previewModel(entry, index, d.themes, { mode: d.mode, severity: d.severity }, liveRowsByPath(d.catalog));
  const wrap = h("div", { class: "preview-wrap", "data-preview": id });
  if (!model) { wrap.append(h("p", { class: "small muted", text: "No preview is available for this page." })); return wrap; }
  const frame = h("div", { class: `preview-frame${model.round ? " round" : ""}`, vars: model.vars }, previewSvg(model));
  wrap.append(frame, h("p", { class: "small muted", text: "Approximate preview. The display itself is the final word on spacing and fonts." }));
  return wrap;
}

function previewControls() {
  const d = state.displays;
  return h("div", { class: "pvctl" },
    h("div", { class: "seg", role: "group", "aria-label": "Preview time of day" }, [["day", "Day"], ["night", "Night"]].map(([key, text]) => h("button", { type: "button", "data-preview-mode": key, "aria-pressed": String(d.mode === key), text }))),
    h("div", { class: "seg", role: "group", "aria-label": "Preview colour for values" }, SEVERITIES.map(([key, text]) => h("button", { type: "button", "data-preview-severity": key, "aria-pressed": String(d.severity === key), text }))));
}

function displayCard(entry) {
  const id = displayId(entry);
  if (entry.error) {
    return h("article", { class: "card dcard", "aria-labelledby": `dc-${id}` }, h("h3", { id: `dc-${id}`, text: displayName(entry) }), h("p", { class: "mono small", text: id }),
      h("p", { class: "small warn-text", role: "alert", text: `This display could not be read: ${entry.error}` }));
  }
  const chip = firmwareChip(entry.readiness);
  const themeName = entry.theme === "high-contrast" ? "High contrast" : (entry.theme || "classic").replace(/^./, (ch) => ch.toUpperCase());
  const needsFlash = chip.state !== "on-screen";
  return h("article", { class: "card dcard", "aria-labelledby": `dc-${id}` },
    h("header", { class: "dhead" },
      h("div", {}, h("h3", { id: `dc-${id}`, text: entry.display.name }), h("p", { class: "small muted" }, h("span", { class: "mono", text: id }), ` · ${entry.board?.name || entry.display.board} · ${themeName} theme`)),
      h("span", { class: `chip ${chip.tone}`, "data-chip": chip.state, title: chip.hint, text: chip.label })),
    h("div", { class: "dbody" },
      h("div", { class: "dpreview" }, previewControls(), previewBlock(entry)),
      h("div", { class: "dinfo" },
        h("h4", { text: "Colour theme" }), themeSwatches(entry, state.displays.themes),
        h("p", { class: "small muted", text: `Changing the theme is done on the boat computer for now: sudo vesselstackctl display set-theme ${id} THEME` }),
        h("h4", { text: "Pages" }), pageList(entry),
        h("h4", { text: "Ready to show on the screen?" }), checklist(entry),
        needsFlash ? h("p", { class: "small" }, "After changing settings, build and flash the display:") : null,
        needsFlash ? h("code", { text: `sudo vesselstackctl display build ${id}\nsudo vesselstackctl display flash ${id} --serial DEVICE` }) : null)));
}

function renderDisplays() {
  const d = state.displays;
  const root = $("#displays-root");
  root.textContent = "";
  if (d.error) {
    $("#displays-status").textContent = "";
    $("#displays-status").append(h("div", { class: "banner fail", role: "alert" }, h("span", { class: "icon", "aria-hidden": "true", text: "!" }), h("div", { class: "msg", text: d.error })));
  } else {
    $("#displays-status").textContent = d.loading ? "Loading displays…" : "";
  }
  if (d.loaded && !d.error && !d.list.length) {
    root.append(h("div", { class: "card flat empty" }, h("h2", { text: "No displays yet" }), h("p", { text: "Add your first display from the boat computer, then come back here to see its pages and preview." }), h("code", { text: "sudo vesselstackctl display add helm --board BOARD --name Helm" })));
  }
  d.list.forEach((entry) => root.append(displayCard(entry)));
  $("#displays-updated").textContent = d.loadedAt ? `Updated ${new Date(d.loadedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
}

function rerenderPreviews() {
  state.displays.list.forEach((entry) => {
    if (entry.error) return;
    const old = $$(`[data-preview="${displayId(entry)}"]`)[0];
    if (old) old.replaceWith(previewBlock(entry));
  });
  $$("[data-preview-mode]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.previewMode === state.displays.mode)));
  $$("[data-preview-severity]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.previewSeverity === state.displays.severity)));
}

function renderDataBrowser() {
  const d = state.displays;
  const root = $("#catalog-root");
  root.textContent = "";
  const catalog = d.catalog;
  const note = $("#catalog-note");
  if (!catalog) { note.textContent = d.loading ? "Loading…" : "The data list is not available yet."; return; }
  const live = catalog.live || {};
  note.textContent = live.requested && live.ok ? "Values marked Live are being received from Signal K right now." : live.requested ? `Live values are unavailable${live.error ? ` (${live.error})` : ""}. The list shows common values without live data.` : "Live values were not requested.";
  const names = new Map(d.list.map((entry) => [displayId(entry), displayName(entry)]));
  const nameOf = (id) => names.get(id) || id;
  const rows = filterCatalogRows(catalog.rows || [], d.query);
  if (!rows.length) { root.append(h("p", { class: "muted", text: d.query ? "Nothing matches that search." : "No data is listed." })); return; }
  groupCatalogRows(rows, catalog.groups).forEach((group, index) => {
    const details = h("details", { class: "dgroup", open: Boolean(d.query) || index === 0 }, h("summary", {}, h("strong", { text: group.name }), h("span", { class: "n", text: ` ${group.rows.length}` })));
    details.append(h("ul", { class: "drows" }, group.rows.map((row) => {
      const value = rowValueText(row);
      return h("li", { class: "drow" },
        h("div", { class: "dmain" }, h("strong", { text: row.label }), h("span", { class: "mono small muted", text: row.path })),
        h("div", { class: "dside" }, value ? h("span", { class: "dvalue", text: value }) : null, rowBadges(row, nameOf).map((badge) => h("span", { class: `chip ${badge.tone}`, "data-badge": badge.key, text: badge.text }))));
    })));
    root.append(details);
  });
}

async function loadDisplays(force = false) {
  const d = state.displays;
  if (!state.token || d.loading) return;
  if (!force && d.loaded && Date.now() - d.loadedAt < DISPLAYS_STALE_MS) return;
  const session = state.session;
  const epoch = ++d.epoch;
  d.loading = true;
  renderDisplays();
  try {
    const [list, themes, catalog] = await Promise.all([api("/api/displays"), api("/api/displays/themes"), api("/api/displays/catalog?live=1").catch((error) => ({ error }))]);
    if (session !== state.session || epoch !== d.epoch) return;
    d.list = list.displays || [];
    d.themes = themes;
    d.catalog = catalog.error ? null : catalog;
    d.error = "";
    d.loaded = true;
    d.loadedAt = Date.now();
    if (catalog.error) d.error = `The data list could not be loaded: ${catalog.error.message}`;
  } catch (error) {
    if (session !== state.session || epoch !== d.epoch) return;
    if (error.status === 401) { requireReauth("Your session ended."); return; }
    d.error = `Couldn't load the displays: ${error.message}`;
  } finally {
    if (session === state.session && epoch === d.epoch) { d.loading = false; renderDisplays(); renderDataBrowser(); }
  }
}

function resetDisplays() {
  state.displays = freshDisplays();
  $("#displays-root").textContent = "";
  $("#displays-status").textContent = "";
  $("#catalog-root").textContent = "";
  $("#catalog-note").textContent = "";
  $("#displays-updated").textContent = "";
}

function onDisplaysClick(event) {
  const target = event.target.closest?.("[data-preview-mode], [data-preview-severity], [data-preview-page], #displays-refresh");
  if (!target) return;
  const d = state.displays;
  if (target.dataset.previewMode) d.mode = target.dataset.previewMode;
  else if (target.dataset.previewSeverity) d.severity = target.dataset.previewSeverity;
  else if (target.dataset.previewPage !== undefined) {
    d.page[target.dataset.display] = Number(target.dataset.previewPage) || 0;
    renderDisplays();
    $$("[data-preview-page]").find((item) => item.dataset.display === target.dataset.display && item.dataset.previewPage === target.dataset.previewPage)?.focus();
    return;
  }
  else if (target.id === "displays-refresh") { loadDisplays(true); return; }
  rerenderPreviews();
}


// ---------------------------------------------------------------- wiring

$("#login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const token = normalizeToken($("#token").value);
  if (!token) { loginError("Paste the token first. It is the long value after CONTROL_PANEL_TOKEN= on the boat computer."); $("#token").focus(); return; }
  loginError("");
  state.token = token;
  unlock();
});
$("#token").addEventListener("input", () => loginError(""));
$("#show-token").addEventListener("change", (event) => { $("#token").type = event.target.checked ? "text" : "password"; });

$$("[data-tab]").forEach((button) => button.addEventListener("click", () => selectTab(button.dataset.tab)));
$$("[data-tab]").forEach((button, index, tabs) => button.addEventListener("keydown", (event) => {
  const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
  const edge = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
  if (!step && edge === null) return;
  event.preventDefault();
  const target = edge !== null ? tabs[edge] : tabs[(index + step + tabs.length) % tabs.length];
  target.click();
  target.focus();
}));

document.addEventListener("click", (event) => {
  const target = event.target.closest?.("[data-action], [data-tab-link], [data-dismiss-notice], #banner-x, [data-install]");
  if (!target) return;
  if (target.dataset.action) runAction(target.dataset.action);
  else if (target.dataset.tabLink) selectTab(target.dataset.tabLink);
  else if (target.dataset.dismissNotice) setNotice(target.dataset.dismissNotice, "");
  else if (target.dataset.install) installRelease(target.dataset.install);
  else if (target.id === "banner-x") {
    if (state.operation?.id != null) state.dismissed.add(operationKey(state.operation));
    $("#banner").innerHTML = "";
    state.bannerHtml = "";
  }
});

$("#component-grid").addEventListener("click", (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  if (target.dataset.more) {
    state.menuOpen = state.menuOpen === target.dataset.more ? null : target.dataset.more;
    state.pendingFocus = state.menuOpen ? `${state.menuOpen}-disable` : `${target.dataset.more}-more`;
    renderComponents(state.components, true);
  } else if (target.dataset.disable) {
    state.menuOpen = null;
    runComponent(target.dataset.disable, "remove");
    renderComponents(state.components, true);
  } else if (target.dataset.verb) runComponent(target.dataset.component, target.dataset.verb);
  else if (target.dataset.configureSection) jumpToConfiguration(target.dataset.configureSection);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.menuOpen && !$("#confirm-dialog").open) {
    const id = state.menuOpen;
    state.menuOpen = null;
    state.pendingFocus = `${id}-more`;
    renderComponents(state.components, true);
  }
});

$("#config-form").addEventListener("click", (event) => {
  const link = event.target.closest?.("[data-jump]");
  if (!link) return;
  event.preventDefault();
  const target = $(`#s-${slug(link.dataset.jump)}`);
  if (target?.tagName === "DETAILS") target.open = true;
  target?.scrollIntoView({ behavior: "smooth", block: "start" });
});
window.addEventListener("scroll", updateScrollSpy, { passive: true });

$("#displays").addEventListener("click", onDisplaysClick);
$("#catalog-search").addEventListener("input", (event) => { state.displays.query = event.target.value; renderDataBrowser(); });
$("#toast-close").addEventListener("click", () => { $("#toast").className = ""; });
$("#retry").addEventListener("click", () => { state.reconnectAttempts = 0; clearTimeout(state.reconnectTimer); refreshStatus(); });
$("#sb-save").addEventListener("click", saveConfiguration);
$("#sb-discard").addEventListener("click", discardEdits);
$("#run-update").addEventListener("click", () => runAction("update", { source_directory: $("#update-source").value.trim() }));
$("#check-releases").addEventListener("click", checkReleases);
$("#include-lab").addEventListener("change", () => {
  // Choosing a channel never contacts GitHub; the next check uses it.
  state.releaseEpoch += 1;
  state.releaseCheckPending = false;
  state.releases = [];
  state.releaseChecked = false;
  state.releaseError = "";
  $("#early-note").hidden = !$("#include-lab").checked;
  renderUpdates();
});
$("#lock").addEventListener("click", () => lock());
$("#component-search").addEventListener("input", (event) => { state.componentQuery = event.target.value; renderComponents(state.components, true); });
$$("[data-component-filter]").forEach((button) => button.addEventListener("click", () => {
  state.componentFilter = button.dataset.componentFilter;
  renderComponents(state.components, true);
}));
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.token && !state.busy) refreshStatus(); });

window.addEventListener("beforeunload", (event) => { if (isConfigDirty()) { event.preventDefault(); event.returnValue = ""; } });

if (state.token) {
  $("#token").value = state.token;
  unlock();
}
