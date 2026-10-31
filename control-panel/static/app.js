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
  if (!response.ok) throw new Error(payload.error || `${response.status} ${response.statusText}`);
  return payload;
}

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = isError ? "show error" : "show";
  clearTimeout(node.timer);
  node.timer = setTimeout(() => { node.className = ""; }, 4200);
}

function setConnected(connected) {
  $("#connection-dot").classList.toggle("online", connected);
  $("#connection-label").textContent = connected ? "Connected" : "Locked";
}

function lock() {
  if (isConfigDirty() && !window.confirm("Discard unsaved configuration edits and lock the panel?")) return;
  clearTimeout(state.poller);
  state.poller = null;
  state.token = "";
  state.session += 1;
  state.saving = false;
  state.config = null;
  state.components = [];
  $("#config-form").innerHTML = "";
  $("#operation-output").textContent = "No operation has run in this session.";
  $("#operation-state").textContent = "Idle";
  sessionStorage.removeItem("vesselstack_control_token");
  $("#app").hidden = true;
  $("#lock").hidden = true;
  $("#login").hidden = false;
  $("#token").value = "";
  $("#login-error").textContent = "";
  setConnected(false);
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
    renderConfiguration(config);
    await refreshStatus();
  } catch (error) {
    if (session !== state.session) return;
    setConnected(false);
    $("#login-error").textContent = error.message;
    sessionStorage.removeItem("vesselstack_control_token");
  }
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
    const haystack = `${component.label || component.id} ${component.description || ""}`.toLowerCase();
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
    const componentLabel = escapeHtml(component.label);
    const description = escapeHtml(component.description || (component.kind === "container" ? "Docker Compose service" : "Host systemd service"));
    let actions;
    const fallbackSections = { "signalk-native": "SignalK", socketcan: "SignalK", firewall: "Network", "ais-catcher": "AIS" };
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
  const label = operation.state || "idle";
  const badge = $("#operation-state");
  badge.textContent = label;
  badge.className = `pill ${statusClass(label)} ${label}`;
  setBusy(label === "running");
  state.operationRunning = label === "running";
  const output = (operation.output || []).join("\n");
  $("#operation-output").textContent = output || "No operation has run in this session.";
  if (label === "running") {
    clearTimeout(state.poller);
    state.poller = setTimeout(refreshStatus, 1200);
  }
}

async function refreshStatus() {
  const session = state.session;
  try {
    const data = await api("/api/status");
    if (session !== state.session || !state.token) return;
    renderComponents(data.components);
    renderOperation(data.operation);
    const active = data.components.filter((item) => item.enabled && statusClass(item.state) === "active").length;
    const enabled = data.components.filter((item) => item.enabled).length;
    $("#summary").textContent = `${active} of ${enabled} enabled components are active.`;
    setConnected(true);
  } catch (error) {
    if (session !== state.session || !state.token) return;
    setConnected(false);
    $("#component-grid").innerHTML = `<div class="empty-state"><strong>Component status unavailable</strong><p>${escapeHtml(error.message)}. Check the connection and try again.</p></div>`;
    toast(error.message, true);
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
  const help = spec.description ? `<small>${spec.description}</small>` : "";
  const source = spec.source === "chat" ? "Boat Chat" : "VesselStack";
  const sourceNote = `<span class="field-source">${source}${spec.read_only ? " · read-only" : ""}</span>`;
  let input;
  if (spec.kind === "select") {
    input = `<select id="${id}" name="${spec.key}" data-read-only="${spec.read_only}" ${spec.read_only ? "disabled" : ""}>${spec.choices.map((choice) => `<option value="${choice}" ${choice === value ? "selected" : ""}>${choice || "None"}</option>`).join("")}</select>`;
  } else if (spec.kind === "boolean") {
    input = `<select id="${id}" name="${spec.key}" data-read-only="${spec.read_only}" ${spec.read_only ? "disabled" : ""}><option value="false" ${value !== "true" ? "selected" : ""}>Disabled</option><option value="true" ${value === "true" ? "selected" : ""}>Enabled</option></select>`;
  } else {
    const type = spec.kind === "secret" ? "password" : ["number", "port"].includes(spec.kind) ? "number" : "text";
    const shown = spec.kind === "secret" ? "" : escapeHtml(value || "");
    const placeholder = spec.kind === "secret" && value?.configured ? "Configured — leave blank to keep" : "";
    const numeric = spec.kind === "number" ? 'step="any"' : spec.kind === "port" ? 'step="1" min="1" max="65535"' : "";
    input = `<input id="${id}" name="${spec.key}" type="${type}" data-read-only="${spec.read_only}" value="${shown}" placeholder="${placeholder}" ${numeric} ${spec.read_only ? "disabled" : ""}>`;
  }
  const clear = spec.kind === "secret" && value?.configured && !spec.read_only
    ? `<label class="clear-secret"><input type="checkbox" data-clear-secret="${spec.key}"> Clear stored value</label>`
    : "";
  return `<div class="field ${spec.kind}"><label for="${id}">${spec.label}${sourceNote}</label>${input}${clear}${help}</div>`;
}

function renderConfiguration(config) {
  const sections = groupFields(config.fields);
  $("#config-form").innerHTML = Object.entries(sections).map(([name, fields]) => `
    <section class="config-section"><h3>${name}</h3><div class="field-grid">${fields.map((spec) => fieldMarkup(spec, config.values[spec.key])).join("")}</div></section>
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
  $("#config-status").textContent = dirty ? "Unsaved changes" : "All changes saved";
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
  setBusy(true);
  try {
    const operation = await api(payload.component ? "/api/component" : "/api/action", { method: "POST", body: JSON.stringify(payload) });
    if (session !== state.session || !state.token) return;
    renderOperation(operation);
    toast(`Started: ${operation.label}`);
    refreshStatus();
  } catch (error) {
    if (session !== state.session || !state.token) return;
    setBusy(false);
    toast(error.message, true);
  }
}

function runAction(action, extra = {}) {
  const confirmations = {
    install: "Install and start the configured VesselStack components?",
    apply: "Apply the saved configuration to the installed system?",
    stop: "Stop the managed VesselStack services? The control panel will remain available.",
    update: "Create a backup and apply this reviewed release?",
  };
  if (confirmations[action] && !window.confirm(confirmations[action])) return;
  startOperation({ action, ...extra });
}

function runComponent(component, action) {
  if (action === "remove" && !window.confirm(`Remove ${component}? Existing data is preserved, but integrations may stop working.`)) return;
  if (action === "stop" && !window.confirm(`Stop ${component}?`)) return;
  startOperation({ component, action });
}

function setBusy(busy) {
  state.busy = busy;
  state.operationRunning = busy;
  $$('[data-action], #save-config, #discard-config, #run-update, [data-component]').forEach((button) => { button.disabled = busy || button.dataset.configDisabled === "true"; });
  $("#refresh").disabled = false;
  $$("#config-form input, #config-form select").forEach((control) => { control.disabled = busy || state.saving || control.dataset.readOnly === "true"; });
  updateConfigStatus();
}

$("#login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  state.token = $("#token").value.trim();
  unlock();
});
$$('[data-tab]').forEach((button) => button.addEventListener("click", () => {
  $$('[data-tab]').forEach((item) => item.classList.toggle("active", item === button));
  $$('.tab-panel').forEach((panel) => { panel.hidden = panel.id !== button.dataset.tab; });
}));
$$('[data-action]').forEach((button) => button.addEventListener("click", () => runAction(button.dataset.action)));
$("#refresh").addEventListener("click", refreshStatus);
$("#save-config").addEventListener("click", saveConfiguration);
$("#discard-config").addEventListener("click", () => { if (!isConfigDirty() || window.confirm("Discard unsaved configuration edits?")) renderConfiguration(state.config); });
$("#run-update").addEventListener("click", () => runAction("update", { source_directory: $("#update-source").value.trim() }));
$("#lock").addEventListener("click", lock);
$("#component-search").addEventListener("input", (event) => { state.componentQuery = event.target.value; renderComponents(state.components); });
$$('[data-component-filter]').forEach((button) => button.addEventListener("click", () => {
  state.componentFilter = button.dataset.componentFilter;
  $$('[data-component-filter]').forEach((item) => item.classList.toggle("active", item === button));
  renderComponents(state.components);
}));
$$('[data-tab]').forEach((button) => button.addEventListener("click", () => {
  if (button.dataset.tab === "configuration" && state.config) setTimeout(() => $("#config-form input:not([disabled]), #config-form select:not([disabled])")?.focus(), 0);
}));

window.addEventListener("beforeunload", (event) => { if (isConfigDirty()) { event.preventDefault(); event.returnValue = ""; } });

if (state.token) {
  $("#token").value = state.token;
  unlock();
}
