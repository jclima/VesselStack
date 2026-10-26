#!/usr/bin/env python3
"""Fake SignalK websocket server for tests/test-displays-bridge.sh.

Sends a hello, waits for a subscribe frame, then streams navigation.speedOverGround
deltas for --duration seconds and exits (closing the server, like a SignalK
restart). When the client subscribed to NOTIFICATION_PATH it also raises an alarm
notification after --alarm-at seconds and clears it (value null) after --clear-at seconds. With --token, connections without "Authorization: Bearer <token>"
are rejected with 401. Uses the same pinned websockets as the bridge.
"""
import argparse
import json
import sys
import threading
import time

from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response
from websockets.sync.server import serve

SELF = "vessels.urn:mrn:signalk:uuid:00000000-0000-4000-8000-000000000001"
NOTIFICATION_PATH = "notifications.navigation.anchor"
SOG_MS = 10 * 1852 / 3600  # exactly 10.0 kn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--duration", type=float, default=6.0)
    parser.add_argument("--token", default="")
    parser.add_argument("--alarm-at", type=float, default=3.0)
    parser.add_argument("--clear-at", type=float, default=8.0)
    args = parser.parse_args()
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
