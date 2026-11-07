#!/usr/bin/env python3
"""Fake SignalK websocket server for tests/test-displays-bridge.sh.

Sends a hello, waits for a subscribe frame, then streams navigation.speedOverGround
deltas for --duration seconds and exits (closing the server, like a SignalK
restart). With --rest it instead serves GET /signalk/v1/api/vessels/self from REST_TREE (see below)
until --duration seconds pass. When the client subscribed to NOTIFICATION_PATH it also raises an alarm
notification after --alarm-at seconds and clears it (value null) after --clear-at seconds. With --token, connections without "Authorization: Bearer <token>"
are rejected with 401. Uses the same pinned websockets as the bridge.
"""
import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SELF = "vessels.urn:mrn:signalk:uuid:00000000-0000-4000-8000-000000000001"
NOTIFICATION_PATH = "notifications.navigation.anchor"
SOG_MS = 10 * 1852 / 3600  # exactly 10.0 kn
REST_PATH = "/signalk/v1/api/vessels/self"
REST_TOKEN = "fake-rest-token"

# A vessels/self tree for the catalog tests. Everything private is an obviously fake value: the tests
# prove that none of it (MMSI, name, UUID, call sign, positions, destination) ever leaves fetch_live.
FAKE_MMSI = "000000000"
FAKE_NAME = "FAKE VESSEL NAME"
FAKE_CALLSIGN = "FAKE0"
FAKE_UUID = "urn:mrn:signalk:uuid:00000000-0000-4000-8000-000000000001"
FAKE_DESTINATION = "FAKE HARBOUR"


def _leaf(value, units=None, stamp="2026-01-01T00:00:00.000Z"):
    leaf = {"value": value, "$source": "fake.0", "timestamp": stamp}
    if units:
        leaf["meta"] = {"units": units}
    return leaf


REST_TREE = {
    "uuid": FAKE_UUID,
    "mmsi": FAKE_MMSI,
    "name": FAKE_NAME,
    "communication": {"callsignVhf": FAKE_CALLSIGN},
    "navigation": {
        "speedOverGround": _leaf(3.0, "m/s"),
        "courseOverGroundTrue": _leaf(1.0, "rad"),
        "position": _leaf({"latitude": 0.0, "longitude": 0.0}),
        "destination": {"commonName": _leaf(FAKE_DESTINATION), "eta": _leaf("2026-01-02T00:00:00Z")},
        "anchor": {"position": _leaf({"latitude": 0.0, "longitude": 0.0}), "maxRadius": _leaf(30, "m")},
        "courseRhumbline": {"nextPoint": {"position": _leaf({"latitude": 0.0, "longitude": 0.0}),
                                          "bearingTrue": _leaf(2.0, "rad")}},
        "state": _leaf("motoring"),
        "trip": {"log": _leaf(1852, "m")},
    },
    "environment": {
        "depth": {"belowTransducer": _leaf(3.5, "m")},
        "wind": {"angleApparent": _leaf(0.5, "rad"), "speedApparent": _leaf(6.0, "m/s")},
        "water": {"temperature": _leaf(290.0, "K")},
        "outside": {"humidity": _leaf(0.55, "ratio")},
    },
    "electrical": {
        "batteries": {
            "house": {"voltage": _leaf(12.6, "V"), "current": _leaf(-3.0, "A"),
                      "capacity": {"stateOfCharge": _leaf(0.8, "ratio")}},
            "start": {"voltage": _leaf(12.9, "V")},
        },
        "inverter": {"acout": {"power": _leaf(150, "W")}},
    },
    "tanks": {"fuel": {"0": {"currentLevel": _leaf(0.4, "ratio")}},
              "freshWater": {"main": {"currentLevel": _leaf(0.7, "ratio")}}},
    "propulsion": {"port": {"revolutions": _leaf(20.0, "Hz")},
                   "main": {"revolutions": _leaf(25.0, "Hz"), "temperature": _leaf(350.0, "K"),
                            "alternatorVoltage": _leaf(13.8, "V")}},
    "sensors": {"weird": {"mystery": _leaf(7.5, "kg/m3"), "flag": _leaf(True), "nan": _leaf(float("nan")),
                          "text": _leaf("7")}},
    "notifications": {"navigation": {"anchor": _leaf({"state": "alarm", "message": FAKE_NAME + " dragging"})},
                      "electrical": {"batteries": {"house": {"voltage": _leaf({"state": "warn", "message": "low"})}}},
                      "mob": _leaf(None)},
}


def serve_rest(port: int, token: str = "", tree=None, redirect: bool = False, big: int = 0):
    """A stdlib HTTP server for REST_TREE; returns (server, thread). ``token`` demands a Bearer header.

    ``redirect`` answers 302 to another host (to prove redirects are refused); ``big`` pads the body
    with that many bytes (to prove the size cap). Port 0 picks a free one: read server.server_port.
    """
    document = tree if tree is not None else REST_TREE

    class Handler(BaseHTTPRequestHandler):
        seen = []

        def do_GET(self):   # noqa: N802
            Handler.seen.append((self.path, self.headers.get("Authorization")))
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                self.send_response(401)
                self.end_headers()
                return
            if redirect:
                self.send_response(302)
                self.send_header("Location", "http://192.0.2.1:3000" + REST_PATH)
                self.end_headers()
                return
            if self.path != REST_PATH:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(document, allow_nan=True).encode()
            if big:
                body += b" " * big
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):   # the client gave up (size cap test)
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    server.handler_class = Handler
    return server, thread


def main():
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import serve

    parser = argparse.ArgumentParser()
    parser.add_argument("--rest", action="store_true", help="serve the REST tree instead of the websocket")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--duration", type=float, default=6.0)
    parser.add_argument("--token", default="")
    parser.add_argument("--alarm-at", type=float, default=3.0)
    parser.add_argument("--clear-at", type=float, default=8.0)
    args = parser.parse_args()
    if args.rest:
        server, _thread = serve_rest(args.port, args.token)
        time.sleep(args.duration)
        server.shutdown()
        return 0
    done = threading.Event()
    server = None

    def authorize(connection, request):
        if args.token and request.headers.get("Authorization") != f"Bearer {args.token}":
            print("rejected connection without the expected bearer token", flush=True)
            return connection.respond(401, "Unauthorized\n")
        return None

    def handler(ws):
        ws.send(json.dumps({"name": "fake-signalk", "version": "1.0.0", "self": SELF,
                            "roles": ["main"]}))
        subscribed = json.loads(ws.recv(timeout=10))
        assert subscribed["context"] == "vessels.self", subscribed
        print("subscribed:", json.dumps(subscribed), flush=True)
        wants_notification = any(item.get("path") == NOTIFICATION_PATH
                                 for item in subscribed.get("subscribe", []))
        start = time.monotonic()
        end = start + args.duration
        sent_alarm = sent_clear = False

        def notify(value):
            ws.send(json.dumps({
                "context": SELF,
                "updates": [{"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                             "values": [{"path": NOTIFICATION_PATH, "value": value}]}]}))

        try:
            while time.monotonic() < end:
                elapsed = time.monotonic() - start
                if wants_notification and not sent_alarm and elapsed >= args.alarm_at:
                    notify({"state": "alarm", "method": ["visual", "sound"], "message": "Anchor alarm"})
                    sent_alarm = True
                if wants_notification and sent_alarm and not sent_clear and elapsed >= args.clear_at:
                    notify(None)
                    sent_clear = True
                ws.send(json.dumps({
                    "context": SELF,
                    "updates": [{"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                                 "values": [{"path": "navigation.speedOverGround",
                                             "value": SOG_MS}]}]}))
                time.sleep(0.5)
        except ConnectionClosed:
            pass
        done.set()

    with serve(handler, "0.0.0.0", args.port, process_request=authorize,
               compression=None) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        done.wait()
    print("fake SignalK stopping", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
