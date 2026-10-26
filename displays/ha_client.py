"""Home Assistant client thread for the displays bridge (stdlib only; the websocket is injected).

``HaRunner`` is the connection loop (connect, authenticate, subscribe, read, reconnect with a
jittered backoff) and ``HaThread`` runs it on a thread that survives any crash. Neither ever
touches the bridge's engines: they only put small tuples on a queue that the bridge's main loop
drains.

Queue items: ``("connected",)``, ``("disconnected",)``, ``("disabled",)`` (Home Assistant
rejected the token) and ``("update", entity_id, state_or_None)``.

The access token is only ever handed to ``HaSession`` (which puts it in the one auth frame) and
is never logged here; exception text is logged through the bridge's redacting filter.
"""
from __future__ import annotations

import json
import logging
import queue
import random
import threading
import time

import ha_protocol

LOG = logging.getLogger("vesselstack.displays.bridge.ha")

BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 30.0
STABLE_CONNECTION_S = 10.0
DISABLED_RETRY_S = 300.0
HANDSHAKE_TIMEOUT_S = 30.0
RECV_TIMEOUT_S = 1.0


class HaRunner:
    """connect: () -> source with send(text), recv(timeout) -> str|bytes|None, close()."""

    def __init__(self, token, entity_ids, out, connect, clock=time.monotonic, wait=None, rng=random.random,
                 disabled_retry_s=DISABLED_RETRY_S):
        self._token, self._entities = token, list(entity_ids)
        self.out = out
        self._connect = connect
        self._clock = clock
        self._wait = wait or (lambda seconds, stop: stop.wait(seconds))
        self._rng = rng
        self._disabled_retry_s = disabled_retry_s
        self._announced = "disconnected"
        self._disabled_logged = False

    def _announce(self, state: str) -> None:
        if state != self._announced:
            self._announced = state
            self.out.put((state,))

    def _jittered(self, delay: float) -> float:
        return min(BACKOFF_MAX_S, delay * (0.8 + 0.4 * self._rng()))

    def run(self, stop) -> None:
        delay = BACKOFF_START_S
        while not stop.is_set():
            started = self._clock()
            outcome = self._session(stop)
            if stop.is_set():
                break
            if outcome == "auth_invalid":
                self._announce("disabled")
                if not self._disabled_logged:
                    self._disabled_logged = True
                    LOG.error("Home Assistant rejected the access token (auth_invalid); Home Assistant items are "
                              "disabled and shown as --; check DISPLAYS_HA_TOKEN, retrying every %d s",
                              int(self._disabled_retry_s))
                self._wait(self._disabled_retry_s, stop)
                delay = BACKOFF_START_S
                continue
            self._announce("disconnected")
            if self._clock() - started >= STABLE_CONNECTION_S:
                delay = BACKOFF_START_S
            self._wait(self._jittered(delay), stop)
            delay = min(delay * 2, BACKOFF_MAX_S)

    def _session(self, stop) -> str:
        """One connection: returns "auth_invalid" or "closed" (any other end of the connection)."""
        source = None
        try:
            source = self._connect()
            return self._read(source, stop)
        except Exception as exc:  # a failed connect, a dropped socket, a bad frame: reconnect
            LOG.warning("Home Assistant connection ended: %s: %s", type(exc).__name__, exc)
            return "closed"
        finally:
            if source is not None:
                try:
                    source.close()
                except Exception:  # closing a dead socket is best effort
                    pass

    def _read(self, source, stop) -> str:
        session = ha_protocol.HaSession(self._token, self._entities)
        deadline = self._clock() + HANDSHAKE_TIMEOUT_S
        ready = False
        while not stop.is_set():
            message = source.recv(RECV_TIMEOUT_S)
            if message is None:
                if not ready and self._clock() > deadline:
                    raise TimeoutError("Home Assistant did not finish the handshake in time")
                continue
            frames, updates, events = session.feed(message)
            for frame in frames:
                source.send(json.dumps(frame, separators=(",", ":")))
            for event in events:
                if event[0] == "auth_invalid":
                    return "auth_invalid"
                if event[0] == "failed":
                    raise RuntimeError("Home Assistant refused the state subscription")
                if event[0] == "fallback":
                    LOG.info("Home Assistant has no subscribe_entities; using get_states + subscribe_events")
                if event[0] == "ready":
                    ready = True
                    self._announce("connected")
                    LOG.info("Home Assistant connected (%s)", event[1])
            for entity_id, state in updates:
                self.out.put(("update", entity_id, state))
        return "closed"


class HaThread:
    """Runs a HaRunner on a daemon thread and restarts it, with a backoff, if it ever crashes."""

    def __init__(self, runner: HaRunner, wait=None):
        self.runner = runner
        self.queue = runner.out
        self._stop = threading.Event()
        self._wait = wait or (lambda seconds, stop: stop.wait(seconds))
        self._thread = threading.Thread(target=self._main, name="displays-ha", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _main(self) -> None:
        delay = BACKOFF_START_S
        while not self._stop.is_set():
            try:
                self.runner.run(self._stop)
                return
            except BaseException as exc:  # nothing here may ever take the SignalK bridge down
                LOG.error("Home Assistant client crashed: %s: %s; restarting", type(exc).__name__, exc)
                self.queue.put(("disconnected",))
                self._wait(delay, self._stop)
                delay = min(delay * 2, BACKOFF_MAX_S)


def make_queue() -> "queue.Queue":
    return queue.Queue()
