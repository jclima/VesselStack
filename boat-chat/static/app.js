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
function readConversation() {
  try {
    const saved = JSON.parse(sessionStorage.getItem("boatChatConversation") || "[]");
    return Array.isArray(saved) ? saved.filter(item => item && ["user", "assistant"].includes(item.role) && typeof item.content === "string").slice(-8) : [];
  } catch { return []; }
}
let conversation = readConversation();

function createSessionId() {
  if (typeof crypto?.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const random = new Uint32Array(4);
  if (typeof crypto?.getRandomValues === "function") {
    crypto.getRandomValues(random);
  } else {
    for (let index = 0; index < random.length; index += 1) {
      random[index] = Math.floor(Math.random() * 0x100000000);
    }
  }
  return `browser-${Date.now().toString(36)}-${Array.from(random, (value) => value.toString(36)).join("")}`;
}

let sessionId = sessionStorage.getItem("boatChatSessionId") || createSessionId();
sessionStorage.setItem("boatChatSessionId", sessionId);
answerMode.value = localStorage.getItem("boatChatAnswerMode") || "concise";
answerMode.addEventListener("change",()=>localStorage.setItem("boatChatAnswerMode",answerMode.value));
let activeController = null;
let progressTimer = null;

function appendInline(parent, text) {
  // Deliberately small text-only markup parser. Never interpret model HTML.
  const parts = String(text).split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g);
  parts.forEach(part => {
    const bold = part.startsWith("**") && part.endsWith("**");
    const code = part.startsWith("`") && part.endsWith("`");
    if (bold || code) { const node = document.createElement(bold ? "strong" : "code"); node.textContent = part.slice(bold ? 2 : 1, bold ? -2 : -1); parent.appendChild(node); }
    else parent.appendChild(document.createTextNode(part));
  });
}
function renderAnswer(parent, text) {
  const body = document.createElement("div"); body.className = "answer-body";
  let list = null;
  String(text).split("\n").forEach(line => {
    const bullet = line.match(/^\s*([-*]|\d+\.)\s+(.+)$/);
    if (bullet) {
      const type = /\d/.test(bullet[1]) ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== type) { list = document.createElement(type); body.appendChild(list); }
      const item = document.createElement("li"); appendInline(item, bullet[2]); list.appendChild(item); return;
    }
    list = null;
    if (!line.trim()) return;
    const heading = line.match(/^#{1,6}\s+(.+)$/);
    const row = document.createElement(heading ? "h3" : "p"); appendInline(row, heading ? heading[1] : line); body.appendChild(row);
  });
  parent.appendChild(body);
}
function addMessage(role, text) {
  const item = document.createElement("article");
  item.className = `message ${role}`;
  item.setAttribute("aria-label", role === "user" ? "Your question" : "Boat Chat answer");
  if (role === "assistant") renderAnswer(item, text); else item.textContent = text;
  messages.appendChild(item);
  messages.scrollTop = messages.scrollHeight;
  return item;
}
function addAnswerEvidence(item, experience, answeredAt) {
  const evidence = Array.isArray(experience?.evidence) ? experience.evidence : [];
  if (!evidence.length && !answeredAt) return;
  const details = document.createElement("details"); details.className = "message-evidence";
  const summary = document.createElement("summary"); summary.textContent = "Sources & answer time"; details.appendChild(summary);
  if (answeredAt) { const stamp = document.createElement("p"); stamp.textContent = `Answered ${new Date(answeredAt).toLocaleString()}. This answer is a snapshot, not live status.`; details.appendChild(stamp); }
  const list = document.createElement("ul");
  evidence.forEach(source => { const row = document.createElement("li"); const label = document.createElement("strong"); label.textContent = `${source.label}: `; row.append(label, document.createTextNode(source.detail || "")); list.appendChild(row); });
  details.appendChild(list); item.appendChild(details);
}
function decorateAnswer(item, turn, question) {
  addMetricCards(item, turn.experience?.metrics);
  addCharts(item, turn.experience?.charts);
  addAnswerEvidence(item, turn.experience, turn.answered_at);
  addFollowups(item, turn.experience?.followups);
  addFeedbackControls(item, turn.request_id, question, turn.session_id || sessionId);
}
function persistConversation() {
  try { sessionStorage.setItem("boatChatConversation", JSON.stringify(conversation)); return true; }
  catch { return false; }
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

let statusBusy = false;
let lastBoatStatus = null;
let lastStatusReceived = 0;
let connectionState = "disconnected";
let accessTokenRequired = false;
const connectDialog = document.querySelector("#connect-dialog");
function showView(view) { document.body.dataset.view = view; }
function updateAccessControls() {
  const connectButton = document.querySelector("#connect-open");
  connectButton.hidden = !accessTokenRequired;
  if (!accessTokenRequired && connectDialog.open) connectDialog.close();
}
function ageText() {
  const output = document.querySelector("#observation-age");
  if (!lastBoatStatus) return;
  const age = lastBoatStatus.age_seconds;
  const elapsed = Math.floor((Date.now() - lastStatusReceived) / 1000);
  const seconds = age == null ? null : age + elapsed;
  const outdated = seconds == null || seconds > 600 || connectionState !== "connected";
  output.textContent = `${connectionState === "offline" ? "Connection lost — retained observations. " : ""}${seconds == null ? "Observation time unavailable" : `Snapshot collected ${Math.floor(seconds / 60)} min ago`}. ${outdated ? "Outdated or incomplete — refresh before relying on it." : "Individual sensors may report less often."}`;
  document.querySelector("#owner-overview").dataset.outdated = String(outdated);
  if (outdated) document.querySelector("#boat-condition").textContent = "Status needs refreshing";
}
function renderBoatStatus(data) {
  lastBoatStatus = data; lastStatusReceived = Date.now();
  document.querySelector("#boat-condition").textContent = data.condition;
  boatStatusStrip.replaceChildren(); boatStatusStrip.hidden = false;
  data.cards.forEach((card) => {
    const button=document.createElement("button"); button.type="button"; button.className="status-card"; button.dataset.tone=card.tone;
    const label=document.createElement("span"); label.textContent=card.label;
    const value=document.createElement("strong"); value.textContent=card.value;
    button.append(label,value); button.addEventListener("click",()=>ask(card.prompt)); boatStatusStrip.appendChild(button);
  });
  const attention=document.querySelector("#owner-attention"); attention.replaceChildren();
  if (!data.attention.length) attention.textContent="No issues reported by the available status checks.";
  data.attention.forEach((item)=>{
    const row=document.createElement("div"); row.className="owner-issue"; row.dataset.severity=item.severity;
    const text=document.createElement("p"); text.textContent=`${item.label}: ${item.reason}`;
    const button=document.createElement("button"); button.type="button"; button.textContent="Explain"; button.addEventListener("click",()=>ask(item.prompt || `Explain this boat status: ${item.label}: ${item.reason}`)); row.append(text,button);
    if(item.path) { const link=document.createElement("a"); link.textContent="Open boat controls"; link.href=`http://${window.location.hostname}:8123${item.path}`; link.target="_blank"; link.rel="noopener"; row.appendChild(link); }
    attention.appendChild(row);
  });
  document.querySelector("#owner-system").textContent=data.system_status;
  document.querySelector("#connect-open").textContent="Connected";
  updateAccessControls();
  ageText();
}
function clearDeviceStatus(message) {
  lastBoatStatus=null; boatStatusStrip.replaceChildren(); boatStatusStrip.hidden=true;
  document.querySelector("#boat-condition").textContent="Connect to your boat";
  document.querySelector("#observation-age").textContent=message;
  document.querySelector("#owner-attention").textContent="Boat status has not been checked.";
  document.querySelector("#owner-system").textContent="Connect to check system status.";
  insightsContent.replaceChildren(); document.querySelector("#connect-open").textContent="Connect"; updateAccessControls();
}
async function loadBoatStatus() {
  if(statusBusy) return;
  const token=sessionStorage.getItem("boatChatAccessToken") || "";
  if(accessTokenRequired && !token) { connectionState="disconnected"; clearDeviceStatus("Connect this device to view boat observations."); return; }
  statusBusy=true;
  try {
    const response=await fetch("/api/status",{headers:{"X-Boat-Chat-Token":token},signal:AbortSignal.timeout(10000)});
    if(response.status===401) { accessTokenRequired=true; updateAccessControls(); connectionState="disconnected"; clearDeviceStatus("Access token rejected. Choose Connect to sign in again."); return; }
    if(!response.ok) throw new Error(`HTTP ${response.status}`);
    connectionState="connected"; renderBoatStatus(await response.json());
  } catch(error) {
    connectionState="offline";
    if(lastBoatStatus) ageText();
    else { clearDeviceStatus("Cannot reach the boat. Check your boat Wi-Fi or ZeroTier connection, then refresh."); document.querySelector("#boat-condition").textContent="Boat connection unavailable"; }
  } finally { statusBusy=false; }
}
document.querySelector("#overview-open").addEventListener("click",()=>{showView("overview");loadBoatStatus();});
document.querySelector("#chat-open").addEventListener("click",()=>{showView("chat");input.focus();});
document.querySelector("#refresh-status").addEventListener("click",loadBoatStatus);
document.querySelector("#connect-open").addEventListener("click",()=>connectDialog.showModal());
document.querySelector("#connect-close").addEventListener("click",()=>connectDialog.close());
document.querySelector("#disconnect-device").addEventListener("click",()=>{sessionStorage.removeItem("boatChatAccessToken");connectionState="disconnected";clearDeviceStatus("Device disconnected.");connectDialog.close();});
document.querySelector("#connect-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); const field=document.querySelector("#connect-token"); const token=field.value.trim(); const message=document.querySelector("#connect-message"); message.textContent="Checking access…";
  try { const response=await fetch("/api/status",{headers:{"X-Boat-Chat-Token":token},signal:AbortSignal.timeout(10000)});
    if(!response.ok) throw new Error(response.status===401 ? "Token not accepted. Check the Boat Chat access token and try again." : "Boat status is unavailable. Try again.");
    const data=await response.json(); sessionStorage.setItem("boatChatAccessToken",token); field.value=""; connectionState="connected";renderBoatStatus(data);message.textContent="";connectDialog.close();
  } catch(error) { message.textContent=error.message; }
});
setInterval(()=>{if(!document.hidden) {ageText();loadBoatStatus();}},30000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)loadBoatStatus();});
window.addEventListener("online",loadBoatStatus);

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
  const blob=new Blob([`${lines.join("\n")}\n`],{type:"text/plain"}); const url=URL.createObjectURL(blob); const link=document.createElement("a"); link.href=url; link.download=`theoffice-trip-${(trip.start_local || "report").slice(0,10)}.txt`; link.click(); URL.revokeObjectURL(url);
}

async function loadInsights() {
  const token=sessionStorage.getItem("boatChatAccessToken") || "";
  insightsContent.textContent="Loading boat insights…";
  if (accessTokenRequired && !token) { insightsContent.textContent="Choose Connect to sign in to this device and view your trips and maintenance."; return; }
  try {
    const response=await fetch("/api/insights",{headers:{"X-Boat-Chat-Token":token}}); const data=await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`); insightsContent.replaceChildren();
    const alerts=hubCard(`Boat · ${data.status.condition}`); const alertList=document.createElement("ul"); alertList.className="alert-list";
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

function addFeedbackControls(messageElement, requestId, question, feedbackSessionId = sessionId) {
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
      button.disabled = true;
      try {
        const response = await fetch("/api/feedback", {method:"POST", headers, body:JSON.stringify({request_id:requestId, session_id:feedbackSessionId, rating, note})});
        if (!response.ok) throw new Error("Feedback could not be saved");
        controls.textContent = "Feedback saved";
      } catch {
        button.disabled = false;
        button.textContent = "Retry feedback";
      }
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
  message = String(message || "").trim();
  if (!message || activeController) return;
  showView("chat");
  const history = conversation.slice(-8).map(({role, content}) => ({role, content}));
  const requestSession = sessionId;
  addMessage("user", message);
  const pending = addMessage("assistant", "Waiting for your boat's answer…");
  pending.classList.add("is-pending");
  setStatus("Working", "busy"); setBusy(true);
  const controller = new AbortController(); activeController = controller;
  const began = Date.now();
  progressTimer = setInterval(() => { pending.textContent = `Waiting for your boat's answer… ${Math.floor((Date.now() - began) / 1000)}s`; }, 1000);
  try {
    const token = sessionStorage.getItem("boatChatAccessToken") || "";
    const headers = {"Content-Type": "application/json"};
    if (token) headers["X-Boat-Chat-Token"] = token;
    const response = await fetch("/api/chat", {method:"POST", headers, signal:controller.signal,
      body:JSON.stringify({message, history, session_id:requestSession, answer_mode:answerMode.value})});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error(data.error || `HTTP ${response.status}`); error.status = response.status; throw error; }
    if (typeof data.answer !== "string" || !data.answer.trim()) throw new Error("The boat returned an empty answer.");
    clearInterval(progressTimer);
    pending.classList.remove("is-pending"); pending.replaceChildren(); renderAnswer(pending, data.answer);
    const turn = {role:"assistant", content:data.answer, experience:data.experience || {}, request_id:data.request_id,
      answered_at:new Date().toISOString(), session_id:requestSession};
    decorateAnswer(pending, turn, message);
    conversation.push({role:"user", content:message}, turn); conversation = conversation.slice(-8);
    setContext(null);
    setStatus(persistConversation() ? "Answer ready" : "Answer ready · history not saved");
    messages.scrollTop = messages.scrollHeight;
  } catch (error) {
    clearInterval(progressTimer); pending.classList.remove("is-pending"); pending.replaceChildren();
    const cancelled = error.name === "AbortError";
    const explanation = cancelled ? "Stopped waiting. The boat may still finish processing this question." :
      error.status === 401 ? "Connect this device to ask questions. Your question is kept below." :
      error.status === 429 ? "Boat Chat is busy. Wait a moment, then retry." : "Couldn't get an answer. Check your connection and try again.";
    renderAnswer(pending, explanation);
    // A cancelled/failed request can still finish on the server. Isolate its session
    // so that an unseen answer cannot contaminate the next conversational turn.
    sessionId = createSessionId(); sessionStorage.setItem("boatChatSessionId", sessionId);
    const actions = document.createElement("div"); actions.className = "followups";
    if(error.status === 401) { const connect=document.createElement("button"); connect.type="button"; connect.textContent="Connect"; connect.addEventListener("click",()=>connectDialog.showModal()); actions.appendChild(connect); }
    const retry=document.createElement("button"); retry.type="button"; retry.textContent="Retry question"; retry.addEventListener("click",()=>ask(message));
    const edit=document.createElement("button"); edit.type="button"; edit.textContent="Edit question"; edit.addEventListener("click",()=>{input.value=message;resizeInput();input.focus();}); actions.append(retry,edit); pending.appendChild(actions);
    setStatus(cancelled ? "Stopped waiting" : error.status === 401 ? "Sign-in needed" : "Try again", cancelled ? "ready" : "error");
  } finally {
    clearInterval(progressTimer); progressTimer = null; activeController = null; setBusy(false);
  }
}

cancelButton.addEventListener("click", () => activeController?.abort());

async function loadHealth() {
  try {
    const response = await fetch("/health");
    const data = await response.json();
    accessTokenRequired = data.access_token_required === true;
    updateAccessControls();
    setStatus(data.ready ? "Connected" : "Limited data", data.ready ? "ready" : "error");
    setRoute(data.provider || "local", data.fallback_provider || "", data.models || {});
  } catch (error) {
    setStatus("Offline", "error");
    setRoute("local", "", {});
  }
}

function setSettingValue(name, value) {
  const field = settingsForm.elements[name];
  if (field) {
    if (field.type === "checkbox") {
      field.checked = ["1", "true", "yes", "on"].includes(String(value || "").toLowerCase());
    } else {
      field.value = value || "";
    }
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

  setSettingValue("BOAT_CHAT_REQUIRE_ACCESS_TOKEN", loadedSettings.BOAT_CHAT_REQUIRE_ACCESS_TOKEN || "false");
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
  settings.BOAT_CHAT_REQUIRE_ACCESS_TOKEN = settingsForm.elements.BOAT_CHAT_REQUIRE_ACCESS_TOKEN.checked ? "true" : "false";

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
  const clearedSession = sessionId;
  sessionId = createSessionId();
  sessionStorage.setItem("boatChatSessionId", sessionId);
  conversation = [];
  sessionStorage.removeItem("boatChatConversation");
  const token = sessionStorage.getItem("boatChatAccessToken") || "";
  const headers = {"Content-Type":"application/json"};
  if (token) headers["X-Boat-Chat-Token"] = token;
  fetch("/api/session/clear", {method:"POST", headers, body:JSON.stringify({session_id:clearedSession})}).catch(() => {});
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
document.querySelector("#maintenance-open").addEventListener("click",()=>{insightsDialog.showModal();loadInsights();});
document.querySelector("#insights-close").addEventListener("click",()=>insightsDialog.close());

setInterval(async()=>{if(localStorage.getItem("boatChatBrowserAlerts")!=="on") return; const token=sessionStorage.getItem("boatChatAccessToken")||""; if(accessTokenRequired && !token) return; try {const response=await fetch("/api/insights",{headers:{"X-Boat-Chat-Token":token}}); if(response.ok) processProactiveAlerts(await response.json());} catch(error) { /* Retry on the next interval. */ }},60000);

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
    conversation.forEach((item, index) => {
      const message = addMessage(item.role, item.content);
      if(item.role === "assistant") decorateAnswer(message, item, conversation[index - 1]?.content || "");
    });
    return;
  }
  addMessage(
    "assistant",
    "Ask about the boat now, investigate a change, or review a trip. I’ll show the evidence with each answer."
  );
}

setContext(null);
addWelcomeMessage();
renderSavedQuestions();
loadHealth();
loadBoatStatus();
