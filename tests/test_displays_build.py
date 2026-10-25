#!/usr/bin/env python3
"""Tests for the optional on-Pi firmware build and flash (displays/firmware.py).

Docker is never run: the capture runner and the streaming runner are fakes.
"""

from __future__ import annotations

import datetime
import json
import os
import stat
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from subprocess import CompletedProcess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))
sys.path.insert(0, str(ROOT / "tests"))

import displayctl  # noqa: E402
import firmware  # noqa: E402
import lifecycle  # noqa: E402
import render  # noqa: E402
from test_displays_render import Base, MQTT_PW  # noqa: E402

IMAGE = render.pinned_image()
Vfs = namedtuple("Vfs", "f_bavail f_frsize")
GIB = 1024 ** 3
FACTORY_BYTES = b"FACTORY-IMAGE\x00" * 100
OTA_BYTES = b"OTA-IMAGE\x01" * 100
NOW = datetime.datetime(2026, 10, 24, 12, 30, 5, tzinfo=datetime.timezone.utc)


def vfs_free(gib):
    return lambda path: Vfs(int(gib * GIB) // 4096, 4096)


class FakeDocker:
    """Capture runner (inspect/info) plus streaming runner (run) in one object."""

    def __init__(self, image_present=True, volume_present=True, root_dir="/var/lib/docker",
                 compile_status=0, produce=True, factory=FACTORY_BYTES, ota=OTA_BYTES):
        self.image_present = image_present
        self.volume_present = volume_present
        self.root_dir = root_dir
        self.compile_status = compile_status
        self.produce = produce
        self.factory = factory
        self.ota = ota
        self.captured: list = []
        self.streamed: list = []
        self.upload_status = 0
        self.uploaded: list = []  # bytes of the file mounted at /fw.bin, read during the call
        self.staged_paths: list = []

    def capture(self, argv):
        self.captured.append(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            return CompletedProcess(argv, 0 if self.image_present else 1, "", "")
        if argv[:3] == ["docker", "volume", "inspect"]:
            return CompletedProcess(argv, 0 if self.volume_present else 1, "", "")
        if argv[:2] == ["docker", "info"]:
            return CompletedProcess(argv, 0 if self.root_dir else 1, (self.root_dir or "") + "\n", "")
        raise AssertionError(f"unexpected capture command {argv}")

    def stream(self, argv):
        self.streamed.append(argv)
        if "compile" in argv:
            if self.compile_status == 0 and self.produce:
                mount = next(a for a in argv if a.endswith(":/config")).rsplit(":", 1)[0]
                out = Path(mount) / ".esphome" / "build" / "vs-helm" / "build"
                out.mkdir(parents=True)
                (out / "firmware.factory.bin").write_bytes(self.factory)
                (out / "firmware.ota.bin").write_bytes(self.ota)
                (out / "vs-helm.bin").write_bytes(self.ota)
            return self.compile_status
        staged = next(a for a in argv if a.endswith(":/fw.bin:ro")).rsplit(":/fw.bin", 1)[0]
        self.staged_paths.append(staged)
        self.staged_modes = (stat.S_IMODE(os.stat(Path(staged).parent).st_mode),
                             stat.S_IMODE(os.stat(staged).st_mode))
        self.uploaded.append(Path(staged).read_bytes())
        return self.upload_status


class FirmwareBase(Base):
    def setUp(self):
        super().setUp()
        self.docker = FakeDocker()
        self.clock = iter(range(1000, 100000, 7))

    def ctx(self, **kw):
        kw.setdefault("runner", self.docker.capture)
        return super().ctx(**kw)

    def build(self, display="helm", free=50, **kw):
        return firmware.build(self.ctx(), display, stream=self.docker.stream,
                              statvfs=vfs_free(free), monotonic=lambda: next(self.clock),
                              now=lambda: NOW, warn=self.messages.append, **kw)

    def flash(self, display="helm", **kw):
        return firmware.flash(self.ctx(), display, stream=self.docker.stream, **kw)

    def project(self, display="helm"):
        return self.root / "build" / "displays" / display

    def project_files(self, display="helm"):
        """The ESPHome project directory (the only one mounted into a container)."""
        return self.project(display) / "project"

    def fake_serial(self, name="ttyACM0", by_id=None):
        """Replace lstat/realpath with a tiny virtual /dev."""
        nodes = {f"/dev/{name}": stat.S_IFCHR | 0o660}
        links = {}
        if by_id:
            links[f"/dev/serial/by-id/{by_id}"] = f"/dev/{name}"
        return nodes, links

    def seam(self, nodes, links):
        def lstat(path):
            if path in links:
                return os.stat_result((stat.S_IFLNK | 0o777,) + (0,) * 9)
            if path in nodes:
                return os.stat_result((nodes[path],) + (0,) * 9)
            raise FileNotFoundError(2, "No such file or directory", path)
        return {"lstat": lstat, "realpath": lambda p: links.get(p, p)}

    def mode(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)


class BuildTests(FirmwareBase):
    def test_build_runs_exactly_one_compile_container_without_secrets(self):
        self.build()
        self.assertEqual(len(self.docker.streamed), 1)
        argv = self.docker.streamed[0]
        project = str(self.project_files())
        self.assertEqual(argv[:4], ["docker", "run", "--rm", "--pull"])
        self.assertEqual(argv[4], "missing")
        self.assertIn(f"{project}:/config", argv)
        self.assertIn("vesselstack-esphome-cache:/cache", argv)
        self.assertEqual(argv[-3:], [IMAGE, "compile", "/config/helm.yaml"][-3:])
        self.assertEqual(argv[argv.index(IMAGE) + 1:], ["compile", "/config/helm.yaml"])
        self.assertIn("--cap-drop", argv)
        self.assertNotIn("--privileged", argv)
        self.assertNotIn("--device", argv)
        joined = " ".join(argv)
        for secret in (MQTT_PW, "wifi-pass-123", "boat-wifi"):
            self.assertNotIn(secret, joined)
        secrets = json.loads(self.secrets_path.read_text())
        self.assertNotIn(secrets["displays"]["helm"]["ota_password"], joined)

    def test_build_renders_first_and_requires_wifi(self):
        self.set_config(None, wifi=None)
        with self.assertRaises(render.RenderError) as caught:
            self.build()
        self.assertIn("Wi-Fi", str(caught.exception))
        self.assertEqual(self.docker.streamed, [])
        self.assertFalse(self.project().exists())

    def test_unknown_display_is_an_error(self):
        with self.assertRaises((render.RenderError, lifecycle.LifecycleError)):
            self.build("nope")
        self.assertEqual(self.docker.streamed, [])

    def test_artifacts_copied_with_modes_and_manifest(self):
        path = self.build()
        self.assertEqual(path, self.project() / "firmware")
        self.assertEqual(self.mode(path), 0o700)
        self.assertEqual(sorted(p.name for p in path.iterdir()),
                         ["firmware.factory.bin", "firmware.ota.bin", "manifest.json"])
        for child in path.iterdir():
            self.assertEqual(self.mode(child), 0o600, child.name)
        self.assertEqual((path / "firmware.factory.bin").read_bytes(), FACTORY_BYTES)
        manifest = json.loads((path / "manifest.json").read_text())
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["display"], "helm")
        self.assertEqual(manifest["device_name"], "vs-helm")
        self.assertEqual(manifest["image"], IMAGE)
        self.assertEqual(manifest["image_digest"], IMAGE.split("@")[1])
        self.assertEqual(manifest["esphome_version"], render.esphome_version())
        self.assertEqual(manifest["built_at_utc"], "2026-10-24T12:30:05Z")
        self.assertRegex(manifest["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(manifest["vesselstack_version"],
                         (ROOT / "VERSION").read_text().strip())
        import hashlib
        self.assertEqual(manifest["files"]["firmware.factory.bin"],
                         {"sha256": hashlib.sha256(FACTORY_BYTES).hexdigest(),
                          "size": len(FACTORY_BYTES)})
        self.assertEqual(manifest["files"]["firmware.ota.bin"]["sha256"],
                         hashlib.sha256(OTA_BYTES).hexdigest())
        # secrets never enter the manifest
        self.assertNotIn(MQTT_PW, (path / "manifest.json").read_text())
        # the scratch tree is gone
        self.assertFalse((self.project() / ".esphome").exists())

    def test_source_hash_ignores_secrets_but_follows_the_config(self):
        first = firmware.source_hash(self.collect().files, "helm")
        self.assertEqual(first, firmware.source_hash(self.collect().files, "helm"))
        self.set_config(None, wifi={"ssid": "other", "password": "other-pass-1"})
        self.assertEqual(first, firmware.source_hash(self.collect().files, "helm"))

        def mutate(cfg):
            cfg["instruments"]["sog"]["label"] = "Speed2"
        self.set_config(mutate)
        self.assertNotEqual(first, firmware.source_hash(self.collect().files, "helm"))

    def test_previous_build_rotated_keeping_exactly_one(self):
        self.build()
        first = (self.project() / "firmware" / "manifest.json").read_bytes()
        self.docker.factory = b"SECOND" * 50
        self.build()
        self.assertEqual((self.project() / "firmware.prev" / "manifest.json").read_bytes(), first)
        self.assertEqual((self.project() / "firmware" / "firmware.factory.bin").read_bytes(),
                         b"SECOND" * 50)
        second = (self.project() / "firmware" / "manifest.json").read_bytes()
        self.docker.factory = b"THIRD" * 50
        self.build()
        self.assertEqual((self.project() / "firmware.prev" / "manifest.json").read_bytes(), second)
        self.assertFalse((self.project() / "firmware.new").exists())
        self.assertEqual(self.mode(self.project() / "firmware.prev"), 0o700)

    def test_render_keeps_finished_firmware(self):
        self.build()
        build_manifest = (self.project() / "firmware" / "manifest.json").read_bytes()
        render.render(self.ctx(), "helm", warn=lambda text: None)
        render.render(self.ctx(), "helm", warn=lambda text: None)
        self.assertEqual((self.project() / "firmware" / "manifest.json").read_bytes(), build_manifest)
        self.assertEqual((self.project() / "firmware" / "firmware.factory.bin").read_bytes(),
                         FACTORY_BYTES)

    def test_failed_compile_keeps_existing_firmware(self):
        self.build()
        before = (self.project() / "firmware" / "manifest.json").read_bytes()
        self.docker.compile_status = 2
        with self.assertRaises(render.RenderError) as caught:
            self.build()
        self.assertIn("exit status 2", str(caught.exception))
        self.assertEqual((self.project() / "firmware" / "manifest.json").read_bytes(), before)
        self.assertFalse((self.project() / "firmware.prev").exists())

    def test_missing_artifact_is_an_error_and_stores_nothing(self):
        self.docker.produce = False
        with self.assertRaises(render.RenderError) as caught:
            self.build()
        self.assertIn("did not produce", str(caught.exception))
        self.assertFalse((self.project() / "firmware").exists())

    def test_symlinked_artifact_refused(self):
        original = self.docker.stream

        def stream(argv):
            status = original(argv)
            mount = next(a for a in argv if a.endswith(":/config")).rsplit(":", 1)[0]
            target = Path(mount) / ".esphome" / "build" / "vs-helm" / "build" / "firmware.ota.bin"
            target.unlink()
            target.symlink_to("/etc/hostname")
            return status
        self.docker.stream = stream
        with self.assertRaises(render.RenderError):
            self.build()
        self.assertFalse((self.project() / "firmware").exists())

    def test_pull_notice_only_when_image_missing(self):
        self.build()
        self.assertFalse(any("not present locally" in m for m in self.messages if isinstance(m, str)))
        self.messages.clear()
        self.docker.image_present = False
        self.build()
        self.assertTrue(any("not present locally" in m and "pulling" in m for m in self.messages))
        self.assertEqual(self.docker.streamed[-1][:5], ["docker", "run", "--rm", "--pull", "missing"])

    def test_disk_preflight_thresholds(self):
        for volume_present, needed in ((False, 3), (True, 1)):
            self.docker = FakeDocker(volume_present=volume_present)
            with self.assertRaises(render.RenderError) as caught:
                self.build(free=needed - 0.01)
            self.assertIn("not enough free disk space", str(caught.exception))
            self.assertIn(f"{needed} GB needed", str(caught.exception))
            self.assertEqual(self.docker.streamed, [])
            self.build(free=needed)  # exactly enough passes
            self.assertEqual(len(self.docker.streamed), 1)

    def test_disk_check_uses_docker_root_then_fallbacks(self):
        seen = []

        def statvfs(path):
            seen.append(path)
            if path == "/srv/docker":
                raise OSError("unreadable")
            if path == "/var/lib/docker":
                raise OSError("unreadable")
            return Vfs(10 ** 9, 4096)
        self.docker.root_dir = "/srv/docker"
        free, where = firmware.free_bytes(self.ctx(), statvfs)
        self.assertEqual(seen, ["/srv/docker", "/var/lib/docker", str(self.root)])
        self.assertEqual(where, str(self.root))
        self.assertEqual(free, 10 ** 9 * 4096)
        self.assertIn(["docker", "info", "--format", "{{.DockerRootDir}}"], self.docker.captured)

    def test_elapsed_time_is_printed(self):
        self.build()
        self.assertTrue(any("built in 7 s" in m for m in self.messages))

    def test_docker_missing_is_a_clear_error(self):
        def runner(argv):
            raise FileNotFoundError("docker")
        with self.assertRaises(render.RenderError) as caught:
            firmware.build(self.ctx(runner=runner), "helm", stream=self.docker.stream,
                           statvfs=vfs_free(50), warn=lambda t: None)
        self.assertIn("docker is not installed", str(caught.exception))

    def test_build_requires_root(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root")
        with self.assertRaises(lifecycle.LifecycleError):
            firmware.build(self.ctx(require_root=True), "helm", stream=self.docker.stream)

    def test_mount_path_with_colon_refused(self):
        with self.assertRaises(render.RenderError):
            firmware._check_mount_path(Path("/opt/a:b"))

    def test_build_dir_symlink_refused(self):
        self.build()
        build = self.root / "build"
        moved = self.root / "elsewhere"
        build.rename(moved)
        build.symlink_to(moved)
        with self.assertRaises(render.RenderError):
            self.build()


class ServerFlashBase(FirmwareBase):
    def setUp(self):
        super().setUp()
        self.build()
        self.docker.streamed.clear()
        self.docker.captured.clear()
        self.messages.clear()


class FlashSerialTests(ServerFlashBase):
    def test_serial_passes_device_and_stored_factory_image(self):
        nodes, links = self.fake_serial()
        self.flash(serial="/dev/ttyACM0", **self.seam(nodes, links))
        argv = self.docker.streamed[0]
        self.assertEqual(argv[:5], ["docker", "run", "--rm", "--pull", "never"])
        self.assertEqual(argv[argv.index("--device") + 1], "/dev/ttyACM0")
        self.assertNotIn("--network", argv)
        self.assertEqual(argv[argv.index(IMAGE) + 1:],
                         ["upload", "/config/helm.yaml", "--device", "/dev/ttyACM0",
                          "--file", "/fw.bin"])
        self.assertNotIn("compile", argv)
        self.assertIn(f"{self.project_files()}:/config:ro", argv)
        self.assertEqual(self.docker.uploaded, [FACTORY_BYTES])
        self.assertEqual(len(self.docker.streamed), 1)

    def test_by_id_symlink_resolved_to_the_real_device(self):
        nodes, links = self.fake_serial("ttyUSB0", "usb-Espressif_USB_JTAG-if00")
        self.flash(serial="/dev/serial/by-id/usb-Espressif_USB_JTAG-if00", **self.seam(nodes, links))
        argv = self.docker.streamed[0]
        self.assertEqual(argv[argv.index("--device") + 1], "/dev/ttyUSB0")
        self.assertEqual(argv[argv.index("--device", argv.index(IMAGE)) + 1], "/dev/ttyUSB0")

    def test_serial_validation_refusals(self):
        nodes = {"/dev/ttyACM0": stat.S_IFCHR | 0o660, "/dev/sda": stat.S_IFBLK | 0o660,
                 "/dev/null": stat.S_IFCHR | 0o666, "/dev/ttyfile": stat.S_IFREG | 0o644}
        links = {"/dev/ttylink": "/dev/ttyACM0",                    # symlink outside by-id
                 "/dev/serial/by-id/evil": "/dev/sda",              # by-id to a non-tty
                 "/dev/serial/by-id/up": "/dev/../etc/passwd"}      # escapes /dev
        nodes["/dev/serial/by-id/up"] = 0
        seam = self.seam(nodes, links)
        cases = ["ttyACM0", "/tmp/ttyACM0", "relative/dev/ttyACM0", "/dev/../tmp/x",
                 "/dev/ttylink", "/dev/sda", "/dev/null", "/dev/ttyfile", "/dev/ttyMISSING",
                 "/dev/serial/by-id/evil", "/dev/serial/by-id/up", "/dev/serial/by-id/none",
                 "/dev/ttyACM0\n", "/dev//ttyACM0", "/dev/"]
        for case in cases:
            with self.assertRaises(render.RenderError, msg=case) as caught:
                self.flash(serial=case, **seam)
            self.assertEqual(caught.exception.code, 2, case)
        self.assertEqual(self.docker.streamed, [])

    def test_real_dev_null_and_a_symlink_to_tty_are_refused_with_the_real_lstat(self):
        link = Path(self._tmp.name) / "tty-link"
        link.symlink_to("/dev/null")
        for case in ("/dev/null", str(link)):
            with self.assertRaises(render.RenderError):
                self.flash(serial=case)

    def test_serial_refuses_a_modified_firmware(self):
        nodes, links = self.fake_serial()
        target = self.project() / "firmware" / "firmware.factory.bin"
        target.write_bytes(b"tampered" + FACTORY_BYTES[8:])
        with self.assertRaises(render.RenderError) as caught:
            self.flash(serial="/dev/ttyACM0", **self.seam(nodes, links))
        self.assertIn("does not match", str(caught.exception))
        self.assertEqual(self.docker.streamed, [])
        target.write_bytes(FACTORY_BYTES + b"x")  # size differs
        with self.assertRaises(render.RenderError):
            self.flash(serial="/dev/ttyACM0", **self.seam(nodes, links))
        self.assertEqual(self.docker.streamed, [])

    def test_serial_refuses_a_symlinked_image_file(self):
        nodes, links = self.fake_serial()
        target = self.project() / "firmware" / "firmware.factory.bin"
        target.unlink()
        target.symlink_to("/etc/hostname")
        with self.assertRaises(render.RenderError):
            self.flash(serial="/dev/ttyACM0", **self.seam(nodes, links))
        self.assertEqual(self.docker.streamed, [])

    def test_upload_failure_is_reported(self):
        nodes, links = self.fake_serial()
        self.docker.upload_status = 2
        with self.assertRaises(render.RenderError) as caught:
            self.flash(serial="/dev/ttyACM0", **self.seam(nodes, links))
        self.assertIn("exit status 2", str(caught.exception))


class FlashStagingTests(ServerFlashBase):
    def test_upload_reads_a_private_copy_that_is_removed_afterwards(self):
        self.flash(ota="192.0.2.50")
        staged = Path(self.docker.staged_paths[0])
        self.assertEqual(staged.parent.parent, self.root / "build")  # not a shared /tmp
        self.assertEqual(self.docker.staged_modes, (0o700, 0o600))
        self.assertFalse(staged.parent.exists())
        self.assertEqual([p.name for p in (self.root / "build").iterdir()], ["displays"])

    def test_temp_dir_removed_when_the_upload_fails(self):
        self.docker.upload_status = 1
        with self.assertRaises(render.RenderError):
            self.flash(ota="192.0.2.50")
        self.assertEqual([p.name for p in (self.root / "build").iterdir()], ["displays"])

    def test_stored_bytes_that_change_after_verification_are_not_what_is_mounted(self):
        # flash() reads and verifies the stored file once and mounts a copy of THOSE bytes.
        original = firmware.load_firmware

        def load_then_tamper(dd, directory, name):
            result = original(dd, directory, name)
            (self.project() / "firmware" / name).write_bytes(b"TAMPERED")
            return result

        firmware.load_firmware = load_then_tamper
        self.addCleanup(setattr, firmware, "load_firmware", original)
        self.flash(ota="192.0.2.50")
        self.assertEqual(self.docker.uploaded, [OTA_BYTES])

    def test_staged_copy_with_wrong_digest_is_refused(self):
        with self.assertRaises(render.RenderError):
            with firmware.staged_firmware(self.ctx(), b"abc", "0" * 64):
                self.fail("must not be reached")
        self.assertEqual([p.name for p in (self.root / "build").iterdir()], ["displays"])

    def test_old_layout_asks_for_a_render_first(self):
        (self.project() / "helm.yaml").write_text("legacy")
        with self.assertRaises(render.RenderError) as caught:
            self.flash(ota="192.0.2.50")
        self.assertIn("display render helm", str(caught.exception))
        self.assertEqual(self.docker.streamed, [])


class FlashOtaTests(ServerFlashBase):
    def test_ota_uses_host_network_and_the_stored_ota_image(self):
        self.flash(ota="192.0.2.50")
        argv = self.docker.streamed[0]
        self.assertEqual(argv[argv.index("--network") + 1], "host")
        self.assertNotIn("--device", argv[:argv.index(IMAGE)])
        self.assertEqual(argv[argv.index(IMAGE) + 1:],
                         ["upload", "/config/helm.yaml", "--device", "192.0.2.50",
                          "--file", "/fw.bin"])
        self.assertEqual(self.docker.uploaded, [OTA_BYTES])
        # the project is read-only; ESPHome's scratch space is tmpfs
        self.assertIn(f"{self.project_files()}:/config:ro", argv)
        self.assertIn("--tmpfs", argv)
        self.assertNotIn("firmware", " ".join(a for a in argv if a.startswith("/config")))
        joined = " ".join(argv)
        secrets = json.loads(self.secrets_path.read_text())
        for secret in (secrets["displays"]["helm"]["ota_password"], MQTT_PW, "wifi-pass-123"):
            self.assertNotIn(secret, joined)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-e"],
                         ["ESPHOME_DATA_DIR=/tmp/esphome-data"])

    def test_host_validation(self):
        for good in ("192.0.2.50", "helm", "vs-helm.local", "display-1.example.com",
                     "fe80::1", "192.0.2.1", "a" * 63 + ".example"):
            self.assertEqual(firmware.validate_host(good), good)
        for bad in ("", "http://192.0.2.50", "192.0.2.50:6053", "host/path", "-oops",
                    "--file=x", "a b", "host\n", "bad_name", "1.2.3", "300.1.1.1.1", "a." + "b" * 64,
                    "/dev/ttyACM0", "COM3", "MQTT", "mqttip", "BOOTSEL", "x" * 254, "ex ample.com",
                    "host;rm", "$(id)", "über.example"):
            with self.assertRaises(render.RenderError, msg=bad) as caught:
                firmware.validate_host(bad)
            self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(render.RenderError):
            self.flash(ota="http://192.0.2.50")
        self.assertEqual(self.docker.streamed, [])

    def test_ota_refuses_a_modified_image(self):
        (self.project() / "firmware" / "firmware.ota.bin").write_bytes(b"tampered")
        with self.assertRaises(render.RenderError):
            self.flash(ota="192.0.2.50")
        self.assertEqual(self.docker.streamed, [])

    def test_exactly_one_target_required(self):
        with self.assertRaises(render.RenderError):
            self.flash()
        with self.assertRaises(render.RenderError):
            self.flash(ota="192.0.2.50", serial="/dev/ttyACM0")


class FlashCommonTests(ServerFlashBase):
    def test_previous_flashes_firmware_prev(self):
        with self.assertRaises(render.RenderError) as caught:
            self.flash(ota="192.0.2.50", previous=True)
        self.assertIn("no previous firmware", str(caught.exception))
        self.assertEqual(self.docker.streamed, [])
        self.docker.ota = b"NEWER-OTA" * 20
        self.build()
        self.docker.streamed.clear()
        self.flash(ota="192.0.2.50", previous=True)
        self.assertEqual(self.docker.uploaded, [OTA_BYTES])  # the OLD build, not NEWER-OTA
        nodes, links = self.fake_serial()
        self.flash(serial="/dev/ttyACM0", previous=True, **self.seam(nodes, links))
        self.assertEqual(self.docker.uploaded[1], FACTORY_BYTES)

    def test_previous_is_verified_against_its_own_manifest(self):
        self.build()
        (self.project() / "firmware.prev" / "firmware.ota.bin").write_bytes(b"bad")
        with self.assertRaises(render.RenderError):
            self.flash(ota="192.0.2.50", previous=True)

    def test_missing_firmware_says_run_build_first(self):
        import shutil
        shutil.rmtree(self.project() / "firmware")
        with self.assertRaises(render.RenderError) as caught:
            self.flash(ota="192.0.2.50")
        self.assertIn("display build helm", str(caught.exception))
        self.assertEqual(self.docker.streamed, [])

    def test_never_built_display_says_run_build_first(self):
        shutil_root = self.root / "build"
        import shutil
        shutil.rmtree(shutil_root)
        with self.assertRaises(render.RenderError) as caught:
            self.flash(ota="192.0.2.50")
        self.assertIn("display build helm", str(caught.exception))

    def test_flash_never_pulls_and_needs_the_image_locally(self):
        self.docker.image_present = False
        with self.assertRaises(render.RenderError) as caught:
            self.flash(ota="192.0.2.50")
        self.assertIn("never pulls", str(caught.exception))
        self.assertEqual(self.docker.streamed, [])
        self.docker.image_present = True
        self.flash(ota="192.0.2.50")
        argv = self.docker.streamed[0]
        self.assertEqual(argv[argv.index("--pull") + 1], "never")
        self.assertFalse([a for a in self.docker.captured if a[:2] == ["docker", "pull"]])

    def test_corrupt_manifest_is_an_error(self):
        (self.project() / "firmware" / "manifest.json").write_text("{not json")
        with self.assertRaises(render.RenderError):
            self.flash(ota="192.0.2.50")
        (self.project() / "firmware" / "manifest.json").write_text('{"files": {}}')
        with self.assertRaises(render.RenderError):
            self.flash(ota="192.0.2.50")


class CommandLineTests(FirmwareBase):
    def test_parser_requires_one_flash_target(self):
        parser = displayctl.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["flash", "helm"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["flash", "helm", "--serial", "/dev/ttyACM0", "--ota", "h"])
        args = parser.parse_args(["flash", "helm", "--ota", "192.0.2.2", "--previous"])
        self.assertEqual((args.ota, args.serial, args.previous), ("192.0.2.2", None, True))
        self.assertEqual(parser.parse_args(["build", "helm"]).id, "helm")

    def test_exit_codes_for_usage_errors(self):
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stderr(err):
            code = displayctl.main(["flash", "helm", "--ota", "http://x", "--root", str(self.root),
                                    "--allow-non-root"])
        self.assertEqual(code, 2)
        self.assertIn("error:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
