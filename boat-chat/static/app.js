if (new URLSearchParams(window.location.search).get("embedded") === "ha") {
  document.documentElement.dataset.embedded = "ha";
}

const form = document.querySelector("#chat-form");
const input = document.querySelector("#message");
const messages = document.querySelector("#messages");
const context = document.querySelector("#context-json");
const contextSummary = document.querySelector("#context-summary");
const contextDrawer = document.querySelector(".context-drawer");
const status = document.querySelector("#status");
const routeStatus = document.querySelector("#route-status");
const settingsDialog = document.querySelector("#settings-dialog");
const settingsForm = document.querySelector("#settings-form");
const settingsMessage = document.querySelector("#settings-message");
const sendButton = document.querySelector("#send-button");
const clearButton = document.querySelector("#clear-chat");
const answerMode = document.querySelector("#answer-mode");
const cancelButton = document.querySelector("#cancel-button");
const boatStatusStrip = document.querySelector("#boat-status-strip");
const capabilitiesDialog = document.querySelector("#capabilities-dialog");
const capabilitiesContent = document.querySelector("#capabilities-content");
const insightsDialog = document.querySelector("#insights-dialog");
const insightsContent = document.querySelector("#insights-content");
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
  "BOAT_CHAT_ACCESS_TOKEN",
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
let conversation = JSON.parse(sessionStorage.getItem("boatChatConversation") || "[]");
const sessionId = sessionStorage.getItem("boatChatSessionId") || crypto.randomUUID();
sessionStorage.setItem("boatChatSessionId", sessionId);
answerMode.value = localStorage.getItem("boatChatAnswerMode") || "concise";
answerMode.addEventListener("change",()=>localStorage.setItem("boatChatAnswerMode",answerMode.value));
let activeController = null;
let progressTimer = null;

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
  return payload.evidence?.length ? `${payload.evidence.length} evidence item${payload.evidence.length === 1 ? "" : "s"}` : `${count} detail${count === 1 ? "" : "s"}`;
}

function setContext(payload) {
  context.replaceChildren();
  if (!payload) {
    context.textContent = "No request yet.";
    contextSummary.textContent = "No request yet";
    contextDrawer.hidden = true;
    return;
  }
  contextDrawer.hidden = false;
  const list = document.createElement("ul"); list.className = "evidence-list";
  (payload.evidence || []).forEach((item) => {
    const row = document.createElement("li"); const label = document.createElement("strong"); const detail = document.createElement("span");
    label.textContent = item.label; detail.textContent = item.detail; row.append(label, detail); list.appendChild(row);
  });
  if (!(payload.evidence || []).length) list.textContent = "No additional evidence details for this answer.";
  context.appendChild(list);
  contextSummary.textContent = summarizeContext(payload);
}

function setBusy(isBusy) {
  sendButton.disabled = isBusy;
  clearButton.disabled = isBusy;
  cancelButton.hidden = !isBusy;
  sendButton.textContent = isBusy ? "Asking" : "Ask";
}

function addFollowups(messageElement, prompts) {
  if (!prompts?.length) return;
  const wrap = document.createElement("div"); wrap.className = "followups";
  prompts.forEach((prompt) => { const button=document.createElement("button"); button.type="button"; button.textContent=prompt; button.addEventListener("click",()=>ask(prompt)); wrap.appendChild(button); });
  messageElement.appendChild(wrap);
}

async function loadBoatStatus() {
  const token = sessionStorage.getItem("boatChatAccessToken") || "";
  if (!token) {
    boatStatusStrip.hidden = true;
    boatStatusStrip.replaceChildren();
    return;
  }
  try {
    const response = await fetch("/api/status", {headers:{"X-Boat-Chat-Token":token}});
    if (!response.ok) {
      boatStatusStrip.hidden = true;
      return;
    }
    const data = await response.json(); boatStatusStrip.replaceChildren(); boatStatusStrip.hidden = false;
    data.cards.forEach((card) => { const button=document.createElement("button"); button.type="button"; button.className="status-card"; button.dataset.tone=card.tone; button.innerHTML=`<span></span><strong></strong>`; button.querySelector("span").textContent=card.label; button.querySelector("strong").textContent=card.value; button.addEventListener("click",()=>ask(card.prompt)); boatStatusStrip.appendChild(button); });
  } catch (error) {
    boatStatusStrip.hidden = true;
  }
}

function savedQuestions() { try { return JSON.parse(localStorage.getItem("boatChatSavedQuestions") || "[]"); } catch { return []; } }
function saveQuestion(question) { const saved=[question,...savedQuestions().filter((item)=>item!==question)].slice(0,8); localStorage.setItem("boatChatSavedQuestions",JSON.stringify(saved)); renderSavedQuestions(); }
function renderSavedQuestions() { document.querySelectorAll(".saved-prompt").forEach((item)=>item.remove()); savedQuestions().forEach((prompt)=>{ const button=document.createElement("button"); button.type="button"; button.className="saved-prompt"; button.textContent=`★ ${prompt}`; button.title=prompt; button.addEventListener("click",()=>ask(prompt)); document.querySelector(".examples").appendChild(button); }); }

function hubCard(title, wide = false) {
  const card=document.createElement("section"); card.className=`hub-card${wide ? " hub-card-wide" : ""}`;
  const heading=document.createElement("h3"); heading.textContent=title; card.appendChild(heading); return card;
}

async function saveMaintenance(task) {
  const token=sessionStorage.getItem("boatChatAccessToken") || "";
  const response=await fetch("/api/maintenance",{method:"POST",headers:{"Content-Type":"application/json","X-Boat-Chat-Token":token},body:JSON.stringify(task)});
  const data=await response.json(); if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`); await loadInsights();
}

function alertSignature(data) {
  return (data.alerts || []).filter((item)=>item.severity!=="ok").map((item)=>`${item.label}:${item.value}`).sort().join("|");
}

function processProactiveAlerts(data) {
  const signature=alertSignature(data); const previous=localStorage.getItem("boatChatAlertSignature");
  localStorage.setItem("boatChatAlertSignature",signature);
  if (previous !== null && signature && signature !== previous && localStorage.getItem("boatChatBrowserAlerts")==="on" && Notification.permission==="granted") {
    const warnings=(data.alerts || []).filter((item)=>item.severity!=="ok");
    new Notification("VesselStack boat alert",{body:warnings.map((item)=>`${item.label}: ${item.value}`).join(" · "),tag:"boat-health",renotify:true});
  }
}

async function enableBrowserAlerts(button) {
  if (!("Notification" in window)) { button.textContent="Notifications unavailable"; return; }
  const permission=await Notification.requestPermission();
  localStorage.setItem("boatChatBrowserAlerts",permission==="granted" ? "on" : "off");
  button.textContent=permission==="granted" ? "Browser alerts enabled" : "Browser alerts blocked";
}

function addAisRadar(card, ais) {
  const targets=(ais?.targets || []).filter((item)=>Number.isFinite(Number(item.distance_nm)) && Number.isFinite(Number(item.bearing_deg)));
  if (!targets.length) { const empty=document.createElement("p"); empty.className="empty-state"; empty.textContent=ais?.error ? `AIS unavailable: ${ais.error}` : "No positioned AIS targets nearby."; card.appendChild(empty); return; }
  const range=Math.max(1,Math.min(10,Math.ceil(Math.max(...targets.map((item)=>Number(item.distance_nm))))));
  const svg=document.createElementNS("http://www.w3.org/2000/svg","svg"); svg.classList.add("radar"); svg.setAttribute("viewBox","0 0 300 300"); svg.setAttribute("role","img"); svg.setAttribute("aria-label",`${targets.length} AIS targets within ${range} nautical miles`);
  [50,100,140].forEach((radius)=>{const circle=document.createElementNS(svg.namespaceURI,"circle"); circle.setAttribute("cx","150"); circle.setAttribute("cy","150"); circle.setAttribute("r",String(radius)); circle.setAttribute("fill","none"); circle.setAttribute("stroke","#bdd0d2"); circle.setAttribute("stroke-width","1"); svg.appendChild(circle);});
  const own=document.createElementNS(svg.namespaceURI,"circle"); own.setAttribute("cx","150"); own.setAttribute("cy","150"); own.setAttribute("r","6"); own.setAttribute("fill","currentColor"); svg.appendChild(own);
  targets.forEach((target)=>{const angle=(Number(target.bearing_deg)-90)*Math.PI/180; const radius=Math.min(136,(Number(target.distance_nm)/range)*136); const dot=document.createElementNS(svg.namespaceURI,"circle"); dot.setAttribute("cx",String(150+Math.cos(angle)*radius)); dot.setAttribute("cy",String(150+Math.sin(angle)*radius)); dot.setAttribute("r",target.position_stale ? "3" : "5"); dot.setAttribute("fill",target.position_stale ? "#d98a17" : "#176b70"); const title=document.createElementNS(svg.namespaceURI,"title"); title.textContent=`${target.name || "Unknown"}: ${target.distance_nm} nm`; dot.appendChild(title); svg.appendChild(dot);}); card.appendChild(svg);
  const list=document.createElement("ol"); list.className="ais-list"; targets.slice(0,5).forEach((target)=>{const row=document.createElement("li"); row.textContent=`${target.name || "Unknown"} · ${target.distance_nm} nm · ${target.bearing_deg}°${target.position_stale ? " · stale" : ""}`; list.appendChild(row);}); card.appendChild(list);
}

function downloadTripReport(trip) {
  const lines=["VesselStack trip report",`Window: ${trip.start_local} to ${trip.stop_local}`,`Duration: ${trip.duration_minutes ?? "unknown"} minutes`,"",trip.trip_summary || "No summary recorded."];
  const blob=new Blob([`${lines.join("\n")}\n`],{type:"text/plain"}); const url=URL.createObjectURL(blob); const link=document.createElement("a"); link.href=url; link.download=`vesselstack-trip-${(trip.start_local || "report").slice(0,10)}.txt`; link.click(); URL.revokeObjectURL(url);
}

async function loadInsights() {
  const token=sessionStorage.getItem("boatChatAccessToken") || "";
  insightsContent.textContent="Loading boat insights…";
  if (!token) { insightsContent.textContent="Add the Boat Chat access token in Settings to view vessel insights."; return; }
  try {
    const response=await fetch("/api/insights",{headers:{"X-Boat-Chat-Token":token}}); const data=await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`); insightsContent.replaceChildren();
    const alerts=hubCard(`Safety · ${data.alert_count ? `${data.alert_count} to review` : "all clear"}`); const alertList=document.createElement("ul"); alertList.className="alert-list";
    data.alerts.forEach((item)=>{const row=document.createElement("li"); row.className="alert-row"; row.dataset.severity=item.severity; const label=document.createElement("span"); label.className="alert-label"; const dot=document.createElement("i"); dot.className="alert-dot"; const name=document.createElement("span"); name.textContent=item.label; label.append(dot,name); const value=document.createElement("strong"); value.textContent=item.value; row.append(label,value); alertList.appendChild(row);}); alerts.appendChild(alertList); const enableAlerts=document.createElement("button"); enableAlerts.type="button"; enableAlerts.className="alert-opt-in"; enableAlerts.textContent=localStorage.getItem("boatChatBrowserAlerts")==="on" ? "Browser alerts enabled" : "Enable browser alerts"; enableAlerts.addEventListener("click",()=>enableBrowserAlerts(enableAlerts)); alerts.appendChild(enableAlerts); processProactiveAlerts(data);
    const trip=hubCard("Last recorded trip");
    if (data.trip) { const summary=document.createElement("p"); summary.textContent=data.trip.trip_summary || `${data.trip.start_local} to ${data.trip.stop_local}`; const actions=document.createElement("div"); actions.className="trip-actions"; const askTrip=document.createElement("button"); askTrip.type="button"; askTrip.textContent="Analyze trip"; askTrip.addEventListener("click",()=>{insightsDialog.close();ask("Give me a detailed report for the last trip");}); const download=document.createElement("button"); download.type="button"; download.textContent="Download report"; download.addEventListener("click",()=>downloadTripReport(data.trip)); actions.append(askTrip,download); trip.append(summary,actions); } else { const empty=document.createElement("p"); empty.className="empty-state"; empty.textContent="No recorded trip window is available yet."; trip.appendChild(empty); }
    const position=hubCard("Vessel position");
    if (data.position) { const coords=document.createElement("strong"); coords.textContent=data.position.label; const note=document.createElement("p"); note.className="empty-state"; note.textContent="Kept local; no coordinates are sent to a map provider."; position.append(coords,note); } else { const empty=document.createElement("p"); empty.className="empty-state"; empty.textContent="Position is not present in the cached SignalK snapshot."; position.appendChild(empty); }
    const ais=hubCard(`Nearby AIS · ${data.ais?.target_count || 0}`,true); addAisRadar(ais,data.ais);
    const maintenance=hubCard(`Maintenance · ${data.maintenance_counts?.open || 0} open${data.maintenance_counts?.overdue ? ` · ${data.maintenance_counts.overdue} overdue` : ""}`,true); const list=document.createElement("ul"); list.className="maintenance-list";
    if (!data.maintenance.length) { const empty=document.createElement("li"); empty.className="empty-state"; empty.textContent="No maintenance tasks yet."; list.appendChild(empty); }
    data.maintenance.forEach((task)=>{const row=document.createElement("li"); row.className="maintenance-row"; row.dataset.due=task.due_status; const check=document.createElement("input"); check.type="checkbox"; check.checked=task.completed; check.addEventListener("change",()=>saveMaintenance({...task,completed:check.checked})); const copy=document.createElement("span"); copy.className="maintenance-copy"; const titleText=document.createElement("strong"); titleText.textContent=`${task.title}${task.due_date ? ` · due ${task.due_date}` : ""}`; copy.appendChild(titleText); if(task.notes){const note=document.createElement("small"); note.textContent=task.notes; copy.appendChild(note);} const edit=document.createElement("button"); edit.type="button"; edit.textContent="Notes"; edit.addEventListener("click",()=>{const notes=window.prompt("Maintenance notes",task.notes || ""); if(notes!==null) saveMaintenance({...task,notes});}); row.append(check,copy,edit); list.appendChild(row);});
    const addForm=document.createElement("form"); addForm.className="maintenance-form"; const title=document.createElement("input"); title.required=true; title.placeholder="New maintenance task"; const due=document.createElement("input"); due.type="date"; const add=document.createElement("button"); add.type="submit"; add.textContent="Add task"; addForm.append(title,due,add); addForm.addEventListener("submit",async(event)=>{event.preventDefault(); add.disabled=true; try { await saveMaintenance({title:title.value,due_date:due.value}); } catch(error) { window.alert(error.message); add.disabled=false; }}); maintenance.append(list,addForm);
    insightsContent.append(alerts,trip,position,ais,maintenance);
  } catch(error) { insightsContent.textContent=`Boat Hub unavailable: ${error.message}`; }
}

function addFeedbackControls(messageElement, requestId, question) {
  if (!requestId) return;
  const controls = document.createElement("div");
  controls.className = "feedback-controls";
  [["Helpful", "helpful"], ["Incomplete", "incomplete"], ["Wrong", "wrong"]].forEach(([label, rating]) => {
    const button = document.createElement("button");
    button.type = "button"; button.textContent = label;
    button.addEventListener("click", async () => {
      const token = sessionStorage.getItem("boatChatAccessToken") || "";
      const headers = {"Content-Type":"application/json"};
      if (token) headers["X-Boat-Chat-Token"] = token;
      const note = rating === "helpful" ? "" : (window.prompt("What could be improved? (optional)", "") || "");
      const response = await fetch("/api/feedback", {method:"POST", headers, body:JSON.stringify({request_id:requestId, session_id:sessionId, rating, note})});
      if (response.ok) { controls.textContent = "Feedback saved"; }
    });
    controls.appendChild(button);
  });
  const save=document.createElement("button"); save.type="button"; save.textContent="Save question"; save.addEventListener("click",()=>{saveQuestion(question); save.textContent="Saved";}); controls.appendChild(save);
  messageElement.appendChild(controls);
}

function addMetricCards(messageElement, metrics) {
  if (!metrics?.length) return;
  const grid=document.createElement("div"); grid.className="metric-grid";
  metrics.forEach((metric)=>{ const card=document.createElement("div"); card.className="metric-card"; const latest=metric.latest ?? metric.avg; card.innerHTML="<span></span><strong></strong><small></small>"; card.querySelector("span").textContent=metric.label; card.querySelector("strong").textContent=`${latest ?? "—"} ${metric.unit || ""}`; card.querySelector("small").textContent=`min ${metric.min ?? "—"} · avg ${metric.avg ?? "—"} · max ${metric.max ?? "—"}`; grid.appendChild(card); });
  messageElement.appendChild(grid);
}

function addCharts(messageElement, charts) {
  if (!charts?.length) return;
  charts.forEach((series) => {
    const values=series.points.map((point)=>Number(point.value)).filter(Number.isFinite); if (values.length<2) return;
    const min=Math.min(...values), max=Math.max(...values), span=max-min || 1;
    const points=values.map((value,index)=>`${(index/(values.length-1))*300},${68-((value-min)/span)*58}`).join(" ");
    const figure=document.createElement("figure"); figure.className="sparkline-card";
    const caption=document.createElement("figcaption"); caption.textContent=`${series.label} · ${min.toFixed(2)}–${max.toFixed(2)} ${series.unit || ""}`;
    const svg=document.createElementNS("http://www.w3.org/2000/svg","svg"); svg.setAttribute("viewBox","0 0 300 76"); svg.setAttribute("role","img"); svg.setAttribute("aria-label",caption.textContent);
    const line=document.createElementNS("http://www.w3.org/2000/svg","polyline"); line.setAttribute("points",points); line.setAttribute("fill","none"); line.setAttribute("stroke","currentColor"); line.setAttribute("stroke-width","2"); svg.appendChild(line); figure.append(caption,svg); messageElement.appendChild(figure);
  });
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
  activeController = new AbortController();
  const stages = ["Reading boat state…", "Checking retained history…", "Preparing answer…"];
  let stage = 0; progressTimer = setInterval(() => { if (stage < stages.length) pending.firstChild ? pending.firstChild.textContent = stages[stage++] : pending.textContent = stages[stage++]; }, 2500);

  try {
    const token = sessionStorage.getItem("boatChatAccessToken") || "";
    const headers = { "Content-Type": "application/json" };
    if (token) headers["X-Boat-Chat-Token"] = token;
    const response = await fetch("/api/chat", {
      method: "POST",
      headers,
      signal: activeController.signal,
      body: JSON.stringify({ message, history, session_id: sessionId, answer_mode: answerMode.value }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    pending.classList.remove("is-pending");
    pending.textContent = data.answer || "(No answer returned)";
    addMetricCards(pending, data.experience?.metrics);
    addCharts(pending, data.experience?.charts);
    addFollowups(pending, data.experience?.followups);
    addFeedbackControls(pending, data.request_id, message);
    conversation.push(
      { role: "user", content: message },
      { role: "assistant", content: data.answer || "(No answer returned)" }
    );
    conversation = conversation.slice(-8);
    sessionStorage.setItem("boatChatConversation", JSON.stringify(conversation));
    setContext(data.experience || {});
    setStatus("Ready");
  } catch (error) {
    pending.classList.remove("is-pending");
    pending.textContent = error.name === "AbortError" ? "Request cancelled." : `Request failed: ${error.message}`;
    setStatus("Error", "error");
  } finally {
    clearInterval(progressTimer); progressTimer = null; activeController = null;
    setBusy(false);
    input.focus();
  }
}

cancelButton.addEventListener("click", () => activeController?.abort());

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
  return typed || sessionStorage.getItem("boatChatSettingsToken") || "";
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
      sessionStorage.setItem("boatChatSettingsToken", settings.BOAT_CHAT_SETTINGS_TOKEN);
    } else if (token) {
      sessionStorage.setItem("boatChatSettingsToken", token);
    }
    if (settings.BOAT_CHAT_ACCESS_TOKEN) {
      sessionStorage.setItem("boatChatAccessToken", settings.BOAT_CHAT_ACCESS_TOKEN);
    }
    settingsMessage.textContent = "Saved";
    await loadModelCatalog();
    await loadSettings();
    await loadHealth();
    await loadBoatStatus();
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
input.addEventListener("focus", () => {
  window.setTimeout(() => {
    messages.scrollTop = messages.scrollHeight;
  }, 150);
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    const message = input.value.trim();
    if (!message || sendButton.disabled) return;
    input.value = "";
    resizeInput();
    ask(message);
  }
});

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    if (sendButton.disabled) return;
    ask(button.dataset.prompt);
  });
});

clearButton.addEventListener("click", () => {
  conversation = [];
  sessionStorage.removeItem("boatChatConversation");
  const token = sessionStorage.getItem("boatChatAccessToken") || "";
  const headers = {"Content-Type":"application/json"};
  if (token) headers["X-Boat-Chat-Token"] = token;
  fetch("/api/session/clear", {method:"POST", headers, body:JSON.stringify({session_id:sessionId})}).catch(() => {});
  messages.replaceChildren();
  setContext(null);
  addWelcomeMessage();
  input.focus();
});

document.querySelector("#capabilities-open").addEventListener("click", async () => {
  capabilitiesContent.textContent = "Loading capabilities…"; capabilitiesDialog.showModal();
  try { const data=await (await fetch("/api/capabilities")).json(); capabilitiesContent.replaceChildren(); data.groups.forEach((group)=>{ const section=document.createElement("section"); section.className="capability-group"; const title=document.createElement("h2"); title.textContent=group.name; const prompts=document.createElement("div"); prompts.className="capability-prompts"; group.prompts.forEach((prompt)=>{ const button=document.createElement("button"); button.type="button"; button.textContent=prompt; button.addEventListener("click",()=>{capabilitiesDialog.close(); ask(prompt);}); prompts.appendChild(button); }); section.append(title,prompts); capabilitiesContent.appendChild(section); }); } catch(error) { capabilitiesContent.textContent="Capabilities are unavailable."; }
});
document.querySelector("#capabilities-close").addEventListener("click",()=>capabilitiesDialog.close());
document.querySelector("#insights-open").addEventListener("click",()=>{insightsDialog.showModal();loadInsights();});
document.querySelector("#insights-close").addEventListener("click",()=>insightsDialog.close());

setInterval(async()=>{if(localStorage.getItem("boatChatBrowserAlerts")!=="on") return; const token=sessionStorage.getItem("boatChatAccessToken")||""; if(!token) return; try {const response=await fetch("/api/insights",{headers:{"X-Boat-Chat-Token":token}}); if(response.ok) processProactiveAlerts(await response.json());} catch(error) { /* Retry on the next interval. */ }},60000);

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
  if (conversation.length) {
    conversation.forEach((item) => addMessage(item.role, item.content));
    return;
  }
  addMessage(
    "assistant",
    "What would you like to know about your vessel?"
  );
}

setContext(null);
addWelcomeMessage();
renderSavedQuestions();
loadHealth();
loadBoatStatus();
