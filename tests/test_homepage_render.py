import importlib.util
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "render-homepage.py"
ICON = REPO / "brand" / "icon.png"

spec = importlib.util.spec_from_file_location("render_homepage", SCRIPT)
render_homepage = importlib.util.module_from_spec(spec)
sys.modules["render_homepage"] = render_homepage
spec.loader.exec_module(render_homepage)

SECRETS = {
    "INFLUXDB_TOKEN": "tok-influx-0123456789",
    "INFLUXDB_PASSWORD": "pw-influx-0123456789",
    "GRAFANA_ADMIN_PASSWORD": "pw-grafana-0123456789",
    "MQTT_PASSWORD": "pw-mqtt-0123456789",
    "HOME_ASSISTANT_TOKEN": "tok-ha-0123456789",
    "DISPLAYS_HA_TOKEN": "tok-displays-ha-0123456789",
    "DISPLAYS_SIGNALK_TOKEN": "tok-displays-sk-0123456789",
    "BOAT_CHAT_ACCESS_TOKEN": "tok-chat-0123456789",
    "BOAT_CHAT_SETTINGS_TOKEN": "tok-chat-settings-0123456789",
    "CONTROL_PANEL_TOKEN": "tok-panel-0123456789",
    "BOAT_MMSI": "999000111",
    "BOAT_CALLSIGN": "XX9999",
}


def write_env(path: Path, values: dict) -> None:
    path.write_text("".join(f"{k}={v}\n" if re.fullmatch(r"[A-Za-z0-9_.:/@-]*", v) else f'{k}="{v}"\n'
                            for k, v in values.items()))


class Fixture:
    def __init__(self, test: unittest.TestCase, vs=None, panel=None, chat=None):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        config = self.root / "config"
        config.mkdir()
        base = {
            "BOAT_NAME": "Sea Wolf",
            "SIGNALK_MODE": "docker",
            "SIGNALK_URL": "http://127.0.0.1:3000",
            "HOME_ASSISTANT_URL": "http://127.0.0.1:8123",
            "VESSELSTACK_WEB_BIND_ADDRESS": "0.0.0.0",
            "VESSELSTACK_DATA_BIND_ADDRESS": "127.0.0.1",
            "GRAFANA_PORT": "43000",
            "HOMEPAGE_PORT": "80",
            **SECRETS,
        }
        base.update(vs or {})
        write_env(config / "vesselstack.env", base)
        write_env(config / "control-panel.env", {"CONTROL_PANEL_HOST": "127.0.0.1", "CONTROL_PANEL_PORT": "8780",
                                                 "CONTROL_PANEL_TOKEN": SECRETS["CONTROL_PANEL_TOKEN"], **(panel or {})})
        write_env(config / "boat-chat.env", {"BOAT_CHAT_HOST": "0.0.0.0", "BOAT_CHAT_PORT": "8765",
                                             "BOAT_CHAT_ACCESS_TOKEN": SECRETS["BOAT_CHAT_ACCESS_TOKEN"], **(chat or {})})

    def run(self, command="render", hostname="boat", addresses="192.0.2.7"):
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), command, "--root", str(self.root), "--hostname", hostname,
             "--addresses", addresses, "--brand-icon", str(ICON)],
            capture_output=True, text=True)

    @property
    def out(self) -> Path:
        return self.root / "config" / "homepage"

    def read(self, name: str) -> str:
        return (self.out / name).read_text()


class TilesTests(unittest.TestCase):
    def tiles(self, **kwargs):
        fixture = Fixture(self, **kwargs)
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        return fixture, fixture.read("services.yaml")

    def test_default_tiles_follow_the_enabled_components(self):
        _, services = self.tiles()
        for name in ("Boat Chat", "Home Assistant", "Signal K", "Grafana", "Control Panel"):
            self.assertIn(f'- "{name}":', services)
        self.assertIn('href: "http://boat.local:43000"', services)
        self.assertNotIn('"AIS"', services)
        self.assertNotIn('"Displays"', services)
        # Databases stay off the page while they listen on loopback only.
        self.assertNotIn("InfluxDB", services)
        self.assertNotIn("Prometheus", services)

    def test_nodered_tile_only_when_enabled_and_follows_its_own_bind_address(self):
        _, services = self.tiles()
        self.assertNotIn("Node-RED", services)
        _, services = self.tiles(vs={"NODERED_ENABLE": "true", "NODERED_PORT": "1881"})  # local-only by default
        tile = services.split('- "Node-RED":')[1].split("- \"")[0]
        self.assertNotIn("href", tile)
        _, services = self.tiles(vs={"NODERED_ENABLE": "true", "NODERED_PORT": "1881", "NODERED_BIND_ADDRESS": "0.0.0.0"})
        tile = services.split('- "Node-RED":')[1].split("- \"")[0]
        self.assertIn('href: "http://boat.local:1881"', tile)
        self.assertIn("sign-in required", tile.lower())
        _, services = self.tiles(vs={"NODERED_ENABLE": "true", "NODERED_BIND_ADDRESS": "127.0.0.1"})
        tile = services.split('- "Node-RED":')[1].split("- \"")[0]
        self.assertNotIn("href", tile)
        self.assertIn("SSH tunnel", tile)

    def test_marine_bundle_tiles_only_in_managed_modes_with_the_bundle_on(self):
        for vs in ({}, {"SIGNALK_MARINE_BUNDLE": "false"}, {"SIGNALK_MODE": "existing", "SIGNALK_MARINE_BUNDLE": "true"}):
            _, services = self.tiles(vs=vs)
            for name in ("KIP", "Freeboard", "Logbook", "Anchor alarm"):
                self.assertNotIn(f'- "{name}":', services)
        _, services = self.tiles(vs={"SIGNALK_MODE": "docker", "SIGNALK_MARINE_BUNDLE": "true"})
        for href in ("http://boat.local:3000/@mxtommy/kip/", "http://boat.local:3000/@signalk/freeboard-sk/",
                     "http://boat.local:3000/@meri-imperiumi/signalk-logbook/",
                     "http://boat.local:3000/signalk-anchoralarm-plugin/"):
            self.assertIn(f'href: "{href}"', services)

    def test_removed_and_unmanaged_components_have_no_tile(self):
        _, services = self.tiles(vs={"VESSELSTACK_DISABLED_COMPONENTS": "grafana,homeassistant",
                                     "SIGNALK_MODE": "existing"})
        for name in ("Grafana", "Home Assistant", "Signal K"):
            self.assertNotIn(f'"{name}"', services)
        _, services = self.tiles(vs={"HOME_ASSISTANT_URL": "http://192.0.2.99:8123"})
        self.assertNotIn("Home Assistant", services)

    def test_existing_signalk_on_another_host_is_linked_without_monitoring(self):
        _, services = self.tiles(vs={"SIGNALK_MODE": "existing", "SIGNALK_URL": "http://192.0.2.50:3000/signalk"})
        sk = services.split('- "Signal K":')[1].split("- \"")[0]
        self.assertIn('href: "http://192.0.2.50:3000"', sk)
        self.assertNotIn("siteMonitor", sk)
        _, services = self.tiles(vs={"SIGNALK_MODE": "existing", "SIGNALK_URL": "http://127.0.0.1:3000"})
        self.assertNotIn('"Signal K"', services)
        _, services = self.tiles(vs={"SIGNALK_MODE": "existing", "SIGNALK_URL": "javascript:alert(1)"})
        self.assertNotIn('"Signal K"', services)

    def test_ais_and_displays_tiles_only_when_enabled(self):
        _, services = self.tiles(vs={"AIS_ENABLE": "true", "AIS_WEB_PORT": "8101", "DISPLAYS_ENABLE": "true"})
        self.assertIn('href: "http://boat.local:8101"', services)
        self.assertIn('- "Displays":', services)
        # Panel is loopback only: the Displays tile explains the tunnel and has no link.
        displays = services.split('- "Displays":')[1].split("- \"")[0]
        self.assertNotIn("href", displays)
        self.assertIn("SSH tunnel", displays)
        _, services = self.tiles(vs={"DISPLAYS_ENABLE": "true"}, panel={"CONTROL_PANEL_HOST": "0.0.0.0"})
        self.assertIn('href: "http://boat.local:8780/?tab=displays"', services)

    def test_data_tiles_only_when_the_data_bind_is_not_loopback(self):
        _, services = self.tiles(vs={"VESSELSTACK_DATA_BIND_ADDRESS": "0.0.0.0"})
        self.assertIn('href: "http://boat.local:8086"', services)
        self.assertIn('href: "http://boat.local:9090"', services)
        self.assertIn("no login", services.split('"Prometheus"')[1])
        _, services = self.tiles(vs={"VESSELSTACK_DATA_BIND_ADDRESS": "0.0.0.0",
                                     "VESSELSTACK_DISABLED_COMPONENTS": "prometheus"})
        self.assertIn('"InfluxDB"', services)
        self.assertNotIn('"Prometheus"', services)

    def test_control_panel_links_only_in_lan_mode(self):
        _, services = self.tiles()
        panel = services.split('- "Control Panel":')[1]
        self.assertNotIn("href", panel)
        self.assertIn("SSH tunnel", panel)
        _, services = self.tiles(panel={"CONTROL_PANEL_HOST": "0.0.0.0"})
        panel = services.split('- "Control Panel":')[1]
        self.assertIn('href: "http://boat.local:8780"', panel)

    def test_loopback_services_are_not_linked_or_monitored(self):
        _, services = self.tiles(chat={"BOAT_CHAT_HOST": "127.0.0.1"})
        chat = services.split('- "Boat Chat":')[1].split("- \"")[0]
        self.assertNotIn("href", chat)
        self.assertNotIn("siteMonitor", chat)

    def test_site_monitor_only_for_services_listening_on_all_interfaces(self):
        _, services = self.tiles(vs={"VESSELSTACK_WEB_BIND_ADDRESS": "192.0.2.7"})
        grafana = services.split('- "Grafana":')[1].split("- \"")[0]
        self.assertIn('href: "http://boat.local:43000"', grafana)
        self.assertNotIn("siteMonitor", grafana)
        _, services = self.tiles()
        self.assertIn('siteMonitor: "http://host.docker.internal:43000"', services)

    def test_rendered_services_use_only_json_quoted_scalars(self):
        fixture, services = self.tiles(vs={"BOAT_NAME": 'Sea "Wolf": #1\\'})
        settings = fixture.read("settings.yaml")
        self.assertIn('title: "Sea \\"Wolf\\": #1\\\\"', settings)
        for line in services.splitlines():
            self.assertTrue(re.match(r'^(- "[^"]+":|    - "[^"]+":|        (href|description|siteMonitor): ".*")$', line), line)

    def test_branding_and_privacy_settings(self):
        fixture, _ = self.tiles()
        settings = fixture.read("settings.yaml")
        self.assertIn('title: "Sea Wolf"', settings)
        self.assertIn("hideVersion: true", settings)
        self.assertIn("disableUpdateCheck: true", settings)
        self.assertIn("/icons/vesselstack.png", settings)
        self.assertEqual((fixture.root / "config" / "homepage-icons" / "vesselstack.png").read_bytes(), ICON.read_bytes())
        self.assertNotRegex(settings + fixture.read("widgets.yaml") + fixture.read("services.yaml"), r"https?://(?!(boat\.local|host\.docker\.internal))")
        self.assertIn('"boat.local"', fixture.read("custom.js"))
        # The Docker integration is empty: the socket is never offered to Homepage.
        self.assertNotRegex(fixture.read("docker.yaml"), r"(?m)^[^#\s-]")

    def test_appearance_preferences_render_with_defaults_for_invalid_values(self):
        fixture, _ = self.tiles(vs={"HOMEPAGE_THEME": "light", "HOMEPAGE_COLUMNS": "wide",
                                     "HOMEPAGE_HEADING": "brand", "HOMEPAGE_CLOCK": "hidden"})
        settings = fixture.read("settings.yaml")
        self.assertIn('title: "VesselStack"', settings)
        self.assertIn("theme: light", settings)
        self.assertEqual(settings.count("columns: 4"), 3)
        self.assertNotIn("datetime:", fixture.read("widgets.yaml"))
        self.assertIn(render_homepage.LIGHT_CSS, fixture.read("custom.css"))

        fixture, _ = self.tiles(vs={"HOMEPAGE_THEME": "unknown", "HOMEPAGE_COLUMNS": "5",
                                     "HOMEPAGE_CLOCK": "12h"})
        self.assertIn("theme: dark", fixture.read("settings.yaml"))
        self.assertIn("columns: 3", fixture.read("settings.yaml"))
        self.assertIn("hour12: true", fixture.read("widgets.yaml"))


class SecretsTests(unittest.TestCase):
    def test_no_secret_value_reaches_any_rendered_file(self):
        fixture = Fixture(self, vs={"VESSELSTACK_DATA_BIND_ADDRESS": "0.0.0.0", "AIS_ENABLE": "true",
                                    "DISPLAYS_ENABLE": "true"}, panel={"CONTROL_PANEL_HOST": "0.0.0.0"})
        self.assertEqual(fixture.run().returncode, 0)
        hosts = fixture.run("allowed-hosts").stdout
        blobs = [hosts]
        for base in (fixture.root / "config" / "homepage", fixture.root / "config" / "homepage-custom"):
            for path in base.iterdir():
                blobs.append(path.read_text())
        blob = "\n".join(blobs)
        for name, value in SECRETS.items():
            self.assertNotIn(value, blob, name)

    def test_renderer_ignores_every_key_outside_its_allow_list(self):
        fixture = Fixture(self)
        env = render_homepage.read_env(fixture.root / "config")
        self.assertLessEqual(set(env), render_homepage.VESSELSTACK_KEYS)
        for name in SECRETS:
            self.assertNotIn(name, env)


class CustomFileTests(unittest.TestCase):
    def test_custom_files_are_created_once_and_never_overwritten(self):
        fixture = Fixture(self)
        fixture.run()
        custom = fixture.root / "config" / "homepage-custom"
        self.assertIn("never overwrites", (custom / "services.yaml").read_text())
        self.assertIn("never overwrites", (custom / "bookmarks.yaml").read_text())
        mine = "- Mine:\n    - Marina:\n        href: http://192.0.2.50/\n"
        (custom / "services.yaml").write_text(mine)
        fixture.run()
        fixture.run()
        self.assertEqual((custom / "services.yaml").read_text(), mine)

    def test_custom_services_are_merged_after_the_generated_tiles(self):
        fixture = Fixture(self)
        fixture.run()
        mine = "- Mine:\n    - Marina:\n        href: http://192.0.2.50/\n"
        (fixture.root / "config" / "homepage-custom" / "services.yaml").write_text(mine)
        fixture.run()
        services = fixture.read("services.yaml")
        self.assertTrue(services.endswith(mine))
        self.assertLess(services.index('"Boat Chat"'), services.index("Marina"))
        # The generated tiles are intact when the custom file is only comments.
        (fixture.root / "config" / "homepage-custom" / "services.yaml").write_text("# nothing\n")
        fixture.run()
        self.assertNotIn("Marina", fixture.read("services.yaml"))

    def test_custom_bookmarks_are_used(self):
        fixture = Fixture(self)
        fixture.run()
        marks = "- Weather:\n    - Forecast:\n        - abbr: FC\n          href: https://example.com/\n"
        (fixture.root / "config" / "homepage-custom" / "bookmarks.yaml").write_text(marks)
        fixture.run()
        self.assertEqual(fixture.read("bookmarks.yaml"), marks)

    def test_invalid_custom_files_are_skipped_with_a_warning(self):
        fixture = Fixture(self)
        fixture.run()
        custom = fixture.root / "config" / "homepage-custom"
        for bad in ("key: value\n", "- ok:\n\tbad: tab\n", "   - indented first\n", "\x00- nul\n", "- a\nkey: b\n"):
            (custom / "services.yaml").write_text(bad)
            result = fixture.run()
            self.assertEqual(result.returncode, 0, bad)
            self.assertIn("Skipping", result.stderr, bad)
            services = fixture.read("services.yaml")
            self.assertIn('"Boat Chat"', services)
            self.assertNotIn("nul", services)
            self.assertNotIn("key: value", services)
        (custom / "services.yaml").write_bytes(b"\xff\xfe- bad\n")
        self.assertIn("Skipping", fixture.run().stderr)
        (custom / "services.yaml").write_text("- " + "x" * (300 * 1024) + "\n")
        self.assertIn("Skipping", fixture.run().stderr)

    def test_a_symlinked_custom_file_is_not_followed(self):
        fixture = Fixture(self)
        fixture.run()
        custom = fixture.root / "config" / "homepage-custom"
        secret = fixture.root / "secret.txt"
        secret.write_text("- Leak:\n")
        (custom / "services.yaml").unlink()
        (custom / "services.yaml").symlink_to(secret)
        result = fixture.run()
        self.assertIn("Skipping", result.stderr)
        self.assertNotIn("Leak", fixture.read("services.yaml"))


class AtomicWriteTests(unittest.TestCase):
    def test_directories_and_files_are_readable_by_the_container_under_umask_077(self):
        fixture = Fixture(self)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        self.assertEqual(fixture.run().returncode, 0)
        config = fixture.root / "config"
        for name in ("homepage", "homepage-custom", "homepage-icons"):
            self.assertEqual(stat.S_IMODE((config / name).stat().st_mode), 0o755, name)
            for path in (config / name).iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644, str(path))
        # An earlier run with a private mode is repaired.
        os.chmod(config / "homepage", 0o700)
        self.assertEqual(fixture.run().returncode, 0)
        self.assertEqual(stat.S_IMODE((config / "homepage").stat().st_mode), 0o755)

    def test_an_invalid_hostname_is_refused(self):
        fixture = Fixture(self)
        for bad in ("bo at", "a;b", "-x"):
            result = fixture.run(hostname=bad)
            self.assertNotEqual(result.returncode, 0, bad)

    def test_files_are_replaced_atomically_with_world_readable_mode(self):
        fixture = Fixture(self)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        fixture.run()
        path = fixture.out / "services.yaml"
        before = path.stat().st_ino
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        fixture.run(hostname="other")
        self.assertNotEqual(path.stat().st_ino, before)  # replaced via rename, not rewritten in place
        self.assertIn("other.local", path.read_text())
        self.assertEqual([p.name for p in fixture.out.iterdir() if ".tmp" in p.name], [])

    def test_a_failed_write_leaves_the_previous_file_and_no_temp_file(self):
        fixture = Fixture(self)
        fixture.run()
        before = (fixture.out / "services.yaml").read_text()
        fd = render_homepage.open_dir(fixture.out)
        self.addCleanup(os.close, fd)
        original = os.replace

        def boom(*args, **kwargs):
            raise OSError("disk full")

        render_homepage.os.replace = boom
        try:
            with self.assertRaises(OSError):
                render_homepage.atomic_write(fd, "services.yaml", b"partial")
        finally:
            render_homepage.os.replace = original
        self.assertEqual((fixture.out / "services.yaml").read_text(), before)
        self.assertEqual([p.name for p in fixture.out.iterdir() if ".tmp" in p.name], [])

    def test_a_planted_symlink_is_replaced_not_followed(self):
        fixture = Fixture(self)
        fixture.run()
        target = fixture.root / "victim.txt"
        target.write_text("keep")
        (fixture.out / "settings.yaml").unlink()
        (fixture.out / "settings.yaml").symlink_to(target)
        self.assertEqual(fixture.run().returncode, 0)
        self.assertEqual(target.read_text(), "keep")
        self.assertFalse((fixture.out / "settings.yaml").is_symlink())

    def test_a_symlinked_output_directory_is_refused(self):
        fixture = Fixture(self)
        elsewhere = fixture.root / "elsewhere"
        elsewhere.mkdir()
        (fixture.root / "config" / "homepage").symlink_to(elsewhere)
        result = fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(elsewhere.iterdir()), [])


class ComposeHardeningTests(unittest.TestCase):
    def test_homepage_drops_all_capabilities_but_the_three_its_entrypoint_needs(self):
        compose = (REPO / "templates" / "compose.yml").read_text()
        block = compose.split("\n  homepage:\n")[1]
        block = re.split(r"\n  [a-z]", block)[0]
        self.assertIn('cap_drop: ["ALL"]', block)
        self.assertIn('cap_add: ["CHOWN", "SETUID", "SETGID"]', block)


class AllowedHostsTests(unittest.TestCase):
    def hosts(self, vs=None, hostname="boat", addresses="192.0.2.7,192.0.2.8"):
        fixture = Fixture(self, vs=vs)
        result = fixture.run("allowed-hosts", hostname=hostname, addresses=addresses)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().split(",")

    def test_port_80_needs_no_port_suffix(self):
        self.assertEqual(self.hosts(), ["boat.local", "boat", "192.0.2.7", "192.0.2.8", "localhost", "127.0.0.1"])

    def test_other_ports_are_appended_to_every_name(self):
        self.assertEqual(self.hosts(vs={"HOMEPAGE_PORT": "8088"}, addresses="192.0.2.7"),
                         ["boat.local:8088", "boat:8088", "192.0.2.7:8088", "localhost:8088", "127.0.0.1:8088"])

    def test_extra_hosts_are_appended_verbatim_and_validated(self):
        hosts = self.hosts(vs={"HOMEPAGE_ALLOWED_HOSTS_EXTRA": "proxy.example.test, bad host,x;y,proxy.example.test:8443"})
        self.assertEqual(hosts[-2:], ["proxy.example.test", "proxy.example.test:8443"])
        self.assertNotIn("bad host", ",".join(hosts))

    def test_no_addresses_still_gives_names_and_loopback(self):
        self.assertEqual(self.hosts(addresses=""), ["boat.local", "boat", "localhost", "127.0.0.1"])


if __name__ == "__main__":
    unittest.main()
