"""Home Assistant client thread for the displays bridge (stdlib only; the websocket is injected).

``HaRunner`` is the connection loop (connect, authenticate, subscribe, read, reconnect with a
jittered backoff) and ``HaThread`` runs it on a thread that survives any crash. Neither ever
touches the bridge's engines: they only put small tuples on a queue that the bridge's main loop
drains.

Items put on the mailbox: ``("connected",)``, ``("disconnected",)``, ``("disabled",)`` (Home
Assistant rejected the token) and ``("update", entity_id, state_or_None)``. The mailbox is bounded:
it keeps only the latest state per entity between control events (see ``HaMailbox``), so a
flapping sensor or a hostile frame cannot grow it.

The access token is only ever handed to ``HaSession`` (which puts it in the one auth frame) and
is never logged here; exceptions are logged by class name and numeric close code / errno only (never their text,
which can carry server-controlled data).
"""
from __future__ import annotations

import json
import logging
import queue
import collections
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


MAX_CONTROL_EVENTS = 32


class HaMailbox:
    """Bounded hand-over from the client thread to the bridge's main loop (queue.Queue-like).

    ``put(("update", entity_id, state))`` replaces the pending state of that entity, so memory is
    bounded by the number of configured entities per segment, whatever Home Assistant sends or
    however fast a sensor flaps. A control event (``connected`` / ``disconnected`` / ``disabled``)
    closes the segment, so order between states and connection changes is kept; at most
    ``MAX_CONTROL_EVENTS`` segments are kept (the oldest are dropped, the latest state always
    survives). ``get_nowait`` pops in order and raises ``queue.Empty``. Thread-safe.
    """

    def __init__(self, max_controls: int = MAX_CONTROL_EVENTS):
        self._lock = threading.Lock()
        self._items: collections.deque = collections.deque()
        self._latest: dict = {}         # entity_id -> its pending entry in the current segment
        self._controls = 0
        self._max_controls = max_controls

    def put(self, item) -> None:
        with self._lock:
            if isinstance(item, tuple) and len(item) == 3 and item[0] == "update" and isinstance(item[1], str):
                entry = self._latest.get(item[1])
                if entry is not None:
                    entry[2] = item[2]
                else:
                    entry = ["update", item[1], item[2]]
                    self._latest[item[1]] = entry
                    self._items.append(entry)
                return
            self._latest = {}
            self._items.append(item)
            self._controls += 1
            while self._controls > self._max_controls:   # drop the oldest segment and its closing event
                while self._items:
                    old = self._items.popleft()
                    if not (isinstance(old, list) and old[:1] == ["update"]):
                        break
                    if self._latest.get(old[1]) is old:
                        del self._latest[old[1]]
                self._controls -= 1

    def get_nowait(self):
        with self._lock:
            if not self._items:
                raise queue.Empty
            item = self._items.popleft()
            if isinstance(item, list) and item[:1] == ["update"]:
                if self._latest.get(item[1]) is item:
                    del self._latest[item[1]]
                return tuple(item)
            self._controls -= 1
            return item

    def empty(self) -> bool:
        with self._lock:
            return not self._items

    def qsize(self) -> int:
        with self._lock:
            return len(self._items)


def describe(exc: BaseException) -> str:
    """Class name plus a numeric close code / errno: never exception text (it may carry server data)."""
    out = type(exc).__name__
    for attr in ("code", "errno"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            out += f" {attr}={value}"
    return out


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
            LOG.warning("Home Assistant connection ended: %s", describe(exc))
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
            if not ready and self._clock() > deadline:      # every pass: junk frames must not postpone it
                raise TimeoutError("Home Assistant did not finish the handshake in time")
            message = source.recv(RECV_TIMEOUT_S)
            if message is None:
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
                LOG.error("Home Assistant client crashed: %s; restarting", describe(exc))
                try:     # goes through the runner so a later reconnect announces "connected" again
                    self.runner._announce("disconnected")
                except Exception:
                    LOG.error("could not announce the Home Assistant disconnect")
                self._wait(delay, self._stop)
                delay = min(delay * 2, BACKOFF_MAX_S)


def make_queue() -> HaMailbox:
    return HaMailbox()
