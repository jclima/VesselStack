const state = {
  token: sessionStorage.getItem("vesselstack_control_token") || "",
  config: null,
  poller: null,
  components: [],
  componentFilter: "all",
  componentQuery: "",
  busy: false,
  saving: false,
  session: 0,
  operationRunning: false,
  pendingOperation: null,
  lastOperationId: null,
  releaseEpoch: 0,
  releaseChannel: "stable",
  releases: [],
  bootId: null,
  reconnectTimer: null,
  reconnectAttempts: 0,
  reauthDirty: false,
  releaseCheckPending: false,
  installedVersion: "unknown",
  runningVersion: "unknown",
  releaseCheckedAt: "",
  rollbackPath: "",
};

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
  if (!response.ok) { const error = new Error(payload.error || `${response.status} ${response.statusText}`); error.status = response.status; throw error; }
  return payload;
}

const OPTION_LABELS = {
  BOAT_UNITS: { us_customary: "US customary", metric: "Metric" },
  SIGNALK_MODE: { docker: "Docker (built-in)", existing: "Existing server", native: "Native (on this Pi)" },
  PROVIDER: { "": "None", local: "Local (built-in)", codex_cli: "Codex CLI", claude_cli: "Claude CLI", openai: "OpenAI", vercel: "Vercel AI Gateway", bedrock: "Amazon Bedrock", google: "Google Gemini", ollama: "Ollama", openai_compatible: "OpenAI-compatible API" },
};
const optionLabel = (key, choice) => {
  const table = OPTION_LABELS[key] || (key.startsWith("BOAT_CHAT_") && key.endsWith("PROVIDER") ? OPTION_LABELS.PROVIDER : null);
  return (table && table[choice]) || choice || "None";
};
const OPERATION_NAMES = {
  apply: "Apply configuration", install: "Install and start", preflight: "Preflight check", backup: "Backup",
  "bootstrap-history": "Provision history", update: "Reviewed release update", "update-release": "Release update",
  "restart-panel": "Control Panel restart", start: "Start", stop: "Stop", restart: "Restart",
};
const operationName = (label) => OPERATION_NAMES[label] || String(label || "Operation");
const componentName = (component) => String(component.label || component.id || "").replace(/^Signalk\b/, "Signal K");
const LIVE_DATA_NOTE = "Live instrument data will pause briefly.";

function toast(message, isError = false) {
  const node = $("#toast");
  $("#toast-text").textContent = message;
  node.className = isError ? "show error" : "show";
  $("#toast-close").hidden = !isError;
  clearTimeout(node.timer);
  // Errors stay until dismissed; confirmations fade on their own.
  if (!isError) node.timer = setTimeout(() => { node.className = ""; }, 4200);
}

function setConnected(connected) {
  $("#connection-dot").classList.toggle("online", connected);
  $("#connection-label").textContent = connected ? "Connected" : "Locked";
}

function lock() {
  if (isConfigDirty() && !window.confirm("Discard unsaved configuration edits and lock the panel?")) return;
  clearTimeout(state.poller);
  clearTimeout(state.reconnectTimer);
  state.poller = null;
  state.token = "";
  state.session += 1;
  state.saving = false;
  state.config = null;
  state.components = [];
  state.releases = [];
  state.busy = false;
  state.operationRunning = false;
  state.pendingOperation = null;
  state.lastOperationId = null;
  state.bootId = null;
  state.rollbackPath = "";
  $("#toast").className = "";
  $("#config-form").innerHTML = "";
  $("#operation-output").textContent = "No operation has run in this session.";
  $("#operation-state").textContent = "Idle";
  $("#operation-progress").hidden = true;
  $("#release-select").innerHTML = "<option>Unlock to check for releases</option>";
  $("#release-select").disabled = true;
  $("#install-release").disabled = true;
  sessionStorage.removeItem("vesselstack_control_token");
  $("#app").hidden = true;
  $("#lock").hidden = true;
  $("#login").hidden = false;
  $("#token").value = "";
  $("#login-error").textContent = "";
  setConnected(false);
}

function releaseUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "github.com" && /^\/[^/]+\/[^/]+\/releases\/tag\/[^/]+$/.test(url.pathname) ? url.href : "";
  } catch (_) { return ""; }
}

function selectedRelease() {
  const id = Number($("#release-select")?.value);
  return state.releases.find((release) => release.id === id && releaseEligible(release)) || null;
}

function versionTuple(value) {
  const match = String(value || "").match(/^(\d+)\.(\d+)\.(\d+)/);
  return match ? match.slice(1).map(Number) : null;
}

function releaseEligible(release) {
  const candidate = versionTuple(release.version);
  const installed = versionTuple(state.installedVersion);
  if (!candidate || !installed) return false;
  for (let index = 0; index < 3; index += 1) {
    if (candidate[index] !== installed[index]) return candidate[index] > installed[index];
  }
  return false;
}

function updateReleaseControls() {
  const release = selectedRelease();
  $("#install-release").disabled = state.releaseCheckPending || state.busy || !release;
  $("#release-select").disabled = state.releaseCheckPending || state.busy || !state.releases.some(releaseEligible);
  $("#install-release").textContent = release ? `Back up & install ${release.version}` : "Back up & install version";
}

function renderReleases(data) {
  state.installedVersion = data.installed_version || state.installedVersion;
  state.runningVersion = data.running_version || state.runningVersion;
  state.releaseCheckedAt = data.checked_at ? new Date(data.checked_at).toLocaleString() : new Date().toLocaleString();
  state.releases = Array.isArray(data.releases) ? data.releases.filter((release) => Number.isInteger(release.id) && release.tag && release.version && release.name && releaseUrl(release.html_url)) : [];
  $("#release-versions").textContent = `Installed: ${state.installedVersion} · Running: ${state.runningVersion} · Checked ${state.releaseCheckedAt}`;
  $("#release-warning").hidden = data.channel !== "lab";
  $("#release-warning").textContent = data.channel === "lab" ? "Early test (lab) releases are for testing. Read the release notes before installing." : "";
  $("#release-select").innerHTML = state.releases.length ? state.releases.map((release) => `<option value="${escapeHtml(release.id)}">${escapeHtml(release.version)} — ${escapeHtml(release.name)}${release.prerelease ? " (early test release)" : ""}</option>`).join("") : "<option>No newer releases available</option>";
  updateReleaseControls();
  updateReleaseDetail();
}

function updateReleaseDetail() {
  const release = selectedRelease();
  $("#release-detail").innerHTML = release ? `${escapeHtml(release.name)} · ${escapeHtml(release.tag)} · <a href="${releaseUrl(release.html_url)}" target="_blank" rel="noreferrer">View fixed GitHub release</a>` : "";
}

async function checkReleases() {
  const session = state.session;
  const epoch = ++state.releaseEpoch;
  const channel = $("#include-lab").checked ? "lab" : "stable";
  state.releaseChannel = channel;
  state.releaseCheckPending = true;
  updateReleaseControls();
  try {
    const data = await api(`/api/releases?channel=${channel}`);
    if (session !== state.session || epoch !== state.releaseEpoch || !state.token) return;
    renderReleases(data);
  } catch (error) {
    if (session === state.session && epoch === state.releaseEpoch && state.token) {
      if (error.status === 401) lock();
      else toast(`Could not check for updates: ${error.message}`, true);
    }
  } finally {
    if (session === state.session && epoch === state.releaseEpoch && state.token) {
      state.releaseCheckPending = false;
      $("#check-releases").disabled = state.busy;
      updateReleaseControls();
    }
  }
}

function operationSucceeded(operation) {
  if (operation?.state !== "succeeded") return false;
  // Only these exact operation labels require boot/version confirmation. The
  // legacy operation store omits phase for ordinary backup/apply/component work.
  if (!["update-release", "restart-panel"].includes(operation.label)) return true;
  return operation.phase === "complete" && Boolean(operation.target_version && operation.running_version === operation.target_version);
}

async function installSelectedRelease() {
  const release = selectedRelease();
  if (!release || state.busy) return;
  if (isConfigDirty() && !window.confirm("You have unsaved configuration edits. They will be kept but not applied by this update. Continue?")) return;
  if (!window.confirm(`Back up and install ${release.version}? Services and this panel may be interrupted, and a writable backup mount is required.`)) return;
  startOperation({ action: "update-release", release_id: release.id, channel: state.releaseChannel });
}

async function unlock() {
  const session = ++state.session;
  try {
    const config = await api("/api/config");
    if (session !== state.session) return;
    state.config = config;
    sessionStorage.setItem("vesselstack_control_token", state.token);
    $("#login").hidden = true;
    $("#app").hidden = false;
    $("#lock").hidden = false;
    setConnected(true);
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
    setConnected(false);
    $("#login-error").textContent = error.message;
    sessionStorage.removeItem("vesselstack_control_token");
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
  $("#login-error").textContent = `${message} Unlock again to continue. Unsaved edits are preserved.`;
  $("#token").value = "";
  setConnected(false);
}

function statusClass(value) {
  const text = String(value || "unknown").toLowerCase().trim();
  if (["active", "running", "succeeded"].includes(text)) return "active";
  if (["failed", "error", "exited"].includes(text)) return "failed";
  return "";
}

function renderComponents(components) {
  state.components = components || [];
  const query = state.componentQuery.toLowerCase();
  const filtered = state.components.filter((component) => {
    const availability = component.enabled ? "enabled" : "available";
    const haystack = `${componentName(component)} ${component.description || ""}`.toLowerCase();
    return (state.componentFilter === "all" || state.componentFilter === availability) && (!query || haystack.includes(query));
  });
  const active = state.components.filter((item) => item.enabled && statusClass(item.state) === "active").length;
  const enabled = state.components.filter((item) => item.enabled).length;
  $("#component-overview").innerHTML = `<div><strong>${state.components.length}</strong><span>Total</span></div><div><strong>${enabled}</strong><span>Enabled</span></div><div><strong>${active}</strong><span>Active</span></div><div><strong>${state.components.length - enabled}</strong><span>Available</span></div>`;
  $("#component-grid").innerHTML = filtered.length ? filtered.map((component) => {
    const disabled = !component.enabled;
    const stateLabel = escapeHtml(component.state || "unknown");
    const availabilityLabel = disabled ? "available" : "enabled";
    const componentId = escapeHtml(component.id);
    const componentLabel = escapeHtml(componentName(component));
    const description = escapeHtml(component.description || (component.kind === "container" ? "Docker Compose service" : "Host systemd service"));
    let actions;
    const fallbackSections = { "signalk-native": "Signal K", socketcan: "Signal K", firewall: "Network", "ais-catcher": "AIS" };
    const configureSection = component.section || fallbackSections[component.id];
    const configure = configureSection ? `<button data-configure-section="${escapeHtml(configureSection)}">Configure</button>` : "";
    const controls = `<button data-component="${componentId}" data-verb="start" data-config-disabled="${disabled}" ${disabled ? "disabled" : ""}>Start</button><button data-component="${componentId}" data-verb="restart" data-config-disabled="${disabled}" ${disabled ? "disabled" : ""}>Restart</button><button data-component="${componentId}" data-verb="stop" data-config-disabled="${disabled}" ${disabled ? "disabled" : ""}>Stop</button>`;
    if (component.manageable) {
      const retryRemoval = disabled && ["running", "restarting", "paused"].includes(String(component.state || "").toLowerCase());
      const lifecycleAction = retryRemoval
        ? `<button class="danger" data-component="${componentId}" data-verb="remove">Retry removal</button>`
        : `<button data-component="${componentId}" data-verb="add">Add component</button>`;
      actions = disabled ? `<div class="component-actions">${configure}${lifecycleAction}</div>` : `<div class="component-actions">${configure}${controls}<button class="danger" data-component="${componentId}" data-verb="remove">Remove</button></div>`;
    } else if (component.id === "control-panel") {
      actions = "<p>Managed from the recovery shell</p>";
    } else {
      actions = `<div class="component-actions">${configure}${controls}</div>`;
    }
    return `<article class="component">
      <div><div class="component-top"><h3>${componentLabel}</h3><div class="component-status"><span class="pill ${statusClass(component.state)}">${stateLabel}</span><span class="availability">${availabilityLabel}</span></div></div><p>${description}</p></div>
      ${actions}
    </article>`;
  }).join("") : `<div class="empty-state"><strong>No matching components</strong><p>Try a different search or filter.</p></div>`;
  $$('[data-component]').forEach((button) => button.addEventListener("click", () => runComponent(button.dataset.component, button.dataset.verb)));
  $$('[data-configure-section]').forEach((button) => button.addEventListener("click", () => jumpToConfiguration(button.dataset.configureSection)));
  if (state.busy) setBusy(true);
}

function jumpToConfiguration(sectionName) {
  $('[data-tab="configuration"]')?.click();
  const section = $$(".config-section").find((item) => item.querySelector("h3")?.textContent === sectionName);
  section?.scrollIntoView({ behavior: "smooth", block: "start" });
  section?.querySelector("input:not([disabled]), select:not([disabled])")?.focus({ preventScroll: true });
}

function renderOperation(operation) {
  if (!operation && state.operationRunning) {
    setBusy(true);
    $("#operation-progress").hidden = false;
    $("#operation-progress").textContent = "Operation status is unavailable. Controls stay locked; retry status or unlock to recover the session.";
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
  const badge = $("#operation-state");
  badge.textContent = label;
  badge.className = `pill ${statusClass(label)} ${label}`;
  const active = Boolean(state.pendingOperation || label === "running" || label === "verification-required" || (label !== "succeeded" && label !== "failed" && current.phase && current.phase !== "complete"));
  setBusy(active);
  state.operationRunning = active;
  const progress = $("#operation-progress");
  progress.hidden = !active && !current.phase;
  progress.textContent = state.pendingOperation && !["running", "succeeded", "failed"].includes(current.state) ? "Operation status is unavailable. Controls stay locked; retry status or unlock to recover the session." : (label === "failed" ? "Update failed. Review the operation log and backend guidance, then retry when ready." : (current.phase ? `${current.label || "Operation"}: ${current.phase}${current.target_version ? ` · ${current.target_version}` : ""}${current.backup_path ? ` · backup ${current.backup_path}` : ""}` : ""));
  const output = (current.output || []).join("\n");
  $("#operation-output").textContent = output || "No operation has run in this session.";
  if (active) {
    clearTimeout(state.poller);
    state.poller = setTimeout(refreshStatus, 1200);
  }
  if (label === "failed") toast("Operation failed. Review guidance and retry.", true);
  if (verified && current.label === "update-release") toast(`Installed ${current.target_version}. The running panel confirmed the new version.`);
  if (verified && current.label === "restart-panel") toast("Control panel restart completed and was verified.");
}

async function refreshStatus() {
  const session = state.session;
  try {
    const data = await api("/api/status");
    if (session !== state.session || !state.token) return;
    if (state.bootId && data.boot_id && state.bootId !== data.boot_id) {
      if (isConfigDirty()) {
        toast("The panel restarted, but unsaved configuration edits were preserved. Reload after saving or discarding them.", true);
      } else {
        state.bootId = data.boot_id;
        window.location.reload();
        return;
      }
    }
    state.bootId = data.boot_id || state.bootId;
    state.reconnectAttempts = 0;
    if (data.operation?.id != null) state.lastOperationId = data.operation.id;
    if (data.installed_version) state.installedVersion = data.installed_version;
    if (data.running_version) state.runningVersion = data.running_version;
    $("#release-versions").textContent = `Installed: ${state.installedVersion} · Running: ${state.runningVersion}${state.releaseCheckedAt ? ` · Checked ${state.releaseCheckedAt}` : ""}`;
    renderComponents(data.components);
    renderOperation(data.operation);
    const active = data.components.filter((item) => item.enabled && statusClass(item.state) === "active").length;
    const enabled = data.components.filter((item) => item.enabled).length;
    $("#summary").textContent = `${active} of ${enabled} enabled components are active.`;
    setConnected(true);
  } catch (error) {
    if (session !== state.session || !state.token) return;
    if (error.status === 401) { requireReauth("Session expired."); return; }
    setConnected(false);
    setBusy(true);
    state.reconnectAttempts += 1;
    $("#component-grid").innerHTML = `<div class="empty-state"><strong>Reconnecting…</strong><p>${escapeHtml(error.message)} Controls stay locked until the backend confirms state. Retry ${Math.min(state.reconnectAttempts, 5)} of 5.</p></div>`;
    clearTimeout(state.reconnectTimer);
    if (state.reconnectAttempts <= 5) state.reconnectTimer = setTimeout(refreshStatus, Math.min(2000 * state.reconnectAttempts, 8000));
    else toast("Reconnect timed out. Use Unlock to retry the session.", true);
  }
}

function groupFields(fields) {
  return fields.reduce((groups, item) => {
    (groups[item.section] ||= []).push(item);
    return groups;
  }, {});
}

function fieldMarkup(spec, value) {
  const id = `setting-${spec.key}`;
  const help = spec.description ? `<small id="${id}-help">${escapeHtml(spec.description)}</small>` : "";
  const described = spec.description ? ` aria-describedby="${id}-help"` : "";
  const source = spec.source === "chat" ? "Boat Chat" : "VesselStack";
  const sourceNote = `<span class="field-source">${source}${spec.read_only ? " · read-only" : ""}</span>`;
  let input;
  if (spec.kind === "select") {
    input = `<select id="${id}" name="${spec.key}" data-read-only="${spec.read_only}"${described} ${spec.read_only ? "disabled" : ""}>${spec.choices.map((choice) => `<option value="${escapeHtml(choice)}" ${choice === value ? "selected" : ""}>${escapeHtml(optionLabel(spec.key, choice))}</option>`).join("")}</select>`;
  } else if (spec.kind === "boolean") {
    input = `<select id="${id}" name="${spec.key}" data-read-only="${spec.read_only}"${described} ${spec.read_only ? "disabled" : ""}><option value="false" ${value !== "true" ? "selected" : ""}>Disabled</option><option value="true" ${value === "true" ? "selected" : ""}>Enabled</option></select>`;
  } else {
    const type = spec.kind === "secret" ? "password" : ["number", "port"].includes(spec.kind) ? "number" : "text";
    const shown = spec.kind === "secret" ? "" : escapeHtml(value || "");
    const placeholder = spec.kind === "secret" && value?.configured ? "Configured — leave blank to keep" : "";
    const numeric = spec.kind === "number" ? 'step="any"' : spec.kind === "port" ? 'step="1" min="1" max="65535"' : "";
    input = `<input id="${id}" name="${spec.key}" type="${type}" data-read-only="${spec.read_only}"${described} value="${shown}" placeholder="${placeholder}" ${numeric} ${spec.read_only ? "disabled" : ""}>`;
  }
  const clear = spec.kind === "secret" && value?.configured && !spec.read_only
    ? `<label class="clear-secret"><input type="checkbox" data-clear-secret="${spec.key}"> Clear stored value</label>`
    : "";
  return `<div class="field ${spec.kind}"><label for="${id}">${escapeHtml(spec.label)}${sourceNote}</label>${input}${clear}${help}</div>`;
}

function renderConfiguration(config) {
  const sections = groupFields(config.fields);
  $("#config-form").innerHTML = Object.entries(sections).map(([name, fields]) => `
    <section class="config-section"><h3>${escapeHtml(name)}</h3><div class="field-grid">${fields.map((spec) => fieldMarkup(spec, config.values[spec.key])).join("")}</div></section>
  `).join("");
  $("#config-form").oninput = () => updateConfigStatus();
  $("#config-form").onchange = () => updateConfigStatus();
  updateConfigStatus();
}

function currentSettings() {
  const form = new FormData($("#config-form"));
  const settings = Object.fromEntries(form.entries());
  // FormData omits disabled controls. Busy-state freezes the form, so read
  // editable controls directly to keep unsaved edits visible to dirty checks.
  $$("#config-form input[name], #config-form select[name]").forEach((control) => {
    if (control.dataset.readOnly !== "true" && control.type !== "checkbox") settings[control.name] = control.value;
  });
  $$('[data-clear-secret]:checked').forEach((checkbox) => { settings[checkbox.dataset.clearSecret] = null; });
  return settings;
}

function defaultFieldValue(spec, original) {
  if (spec.kind === "secret") return "";
  if (original !== undefined && original !== null && original !== "") return original;
  if (spec.kind === "boolean") return "false";
  if (spec.kind === "select") return spec.choices?.[0] || "";
  return "";
}

function isConfigDirty() {
  if (!state.config) return false;
  const current = currentSettings();
  return state.config.fields.some((spec) => {
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
  });
}

function updateConfigStatus() {
  const dirty = isConfigDirty();
  const rollback = state.rollbackPath ? ` Rollback copy of the previous configuration: ${state.rollbackPath}` : "";
  $("#config-status").textContent = (dirty ? "Unsaved changes." : "All changes saved.") + rollback;
  $("#config-status").classList.toggle("dirty", dirty);
  $("#apply-config").disabled = dirty || state.busy || state.saving;
  $("#save-config").disabled = !dirty || state.busy || state.saving;
  $("#discard-config").disabled = !dirty || state.busy || state.saving;
}

async function saveConfiguration() {
  if (!isConfigDirty()) return toast("No configuration changes to save.");
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
    toast(result.backup ? `Configuration saved. Rollback copy: ${result.backup}` : "Configuration saved. Apply it when ready.");
  } catch (error) {
    if (session !== state.session || !state.token) return;
    controls.forEach((control) => { control.disabled = control.dataset.wasDisabled === "true"; });
    toast(error.message, true);
  } finally {
    if (session === state.session && state.token) {
      state.saving = false;
      updateConfigStatus();
    }
  }
}

async function startOperation(payload) {
  const session = state.session;
  if (state.busy) return;
  state.pendingOperation = { baselineId: state.lastOperationId, operationId: null };
  setBusy(true);
  try {
    const operation = await api(payload.component ? "/api/component" : "/api/action", { method: "POST", body: JSON.stringify(payload) });
    if (session !== state.session || !state.token) return;
    if (state.pendingOperation) state.pendingOperation.operationId = operation.id ?? null;
    renderOperation(operation);
    toast(`${operationName(operation.label)} started. Progress appears in the operation log below.`);
    refreshStatus();
  } catch (error) {
    if (session !== state.session || !state.token) return;
    if (error.status === 401) { requireReauth("Session expired."); return; }
    if (error.status >= 400 && error.status < 500) {
      state.pendingOperation = null;
      setBusy(false);
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

function runAction(action, extra = {}) {
  const confirmations = {
    install: "Install and start the configured VesselStack components?",
    apply: "Apply the saved configuration to the installed system?",
    start: `Start the managed VesselStack services? ${LIVE_DATA_NOTE}`,
    restart: `Restart the managed VesselStack services? ${LIVE_DATA_NOTE}`,
    stop: "Stop the managed VesselStack services? The Control Panel will remain available.",
    update: "Create a backup and apply this reviewed release? The panel and services may be interrupted, and a writable backup mount is required.",
  };
  if (confirmations[action] && !window.confirm(confirmations[action])) return;
  startOperation({ action, ...extra });
}

function runComponent(component, action) {
  const known = state.components.find((item) => item.id === component);
  const name = known ? componentName(known) : component;
  if (action === "remove" && !window.confirm(`Remove ${name}? Existing data is preserved, but integrations may stop working.`)) return;
  if (action === "stop" && !window.confirm(`Stop ${name}? ${LIVE_DATA_NOTE}`)) return;
  if (action === "restart" && !window.confirm(`Restart ${name}? ${LIVE_DATA_NOTE}`)) return;
  if (action === "start" && !window.confirm(`Start ${name}? ${LIVE_DATA_NOTE}`)) return;
  startOperation({ component, action });
}

function setBusy(busy) {
  state.busy = busy;
  state.operationRunning = busy;
  $$('[data-action], #save-config, #discard-config, #run-update, #check-releases, #release-select, #install-release, #restart-panel, [data-component]').forEach((button) => { button.disabled = busy || button.dataset.configDisabled === "true"; });
  $("#refresh").disabled = false;
  $$("#config-form input, #config-form select").forEach((control) => { control.disabled = busy || state.saving || control.dataset.readOnly === "true"; });
  $("#check-releases").disabled = busy || state.releaseCheckPending;
  updateReleaseControls();
  updateConfigStatus();
}

$("#login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  state.token = $("#token").value.trim();
  unlock();
});
$$('[data-tab]').forEach((button) => button.addEventListener("click", () => {
  $$('[data-tab]').forEach((item) => {
    const selected = item === button;
    item.classList.toggle("active", selected);
    item.setAttribute("aria-selected", String(selected));
    item.tabIndex = selected ? 0 : -1;
  });
  $$('.tab-panel').forEach((panel) => { panel.hidden = panel.id !== button.dataset.tab; });
}));
$$('[data-tab]').forEach((button, index, tabs) => button.addEventListener("keydown", (event) => {
  const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
  if (!step) return;
  event.preventDefault();
  const target = tabs[(index + step + tabs.length) % tabs.length];
  target.click();
  target.focus();
}));
$("#toast-close").addEventListener("click", () => { $("#toast").className = ""; });
$$('[data-action]').forEach((button) => button.addEventListener("click", () => runAction(button.dataset.action)));
$("#refresh").addEventListener("click", refreshStatus);
$("#save-config").addEventListener("click", saveConfiguration);
$("#discard-config").addEventListener("click", () => { if (!isConfigDirty() || window.confirm("Discard unsaved configuration edits?")) renderConfiguration(state.config); });
$("#run-update").addEventListener("click", () => runAction("update", { source_directory: $("#update-source").value.trim() }));
$("#check-releases").addEventListener("click", checkReleases);
$("#include-lab").addEventListener("change", checkReleases);
$("#release-select").addEventListener("change", updateReleaseDetail);
$("#install-release").addEventListener("click", installSelectedRelease);
$("#restart-panel").addEventListener("click", () => { if (window.confirm("Restart the Control Panel? The panel will briefly disconnect.")) startOperation({ action: "restart-panel" }); });
$("#lock").addEventListener("click", lock);
$("#component-search").addEventListener("input", (event) => { state.componentQuery = event.target.value; renderComponents(state.components); });
$$('[data-component-filter]').forEach((button) => button.addEventListener("click", () => {
  state.componentFilter = button.dataset.componentFilter;
  $$('[data-component-filter]').forEach((item) => {
    item.classList.toggle("active", item === button);
    item.setAttribute("aria-pressed", String(item === button));
  });
  renderComponents(state.components);
}));
$$('[data-tab]').forEach((button) => button.addEventListener("click", (event) => {
  // Only a pointer click moves focus into the form; keyboard tab navigation stays on the tab.
  if (button.dataset.tab === "configuration" && state.config && event.detail > 0) setTimeout(() => $("#config-form input:not([disabled]), #config-form select:not([disabled])")?.focus(), 0);
}));

window.addEventListener("beforeunload", (event) => { if (isConfigDirty()) { event.preventDefault(); event.returnValue = ""; } });

if (state.token) {
  $("#token").value = state.token;
  unlock();
}
