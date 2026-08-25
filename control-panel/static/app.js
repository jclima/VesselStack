const state = {
  token: sessionStorage.getItem("vesselstack_control_token") || "",
  config: null,
  poller: null,
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

async function unlock() {
  try {
    const config = await api("/api/config");
    state.config = config;
    sessionStorage.setItem("vesselstack_control_token", state.token);
    $("#login").hidden = true;
    $("#app").hidden = false;
    $("#lock").hidden = false;
    setConnected(true);
    renderConfiguration(config);
    await refreshStatus();
  } catch (error) {
    setConnected(false);
    $("#login-error").textContent = error.message;
    sessionStorage.removeItem("vesselstack_control_token");
  }
}

function statusClass(value) {
  const text = String(value || "unknown").toLowerCase();
  if (["active", "running", "succeeded"].some((word) => text.includes(word))) return "active";
  if (["failed", "error", "exited"].some((word) => text.includes(word))) return "failed";
  return "";
}

function lock() {
  clearTimeout(state.poller);
  state.token = "";
  state.config = null;
  sessionStorage.removeItem("vesselstack_control_token");
  $("#token").value = "";
  $("#app").hidden = true;
  $("#lock").hidden = true;
  $("#login").hidden = false;
  setConnected(false);
}

function renderComponents(components) {
  $("#component-grid").innerHTML = components.map((component) => {
    const disabled = !component.enabled || state.operationRunning;
    const stateLabel = escapeHtml(disabled ? "disabled by config" : component.state);
    const componentId = escapeHtml(component.id);
    const componentLabel = escapeHtml(component.label);
    const actions = component.id === "control-panel" ? "<p>Managed from the recovery shell</p>" : `
      <div class="component-actions">
        <button data-component="${componentId}" data-verb="start" ${disabled ? "disabled" : ""}>Start</button>
        <button data-component="${componentId}" data-verb="restart" ${disabled ? "disabled" : ""}>Restart</button>
        <button data-component="${componentId}" data-verb="stop" ${disabled ? "disabled" : ""}>Stop</button>
      </div>`;
    return `<article class="component">
      <div><div class="component-top"><h3>${componentLabel}</h3><span class="pill ${disabled ? "disabled" : statusClass(component.state)}">${stateLabel}</span></div><p>${component.kind === "container" ? "Docker Compose service" : "Host systemd service"}</p></div>
      ${actions}
    </article>`;
  }).join("");
  $$('[data-component]').forEach((button) => button.addEventListener("click", () => runComponent(button.dataset.component, button.dataset.verb)));
}

function renderOperation(operation) {
  const label = operation.state || "idle";
  const badge = $("#operation-state");
  badge.textContent = label;
  badge.className = `pill ${statusClass(label)} ${label}`;
  const output = (operation.output || []).join("\n");
  $("#operation-output").textContent = output || "No operation has run in this session.";
  state.operationRunning = label === "running";
  $$('[data-action]').forEach((button) => { button.disabled = state.operationRunning; });
  $("#run-update").disabled = state.operationRunning;
  if (label === "running") {
    clearTimeout(state.poller);
    state.poller = setTimeout(refreshStatus, 1200);
  }
}

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    renderOperation(data.operation);
    renderComponents(data.components);
    const active = data.components.filter((item) => item.enabled && statusClass(item.state) === "active").length;
    const enabled = data.components.filter((item) => item.enabled).length;
    $("#summary").textContent = `${active} of ${enabled} enabled components are active.`;
    setConnected(true);
  } catch (error) {
    setConnected(false);
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
  let input;
  if (spec.kind === "select") {
    input = `<select id="${id}" name="${spec.key}" ${spec.read_only ? "disabled" : ""}>${spec.choices.map((choice) => `<option value="${choice}" ${choice === value ? "selected" : ""}>${choice || "None"}</option>`).join("")}</select>`;
  } else if (spec.kind === "boolean") {
    input = `<select id="${id}" name="${spec.key}" ${spec.read_only ? "disabled" : ""}><option value="false" ${value !== "true" ? "selected" : ""}>Disabled</option><option value="true" ${value === "true" ? "selected" : ""}>Enabled</option></select>`;
  } else {
    const type = spec.kind === "secret" ? "password" : ["number", "port"].includes(spec.kind) ? "number" : "text";
    const shown = spec.kind === "secret" ? "" : escapeHtml(value || "");
    const placeholder = spec.kind === "secret" && value?.configured ? "Configured — leave blank to keep" : "";
    const numeric = spec.kind === "number" ? 'step="any"' : spec.kind === "port" ? 'step="1" min="1" max="65535"' : "";
    input = `<input id="${id}" name="${spec.key}" type="${type}" value="${shown}" placeholder="${placeholder}" ${numeric} ${spec.read_only ? "disabled" : ""}>`;
  }
  const clear = spec.kind === "secret" && value?.configured
    ? `<label class="clear-secret"><input type="checkbox" data-clear-secret="${spec.key}"> Clear stored value</label>`
    : "";
  return `<div class="field ${spec.kind}"><label for="${id}">${spec.label}</label>${input}${clear}${help}</div>`;
}

function renderConfiguration(config) {
  const sections = groupFields(config.fields);
  $("#config-form").innerHTML = Object.entries(sections).map(([name, fields]) => `
    <section class="config-section"><h3>${name}</h3><div class="field-grid">${fields.map((spec) => fieldMarkup(spec, config.values[spec.key])).join("")}</div></section>
  `).join("");
}

async function saveConfiguration() {
  const form = new FormData($("#config-form"));
  const settings = Object.fromEntries(form.entries());
  $$('[data-clear-secret]:checked').forEach((checkbox) => { settings[checkbox.dataset.clearSecret] = null; });
  try {
    const result = await api("/api/config", { method: "POST", body: JSON.stringify({ settings }) });
    state.config = result.configuration;
    renderConfiguration(state.config);
    toast(`Configuration saved. Rollback copy: ${result.backup}`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function startOperation(payload) {
  try {
    const operation = await api(payload.component ? "/api/component" : "/api/action", { method: "POST", body: JSON.stringify(payload) });
    renderOperation(operation);
    toast(`Started: ${operation.label}`);
    refreshStatus();
  } catch (error) {
    toast(error.message, true);
  }
}

function runAction(action, extra = {}) {
  if (state.operationRunning) {
    toast("Wait for the current operation to finish.", true);
    return;
  }
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
  if (state.operationRunning) {
    toast("Wait for the current operation to finish.", true);
    return;
  }
  if (action === "stop" && !window.confirm(`Stop ${component}?`)) return;
  startOperation({ component, action });
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
$("#lock").addEventListener("click", lock);
$("#save-config").addEventListener("click", saveConfiguration);
$("#discard-config").addEventListener("click", () => renderConfiguration(state.config));
$("#run-update").addEventListener("click", () => runAction("update", { source_directory: $("#update-source").value.trim() }));

if (state.token) {
  $("#token").value = state.token;
  unlock();
}
