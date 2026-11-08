// A pairing link carries its code in the fragment. Remove it from the address bar and history before anything is requested.
const PAIR_FRAGMENT = (() => {
  try {
    const match = /(?:^|[#&])pair=([^&]*)/.exec(location.hash || "");
    if (!match) return "";
    history.replaceState(null, "", location.pathname + location.search);
    return decodeURIComponent(match[1]);
  } catch { return ""; }
})();
if (new URLSearchParams(window.location.search).get("embedded") === "ha") {
  document.documentElement.dataset.embedded = "ha";
}

const form = document.querySelector("#chat-form");
const input = document.querySelector("#message");
const messages = document.querySelector("#messages");
const announcer = document.querySelector("#sr-announcer");
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
  "TELEGRAM_ALLOWED_CHAT_IDS",
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
const CHAT_TIMEOUT_MS = 180000;

function announce(text) {
  // Separate polite live region: the conversation itself is not live, so restored
  // history and the per-second counter are never read out.
  announcer.textContent = "";
  window.setTimeout(() => { announcer.textContent = text; }, 60);
}
function accessCode() { try { return sessionStorage.getItem("boatChatAccessToken") || ""; } catch { return ""; } }
// Passphrase sign-in: a random device token, kept in localStorage when remembered and sessionStorage otherwise.
const DEVICE_KEY = "boatChatDevice";
function deviceToken() { try { return localStorage.getItem(DEVICE_KEY) || sessionStorage.getItem(DEVICE_KEY) || ""; } catch { return ""; } }
function clearDeviceToken() { try { localStorage.removeItem(DEVICE_KEY); sessionStorage.removeItem(DEVICE_KEY); } catch { /* storage may be blocked */ } }
function saveDeviceToken(token, remember) { clearDeviceToken(); try { (remember ? localStorage : sessionStorage).setItem(DEVICE_KEY, token); } catch { /* storage may be blocked */ } }
function hasAccess() { return Boolean(accessCode() || deviceToken()); }
const nativeFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const url = typeof input === "string" ? input : (input && input.url) || "";
  const token = url.startsWith("/api/") ? deviceToken() : "";
  if (token) {
    const headers = new Headers((init && init.headers) || {});
    if (!headers.has("X-Boat-Chat-Device")) headers.set("X-Boat-Chat-Device", token);
    init = { ...(init || {}), headers };
  }
  const response = await nativeFetch(input, init);
  // A rejected device token (revoked or expired) is forgotten so the sign-in screen can ask again.
  if (token && response.status === 401 && !url.startsWith("/api/auth/")) clearDeviceToken();
  return response;
};
function describeHttpError(status, data) {
  if (status === 401) return "Access code needed or not accepted. Choose Connect.";
  if (status === 429) return "Too many requests. Wait a minute, then try again.";
  return (data && typeof data.error === "string" && data.error) || `The boat returned an error (HTTP ${status}).`;
}
async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) { const error = new Error(describeHttpError(response.status, data)); error.status = response.status; throw error; }
  if (data === null) throw new Error("The boat returned a response that could not be read.");
  return data;
}

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
// The header chip reports the same sources as the Overview (/health plus the boat
// status request), so the two can never contradict each other.
let healthState = "unknown";
function refreshConnectionStatus() {
  if (activeController) return;
  if (accessTokenRequired && !hasAccess()) setStatus("Not connected", "error");
  else if (connectionState === "offline" || healthState === "offline") setStatus("Offline", "error");
  else if (healthState === "limited") setStatus("Limited data", "error");
  else if (connectionState === "connected" && lastBoatStatus) {
    if (isStatusOutdated()) setStatus("Status outdated", "busy"); else setStatus("Connected");
  } else setStatus("Checking…", "busy");
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
function showView(view) {
  document.body.dataset.view = view;
  [["overview", "#overview-open"], ["chat", "#chat-open"]].forEach(([name, selector]) => {
    const button = document.querySelector(selector);
    button.setAttribute("aria-pressed", String(view === name));
    if (view === name) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
}
function updateAccessControls() {
  const connectButton = document.querySelector("#connect-open");
  connectButton.hidden = !accessTokenRequired;
  if (!accessTokenRequired && connectDialog.open) connectDialog.close();
}
function statusSeconds() {
  if (!lastBoatStatus) return null;
  const age = lastBoatStatus.age_seconds;
  const elapsed = Math.floor((Date.now() - lastStatusReceived) / 1000);
  return age == null ? null : age + elapsed;
}
function isStatusOutdated() {
  const seconds = statusSeconds();
  return seconds == null || seconds > 600 || connectionState !== "connected";
}
function ageText() {
  const output = document.querySelector("#observation-age");
  if (!lastBoatStatus) return;
  const seconds = statusSeconds();
  const outdated = isStatusOutdated();
  output.textContent = `${connectionState === "offline" ? "Connection lost — retained observations. " : ""}${seconds == null ? "Observation time unavailable" : `Snapshot collected ${Math.floor(seconds / 60)} min ago`}. ${outdated ? "Outdated or incomplete — refresh before relying on it." : "Individual sensors may report less often."}`;
  document.querySelector("#owner-overview").dataset.outdated = String(outdated);
  if (outdated) document.querySelector("#boat-condition").textContent = "Status needs refreshing";
  refreshConnectionStatus();
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
  refreshConnectionStatus();
}
async function loadBoatStatus() {
  if(statusBusy) return;
  const token=accessCode();
  if(accessTokenRequired && !hasAccess()) { connectionState="disconnected"; clearDeviceStatus("Connect this device to view boat observations."); return; }
  statusBusy=true;
  try {
    const response=await fetch("/api/status",{headers:{"X-Boat-Chat-Token":token},signal:AbortSignal.timeout(10000)});
    if(response.status===401) { accessTokenRequired=true; updateAccessControls(); connectionState="disconnected"; clearDeviceStatus("Sign-in not accepted. Choose Connect to sign in again."); return; }
    if(!response.ok) throw new Error(`HTTP ${response.status}`);
    connectionState="connected"; renderBoatStatus(await response.json());
  } catch(error) {
    connectionState="offline";
    if(lastBoatStatus) ageText();
    else { clearDeviceStatus("Cannot reach the boat. Check that you are on the boat Wi-Fi or connected through your remote-access VPN, then refresh."); document.querySelector("#boat-condition").textContent="Boat connection unavailable"; }
  } finally { statusBusy=false; refreshConnectionStatus(); }
}
document.querySelector("#overview-open").addEventListener("click",()=>{showView("overview");loadBoatStatus();});
document.querySelector("#chat-open").addEventListener("click",()=>{showView("chat");input.focus();});
document.querySelector("#refresh-status").addEventListener("click",loadBoatStatus);
async function prepareConnectDialog() {
  const block = document.querySelector("#passphrase-block"), code = document.querySelector("#code-block");
  let enabled = false;
  try { const response = await fetch("/api/auth/status"); enabled = response.ok && (await response.json()).passphrase_set === true; } catch { enabled = false; }
  block.hidden = !enabled;
  code.open = !enabled;
  document.querySelector("#connect-message").textContent = enabled ? "" : "No passphrase is set yet. Use the access code, then set a passphrase under Settings, Sign-in.";
  (enabled ? document.querySelector("#connect-passphrase") : document.querySelector("#connect-token")).focus();
}
function openConnect() { connectDialog.showModal(); prepareConnectDialog(); }
document.querySelector("#connect-open").addEventListener("click",openConnect);
document.querySelector("#connect-close").addEventListener("click",()=>connectDialog.close());
document.querySelector("#disconnect-device").addEventListener("click",async()=>{
  if(deviceToken()) { try { await fetch("/api/auth/logout",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}); } catch { /* the token is forgotten here regardless */ } }
  clearDeviceToken(); sessionStorage.removeItem("boatChatAccessToken");connectionState="disconnected";clearDeviceStatus("Device disconnected.");connectDialog.close();});
document.querySelector("#connect-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); const field=document.querySelector("#connect-token"); const token=field.value.trim(); const message=document.querySelector("#connect-message"); message.textContent="Checking access…";
  const passField=document.querySelector("#connect-passphrase"); const passphrase=passField.value;
  if(!token && !document.querySelector("#passphrase-block").hidden) {
    if(!passphrase) { message.textContent="Enter the passphrase, or use the access code."; return; }
    try {
      const response=await fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({passphrase,remember:document.querySelector("#connect-remember").checked,label:(navigator.userAgent||"").slice(0,60)}),signal:AbortSignal.timeout(15000)});
      const data=await response.json().catch(()=>({}));
      if(!response.ok) throw new Error(response.status===429 ? `Too many attempts. Try again in ${Math.ceil((data.retry_after||60)/60)} minute(s), or use the access code.` : response.status===421 ? "This address is not recognised by the boat. Use the boat's usual address." : data.error||"Sign-in failed.");
      saveDeviceToken(data.device_token,document.querySelector("#connect-remember").checked); passField.value="";
      const status=await fetch("/api/status",{signal:AbortSignal.timeout(10000)}); if(status.ok){connectionState="connected";renderBoatStatus(await status.json());}
      message.textContent=""; connectDialog.close();
    } catch(error) { message.textContent=error.message; }
    return;
  }
  try { const response=await fetch("/api/status",{headers:{"X-Boat-Chat-Token":token},signal:AbortSignal.timeout(10000)});
    if(!response.ok) throw new Error(response.status===401 ? "Access code not accepted. Check the Boat Chat access code and try again." : "Boat status is unavailable. Try again.");
    const data=await response.json(); sessionStorage.setItem("boatChatAccessToken",token); field.value=""; connectionState="connected";renderBoatStatus(data);message.textContent="";connectDialog.close();
  } catch(error) { message.textContent=error.message; }
});

// What a person typed (case, dashes and spaces ignored; I/L read as 1, O as 0) -> canonical code, or "".
function normalizePairingCode(text) {
  const cleaned = String(text || "").toUpperCase().replace(/[\s-]/g, "").replace(/[IL]/g, "1").replace(/O/g, "0");
  return cleaned.length === 10 && [...cleaned].every((ch) => "0123456789ABCDEFGHJKMNPQRSTVWXYZ".includes(ch)) ? cleaned : "";
}
async function redeemPairing(raw, fromLink) {
  const message = document.querySelector("#connect-message");
  const code = normalizePairingCode(raw);
  if (fromLink) { connectDialog.showModal(); }
  if (!code) { message.textContent = "That pairing code doesn't look right. It has 10 letters and numbers, like ABCDE-FGHJK."; return false; }
  message.textContent = "Pairing…";
  try {
    const response = await fetch("/api/auth/pair", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, label: (navigator.userAgent || "").slice(0, 60) }), signal: AbortSignal.timeout(15000) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(response.status === 429 ? `Too many attempts. Try again in ${Math.ceil((data.retry_after || 60) / 60)} minute(s).` : response.status === 421 ? "This address is not recognised by the boat. Use the boat's usual address." : response.status === 401 ? "That pairing code was not accepted or has expired. Ask for a new one." : data.error || "Pairing failed.");
    saveDeviceToken(data.device_token, true);
    document.querySelector("#connect-pair-code").value = "";
    const status = await fetch("/api/status", { signal: AbortSignal.timeout(10000) });
    if (status.ok) { connectionState = "connected"; renderBoatStatus(await status.json()); }
    message.textContent = ""; if (connectDialog.open) connectDialog.close();
    return true;
  } catch (error) { message.textContent = error.message; document.querySelector("#connect-pair-code").value = ""; return false; }
}
document.querySelector("#connect-pair-submit").addEventListener("click", () => redeemPairing(document.querySelector("#connect-pair-code").value, false));
document.querySelector("#connect-pair-code").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); redeemPairing(event.target.value, false); } });
if (PAIR_FRAGMENT) redeemPairing(PAIR_FRAGMENT, true);
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
  const token=accessCode();
  await fetchJson("/api/maintenance",{method:"POST",headers:{"Content-Type":"application/json","X-Boat-Chat-Token":token},body:JSON.stringify(task)});
  await loadInsights();
}

function alertSignature(data) {
  return (data.alerts || []).filter((item)=>item.severity!=="ok").map((item)=>`${item.label}:${item.value}`).sort().join("|");
}

function processProactiveAlerts(data) {
  const signature=alertSignature(data); const previous=localStorage.getItem("boatChatAlertSignature");
  localStorage.setItem("boatChatAlertSignature",signature);
  if (previous !== null && signature && signature !== previous && localStorage.getItem("boatChatBrowserAlerts")==="on" && Notification.permission==="granted") {
    const warnings=(data.alerts || []).filter((item)=>item.severity!=="ok");
    new Notification("Boat Chat alert",{body:warnings.map((item)=>`${item.label}: ${item.value}`).join(" · "),tag:"boat-health",renotify:true});
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
  [50,100,140].forEach((radius)=>{const circle=document.createElementNS(svg.namespaceURI,"circle"); circle.setAttribute("class","radar-ring"); circle.setAttribute("cx","150"); circle.setAttribute("cy","150"); circle.setAttribute("r",String(radius)); svg.appendChild(circle);});
  const own=document.createElementNS(svg.namespaceURI,"circle"); own.setAttribute("class","radar-own"); own.setAttribute("cx","150"); own.setAttribute("cy","150"); own.setAttribute("r","6"); svg.appendChild(own);
  targets.forEach((target)=>{const angle=(Number(target.bearing_deg)-90)*Math.PI/180; const radius=Math.min(136,(Number(target.distance_nm)/range)*136); const dot=document.createElementNS(svg.namespaceURI,"circle"); dot.setAttribute("cx",String(150+Math.cos(angle)*radius)); dot.setAttribute("cy",String(150+Math.sin(angle)*radius)); dot.setAttribute("r",target.position_stale ? "3" : "5"); dot.setAttribute("class",target.position_stale ? "radar-target is-stale" : "radar-target"); const title=document.createElementNS(svg.namespaceURI,"title"); title.textContent=`${target.name || "Unknown"}: ${target.distance_nm} nm`; dot.appendChild(title); svg.appendChild(dot);}); card.appendChild(svg);
  const list=document.createElement("ol"); list.className="ais-list"; targets.slice(0,5).forEach((target)=>{const row=document.createElement("li"); row.textContent=`${target.name || "Unknown"} · ${target.distance_nm} nm · ${target.bearing_deg}°${target.position_stale ? " · stale" : ""}`; list.appendChild(row);}); card.appendChild(list);
}

function downloadTripReport(trip) {
  const lines=["Boat trip report",`Window: ${trip.start_local} to ${trip.stop_local}`,`Duration: ${trip.duration_minutes ?? "unknown"} minutes`,"",trip.trip_summary || "No summary recorded."];
  const blob=new Blob([`${lines.join("\n")}\n`],{type:"text/plain"}); const url=URL.createObjectURL(blob); const link=document.createElement("a"); link.href=url; link.download=`vesselstack-trip-${(trip.start_local || "report").slice(0,10)}.txt`; link.click(); URL.revokeObjectURL(url);
}

function dueLabel(status) { return status === "overdue" ? "Overdue" : status === "soon" ? "Due soon" : ""; }

async function loadInsights() {
  const token=accessCode();
  insightsContent.textContent="Loading Boat Hub…";
  if (accessTokenRequired && !hasAccess()) { insightsContent.textContent="Choose Connect to sign in to this device and view your trips and maintenance."; return; }
  try {
    const data=await fetchJson("/api/insights",{headers:{"X-Boat-Chat-Token":token}}); insightsContent.replaceChildren();
    const alerts=hubCard(`Boat · ${data.status.condition}`); const alertList=document.createElement("ul"); alertList.className="alert-list";
    data.alerts.forEach((item)=>{const row=document.createElement("li"); row.className="alert-row"; row.dataset.severity=item.severity; const label=document.createElement("span"); label.className="alert-label"; const dot=document.createElement("i"); dot.className="alert-dot"; dot.setAttribute("aria-hidden","true"); const name=document.createElement("span"); name.textContent=item.label; label.append(dot,name); const value=document.createElement("strong"); const level=document.createElement("span"); level.className="sr-only"; level.textContent=`${item.severity}: `; value.append(level,document.createTextNode(item.value)); row.append(label,value); alertList.appendChild(row);}); alerts.appendChild(alertList); const enableAlerts=document.createElement("button"); enableAlerts.type="button"; enableAlerts.className="alert-opt-in"; enableAlerts.textContent=localStorage.getItem("boatChatBrowserAlerts")==="on" ? "Browser alerts enabled" : "Enable browser alerts"; enableAlerts.addEventListener("click",()=>enableBrowserAlerts(enableAlerts)); alerts.appendChild(enableAlerts); processProactiveAlerts(data);
    const trip=hubCard("Last recorded trip");
    if (data.trip) { const summary=document.createElement("p"); summary.textContent=data.trip.trip_summary || `${data.trip.start_local} to ${data.trip.stop_local}`; const actions=document.createElement("div"); actions.className="trip-actions"; const askTrip=document.createElement("button"); askTrip.type="button"; askTrip.textContent="Analyze trip"; askTrip.addEventListener("click",()=>{insightsDialog.close();ask("Give me a detailed report for the last trip");}); const download=document.createElement("button"); download.type="button"; download.textContent="Download report"; download.addEventListener("click",()=>downloadTripReport(data.trip)); actions.append(askTrip,download); trip.append(summary,actions); } else { const empty=document.createElement("p"); empty.className="empty-state"; empty.textContent="No recorded trip window is available yet."; trip.appendChild(empty); }
    const position=hubCard("Vessel position");
    if (data.position) { const coords=document.createElement("strong"); coords.textContent=data.position.label; const note=document.createElement("p"); note.className="empty-state"; note.textContent="Kept local; no coordinates are sent to a map provider."; position.append(coords,note); } else { const empty=document.createElement("p"); empty.className="empty-state"; empty.textContent="Position is not present in the latest Signal K data."; position.appendChild(empty); }
    const ais=hubCard(`Nearby AIS · ${data.ais?.target_count || 0}`,true); addAisRadar(ais,data.ais);
    const maintenance=hubCard(`Maintenance · ${data.maintenance_counts?.open || 0} open${data.maintenance_counts?.overdue ? ` · ${data.maintenance_counts.overdue} overdue` : ""}`,true); const list=document.createElement("ul"); list.className="maintenance-list";
    const maintenanceError=document.createElement("p"); maintenanceError.className="form-error"; maintenanceError.setAttribute("role","alert"); maintenanceError.hidden=true;
    const showMaintenanceError=(error)=>{ maintenanceError.textContent=`Could not save: ${error.message}`; maintenanceError.hidden=false; };
    if (!data.maintenance.length) { const empty=document.createElement("li"); empty.className="empty-state"; empty.textContent="No maintenance tasks yet."; list.appendChild(empty); }
    data.maintenance.forEach((task)=>{
      const row=document.createElement("li"); row.className="maintenance-row"; row.dataset.due=task.due_status;
      const check=document.createElement("input"); check.type="checkbox"; check.checked=task.completed; check.setAttribute("aria-label",`Mark done: ${task.title}`);
      check.addEventListener("change",async()=>{ maintenanceError.hidden=true; check.disabled=true; try { await saveMaintenance({...task,completed:check.checked}); } catch(error) { check.checked=!check.checked; check.disabled=false; showMaintenanceError(error); } });
      const copy=document.createElement("span"); copy.className="maintenance-copy"; const titleText=document.createElement("strong"); titleText.textContent=`${task.title}${task.due_date ? ` · due ${task.due_date}` : ""}`;
      if (dueLabel(task.due_status)) { const badge=document.createElement("span"); badge.className="due-badge"; badge.textContent=dueLabel(task.due_status); titleText.appendChild(badge); }
      copy.appendChild(titleText); if(task.notes){const note=document.createElement("small"); note.textContent=task.notes; copy.appendChild(note);}
      const edit=document.createElement("button"); edit.type="button"; edit.textContent="Notes"; edit.setAttribute("aria-label",`Edit notes: ${task.title}`);
      edit.addEventListener("click",()=>{
        if (copy.querySelector(".inline-form")) return;
        const noteForm=document.createElement("form"); noteForm.className="inline-form";
        const noteInput=document.createElement("input"); noteInput.value=task.notes || ""; noteInput.setAttribute("aria-label",`Maintenance notes for ${task.title}`);
        const saveNote=document.createElement("button"); saveNote.type="submit"; saveNote.textContent="Save notes";
        const cancelNote=document.createElement("button"); cancelNote.type="button"; cancelNote.textContent="Cancel"; cancelNote.addEventListener("click",()=>noteForm.remove());
        noteForm.addEventListener("submit",async(event)=>{ event.preventDefault(); maintenanceError.hidden=true; saveNote.disabled=true; try { await saveMaintenance({...task,notes:noteInput.value}); } catch(error) { saveNote.disabled=false; showMaintenanceError(error); } });
        noteForm.append(noteInput,saveNote,cancelNote); copy.appendChild(noteForm); noteInput.focus();
      });
      row.append(check,copy,edit); list.appendChild(row);
    });
    const addForm=document.createElement("form"); addForm.className="maintenance-form"; const title=document.createElement("input"); title.required=true; title.placeholder="New maintenance task"; title.setAttribute("aria-label","New maintenance task"); const due=document.createElement("input"); due.type="date"; due.setAttribute("aria-label","Due date (optional)"); const add=document.createElement("button"); add.type="submit"; add.textContent="Add task"; addForm.append(title,due,add);
    addForm.addEventListener("submit",async(event)=>{event.preventDefault(); maintenanceError.hidden=true; add.disabled=true; try { await saveMaintenance({title:title.value,due_date:due.value}); } catch(error) { showMaintenanceError(error); add.disabled=false; }});
    maintenance.append(list,addForm,maintenanceError);
    insightsContent.append(alerts,trip,position,ais,maintenance);
  } catch(error) { insightsContent.textContent=`Boat Hub unavailable: ${error.message}`; }
}

function addFeedbackControls(messageElement, requestId, question, feedbackSessionId = sessionId) {
  if (!requestId) return;
  const controls = document.createElement("div");
  controls.className = "feedback-controls";
  const noteForm = document.createElement("form"); noteForm.className = "inline-form"; noteForm.hidden = true;
  const noteInput = document.createElement("input"); noteInput.placeholder = "What could be improved? (optional)"; noteInput.setAttribute("aria-label", "What could be improved? (optional)");
  const noteSend = document.createElement("button"); noteSend.type = "submit"; noteSend.textContent = "Send feedback";
  const noteCancel = document.createElement("button"); noteCancel.type = "button"; noteCancel.textContent = "Cancel";
  const noteError = document.createElement("p"); noteError.className = "form-error"; noteError.setAttribute("role", "alert"); noteError.hidden = true;
  noteForm.append(noteInput, noteSend, noteCancel);
  let pendingRating = "";
  async function sendFeedback(rating, note, button) {
    const token = accessCode();
    const headers = {"Content-Type":"application/json"};
    if (token) headers["X-Boat-Chat-Token"] = token;
    button.disabled = true; noteError.hidden = true;
    try {
      const response = await fetch("/api/feedback", {method:"POST", headers, body:JSON.stringify({request_id:requestId, session_id:feedbackSessionId, rating, note})});
      if (!response.ok) throw new Error("Feedback could not be saved");
      controls.textContent = "Feedback saved"; noteForm.remove(); noteError.remove();
    } catch {
      button.disabled = false; noteError.textContent = "Feedback could not be saved. Try again."; noteError.hidden = false;
    }
  }
  noteForm.addEventListener("submit", (event) => { event.preventDefault(); sendFeedback(pendingRating, noteInput.value.trim(), noteSend); });
  noteCancel.addEventListener("click", () => { noteForm.hidden = true; noteError.hidden = true; });
  [["Helpful", "helpful"], ["Incomplete", "incomplete"], ["Wrong", "wrong"]].forEach(([label, rating]) => {
    const button = document.createElement("button");
    button.type = "button"; button.textContent = label;
    button.addEventListener("click", () => {
      if (rating === "helpful") { sendFeedback(rating, "", button); return; }
      pendingRating = rating; noteForm.hidden = false; noteInput.focus();
    });
    controls.appendChild(button);
  });
  const save=document.createElement("button"); save.type="button"; save.textContent="Save question"; save.addEventListener("click",()=>{saveQuestion(question); save.textContent="Saved";}); controls.appendChild(save);
  messageElement.append(controls, noteForm, noteError);
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
  const counter = document.createElement("span"); counter.className = "count-hint"; counter.setAttribute("aria-hidden", "true");
  (pending.querySelector("p") || pending).appendChild(counter);
  setStatus("Working", "busy"); setBusy(true);
  messages.setAttribute("aria-busy", "true");
  const controller = new AbortController(); activeController = controller;
  let timedOut = false; let statusIsError = false;
  const timeoutId = setTimeout(() => { timedOut = true; controller.abort(); }, CHAT_TIMEOUT_MS);
  const began = Date.now();
  progressTimer = setInterval(() => { counter.textContent = ` ${Math.floor((Date.now() - began) / 1000)}s`; }, 1000);
  try {
    const token = accessCode();
    const headers = {"Content-Type": "application/json"};
    if (token) headers["X-Boat-Chat-Token"] = token;
    const response = await fetch("/api/chat", {method:"POST", headers, signal:controller.signal,
      body:JSON.stringify({message, history, session_id:requestSession, answer_mode:answerMode.value})});
    const data = await response.json().catch(() => null);
    if (!response.ok) { const error = new Error((data && data.error) || `HTTP ${response.status}`); error.status = response.status; throw error; }
    if (!data || typeof data.answer !== "string" || !data.answer.trim()) throw new Error("The boat returned an empty answer.");
    clearInterval(progressTimer);
    pending.classList.remove("is-pending"); pending.replaceChildren(); renderAnswer(pending, data.answer);
    const turn = {role:"assistant", content:data.answer, experience:data.experience || {}, request_id:data.request_id,
      answered_at:new Date().toISOString(), session_id:requestSession};
    decorateAnswer(pending, turn, message);
    conversation.push({role:"user", content:message}, turn); conversation = conversation.slice(-8);
    const saved = persistConversation();
    announce(saved ? "Answer ready" : "Answer ready. History could not be saved in this browser.");
    messages.scrollTop = messages.scrollHeight;
  } catch (error) {
    clearInterval(progressTimer); pending.classList.remove("is-pending"); pending.replaceChildren();
    const cancelled = error.name === "AbortError" && !timedOut;
    const explanation = timedOut ? "The boat did not answer within 3 minutes. Your question is back in the message box; try again or ask something simpler." :
      cancelled ? "Stopped waiting. The boat may still finish processing this question. Your question is back in the message box." :
      error.status === 401 ? "Connect this device to ask questions. Your question is back in the message box." :
      error.status === 429 ? "Boat Chat is busy. Wait a moment, then try again. Your question is back in the message box." :
      "Couldn't get an answer. Check your connection and try again. Your question is back in the message box.";
    renderAnswer(pending, explanation);
    announce(explanation);
    // Put the question back so nothing typed is lost (unless the user already started a new one).
    if (!input.value.trim()) { input.value = message; resizeInput(); }
    // A cancelled/failed request can still finish on the server. Isolate its session
    // so that an unseen answer cannot contaminate the next conversational turn.
    sessionId = createSessionId(); sessionStorage.setItem("boatChatSessionId", sessionId);
    const actions = document.createElement("div"); actions.className = "followups";
    if(error.status === 401) { const connect=document.createElement("button"); connect.type="button"; connect.textContent="Connect"; connect.addEventListener("click",openConnect); actions.appendChild(connect); }
    const retry=document.createElement("button"); retry.type="button"; retry.textContent="Retry question"; retry.addEventListener("click",()=>{ if (input.value.trim() === message) { input.value = ""; resizeInput(); } ask(message); });
    actions.appendChild(retry); pending.appendChild(actions);
    statusIsError = !cancelled;
    setStatus(timedOut ? "Timed out" : cancelled ? "Stopped waiting" : error.status === 401 ? "Sign-in needed" : "Try again", cancelled ? "ready" : "error");
  } finally {
    clearTimeout(timeoutId); clearInterval(progressTimer); progressTimer = null; activeController = null; setBusy(false);
    messages.removeAttribute("aria-busy");
    if (!statusIsError) refreshConnectionStatus();
  }
}

cancelButton.addEventListener("click", () => activeController?.abort());

async function loadHealth() {
  try {
    const response = await fetch("/health");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    accessTokenRequired = data.access_token_required === true;
    updateAccessControls();
    healthState = data.ready ? "ready" : "limited";
    setRoute(data.provider || "local", data.fallback_provider || "", data.models || {});
  } catch (error) {
    healthState = "offline";
    setRoute("local", "", {});
  }
  refreshConnectionStatus();
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
    section.hidden = !(providers.includes(primary) || providers.includes(fallback));
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

let settingsLoaded = false;
let initialSettings = {};
const REQUIRED_SETTING_LABELS = {
  BOAT_CHAT_PROVIDER: "Primary provider",
  BOAT_CHAT_MAX_TOKENS: "Max tokens",
  BOAT_CHAT_CONTEXT_CHARS: "Context chars",
  BOAT_CHAT_HOST: "Host",
  BOAT_CHAT_PORT: "Port",
};

function currentSettingValues() {
  const values = {};
  for (const [key, value] of new FormData(settingsForm).entries()) values[key] = String(value).trim();
  values.__model = selectedModel("primary");
  values.__fallbackModel = selectedModel("fallback");
  return values;
}

async function loadSettings() {
  settingsLoaded = false;
  const accessToken = accessCode();
  const response = await fetch("/api/settings", { headers: accessToken ? { "X-Boat-Chat-Token": accessToken } : {} });
  const data = await response.json().catch(() => null);
  if (response.status === 401) {
    const error = new Error("Connect this device with your access code before opening Settings.");
    error.status = 401;
    throw error;
  }
  if (!response.ok || !data || typeof data !== "object") {
    throw new Error((data && data.error) || `The boat returned an error (HTTP ${response.status}).`);
  }
  loadedSettings = data.settings || {};
  const currentSettingsToken = document.querySelector("#setting-current-token");
  if (currentSettingsToken) currentSettingsToken.value = sessionStorage.getItem("boatChatSettingsToken") || "";

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
  document.querySelectorAll("#settings-form input[type=password]").forEach((field) => { if (field.name) field.value = ""; });
  initialSettings = currentSettingValues();
  settingsLoaded = true;
}

function settingsToken() {
  const field = document.querySelector("#setting-current-token");
  const typed = field ? String(field.value).trim() : "";
  return typed || sessionStorage.getItem("boatChatSettingsToken") || "";
}

function showSettingsError(message, needsConnect) {
  settingsForm.dataset.state = "error";
  document.querySelector("#settings-error").hidden = false;
  document.querySelector("#settings-error-text").textContent = message;
  document.querySelector("#settings-connect").hidden = !needsConnect;
  settingsMessage.textContent = "";
}
function showSettingsReady() {
  settingsForm.dataset.state = "ok";
  document.querySelector("#settings-error").hidden = true;
}
async function openSettings() {
  settingsMessage.textContent = "";
  try {
    await loadModelCatalog();
    await loadSettings();
    showSettingsReady();
  } catch (error) {
    settingsLoaded = false;
    showSettingsError(error.status === 401 ? error.message : `Settings could not be loaded: ${error.message}`, error.status === 401);
  }
  if (!settingsDialog.open) settingsDialog.showModal();
}

// Only fields the user actually changed are sent, so an untouched or partly loaded form
// can never overwrite saved settings with blanks.
function changedSettings() {
  const now = currentSettingValues();
  const settings = {};
  for (const [key, value] of Object.entries(now)) {
    if (key.startsWith("__")) continue;
    if (secretKeys.has(key)) { if (value) settings[key] = value; continue; }
    if (value === (initialSettings[key] ?? "")) continue;
    if (value === "" && REQUIRED_SETTING_LABELS[key]) throw new Error(`${REQUIRED_SETTING_LABELS[key]} cannot be blank.`);
    settings[key] = value === "" ? null : value;
  }
  const provider = now.BOAT_CHAT_PROVIDER || "";
  const primaryKey = primaryModelKey(provider);
  if (now.__model !== initialSettings.__model || provider !== (initialSettings.BOAT_CHAT_PROVIDER ?? "")) {
    settings[primaryKey] = now.__model || null;
    // Clear the generic key so a stale value can't shadow the CLI model.
    if (primaryKey !== "BOAT_CHAT_MODEL" && loadedSettings.BOAT_CHAT_MODEL) settings.BOAT_CHAT_MODEL = null;
  }
  const fallbackProvider = now.BOAT_CHAT_FALLBACK_PROVIDER || "";
  if (now.__fallbackModel !== initialSettings.__fallbackModel || fallbackProvider !== (initialSettings.BOAT_CHAT_FALLBACK_PROVIDER ?? "")) {
    settings.BOAT_CHAT_FALLBACK_MODEL = fallbackProvider ? (now.__fallbackModel || null) : null;
  }
  return settings;
}

async function saveSettings(event) {
  event.preventDefault();
  if (!settingsLoaded) { settingsMessage.textContent = "Settings are not loaded. Close this window and open Settings again."; return; }
  let settings;
  try { settings = changedSettings(); } catch (error) { settingsMessage.textContent = error.message; return; }
  if (!Object.keys(settings).length) { settingsMessage.textContent = "No changes to save."; return; }
  settingsMessage.textContent = "Saving...";

  const token = settingsToken();
  const accessToken = accessCode();
  const headers = { "Content-Type": "application/json" };
  // X-Boat-Chat-Token carries the admin code; the access code travels separately so one
  // request can prove both when they differ.
  if (token || accessToken) headers["X-Boat-Chat-Token"] = token || accessToken;
  if (accessToken) headers["X-Boat-Chat-Access-Token"] = accessToken;

  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers,
      body: JSON.stringify({ settings }),
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      if (response.status === 401) throw new Error("Access code needed or not accepted. Close Settings and choose Connect.");
      if (response.status === 403) throw new Error("Settings can only be changed from this device, or with the correct admin code.");
      throw new Error((data && data.error) || `HTTP ${response.status}`);
    }
    if (settings.BOAT_CHAT_SETTINGS_TOKEN || token) {
      sessionStorage.setItem("boatChatSettingsToken", settings.BOAT_CHAT_SETTINGS_TOKEN || token);
      const currentSettingsToken = document.querySelector("#setting-current-token");
      if (currentSettingsToken) currentSettingsToken.value = "";
    }
    if (settings.BOAT_CHAT_ACCESS_TOKEN) {
      sessionStorage.setItem("boatChatAccessToken", settings.BOAT_CHAT_ACCESS_TOKEN);
    }
    settingsMessage.textContent = "Saved" + (data && data.devices_signed_out ? `. ${data.devices_signed_out} signed-in device(s) were signed out.` : "") + (data && data.warning ? ` ${data.warning}` : "");
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
  // Enter confirms an IME composition (e.g. Japanese, Chinese); it must not send.
  if (event.isComposing || event.keyCode === 229) return;
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

let clearArmTimer = null;
function disarmClear() {
  clearTimeout(clearArmTimer); clearArmTimer = null;
  clearButton.textContent = "Clear";
  clearButton.removeAttribute("aria-label");
}
clearButton.addEventListener("click", () => {
  if (conversation.length && !clearArmTimer) {
    // Two-step confirmation: the second press within 5 seconds clears.
    clearButton.textContent = "Confirm clear";
    clearButton.setAttribute("aria-label", "Confirm: clear this conversation");
    announce("Press Clear again to confirm clearing this conversation.");
    clearArmTimer = setTimeout(disarmClear, 5000);
    return;
  }
  disarmClear();
  const clearedSession = sessionId;
  sessionId = createSessionId();
  sessionStorage.setItem("boatChatSessionId", sessionId);
  conversation = [];
  sessionStorage.removeItem("boatChatConversation");
  const token = accessCode();
  const headers = {"Content-Type":"application/json"};
  if (token) headers["X-Boat-Chat-Token"] = token;
  fetch("/api/session/clear", {method:"POST", headers, body:JSON.stringify({session_id:clearedSession})}).catch(() => {});
  messages.replaceChildren();
  addWelcomeMessage();
  announce("Conversation cleared");
  input.focus();
});

document.querySelector("#capabilities-open").addEventListener("click", async () => {
  capabilitiesContent.textContent = "Loading the list of things you can ask…"; capabilitiesDialog.showModal();
  try { const data=await fetchJson("/api/capabilities"); capabilitiesContent.replaceChildren(); data.groups.forEach((group)=>{ const section=document.createElement("section"); section.className="capability-group"; const title=document.createElement("h2"); title.textContent=group.name; const prompts=document.createElement("div"); prompts.className="capability-prompts"; group.prompts.forEach((prompt)=>{ const button=document.createElement("button"); button.type="button"; button.textContent=prompt; button.addEventListener("click",()=>{capabilitiesDialog.close(); ask(prompt);}); prompts.appendChild(button); }); section.append(title,prompts); capabilitiesContent.appendChild(section); }); } catch(error) { capabilitiesContent.textContent=`The list of things you can ask is unavailable: ${error.message}`; }
});
document.querySelector("#capabilities-close").addEventListener("click",()=>capabilitiesDialog.close());
document.querySelector("#insights-open").addEventListener("click",()=>{insightsDialog.showModal();loadInsights();});
document.querySelector("#maintenance-open").addEventListener("click",()=>{insightsDialog.showModal();loadInsights();});
document.querySelector("#insights-close").addEventListener("click",()=>insightsDialog.close());

setInterval(async()=>{if(localStorage.getItem("boatChatBrowserAlerts")!=="on") return; const token=accessCode(); if(accessTokenRequired && !token) return; try {const response=await fetch("/api/insights",{headers:{"X-Boat-Chat-Token":token}}); if(response.ok) processProactiveAlerts(await response.json());} catch(error) { /* Retry on the next interval. */ }},60000);

document.querySelector("#settings-open").addEventListener("click", openSettings);
document.querySelector("#settings-retry").addEventListener("click", openSettings);
document.querySelector("#settings-connect").addEventListener("click", () => { settingsDialog.close(); openConnect(); });

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
    "Ask about the boat now, look into a change, or review a trip. Each answer shows where its information came from."
  );
}

addWelcomeMessage();
renderSavedQuestions();
loadHealth();
loadBoatStatus();

// Header: on phones everything except the two view toggles lives under "More".
const moreMenu = document.querySelector(".more-menu");
const narrowHeader = window.matchMedia("(max-width: 480px)");
function syncMoreMenu() { moreMenu.open = !narrowHeader.matches; }
syncMoreMenu();
narrowHeader.addEventListener("change", syncMoreMenu);
moreMenu.querySelector(".more-items").addEventListener("click", (event) => {
  if (narrowHeader.matches && event.target.closest("button")) moreMenu.open = false;
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && narrowHeader.matches && moreMenu.open) { moreMenu.open = false; moreMenu.querySelector("summary").focus(); }
});
showView(document.body.dataset.view || "overview");

// Settings > Sign-in: signed-in devices and the shared passphrase.
const signinMessage = document.querySelector("#signin-message");
function adminHeaders() { const headers = { "Content-Type": "application/json" }; const admin = settingsToken(); if (admin) headers["X-Boat-Chat-Token"] = admin; else if (accessCode()) headers["X-Boat-Chat-Token"] = accessCode(); return headers; }
async function loadSignin() {
  const list = document.querySelector("#signin-devices"), state = document.querySelector("#signin-state");
  list.replaceChildren(); signinMessage.textContent = "";
  try {
    const headers = adminHeaders(); delete headers["Content-Type"];
    const response = await fetch("/api/auth/devices", { headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { state.textContent = "Connect this device to manage sign-in."; return; }
    // Crew can sign out only this device; signing out others or everyone, and setting the first passphrase, need the admin code.
    const admin = data.admin === true;
    document.querySelector("#signin-revoke-all").hidden = !admin;
    const blocked = !data.passphrase_set && !admin;
    document.querySelector("#signin-change").disabled = blocked;
    state.textContent = data.passphrase_set ? `Passphrase sign-in is on. ${data.devices.length} of ${data.max_devices} devices signed in.` : admin ? "No passphrase is set yet. Set one below so crew can sign in without the access code." : "No passphrase is set yet. Setting the first one needs the admin code (enter it in the field above) or an admin device.";
    data.devices.forEach((device) => {
      const row = document.createElement("li"); const info = document.createElement("span");
      info.textContent = device.label + (device.current ? " (this device)" : "");
      const detail = document.createElement("small"); detail.textContent = `Signed in until ${new Date(device.expires * 1000).toLocaleString()}`; info.appendChild(detail);
      row.append(info);
      if (admin || device.current) {
        const revoke = document.createElement("button"); revoke.type = "button"; revoke.textContent = "Sign out";
        revoke.addEventListener("click", async () => {
          const result = await fetch("/api/auth/devices/revoke", { method: "POST", headers: adminHeaders(), body: JSON.stringify({ id: device.id }) });
          if (result.ok && device.current) clearDeviceToken();
          loadSignin();
        });
        row.append(revoke);
      }
      list.appendChild(row);
    });
  } catch { state.textContent = "Could not read the sign-in list."; }
}
document.querySelector("#signin-revoke-all").addEventListener("click", async () => {
  const result = await fetch("/api/auth/devices/revoke", { method: "POST", headers: adminHeaders(), body: JSON.stringify({ id: "all" }) });
  if (result.ok) clearDeviceToken(); loadSignin();
});
document.querySelector("#signin-change").addEventListener("click", async () => {
  const current = document.querySelector("#signin-current"), fresh = document.querySelector("#signin-new");
  signinMessage.textContent = "Saving…";
  try {
    const response = await fetch("/api/auth/passphrase", { method: "POST", headers: adminHeaders(), body: JSON.stringify({ current: current.value, new: fresh.value }) });
    const data = await response.json().catch(() => ({}));
    signinMessage.textContent = response.ok ? `Passphrase saved. ${data.revoked_others || 0} other device(s) signed out.` : response.status === 429 ? "Too many attempts. Try again later." : data.error || "The passphrase was not saved.";
    if (response.ok) { current.value = ""; fresh.value = ""; loadSignin(); }
  } catch { signinMessage.textContent = "Could not reach the boat."; }
});
document.querySelectorAll("#signin-section input").forEach((field) => field.addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); }));
document.querySelector("#settings-open")?.addEventListener("click", () => setTimeout(loadSignin, 0));
