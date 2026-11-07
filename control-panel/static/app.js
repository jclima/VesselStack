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

// ---------------------------------------------------------------- backup choice (drive not mounted)

const SKIP_ACK_TEXT = "I understand there is no backup to restore if this update fails";
const mountFolder = (path) => {
  const parts = String(path || "").split("/").filter(Boolean);
  return ["media", "mnt"].includes(parts[0]) && parts[1] ? `/${parts[0]}/${parts[1]}` : String(path || "");
};

function backupOptions(pre) {
  const folder = mountFolder(pre.path);
  const options = [];
  if (pre.fstab_mount) options.push({ id: "fstab", label: "Mount the backup drive now", detail: `It is listed in /etc/fstab for ${folder}.`, ok: "Mount and continue", backup: { mode: "mount_fstab" } });
  (pre.drives || []).forEach((drive) => options.push({
    id: `drive:${drive.uuid}`,
    label: `Mount ${drive.label ? `"${drive.label}"` : "the drive with no name"} (${drive.size}, ${drive.fstype})`,
    detail: `${drive.device} is mounted at ${folder} until the next restart. /etc/fstab is not changed.`,
    ok: "Mount and continue",
    backup: { mode: "mount_drive", uuid: drive.uuid },
  }));
  options.push({ id: "recheck", label: "I plugged the drive in; check again", ok: "Check again", action: "recheck" });
  if (pre.system_disk_once?.offered) options.push({
    id: "system", label: "Back up to the system disk this once (not recommended)",
    detail: `Saved in ${pre.system_disk_once.target || "the system backup folder"} on this computer's own disk. It will not survive a failed disk. Nothing is remembered; the next update asks again.`,
    ok: "Back up to the system disk and continue", backup: { mode: "system_disk_once", confirm: "system-disk-once" },
  });
  options.push({ id: "folder", label: "Use a different backup folder", detail: "Saves the new folder as your backup folder setting, then checks again.", ok: "Save folder and check again", action: "folder" });
  if (pre.skip?.offered) options.push({
    id: "skip", label: "Install without a backup", danger: true,
    detail: "Nothing is saved first. If the update fails there is no archive to restore from.",
    ok: "Install without a backup", backup: { mode: "skip", confirm: "install-without-backup" },
  });
  return options;
}

// Resolves to the chosen backup object, { mode: "require_mounted" } when the drive turned up while the dialog
// was open (the normal backup then works), or null when cancelled. Nothing chosen here is saved anywhere.
function askBackupChoice(spec, first) {
  const dialog = $("#backup-choice-dialog");
  if (dialog.open) return Promise.resolve(null);
  const opener = document.activeElement;
  let current = first;
  let selected = "";
  let working = false;
  let outcome = null;
  let options = [];
  const chosen = () => options.find((option) => option.id === selected);
  const refresh = () => {
    const option = chosen();
    $("#backup-choice-folder").hidden = option?.id !== "folder";
    $("#backup-choice-skip-ack").hidden = option?.id !== "skip";
    const ok = $("#backup-choice-ok");
    ok.textContent = working ? "Working…" : option?.ok || "Continue";
    ok.className = option?.danger ? "danger solid" : "primary";
    ok.disabled = working || !option
      || (option.id === "skip" && !$("#backup-choice-ack").checked)
      || (option.id === "folder" && !$("#backup-choice-path").value.trim());
    $("#backup-choice-detail").textContent = option?.detail || "";
    $("#backup-choice-detail").hidden = !option?.detail;
  };
  const render = (error = "") => {
    options = backupOptions(current);
    if (!chosen()) selected = (options.find((option) => !option.danger) || options[0]).id;
    const dirty = isConfigDirty();
    $("#backup-choice-options").innerHTML = options.map((option) => {
      const blocked = option.id === "folder" && dirty;
      return `<label class="choice${option.danger ? " danger-choice" : ""}"><input type="radio" name="backup-choice" value="${escapeHtml(option.id)}"${option.id === selected ? " checked" : ""}${blocked ? " disabled" : ""}> <span>${escapeHtml(option.label)}${blocked ? " — save or discard your unsaved Settings edits first" : ""}</span></label>`;
    }).join("");
    const notes = [
      ...(current.refused || []).map((item) => `${item.device} ${item.size} ${item.label ? `"${item.label}" ` : ""}${item.fstype}: ${item.reason}`),
      ...(current.mounted_elsewhere || []).map((item) => `${item.device} ${item.size} ${item.label ? `"${item.label}" ` : ""}${item.fstype}: already mounted at ${item.mountpoint}. Unmount it first to use it here.`),
      ...(current.system_disk_once && !current.system_disk_once.offered && current.system_disk_once.reason ? [`System disk: ${current.system_disk_once.reason}`] : []),
    ];
    $("#backup-choice-notes").innerHTML = notes.map((note) => `<li>${escapeHtml(note)}</li>`).join("");
    $("#backup-choice-notes").hidden = !notes.length;
    $("#backup-choice-error").textContent = error;
    $("#backup-choice-error").hidden = !error;
    refresh();
  };
  const folder = mountFolder(current.path);
  $("#backup-choice-title").textContent = spec.title;
  $("#backup-choice-intro").textContent = current.mount === "not_mounted"
    ? `The backup drive is not mounted (${folder}). ${spec.intro || ""} Choose how to handle the backup for this update only; nothing here is remembered.`.replace(/\s+/g, " ")
    : `The backup folder ${current.path || ""} is not on an external drive, so the normal backup cannot be required. ${spec.intro || ""} Choose how to handle the backup for this update only.`.replace(/\s+/g, " ");
  $("#backup-choice-items").innerHTML = (spec.items || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  $("#backup-choice-items").hidden = !(spec.items || []).length;
  $("#backup-choice-ack-text").textContent = SKIP_ACK_TEXT;
  $("#backup-choice-ack").checked = false;
  $("#backup-choice-path").value = "";
  $("#backup-choice-form").onsubmit = (event) => event.preventDefault();
  $("#backup-choice-options").onchange = (event) => { selected = String(event.target?.value || ""); refresh(); };
  $("#backup-choice-ack").onchange = refresh;
  $("#backup-choice-path").oninput = refresh;
  $("#backup-choice-cancel").onclick = () => dialog.close("cancel");
  $("#backup-choice-ok").onclick = async () => {
    const option = chosen();
    if (!option || working) return;
    if (option.id === "skip" && !$("#backup-choice-ack").checked) return;
    if (option.backup) {
      outcome = option.backup;
      dialog.close("ok");
      return;
    }
    const session = state.session;
    working = true;
    refresh();
    try {
      if (option.action === "folder") {
        const saved = await api("/api/config", { method: "POST", body: JSON.stringify({ settings: { VESSELSTACK_BACKUP: $("#backup-choice-path").value.trim() } }) });
        $("#backup-choice-path").value = "";
        // This option is unavailable while Settings has unsaved edits, so showing the saved values loses nothing.
        if (saved?.configuration && !isConfigDirty()) { state.config = saved.configuration; renderConfiguration(state.config); }
      }
      const next = await api("/api/backup-preflight");
      if (session !== state.session || !state.token) return;
      current = next;
      working = false;
      if (current.default_ok === true) {
        outcome = { mode: "require_mounted" };
        dialog.close("ok");
        return;
      }
      selected = "";
      render();
    } catch (error) {
      if (session !== state.session || !state.token) return;
      if (error.status === 401) { dialog.close("cancel"); requireReauth("Your session ended."); return; }
      working = false;
      render(error.message);
    }
  };
  return new Promise((resolve) => {
    dialog.onclose = () => {
      dialog.onclose = null;
      if (opener && opener.isConnected !== false && typeof opener.focus === "function") opener.focus();
      resolve(dialog.returnValue === "ok" ? outcome : null);
    };
    dialog.returnValue = "";
    render();
    dialog.showModal();
  });
}

// Asks first. When the normal mounted backup will not work, the owner picks what to do for this one
// request; otherwise the usual confirmation is shown unchanged. A failed lookup also falls back to the
// usual confirmation: the server still refuses to install without the normal backup.
async function confirmWithBackup(spec) {
  const session = state.session;
  let pre = null;
  try {
    pre = await api("/api/backup-preflight");
  } catch (error) {
    if (error.status === 401) { requireReauth("Your session ended."); return { proceed: false }; }
  }
  if (session !== state.session || !state.token || state.busy) return { proceed: false };
  if (!pre || pre.default_ok !== false) return { proceed: await ask(spec) };
  const choice = await askBackupChoice(spec, pre);
  if (!choice) return { proceed: false };
  return choice.mode === "require_mounted" ? { proceed: true } : { proceed: true, backup: choice };
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
    if (operation.backup_skipped || operation.backup_mode === "skip") return { running: `Installing ${target} without a backup…`, done: `Installed ${target} (no backup was made)`, failed: `Installing ${target} failed`, next: "No backup was made for this update, so there is no archive to restore from. Check the details before trying again." };
    return { running: `Backing up, then installing ${target}…`, done: `Installed ${target}`, failed: `Installing ${target} failed`, next: "Your boat was backed up first if the backup step finished. Check the details before trying again." };
  }
  if (label === "update" && (operation.backup_skipped || operation.backup_mode === "skip")) {
    return { running: "Updating from the folder without a backup…", done: "Update finished (no backup was made)", failed: "Update failed", next: "No backup was made for this update, so there is no archive to restore from. Check the details before trying again." };
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
  if (action === "update") {
    const decision = await confirmWithBackup(spec);
    if (!decision.proceed) return;
    startOperation({ action, ...extra, ...(decision.backup ? { backup: decision.backup } : {}) });
    return;
  }
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
  const decision = await confirmWithBackup(releaseConfirm(release));
  if (!decision.proceed) return;
  startOperation({ action: "update-release", release_id: release.id, channel: state.releaseChannel, ...(decision.backup ? { backup: decision.backup } : {}) });
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
  const body = (name) => `${info[name]?.advanced && info[name]?.lead && bySection(name).every((item) => item.read_only) ? `<p class="small read-only-note">${escapeHtml(info[name].lead)}</p>` : ""}<div class="fields">${bySection(name).map((spec) => fieldMarkup(spec, config.values[spec.key])).join("")}</div>`;
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
