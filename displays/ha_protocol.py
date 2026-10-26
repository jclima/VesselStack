"""Home Assistant websocket protocol as a pure state machine (stdlib only, no I/O, no clock).

Implemented from Home Assistant's documented websocket API (auth_required / auth / auth_ok |
auth_invalid, command ids that only ever increase, ``subscribe_entities`` with its compressed
``a`` / ``c`` / ``r`` events, and ``get_states`` + ``subscribe_events`` as the fallback). It has
been tested against a scripted fake server, not against a real Home Assistant.

``HaSession.feed(message)`` takes one received frame (text, bytes or an already decoded object)
and returns ``(frames, updates, events)``:

* ``frames``: objects to send back, in order (the caller JSON-encodes them);
* ``updates``: ``[(entity_id, state)]`` for the configured entities only, where ``state`` is the
  state string, or None when the entity is gone or its state is not a string;
* ``events``: ``("auth_ok",)``, ``("auth_invalid",)``, ``("fallback",)``, ``("ready", mode)`` when
  the subscription is established (mode "entities" or "events"), ``("failed", reason)`` when
  Home Assistant refused the subscription (``reason`` is a short fixed word, never server text).

Message bodies are never retained, returned or logged: only the configured entities' state
strings leave this module, and the token only ever appears in the one auth frame.
"""
from __future__ import annotations

import json

PHASE_NEW = "new"
PHASE_AUTH = "auth"            # auth frame sent, waiting for the verdict
PHASE_SUBSCRIBING = "subscribing"
PHASE_READY = "ready"
PHASE_FAILED = "failed"        # auth_invalid or no subscription possible: nothing more is accepted

ID_ENTITIES = 1
ID_EVENTS = 2          # fallback: subscribe_events first ...
ID_GET_STATES = 3      # ... then get_states (command ids must only ever increase)


class HaSession:
    def __init__(self, token: str, entity_ids):
        self._token = token
        self._entities = frozenset(entity_ids)
        self._ids = sorted(self._entities)
        self.phase = PHASE_NEW
        self.mode = None            # "entities" or "events" once chosen
        self._pending: set = set()  # fallback command ids still waiting for their result

    # -- frames the caller sends -------------------------------------------------
    def _subscribe_entities(self) -> dict:
        return {"id": ID_ENTITIES, "type": "subscribe_entities", "entity_ids": list(self._ids)}

    def _fallback_frames(self) -> list:
        # Subscribe first, then read the states: an event that arrives before the snapshot is older
        # than the snapshot, so the snapshot (applied last) wins. Ids must only ever increase.
        return [{"id": ID_EVENTS, "type": "subscribe_events", "event_type": "state_changed"},
                {"id": ID_GET_STATES, "type": "get_states"}]

    # -- input -----------------------------------------------------------------
    def feed(self, message) -> tuple:
        frames: list = []
        updates: list = []
        events: list = []
        data = self._decode(message)
        if data is None or self.phase == PHASE_FAILED:
            return frames, updates, events
        kind = data.get("type")
        if kind == "auth_required":
            if self.phase == PHASE_NEW:
                self.phase = PHASE_AUTH
                frames.append({"type": "auth", "access_token": self._token})
        elif kind == "auth_ok":
            if self.phase == PHASE_AUTH:
                self.phase = PHASE_SUBSCRIBING
                self.mode = "entities"
                frames.append(self._subscribe_entities())
                events.append(("auth_ok",))
        elif kind == "auth_invalid":
            self.phase = PHASE_FAILED
            events.append(("auth_invalid",))
        elif kind == "result":
            self._result(data, frames, updates, events)
        elif kind == "event":
            self._event(data, updates)
        return frames, updates, events

    @staticmethod
    def _decode(message):
        if isinstance(message, (bytes, bytearray)):
            try:
                message = bytes(message).decode("utf-8")
            except UnicodeDecodeError:
                return None
        if isinstance(message, str):
            try:
                message = json.loads(message)
            except ValueError:
                return None
        return message if isinstance(message, dict) else None

    # -- results ---------------------------------------------------------------
    def _result(self, data: dict, frames: list, updates: list, events: list) -> None:
        if self.phase != PHASE_SUBSCRIBING:
            return
        ident, success = data.get("id"), data.get("success") is True
        if self.mode == "entities" and ident == ID_ENTITIES:
            if success:
                self.phase = PHASE_READY
                events.append(("ready", "entities"))
                return
            # An older Home Assistant (before subscribe_entities) or one that refuses the command.
            self.mode = "events"
            self._pending = {ID_EVENTS, ID_GET_STATES}
            frames.extend(self._fallback_frames())
            events.append(("fallback",))
        elif self.mode == "events" and ident in self._pending:
            if not success:
                self.phase = PHASE_FAILED
                events.append(("failed", "subscribe"))
                return
            self._pending.discard(ident)
            if ident == ID_GET_STATES:
                result = data.get("result")
                for item in result if isinstance(result, list) else ():
                    if isinstance(item, dict) and item.get("entity_id") in self._entities:
                        state = item.get("state")
                        updates.append((item["entity_id"], state if isinstance(state, str) else None))
            if not self._pending:
                self.phase = PHASE_READY
                events.append(("ready", "events"))

    # -- events ----------------------------------------------------------------
    def _event(self, data: dict, updates: list) -> None:
        if self.phase not in (PHASE_SUBSCRIBING, PHASE_READY):
            return
        event = data.get("event")
        if not isinstance(event, dict):
            return
        if self.mode == "entities" and data.get("id") == ID_ENTITIES:
            self._compressed(event, updates)
        elif self.mode == "events" and data.get("id") == ID_EVENTS:
            self._state_changed(event, updates)

    def _compressed(self, event: dict, updates: list) -> None:
        initial = event.get("a")
        if isinstance(initial, dict):
            for eid, body in initial.items():
                if eid in self._entities and isinstance(body, dict):
                    state = body.get("s")
                    updates.append((eid, state if isinstance(state, str) else None))
        changes = event.get("c")
        if isinstance(changes, dict):
            for eid, body in changes.items():
                if eid not in self._entities or not isinstance(body, dict):
                    continue
                added = body.get("+")
                if isinstance(added, dict) and "s" in added:   # attribute-only changes carry no "s"
                    state = added["s"]
                    updates.append((eid, state if isinstance(state, str) else None))
        removed = event.get("r")
        if isinstance(removed, list):
            for eid in removed:
                if isinstance(eid, str) and eid in self._entities:
                    updates.append((eid, None))

    def _state_changed(self, event: dict, updates: list) -> None:
        if event.get("event_type") != "state_changed":
            return
        body = event.get("data")
        if not isinstance(body, dict):
            return
        eid = body.get("entity_id")
        if not isinstance(eid, str) or eid not in self._entities:
            return
        new = body.get("new_state")
        state = new.get("state") if isinstance(new, dict) else None
        updates.append((eid, state if isinstance(state, str) else None))
