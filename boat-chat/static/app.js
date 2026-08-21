const form = document.querySelector("#chat-form");
const input = document.querySelector("#message");
const messages = document.querySelector("#messages");
const context = document.querySelector("#context-json");
const contextSummary = document.querySelector("#context-summary");
const status = document.querySelector("#status");
const routeStatus = document.querySelector("#route-status");
const settingsDialog = document.querySelector("#settings-dialog");
const settingsForm = document.querySelector("#settings-form");
const settingsMessage = document.querySelector("#settings-message");
const sendButton = document.querySelector("#send-button");
const clearButton = document.querySelector("#clear-chat");
const providerLabels = {
  local: "Local context",
  codex_cli: "Codex CLI",
  claude_cli: "Claude CLI",
  openai: "OpenAI",
  vercel: "Vercel AI Gateway",
  bedrock: "AWS Bedrock",
  google: "Google",
  ollama: "Ollama",
  openai_compatible: "OpenAI compatible",
};
const secretKeys = new Set([
  "BOAT_CHAT_SETTINGS_TOKEN",
  "TELEGRAM_BOT_TOKEN",
  "OPENAI_API_KEY",
  "AI_GATEWAY_API_KEY",
  "VERCEL_OIDC_TOKEN",
  "AWS_ACCESS_KEY_ID",
  "AWS_SECRET_ACCESS_KEY",
  "AWS_SESSION_TOKEN",
  "GOOGLE_API_KEY",
  "GEMINI_API_KEY",
  "GOOGLE_CLOUD_ACCESS_TOKEN",
  "GOOGLE_OAUTH_ACCESS_TOKEN",
  "BOAT_CHAT_API_KEY",
]);
const CUSTOM_MODEL = "__custom__";
const modelControls = {
  primary: {
    providerSelect: () => settingsForm.elements.BOAT_CHAT_PROVIDER,
    select: document.querySelector("#setting-model-select"),
    customWrap: document.querySelector("#setting-model-custom-wrap"),
    customInput: document.querySelector("#setting-model-custom"),
  },
  fallback: {
    providerSelect: () => settingsForm.elements.BOAT_CHAT_FALLBACK_PROVIDER,
    select: document.querySelector("#setting-fallback-model-select"),
    customWrap: document.querySelector("#setting-fallback-model-custom-wrap"),
    customInput: document.querySelector("#setting-fallback-model-custom"),
  },
};
let modelCatalog = { suggestions: {}, model_setting_key: {}, active: {} };
let loadedSettings = {};
let conversation = [];

function addMessage(role, text) {
  const item = document.createElement("article");
  item.className = `message ${role}`;
  item.setAttribute("aria-label", role === "user" ? "Your question" : "Boat Chat answer");
  item.textContent = text;
  messages.appendChild(item);
  messages.scrollTop = messages.scrollHeight;
  return item;
}

function setStatus(text, state = "ready") {
  status.textContent = text;
  status.classList.toggle("is-busy", state === "busy");
  status.classList.toggle("is-error", state === "error");
}

function providerName(value) {
  return providerLabels[value] || value || "none";
}

function setRoute(provider, fallbackProvider, models) {
  const withModel = (name, model) => (model ? `${providerName(name)} (${model})` : providerName(name));
  const primaryText = withModel(provider, models?.primary_model);
  const route = fallbackProvider
    ? `${primaryText} -> ${withModel(fallbackProvider, models?.fallback_model)}`
    : primaryText;
  routeStatus.textContent = route;
  routeStatus.title = route;
}

function summarizeContext(payload) {
  if (!payload || typeof payload !== "object" || !Object.keys(payload).length) {
    return "No request yet";
  }
  const keys = Object.keys(payload).filter((key) => payload[key] !== undefined && payload[key] !== null);
  const useful = keys.filter((key) => !["context_profile", "question"].includes(key));
  const count = useful.length || keys.length;
  return `${count} section${count === 1 ? "" : "s"} gathered`;
}

function setContext(payload) {
  context.textContent = payload ? JSON.stringify(payload, null, 2) : "No request yet.";
  contextSummary.textContent = summarizeContext(payload);
}

function setBusy(isBusy) {
  sendButton.disabled = isBusy;
  clearButton.disabled = isBusy;
  sendButton.textContent = isBusy ? "Asking" : "Ask";
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 170)}px`;
}

async function ask(message) {
  const history = conversation.slice(-8);
  addMessage("user", message);
  const pending = addMessage("assistant", "Checking telemetry...");
  pending.classList.add("is-pending");
  setStatus("Working", "busy");
  setBusy(true);

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    pending.classList.remove("is-pending");
    pending.textContent = data.answer || "(No answer returned)";
    conversation.push(
      { role: "user", content: message },
      { role: "assistant", content: data.answer || "(No answer returned)" }
    );
    conversation = conversation.slice(-8);
    setContext(data.context || {});
    setStatus("Ready");
  } catch (error) {
    pending.classList.remove("is-pending");
    pending.textContent = `Request failed: ${error.message}`;
    setStatus("Error", "error");
  } finally {
    setBusy(false);
    input.focus();
  }
}

async function loadHealth() {
  try {
    const response = await fetch("/health");
    const data = await response.json();
    setStatus("Ready");
    setRoute(data.provider || "local", data.fallback_provider || "", data.models || {});
  } catch (error) {
    setStatus("Offline", "error");
    setRoute("local", "", {});
  }
}

function setSettingValue(name, value) {
  const field = settingsForm.elements[name];
  if (field) {
    field.value = value || "";
  }
}

function primaryModelKey(provider) {
  return modelCatalog.model_setting_key?.[provider] || "BOAT_CHAT_MODEL";
}

function configuredModelFor(role, provider) {
  if (role === "fallback") {
    return loadedSettings.BOAT_CHAT_FALLBACK_MODEL || modelCatalog.active?.fallback_model || "";
  }
  const key = primaryModelKey(provider);
  return (
    loadedSettings[key] ||
    loadedSettings.BOAT_CHAT_MODEL ||
    modelCatalog.active?.primary_model ||
    ""
  );
}

function populateModelSelect(role) {
  const controls = modelControls[role];
  const provider = controls.providerSelect()?.value || "";
  const select = controls.select;
  select.replaceChildren();

  const currentValue = provider ? configuredModelFor(role, provider) : "";
  const suggestions = [...(modelCatalog.suggestions?.[provider] || [])];
  if (currentValue && !suggestions.includes(currentValue)) {
    suggestions.unshift(currentValue);
  }

  if (!provider) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "select a provider first";
    select.appendChild(option);
    select.disabled = true;
    controls.customWrap.hidden = true;
    return;
  }
  select.disabled = false;

  suggestions.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    select.appendChild(option);
  });
  if (!suggestions.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "provider default";
    select.appendChild(option);
  }
  const custom = document.createElement("option");
  custom.value = CUSTOM_MODEL;
  custom.textContent = "Custom...";
  select.appendChild(custom);

  if (currentValue && suggestions.includes(currentValue)) {
    select.value = currentValue;
    controls.customWrap.hidden = true;
  } else if (currentValue) {
    select.value = CUSTOM_MODEL;
    controls.customInput.value = currentValue;
    controls.customWrap.hidden = false;
  } else {
    select.selectedIndex = 0;
    controls.customWrap.hidden = true;
  }
}

function selectedModel(role) {
  const controls = modelControls[role];
  if (controls.select.disabled) return "";
  if (controls.select.value === CUSTOM_MODEL) {
    return controls.customInput.value.trim();
  }
  return controls.select.value.trim();
}

function updateProviderSettingsState() {
  const primary = settingsForm.elements.BOAT_CHAT_PROVIDER?.value || "";
  const fallback = settingsForm.elements.BOAT_CHAT_FALLBACK_PROVIDER?.value || "";
  const primaryNote = document.querySelector("#primary-provider-note");
  const fallbackNote = document.querySelector("#fallback-provider-note");
  if (primaryNote) {
    primaryNote.textContent = `Primary: ${providerName(primary)}`;
  }
  if (fallbackNote) {
    fallbackNote.textContent = fallback ? `Fallback: ${providerName(fallback)}` : "Fallback: none";
  }

  document.querySelectorAll("[data-provider-config]").forEach((section) => {
    const providers = String(section.dataset.providerConfig || "")
      .split(/\s+/)
      .filter(Boolean);
    section.classList.toggle("is-relevant", providers.includes(primary) || providers.includes(fallback));
  });
}

async function loadModelCatalog() {
  try {
    const response = await fetch("/api/models");
    if (response.ok) {
      modelCatalog = await response.json();
    }
  } catch (error) {
    // Model suggestions are a convenience; settings still work without them.
  }
}

async function loadSettings() {
  const response = await fetch("/api/settings");
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  loadedSettings = data.settings || {};

  function populateProviderSelect(selector, includeBlank) {
    const provider = document.querySelector(selector);
    provider.replaceChildren();
    if (includeBlank) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "none";
      provider.appendChild(option);
    }
    (data.providers || []).forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = providerName(name);
      provider.appendChild(option);
    });
    return provider;
  }

  const provider = populateProviderSelect("#setting-provider", false);
  const fallbackProvider = populateProviderSelect("#setting-fallback-provider", true);

  Object.entries(loadedSettings).forEach(([key, value]) => setSettingValue(key, value));
  if (!provider.value && data.active_provider) {
    provider.value = data.active_provider;
  }
  if (!fallbackProvider.value && data.active_fallback_provider) {
    fallbackProvider.value = data.active_fallback_provider;
  }
  populateModelSelect("primary");
  populateModelSelect("fallback");
  updateProviderSettingsState();

  document.querySelectorAll("[data-secret]").forEach((item) => {
    const key = item.dataset.secret;
    item.textContent = data.secrets?.[key] ? "saved" : "";
  });
}

function settingsToken() {
  const field = settingsForm.elements.BOAT_CHAT_SETTINGS_TOKEN;
  const typed = field ? String(field.value).trim() : "";
  return typed || localStorage.getItem("boatChatSettingsToken") || "";
}

async function saveSettings(event) {
  event.preventDefault();
  settingsMessage.textContent = "Saving...";
  const formData = new FormData(settingsForm);
  const settings = {};
  for (const [key, value] of formData.entries()) {
    const text = String(value).trim();
    if (secretKeys.has(key) && !text) continue;
    settings[key] = text;
  }

  const primaryProvider = settings.BOAT_CHAT_PROVIDER || "";
  const primaryModel = selectedModel("primary");
  const primaryKey = primaryModelKey(primaryProvider);
  settings[primaryKey] = primaryModel;
  if (primaryKey !== "BOAT_CHAT_MODEL") {
    // Clear the generic key so a stale value can't shadow the CLI model.
    settings.BOAT_CHAT_MODEL = "";
  }
  settings.BOAT_CHAT_FALLBACK_MODEL = settings.BOAT_CHAT_FALLBACK_PROVIDER ? selectedModel("fallback") : "";

  const token = settingsToken();
  const headers = { "Content-Type": "application/json" };
  if (token) {
    headers["X-Boat-Chat-Token"] = token;
  }

  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers,
      body: JSON.stringify({ settings }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    if (settings.BOAT_CHAT_SETTINGS_TOKEN) {
      localStorage.setItem("boatChatSettingsToken", settings.BOAT_CHAT_SETTINGS_TOKEN);
    } else if (token) {
      localStorage.setItem("boatChatSettingsToken", token);
    }
    settingsMessage.textContent = "Saved";
    await loadModelCatalog();
    await loadSettings();
    await loadHealth();
  } catch (error) {
    settingsMessage.textContent = `Save failed: ${error.message}`;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  resizeInput();
  ask(message);
});

input.addEventListener("input", resizeInput);

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    input.value = button.dataset.prompt;
    resizeInput();
    input.focus();
  });
});

clearButton.addEventListener("click", () => {
  conversation = [];
  messages.replaceChildren();
  setContext(null);
  addWelcomeMessage();
  input.focus();
});

document.querySelector("#settings-open").addEventListener("click", async () => {
  settingsMessage.textContent = "";
  try {
    await loadModelCatalog();
    await loadSettings();
  } catch (error) {
    settingsMessage.textContent = `Load failed: ${error.message}`;
  }
  settingsDialog.showModal();
});

document.querySelector("#settings-close").addEventListener("click", () => {
  settingsDialog.close();
});

settingsForm.elements.BOAT_CHAT_PROVIDER?.addEventListener("change", () => {
  populateModelSelect("primary");
  updateProviderSettingsState();
});
settingsForm.elements.BOAT_CHAT_FALLBACK_PROVIDER?.addEventListener("change", () => {
  populateModelSelect("fallback");
  updateProviderSettingsState();
});
Object.values(modelControls).forEach((controls) => {
  controls.select.addEventListener("change", () => {
    controls.customWrap.hidden = controls.select.value !== CUSTOM_MODEL;
    if (!controls.customWrap.hidden) {
      controls.customInput.focus();
    }
  });
});
settingsForm.addEventListener("submit", saveSettings);

function addWelcomeMessage() {
  addMessage(
    "assistant",
    "Ready. Ask about fuel, engines, shore power, battery history, or current boat status. I will use local telemetry first and ask when a time window is unclear."
  );
}

setContext(null);
addWelcomeMessage();
loadHealth();
