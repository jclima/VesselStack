#!/usr/bin/env python3
"""Fake Home Assistant websocket server for tests/test-displays-bridge.sh.

Speaks the documented /api/websocket protocol: auth_required, then an auth message that must
carry --token (auth_ok, or auth_invalid and a close), then the commands the bridge sends:

* subscribe_entities: answered with the compressed initial event ("a": added entities) and,
  later, "c" change events ("+": {"s": new state}) and "r" removals. With
  --no-subscribe-entities it answers with an error result instead, which makes a client fall
  back to subscribe_events (state_changed events) and get_states;
* subscribe_events / get_states: the fallback commands.

States are scripted with --state ENTITY=STATE[@SECONDS] (repeatable). Without @ (or @0) the state
exists from the start; otherwise it changes SECONDS after the client authenticated. A state of
"-" removes the entity. --duration closes the connection and stops the server after that many
seconds of an authenticated session (a Home Assistant restart); without it the server runs until
the client goes away. The token is never printed. Uses the same pinned websockets as the bridge.
"""
import argparse
import json
import sys
import threading
import time

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve


def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00", time.gmtime())


def parse_state(text):
    entity, _, rest = text.partition("=")
    state, _, at = rest.rpartition("@") if "@" in rest else (rest, "", "0")
    return entity, state, float(at or 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--token", required=True)
    parser.add_argument("--state", action="append", default=[], type=parse_state)
    parser.add_argument("--no-subscribe-entities", action="store_true")
    parser.add_argument("--duration", type=float, default=0.0)
    args = parser.parse_args()
    done = threading.Event()

    def entity_body(state):
        return {"s": state, "a": {}, "c": "ctx", "lc": time.time(), "lu": time.time()}

    def handler(ws):
        def send(obj):
            ws.send(json.dumps(obj))

        send({"type": "auth_required", "ha_version": "2026.1.0"})
        auth = json.loads(ws.recv(timeout=10))
        if auth.get("type") != "auth" or auth.get("access_token") != args.token:
            print("rejected authentication (bad token)", flush=True)
            send({"type": "auth_invalid", "message": "Invalid access token or password"})
            return
        send({"type": "auth_ok", "ha_version": "2026.1.0"})
        print("authenticated", flush=True)
        start = time.monotonic()
        states = {entity: state for entity, state, at in args.state if at <= 0 and state != "-"}
        later = sorted((at, entity, state) for entity, state, at in args.state if at > 0)
        mode = None          # "entities" or "events" once a subscription is accepted
        sub_id = None
        wanted = set()
        try:
            while not (args.duration and time.monotonic() - start >= args.duration):
                elapsed = time.monotonic() - start
                while later and later[0][0] <= elapsed:
                    _, entity, state = later.pop(0)
                    removed = state == "-"
                    if removed:
                        states.pop(entity, None)
                    else:
                        states[entity] = state
                    print(f"state change: {entity} -> {'(removed)' if removed else state}", flush=True)
                    if mode == "entities" and entity in wanted:
                        event = {"r": [entity]} if removed else {"c": {entity: {"+": {"s": state, "lc": time.time()}}}}
                        send({"id": sub_id, "type": "event", "event": event})
                    elif mode == "events":
                        new = None if removed else {"entity_id": entity, "state": state, "attributes": {}}
                        send({"id": sub_id, "type": "event", "event": {
                            "event_type": "state_changed", "time_fired": stamp(),
                            "data": {"entity_id": entity, "old_state": None, "new_state": new}}})
                try:
                    frame = ws.recv(timeout=0.2)
                except TimeoutError:
                    continue
                msg = json.loads(frame)
                kind, ident = msg.get("type"), msg.get("id")
                if kind == "subscribe_entities":
                    if args.no_subscribe_entities:
                        print("command: subscribe_entities (refused)", flush=True)
                        send({"id": ident, "type": "result", "success": False,
                              "error": {"code": "unknown_command", "message": "Unknown command."}})
                        continue
                    print("command: subscribe_entities", flush=True)
                    mode, sub_id = "entities", ident
                    wanted = set(msg.get("entity_ids") or [])
                    send({"id": ident, "type": "result", "success": True, "result": None})
                    send({"id": ident, "type": "event", "event": {
                        "a": {entity: entity_body(state) for entity, state in states.items() if entity in wanted}}})
                elif kind == "subscribe_events":
                    print(f"command: subscribe_events ({msg.get('event_type')})", flush=True)
                    mode, sub_id = "events", ident
                    send({"id": ident, "type": "result", "success": True, "result": None})
                elif kind == "get_states":
                    print("command: get_states", flush=True)
                    send({"id": ident, "type": "result", "success": True, "result": [
                        {"entity_id": entity, "state": state, "attributes": {}, "last_changed": stamp()}
                        for entity, state in states.items()]})
                else:
                    send({"id": ident, "type": "result", "success": False,
                          "error": {"code": "unknown_command", "message": "Unknown command."}})
            print("closing the connection mid-stream", flush=True)
        except (ConnectionClosed, TimeoutError):
            pass
        finally:
            if args.duration:
                done.set()

    with serve(handler, "0.0.0.0", args.port, compression=None) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        if args.duration:
            done.wait()
            time.sleep(0.3)
        else:
            threading.Event().wait()
    print("fake Home Assistant stopping", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
