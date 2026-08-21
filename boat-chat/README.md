# VesselStack Boat Chat

Local read-only diagnostic chatbot for VesselStack.

## Run

Boat Chat is installed as a systemd service:

```bash
sudo systemctl status boat-chat.service
sudo systemctl restart boat-chat.service
```

It starts on boot and reads runtime settings from `/opt/vesselstack/config/boat-chat.env`.

For a foreground development run:

```bash
cd /opt/vesselstack
python3 -m venv boat-chat/.venv
boat-chat/.venv/bin/python -m pip install -r boat-chat/requirements.txt
boat-chat/.venv/bin/python boat-chat/app.py
```

Open `http://127.0.0.1:8765` over ZeroTier, `http://127.0.0.1:8765` from the current boat LAN, or `http://127.0.0.1:8765` on the Pi itself. In Home Assistant, use the `Boat Chat` view in the `VesselStack` Lovelace dashboard at `/lovelace/boat-chat`; the embedded iframe uses the ZeroTier URL.

Without a configured model provider, the app still gathers and returns local context so telemetry plumbing can be tested.

## Context Strategy

Boat Chat keeps context in tiers so common questions stay fast and small local models do not have to infer stable facts from long runbooks:

- Tier 0: stable boat facts from `boat-chat/boat_facts.json` such as vessel name, model, MMSI, call sign, host, telemetry stack, and engine layout.
- Tier 1: live SignalK and Home Assistant state, fetched only when relevant.
- Tier 2: InfluxDB historical summaries, fetched only for questions that need history such as engine runtime or fuel usage.
- Tier 3: local docs and runbooks from the SQLite memory index, limited to the top matches.

The same SQLite database also stores materialized telemetry summaries in `telemetry_summaries`. These are derived facts such as latest engine run, common fuel-usage windows, shore-power timing, inferred solar contribution, and a latest boat-status context snapshot. They are refreshed by `boat-chat-telemetry-indexer.timer` every five minutes. InfluxDB and Home Assistant remain the source of truth; the cache is only a fast summary layer.

Forward-looking power observations are stored separately in `boat-chat/data/power_tracking.sqlite`. Boat Chat integrates positive SmartShunt power only across adjacent samples where the boat is underway, beyond the dock radius, or not charging, and both engines are below 200 RPM. It reports that result as inferred net solar-to-battery energy, not gross panel output. The Home Assistant shore-power entity is only a charging proxy, so dockside source attribution remains ambiguous until a physical AC-input meter is installed. Observations are retained for 400 days; gaps longer than 15 minutes are excluded. See `POWER_MONITORING.md` for instrumentation and interpretation.

Runtime behavior is governed by `boat-chat/BOAT_CHAT_AGENT.md`. It is optimized for mobile chat: concise answers first, no internal implementation details, and one short clarification question instead of guessing when a time window or required boat data is ambiguous.

Complex questions use semantic signal groups instead of requiring every query word to appear in one measurement name. Boat Chat can gather multiple requested signals, exact local windows (including the recorded last trip), min/max timestamps, aligned values at notable times, port/starboard comparisons, RPM-matched comparisons, threshold durations, and correlations. Engine diagnostics include a separate engine-running view so normal engine-off zero readings do not become false low-pressure or fuel-flow findings.

Current SignalK values are converted to the documented US display units before model reasoning. Nearby-vessel/AIS questions use the SignalK `/vessels` feed and include target distance, speed, course, position timestamp, and staleness. Home Assistant contributes current states, unavailable/stale review lists, and relevant retained state transitions for complex alert, shore-power, trip, bilge, and service questions.

The web and Telegram clients send the last few conversation turns with each request. A short answer to a clarification, such as `last 30 days`, is combined with the prior user question instead of being treated as a new unrelated question. Telegram `/clear` and the web Clear button reset that context.

Basic identity questions such as the boat name, MMSI, call sign, LOA, or beam are answered directly from Tier 0 without calling the LLM. Simple health/status questions such as "is the boat OK?" are answered directly from Home Assistant health entities before falling back to the LLM.

Purpose-built telemetry questions are also answered deterministically before any LLM call:

- "When was the first time motors were running?" queries `INFLUXDB_HISTORY_BUCKET` for the earliest retained RPM sample above 200 RPM.
- "When were the motors last running?" queries `INFLUXDB_HISTORY_BUCKET` for the latest retained RPM sample above 200 RPM.
- "How much fuel did I use this weekend?" integrates 1-minute port/starboard fuel-rate samples during engine-running minutes for the resolved local time window.
- "What is my average fuel economy this year?" integrates fuel burn and speed-over-ground during engine-running minutes to estimate NM/gal.
- "Fuel economy with both engines between 1800 and 2200 RPM" applies the requested RPM band after resolving the time window.
- "When did shore power turn off?" checks Home Assistant's `binary_sensor.shore_power_connected` current state and retained history.
- "How much did solar contribute while shore power was off last week?" uses the durable inferred solar tracker and states its coverage and confidence.
- "Check fuel usage and see if one motor uses more than the other" compares port/starboard fuel only during 1-minute samples where an engine is actually running.
- "Battery voltage over the past 2 weeks" summarizes retained SmartShunt voltage/SOC history and uses the AGM voltage chart only as a resting-voltage reference.

## Settings UI

Use the `Settings` button in the web app to configure:

- Primary and fallback providers and models, all via dropdowns. Model dropdowns are provider-aware: Ollama lists the models actually installed (live from `/api/tags`), other providers show curated suggestions, and a `Custom...` entry accepts any exact model ID. Suggestions come from `GET /api/models`.
- The primary model is stored in the right env key automatically: `BOAT_CHAT_CODEX_MODEL` for `codex_cli`, `BOAT_CHAT_CLAUDE_MODEL` for `claude_cli`, `BOAT_CHAT_MODEL` for everything else (and the generic key is cleared when a CLI provider is chosen, so a stale value cannot shadow it). Fallback models always use `BOAT_CHAT_FALLBACK_MODEL`.
- Context budget, output budget, and CLI timeout.
- Codex CLI, Claude CLI, Ollama, OpenAI, Vercel AI Gateway, AWS Bedrock, Google, and OpenAI-compatible provider settings.
- Telegram bot token and allowed chat IDs.
- Server host/port and an optional settings write token.

Settings are saved to `/opt/vesselstack/config/boat-chat.env`. Provider, model, token, and credential changes are read dynamically by the next chat request. Host/port changes require:

```bash
sudo systemctl restart boat-chat.service
```

Secrets are not shown back in the browser; blank password fields keep the existing saved value.

Settings writes (`POST /api/settings`) are only accepted from loopback or RFC1918 private addresses. Configure `BOAT_CHAT_SETTINGS_TOKEN` whenever the listener is reachable from a shared LAN; private addressing alone is not authentication. The matching `X-Boat-Chat-Token` header is then required, and the web UI remembers the token in browser localStorage after you enter it once in the Server section. Keep the app off the public internet.

## Telemetry Cache

Refresh manually:

```bash
boat-chat/.venv/bin/python boat-chat/telemetry_indexer.py run
```

Inspect cached summaries:

```bash
boat-chat/.venv/bin/python boat-chat/telemetry_indexer.py status
curl -s http://127.0.0.1:8765/health | jq .telemetry_cache
```

Installed scheduler:

```bash
systemctl status boat-chat-telemetry-indexer.timer
journalctl -u boat-chat-telemetry-indexer.service --since "1 hour ago"
```

## Environment

- `BOAT_CHAT_PROVIDER` — optional; auto-detected when unset. Supported values: `codex_cli`, `claude_cli`, `openai`, `vercel`, `bedrock`, `google`, `ollama`, `openai_compatible`, `local`.
- `BOAT_CHAT_MODEL` — provider model name. Defaults are provider-specific where a safe default exists.
- `BOAT_CHAT_FALLBACK_PROVIDER` — optional secondary provider used when the primary LLM call fails. Uses the same provider names.
- `BOAT_CHAT_FALLBACK_MODEL` — optional model name for the secondary provider. For Ollama fallback, defaults to `qwen2.5:3b`.
- `BOAT_CHAT_MAX_TOKENS` — optional, defaults to `1200`.
- `BOAT_CHAT_CONTEXT_CHARS` — optional local-context budget for the prompt. Lower values such as `4000` keep simple small-model requests compact; complex historical questions automatically receive up to at least 18,000 characters so aligned evidence is not discarded.
- `BOAT_CHAT_OLLAMA_NUM_CTX` — optional Ollama context window in tokens; defaults to `8192` so a larger context budget can never be silently truncated. Ollama calls also pass `keep_alive: -1` to keep the model resident in RAM (~2 GB for a 3B Q4 model), and the prompt places stable context before the question so Ollama can reuse its KV prefix cache — warm repeat calls drop from ~85 s to a few seconds.
- `BOAT_CHAT_SETTINGS_TOKEN` — optional write token for `POST /api/settings` (sent as `X-Boat-Chat-Token`).
- `BOAT_CHAT_CODEX_MODEL` — optional model for `codex_cli`; defaults to `gpt-5.4-mini` for lighter subscription usage.
- `BOAT_CHAT_CODEX_EFFORT` — optional Codex reasoning effort; defaults to `low`.
- `BOAT_CHAT_CODEX_BIN` — optional Codex binary path; defaults to `codex`.
- `BOAT_CHAT_CLAUDE_MODEL` — optional model for `claude_cli`; leave blank to use the Claude CLI default.
- `BOAT_CHAT_CLAUDE_EFFORT` — optional Claude effort; defaults to `low`.
- `BOAT_CHAT_CLAUDE_BIN` — optional Claude binary path; defaults to `claude`.
- `BOAT_CHAT_CLAUDE_MAX_BUDGET_USD` — optional per-call budget cap passed to `claude -p`.
- `BOAT_CHAT_CLI_TIMEOUT` — optional timeout in seconds for CLI-backed providers; defaults to `120`.
- `BOAT_CHAT_HOST` — optional, defaults to `0.0.0.0` so trusted LAN devices can reach it.
- `BOAT_CHAT_PORT` — optional, defaults to `8765`.
- `BOAT_CHAT_URL` — optional internal URL used by the Telegram worker. Defaults to `http://127.0.0.1:<BOAT_CHAT_PORT>`.
- `TELEGRAM_BOT_TOKEN` — optional BotFather token for the Telegram worker.
- `TELEGRAM_ALLOWED_CHAT_IDS` — comma-separated Telegram chat IDs allowed to ask boat questions.
- `TELEGRAM_POLL_TIMEOUT` — optional Telegram long-poll timeout in seconds. Defaults to `25`.

## Model Providers

### Codex CLI

Uses the locally installed `codex exec` CLI with the signed-in ChatGPT/Codex account. This avoids Vercel AI Gateway per-token billing for LLM fallback answers, but it still consumes Codex subscription quota and has agent startup overhead. Boat Chat uses `gpt-5.4-mini`, low reasoning effort, read-only sandboxing, disabled shell tools, disabled web search, and ephemeral sessions by default.

This is the recommended subscription-backed path for this app because Codex documents non-interactive trusted local workflows. Keep it on the trusted boat host only.

```bash
BOAT_CHAT_PROVIDER=codex_cli \
BOAT_CHAT_CODEX_MODEL=gpt-5.4-mini \
BOAT_CHAT_CODEX_EFFORT=low \
BOAT_CHAT_FALLBACK_PROVIDER=ollama \
BOAT_CHAT_FALLBACK_MODEL=qwen2.5:3b \
python3 boat-chat/app.py
```

### Claude CLI

Uses the locally installed `claude -p` CLI. Boat Chat runs it in non-interactive print mode with JSON output, disabled tools, no session persistence, and the same supplied local context used by other providers.

```bash
BOAT_CHAT_PROVIDER=claude_cli \
BOAT_CHAT_CLAUDE_MODEL=sonnet \
BOAT_CHAT_CLAUDE_EFFORT=low \
BOAT_CHAT_CLAUDE_MAX_BUDGET_USD=0.25 \
BOAT_CHAT_FALLBACK_PROVIDER=ollama \
BOAT_CHAT_FALLBACK_MODEL=qwen2.5:3b \
python3 boat-chat/app.py
```

This is intended for your trusted local CLI account on the boat host. It is not an OpenAI-compatible API proxy and should stay behind the trusted LAN.

### OpenAI

Uses the OpenAI Responses API with built-in web search.

```bash
BOAT_CHAT_PROVIDER=openai \
OPENAI_API_KEY=... \
BOAT_CHAT_MODEL=gpt-5.5 \
python3 boat-chat/app.py
```

### Vercel AI Gateway

Uses Vercel's OpenAI-compatible gateway endpoint. Set `BOAT_CHAT_MODEL` to a Gateway model slug such as `alibaba/qwen3.5-flash` or another model listed by your Gateway account.

```bash
BOAT_CHAT_PROVIDER=vercel \
AI_GATEWAY_API_KEY=... \
BOAT_CHAT_MODEL=alibaba/qwen3.5-flash \
python3 boat-chat/app.py
```

`VERCEL_OIDC_TOKEN` is also accepted for environments where Vercel provides it.

To use Vercel as the primary provider and local Ollama as a fallback:

```bash
BOAT_CHAT_PROVIDER=vercel \
AI_GATEWAY_API_KEY=... \
BOAT_CHAT_MODEL=alibaba/qwen3.5-flash \
BOAT_CHAT_FALLBACK_PROVIDER=ollama \
BOAT_CHAT_FALLBACK_MODEL=qwen2.5:3b \
OLLAMA_HOST=http://127.0.0.1:11434 \
python3 boat-chat/app.py
```

### AWS Bedrock

Uses the Bedrock Runtime Converse API and signs requests directly with AWS Signature Version 4. `BOAT_CHAT_MODEL` must be a Bedrock model ID, inference profile ID, or ARN available in the selected region.

```bash
BOAT_CHAT_PROVIDER=bedrock \
AWS_REGION=us-west-2 \
AWS_ACCESS_KEY_ID=... \
AWS_SECRET_ACCESS_KEY=... \
AWS_SESSION_TOKEN=... \
BOAT_CHAT_MODEL=<bedrock-model-id-or-inference-profile-id> \
python3 boat-chat/app.py
```

`AWS_SESSION_TOKEN` is only needed for temporary credentials.

### Google

API-key mode uses the Gemini Developer API:

```bash
BOAT_CHAT_PROVIDER=google \
GEMINI_API_KEY=... \
BOAT_CHAT_MODEL=gemini-2.5-flash \
python3 boat-chat/app.py
```

Google Cloud / Vertex AI mode uses a Google Cloud project and OAuth bearer token:

```bash
BOAT_CHAT_PROVIDER=google \
GOOGLE_CLOUD_PROJECT=your-project-id \
GOOGLE_CLOUD_LOCATION=us-central1 \
GOOGLE_CLOUD_ACCESS_TOKEN="$(gcloud auth print-access-token)" \
BOAT_CHAT_MODEL=gemini-2.5-flash \
python3 boat-chat/app.py
```

If `GOOGLE_CLOUD_ACCESS_TOKEN` is not set, the app tries `gcloud auth print-access-token`.

### Ollama

Uses a local or LAN Ollama HTTP API. Default endpoint is `http://127.0.0.1:11434`; default model is `llama3.2`.
`OLLAMA_HOST` can be set as a full URL or as `host:port`; Boat Chat normalizes a missing scheme to `http://`.

First pull a model on the host running Ollama:

```bash
ollama pull llama3.2
```

Then start Boat Chat:

```bash
BOAT_CHAT_PROVIDER=ollama \
BOAT_CHAT_MODEL=llama3.2 \
boat-chat/.venv/bin/python boat-chat/app.py
```

For an Ollama server on another trusted host:

```bash
BOAT_CHAT_PROVIDER=ollama \
OLLAMA_HOST=http://ollama-host.example:11434 \
BOAT_CHAT_MODEL=llama3.2 \
boat-chat/.venv/bin/python boat-chat/app.py
```

For small local models such as `qwen2.5:1.5b`, keep the retrieved context compact:

```bash
BOAT_CHAT_PROVIDER=ollama \
BOAT_CHAT_MODEL=qwen2.5:1.5b \
BOAT_CHAT_CONTEXT_CHARS=4000 \
boat-chat/.venv/bin/python boat-chat/app.py
```

### Generic OpenAI-Compatible

For any provider exposing `/chat/completions`:

```bash
BOAT_CHAT_PROVIDER=openai_compatible \
BOAT_CHAT_BASE_URL=https://provider.example/v1 \
BOAT_CHAT_API_KEY=... \
BOAT_CHAT_MODEL=provider-model-name \
python3 boat-chat/app.py
```

## Telegram Bot

The Telegram bridge runs as a separate systemd service and calls the local Boat Chat HTTP API. It uses the same provider/model settings as the web app.

1. Create a bot with Telegram BotFather and copy the token.
2. In Boat Chat Settings, set `Telegram token`.
3. Start a chat with the bot and send `/id`.
4. Copy the returned chat ID into `Telegram chat IDs`.
5. Enable and start the worker:

```bash
sudo cp /opt/vesselstack/systemd/boat-chat-telegram.service /etc/systemd/system/boat-chat-telegram.service
sudo systemctl daemon-reload
sudo systemctl enable --now boat-chat-telegram.service
```

Useful commands in Telegram:

- `/status` — Boat Chat service and memory status.
- `/clear` — clear recent conversation context for that chat/thread.
- `/id` — show the current Telegram chat ID.
- `/help` — usage summary.

If `TELEGRAM_BOT_TOKEN` is unset, the worker idles and logs that it is waiting for configuration. If `TELEGRAM_ALLOWED_CHAT_IDS` is unset, `/id`, `/help`, and `/status` work, but boat questions are refused until an allowed chat ID is configured.

## Read-Only Data Sources

- SignalK self telemetry: `http://127.0.0.1:3000/signalk/v1/api/vessels/self`
- SignalK nearby AIS traffic: `http://127.0.0.1:3000/signalk/v1/api/vessels`
- Home Assistant: local REST API using the existing `.mcp.json` bearer token.
- InfluxDB: Flux queries using existing `.mcp.json` Influx credentials.
- Curated local docs: `boat-chat/BOAT_CHAT_AGENT.md`, `boat-chat/boat_facts.json`, `boat-chat/README.md`, and `boat-chat/knowledge/*.md`.

Operator/runbook files such as root `BOAT_AGENT.md`, `STACK_OVERVIEW.md`, `AGENTS.md`, and Codex memory are intentionally excluded from normal Boat Chat retrieval so the user-facing bot does not repeat implementation instructions.

## Local Memory Index

Boat Chat uses a local SQLite memory index when present:

- Default path: `/opt/vesselstack/data/boat-chat/memory/boat_memory.sqlite`
- `FTS5` keyword retrieval works with the Python standard library.
- `sqlite-vec` vector retrieval turns on automatically when the Python package is installed.
- Embeddings are deterministic hashed text vectors, so indexing does not require a local transformer model or cloud API call.
- The generated `boat-chat/memory/` directory is ignored by Git.

Build or refresh the index after changing docs/runbooks:

```bash
boat-chat/.venv/bin/python boat-chat/memory_index.py rebuild
```

Inspect status:

```bash
boat-chat/.venv/bin/python boat-chat/memory_index.py status
```

Test retrieval:

```bash
boat-chat/.venv/bin/python boat-chat/memory_index.py search "battery charging behavior"
```

## Verification

Run the focused regression suite with:

```bash
boat-chat/.venv/bin/python -m unittest discover -s tests -p 'test_boat_chat.py' -v
```

The suite covers question routing, multi-signal matching, engine-running filtering, extrema timestamps, aligned side comparisons, display-unit conversions, arbitrary numeric windows, RPM bands, and conversational follow-ups.
