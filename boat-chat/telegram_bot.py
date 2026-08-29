#!/usr/bin/env python3
"""Telegram long-polling bridge for Boat Chat."""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
ENV_CONFIG = ROOT / "boat-chat.env"
STATE_DIR = ROOT / "memory"
OFFSET_FILE = STATE_DIR / "telegram-offset.json"
CONVERSATIONS_FILE = STATE_DIR / "telegram-conversations.json"
DEFAULT_BOAT_CHAT_URL = "http://127.0.0.1:8765"
TELEGRAM_MAX_MESSAGE = 3900
CONVERSATIONS: dict[str, list[dict[str, str]]] = {}

SETTING_KEYS = {
    "BOAT_CHAT_URL",
    "BOAT_CHAT_PORT",
    "BOAT_CHAT_ACCESS_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "TELEGRAM_POLL_TIMEOUT",
}


def log(message: str) -> None:
    sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    sys.stderr.flush()


def parse_env_config() -> dict[str, str]:
    settings: dict[str, str] = {}
    if ENV_CONFIG.exists():
        for raw_line in ENV_CONFIG.read_text(errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in SETTING_KEYS:
                continue
            try:
                parsed = shlex.split(value, comments=False, posix=True)
                settings[key] = parsed[0] if parsed else ""
            except ValueError:
                settings[key] = value.strip().strip("'\"")
    for key in SETTING_KEYS:
        if key in os.environ:
            settings[key] = os.environ[key]
    return settings


def normalize_http_url(value: str, default_scheme: str = "http") -> str:
    text = value.strip()
    if not text:
        return text
    if "://" not in text:
        return f"{default_scheme}://{text}"
    return text


def boat_chat_url(settings: dict[str, str]) -> str:
    configured = settings.get("BOAT_CHAT_URL", "").strip()
    if configured:
        return normalize_http_url(configured).rstrip("/")
    port = settings.get("BOAT_CHAT_PORT", "8765").strip() or "8765"
    return f"http://127.0.0.1:{port}"


def allowed_chat_ids(settings: dict[str, str]) -> set[str]:
    raw = settings.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    return {item.strip() for item in raw.replace(";", ",").split(",") if item.strip()}


def http_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 30, headers: dict[str, str] | None = None) -> Any:
    data = None
    method = "GET"
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        method = "POST"
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def telegram_api(token: str, method: str, payload: dict[str, Any] | None = None, timeout: int = 35) -> Any:
    url = f"https://api.telegram.org/bot{urllib.parse.quote(token)}/{method}"
    return http_json(url, payload=payload, timeout=timeout)


def load_offset() -> int | None:
    try:
        data = json.loads(OFFSET_FILE.read_text())
        offset = data.get("offset")
        return int(offset) if offset is not None else None
    except Exception:
        return None


def save_offset(offset: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(json.dumps({"offset": offset}) + "\n")


def load_conversations() -> None:
    try:
        payload = json.loads(CONVERSATIONS_FILE.read_text())
        if isinstance(payload, dict):
            CONVERSATIONS.update({str(key): value[-8:] for key, value in payload.items() if isinstance(value, list)})
    except Exception:
        return


def save_conversations() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONVERSATIONS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(CONVERSATIONS))
    temporary.replace(CONVERSATIONS_FILE)


def split_message(text: str) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip() or "(No answer returned)"
    while len(remaining) > TELEGRAM_MAX_MESSAGE:
        split_at = remaining.rfind("\n", 0, TELEGRAM_MAX_MESSAGE)
        if split_at < 500:
            split_at = remaining.rfind(" ", 0, TELEGRAM_MAX_MESSAGE)
        if split_at < 500:
            split_at = TELEGRAM_MAX_MESSAGE
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    chunks.append(remaining)
    return chunks


def send_message(token: str, chat_id: int | str, text: str, message_thread_id: int | None = None) -> None:
    for chunk in split_message(text):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        telegram_api(token, "sendMessage", payload)


def send_action(token: str, chat_id: int | str, action: str = "typing", message_thread_id: int | None = None) -> None:
    try:
        payload: dict[str, Any] = {"chat_id": chat_id, "action": action}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        telegram_api(token, "sendChatAction", payload, timeout=10)
    except Exception as exc:
        log(f"sendChatAction failed: {exc}")


def chat_with_boat(settings: dict[str, str], message: str, history: list[dict[str, str]] | None = None) -> str:
    url = boat_chat_url(settings) + "/api/chat"
    headers = {"X-Boat-Chat-Token": settings["BOAT_CHAT_ACCESS_TOKEN"]} if settings.get("BOAT_CHAT_ACCESS_TOKEN") else {}
    response = http_json(url, payload={"message": message, "history": history or [], "session_id": settings.get("_SESSION_ID", "")}, timeout=240, headers=headers)
    if not isinstance(response, dict):
        return str(response)
    if response.get("answer"):
        return str(response["answer"])
    if response.get("error"):
        return f"Boat Chat error: {response['error']}"
    return json.dumps(response, indent=2)[:12000]


def status_text(settings: dict[str, str]) -> str:
    try:
        health = http_json(boat_chat_url(settings) + "/health", timeout=10)
        memory = health.get("memory", {}) if isinstance(health, dict) else {}
        return (
            f"Boat Chat: {'OK' if health.get('ok') else 'not OK'}\n"
            f"Provider: {health.get('provider', 'unknown')}\n"
            f"Memory chunks: {memory.get('chunks', 'unknown')}\n"
            f"sqlite-vec: {memory.get('sqlite_vec', 'unknown')}"
        )
    except Exception as exc:
        return f"Boat Chat is not reachable: {exc}"


def help_text(chat_id: int | str, authorized: bool) -> str:
    auth_line = "authorized" if authorized else "not authorized for boat questions yet"
    return (
        "VesselStack Boat Chat Telegram bot\n\n"
        "Send a boat-specific question and I will answer using the local Boat Chat service.\n\n"
        "Commands:\n"
        "/status - Boat Chat service status\n"
        "/clear - clear this conversation's context\n"
        "/id - show this Telegram chat ID\n"
        "/help - show this help\n\n"
        f"Chat ID: {chat_id}\n"
        f"Access: {auth_line}"
    )


def handle_message(token: str, settings: dict[str, str], message: dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_thread_id = message.get("message_thread_id")
    text = str(message.get("text") or "").strip()
    if chat_id is None or not text:
        return

    allowed = allowed_chat_ids(settings)
    authorized = str(chat_id) in allowed
    command = text.split()[0].split("@", 1)[0].lower()
    log(f"received message chat_id={chat_id} thread_id={message_thread_id} command={command} authorized={authorized}")

    if command in ("/start", "/help"):
        send_message(token, chat_id, help_text(chat_id, authorized), message_thread_id)
        return
    if command == "/id":
        send_message(token, chat_id, f"Telegram chat ID: {chat_id}", message_thread_id)
        return
    if command == "/status":
        send_message(token, chat_id, status_text(settings), message_thread_id)
        return
    conversation_key = f"{chat_id}:{message_thread_id or 0}"
    if command == "/clear":
        CONVERSATIONS.pop(conversation_key, None)
        save_conversations()
        try:
            headers = {"X-Boat-Chat-Token": settings["BOAT_CHAT_ACCESS_TOKEN"]} if settings.get("BOAT_CHAT_ACCESS_TOKEN") else {}
            http_json(boat_chat_url(settings) + "/api/session/clear", payload={"session_id": f"telegram:{conversation_key}"}, timeout=10, headers=headers)
        except Exception as exc:
            log(f"server-side conversation clear failed: {exc}")
        send_message(token, chat_id, "Conversation context cleared.", message_thread_id)
        return
    if not authorized:
        send_message(
            token,
            chat_id,
            "This chat is not authorized for boat questions.\n"
            f"Add this ID to TELEGRAM_ALLOWED_CHAT_IDS in Boat Chat Settings: {chat_id}",
            message_thread_id,
        )
        return

    send_action(token, chat_id, message_thread_id=message_thread_id)
    try:
        send_message(token, chat_id, "Gathering boat telemetry...", message_thread_id)
        history = CONVERSATIONS.get(conversation_key, [])
        request_settings = {**settings, "_SESSION_ID": f"telegram:{conversation_key}"}
        answer = chat_with_boat(request_settings, text, history=history)
        CONVERSATIONS[conversation_key] = (
            history
            + [
                {"role": "user", "content": text},
                {"role": "assistant", "content": answer},
            ]
        )[-8:]
        save_conversations()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        answer = f"Request failed: HTTP {exc.code}\n{body[:2000]}"
    except Exception as exc:
        answer = f"Request failed: {exc}"
    try:
        send_message(token, chat_id, answer, message_thread_id)
    except Exception as exc:
        log(f"sendMessage failed for chat_id={chat_id}: {exc}")


def poll_once(token: str, settings: dict[str, str], offset: int | None) -> int | None:
    timeout = int(settings.get("TELEGRAM_POLL_TIMEOUT", "25") or "25")
    payload: dict[str, Any] = {
        "timeout": timeout,
        "allowed_updates": ["message"],
    }
    if offset is not None:
        payload["offset"] = offset
    result = telegram_api(token, "getUpdates", payload, timeout=timeout + 10)
    updates = result.get("result", []) if isinstance(result, dict) else []
    for update in updates:
        update_id = int(update.get("update_id", 0))
        next_offset = update_id + 1
        try:
            message = update.get("message")
            if isinstance(message, dict):
                handle_message(token, settings, message)
        finally:
            save_offset(next_offset)
            offset = next_offset
    return offset


def main() -> None:
    offset = load_offset()
    load_conversations()
    log("Boat Chat Telegram bot starting")
    while True:
        settings = parse_env_config()
        token = settings.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            log("TELEGRAM_BOT_TOKEN is not configured; sleeping")
            time.sleep(60)
            continue
        try:
            offset = poll_once(token, settings, offset)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            log(f"Telegram API error {exc.code}: {body[:1000]}")
            time.sleep(15)
        except Exception as exc:
            log(f"Telegram polling failed: {exc}")
            time.sleep(15)


if __name__ == "__main__":
    main()
