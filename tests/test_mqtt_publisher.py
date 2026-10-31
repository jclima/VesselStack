"""Deterministic publisher regressions against the pinned Paho API.

The bridge owns all calls into Paho from one worker.  These tests deliberately
drive that worker with callback-shaped events from a fake transport, while the
transport is backed by the real paho-mqtt package in the integration image.
They do not use Bridge's older fake publisher.
"""
import importlib
import os
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "displays"))
bridge = importlib.import_module("bridge")

try:
    paho = importlib.import_module("paho.mqtt.client")
    paho_package = importlib.import_module("paho.mqtt")
except ImportError:  # The host unit environment intentionally has no extras.
    paho = None
    paho_package = None


@unittest.skipUnless(paho is not None, "paho-mqtt is installed by the displays integration environment")
class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.settings = bridge.load_settings({
            "SIGNALK_URL": "http://127.0.0.1:3000", "DISPLAYS_CONFIG": "/x",
            "DISPLAYS_BRIDGE_MQTT_USERNAME": "u", "DISPLAYS_BRIDGE_MQTT_PASSWORD": "p",
        })
        self.publisher = bridge.MqttPublisher(self.settings)
        self.wire = []
        self.acks = {}
        self.progress = threading.Condition()
        self.client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id="publisher-test")
        self.client._state = paho._ConnectionState.MQTT_CS_CONNECTED
        self.client._sock = type("Socket", (), {"close": lambda _self: None})()
        original_publish = self.client.publish

        def publish(topic, payload, qos=0, retain=False):
            result = original_publish(topic, payload, qos, retain)
            mid = result.mid
            with self.progress:
                self.wire.append((topic, payload, qos, retain))
                self.progress.notify_all()
            self.acks[mid] = topic
            return result

        self.client.publish = publish
        self.publisher._client = self.client
        self.publisher._worker = threading.Thread(target=self.publisher._run, daemon=True)
        self.publisher._worker.start()

    def tearDown(self):
        self.publisher.stop()

    def connect(self):
        ready = threading.Event()
        self.client.on_connect = lambda *_args: self.publisher._event("connect", ready.set)
        self.client._in_packet = {"command": 0x10, "packet": b"\x00\x00",
                                  "remaining_length": 2}
        self.client._handle_connack()
        self.assertTrue(ready.wait(1), "publisher worker did not process CONNACK")

    def wait_for(self, predicate):
        with self.progress:
            self.assertTrue(self.progress.wait_for(predicate, timeout=1))

    def ack(self, mid):
        self.publisher._event("ack", mid)

    def test_real_paho_client_and_callback_surface(self):
        self.assertEqual(getattr(paho_package, "__version__", None), "2.1.0")
        self.assertTrue(hasattr(self.client, "_handle_connack"))
        self.assertTrue(callable(self.client.publish))

    def test_real_connack_transport_keeps_latest_signalk_state(self):
        sent = []
        ready = {"tcp": False}
        def transmit(mid, topic, payload=None, qos=0, retain=False, dup=False,
                     info=None, properties=None):
            if not ready["tcp"]:
                return paho.MQTT_ERR_NO_CONN
            sent.append((topic.decode(), payload.decode() if payload else "", qos, retain))
            return paho.MQTT_ERR_SUCCESS
        self.client._send_publish = transmit
        self.client.loop_write = lambda *args, **kwargs: paho.MQTT_ERR_SUCCESS
        self.publisher.publish("vesselstack/v1/bridge/signalk", "disconnected", True, qos=1)
        ready["tcp"] = True
        self.publisher.publish("vesselstack/v1/bridge/signalk", "connected", True, qos=1)
        callback_done = threading.Event()
        def on_connect_callback():
            self.publisher.publish("vesselstack/v1/bridge/signalk", "connected", True, qos=1)
            callback_done.set()
        self.client.on_connect = lambda *_args: self.publisher._event("connect", on_connect_callback)
        self.client._in_packet = {"command": 0x10, "packet": b"\x00\x00", "remaining_length": 2}
        self.client._handle_connack()
        self.assertTrue(callback_done.wait(1))
        self.wait_for(lambda: "vesselstack/v1/bridge/signalk" in self.publisher._outstanding)
        self.client.loop_write()
        self.assertEqual([x[1] for x in sent if x[0].endswith("/signalk")][-1], "connected")

    def test_queued_before_tcp_replays_after_connack_in_order(self):
        self.publisher.publish("bridge/status", "connected", True, qos=1)
        self.publisher.publish("bridge/signalk", "disconnected", True, qos=1)
        self.assertEqual(self.wire, [])
        self.connect()
        self.wait_for(lambda: len(self.wire) == 2)
        self.ack(1)
        self.ack(2)
        self.assertEqual([x[1] for x in self.wire], ["connected", "disconnected"])

    def test_update_between_tcp_and_connack_is_latest_and_retained(self):
        with self.publisher._condition:
            self.publisher.publish("state", "before", True, qos=1)
            self.publisher.publish("state", "after", True, qos=1)
        self.connect()
        self.wait_for(lambda: len(self.wire) == 1)
        self.assertEqual(self.wire[0], ("state", "after", 1, True))

    def test_publish_after_connected_wakes_worker(self):
        self.connect()
        self.publisher.publish("state", "online", True, qos=1)
        self.wait_for(lambda: len(self.wire) == 1)
        self.assertEqual(self.wire[0], ("state", "online", 1, True))

    def test_identical_retained_heartbeat_is_sent_again_after_ack(self):
        self.connect()
        self.publisher.publish("heartbeat", "online", True, qos=1)
        self.wait_for(lambda: len(self.wire) == 1)
        self.ack(1)
        self.publisher.publish("heartbeat", "online", True, qos=1)
        self.wait_for(lambda: len(self.wire) == 2)
        self.assertEqual(self.wire[0], self.wire[1])

    def test_qos0_is_queued_while_disconnected_and_not_rejected(self):
        self.publisher.publish("event", "payload", False, qos=0)
        self.assertEqual(self.wire, [])
        self.connect()
        self.wait_for(lambda: len(self.wire) == 1)
        self.assertEqual(self.wire[0], ("event", "payload", 0, False))

    def test_rejected_paho_publish_is_not_recorded_as_outstanding(self):
        rejected = paho.MQTTMessageInfo(77)
        rejected.rc = paho.MQTT_ERR_QUEUE_SIZE
        attempted = threading.Event()
        def reject(*_args, **_kwargs):
            attempted.set()
            return rejected
        self.client.publish = reject
        self.connect()
        self.publisher.publish("state", "online", True, qos=1)
        self.assertTrue(attempted.wait(1))
        self.assertNotIn("state", self.publisher._outstanding)

    def test_connect_callback_after_stop_is_ignored(self):
        called = threading.Event()
        with self.publisher._condition:
            self.publisher._stopping = True
            self.publisher._commands.append(("connect", called.set))
            self.publisher._condition.notify_all()
        self.assertFalse(called.wait(0.2))

    def test_reconnect_waits_for_old_ack_before_new_state(self):
        self.connect()
        self.publisher.publish("state", "old", True, qos=1)
        self.wait_for(lambda: len(self.wire) == 1)
        self.publisher._event("disconnect", None)
        self.publisher._event("connect", lambda: None)
        self.publisher.publish("state", "new", True, qos=1)
        self.publisher._event("ack", 1)
        self.wait_for(lambda: len(self.wire) == 2)
        self.assertEqual([x[1] for x in self.wire], ["old", "new"])

    def test_rapid_updates_coalesce_to_one_outstanding_packet(self):
        self.connect()
        self.publisher.publish("state", "0", True, qos=1)
        self.wait_for(lambda: len(self.wire) == 1)
        for value in range(1, 100):
            self.publisher.publish("state", str(value), True, qos=1)
        self.assertEqual(self.wire[0][1], "0")
        self.ack(1)
        self.wait_for(lambda: len(self.wire) == 2)
        self.assertEqual(self.wire[1][1], "99")
        self.assertLessEqual(len(self.publisher._retained_mailbox), 1)

    def test_ack_callback_can_arrive_before_publish_returns(self):
        original = self.client.publish

        def publish(topic, payload, qos=0, retain=False):
            result = original(topic, payload, qos, retain)
            if qos:
                self.publisher._event("ack", result.mid)
            return result

        self.client.publish = publish
        self.connect()
        self.publisher.publish("state", "online", True, qos=1)
        self.wait_for(lambda: len(self.wire) == 1)
        self.assertEqual(self.publisher._outstanding, {})

    def test_callback_thread_does_not_hold_ingress_lock(self):
        self.connect()
        entered = threading.Event()
        finished = threading.Event()

        def callback():
            entered.set()
            self.publisher.publish("state", "callback", True, qos=1)
            finished.set()

        self.publisher._event("connect", callback)
        self.assertTrue(entered.wait(1))
        self.assertTrue(finished.wait(1))


if __name__ == "__main__":
    unittest.main()
