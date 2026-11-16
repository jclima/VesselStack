#!/usr/bin/env python3
"""The container components VesselStack runs, their release defaults and version rules.

Data and small pure functions only (standard library, runnable with ``python3 -I``).
Each component has one setting in the settings file. An EMPTY setting means "follow the
release default"; a non-empty value is an owner override and is kept across updates.
The image repository is fixed here (except AIS_IMAGE, which stays a full reference), so
a value can never name another registry or repository.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Component:
    id: str
    label: str
    setting: str
    repository: str
    default: str          # value of the setting at the release default (a full reference for ais-catcher)
    example: str          # a valid example for messages
    pattern: str          # regular expression the tag (without digest) must match
    one_way: bool         # holds data that a newer version may upgrade one way
    service: str          # docker compose service name
    min_minor: int | None = None    # for the major.minor rules: lowest accepted minor
    major: int | None = None


COMPONENTS: tuple[Component, ...] = (
    Component("signalk", "Signal K", "SIGNALK_VERSION", "cr.signalk.io/signalk/signalk-server", "v2.27.0", "v2.28.0",
              r"v2\.[0-9]+\.[0-9]+", True, "signalk"),
    Component("ais-catcher", "AIS-catcher", "AIS_IMAGE", "ghcr.io/jvde-github/ais-catcher", "ghcr.io/jvde-github/ais-catcher:v0.70",
              "ghcr.io/jvde-github/ais-catcher:v0.71", r".*[0-9].*", False, "ais"),
    Component("nodered", "Node-RED", "NODERED_VERSION", "nodered/node-red", "5.0.9-debian", "5.0.10-debian",
              r"5\.[0-9]+\.[0-9]+-debian", True, "nodered"),
    Component("homeassistant", "Home Assistant", "HOMEASSISTANT_VERSION", "homeassistant/home-assistant", "2026.6.4", "2026.7.0",
              r"20[0-9]{2}\.[0-9]{1,2}\.[0-9]+", True, "homeassistant"),
    Component("influxdb", "InfluxDB", "INFLUXDB_VERSION", "influxdb", "2.9.1", "2.9.2",
              r"2\.[0-9]+\.[0-9]+", True, "influxdb"),
    Component("grafana", "Grafana", "GRAFANA_VERSION", "grafana/grafana", "13.0.1", "13.0.2",
              r"13\.[0-9]+\.[0-9]+", True, "grafana"),
    Component("prometheus", "Prometheus", "PROMETHEUS_VERSION", "prom/prometheus", "v2.55.0", "v2.55.1",
              r"v2\.[0-9]+\.[0-9]+", False, "prometheus"),
    Component("node-exporter", "Node exporter", "NODE_EXPORTER_VERSION", "quay.io/prometheus/node-exporter", "v1.10.2", "v1.10.3",
              r"v1\.[0-9]+\.[0-9]+", False, "node-exporter"),
    Component("mosquitto", "Mosquitto", "MOSQUITTO_VERSION", "eclipse-mosquitto", "2.1.2-alpine", "2.1.3-alpine",
              r"2\.[0-9]+\.[0-9]+-alpine", True, "mosquitto", min_minor=1),
    Component("homepage", "Homepage", "HOMEPAGE_VERSION", "ghcr.io/gethomepage/homepage",
              "v2.7.0@sha256:19c5c09184458a41b7b5915e71463a39f36cd33540f41bdbed008791b8182d5f", "v2.7.1",
              r"v2\.[0-9]+\.[0-9]+", False, "homepage"),
)

BY_ID = {c.id: c for c in COMPONENTS}
BY_SETTING = {c.setting: c for c in COMPONENTS}
FLOATING = frozenset({"latest", "stable", "main", "master", "edge", "nightly", "dev", "beta", "alpha", "rc"})
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}")
REPOSITORY_RE = re.compile(r"(?:[a-z0-9.-]+(?::[0-9]{1,5})?/)?[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*")
STATES = ("default", "newer", "older", "different")


def components() -> list[str]:
    return [c.id for c in COMPONENTS]


def _get(component_id: str) -> Component:
    try:
        return BY_ID[component_id]
    except KeyError:
        raise ValueError(f"Unknown component: {component_id}") from None


def default(component_id: str) -> str:
    """The release default, in the terms of the setting (a full reference for ais-catcher)."""
    return _get(component_id).default


def default_reference(component_id: str) -> str:
    return effective(component_id, "")


def _split_reference(comp: Component, value: str) -> tuple[str, str, str]:
    """(repository, tag, digest) of a non-empty setting value; digest without the '@'."""
    body, _, digest = value.partition("@")
    if comp.id == "ais-catcher":
        slash = body.rfind("/")
        colon = body.rfind(":")
        if colon <= slash:
            return body, "", digest
        return body[:colon], body[colon + 1:], digest
    return comp.repository, body, digest


def _version_numbers(tag: str) -> tuple[int, ...] | None:
    match = re.match(r"v?([0-9]+(?:\.[0-9]+)*)", tag)
    return tuple(int(x) for x in match.group(1).split(".")) if match else None


def validate(component_id: str, value: str) -> str | None:
    """None when the value is acceptable (empty follows the release), else a plain-language message."""
    comp = _get(component_id)
    text = str(value if value is not None else "")
    if text == "":
        return None
    name, example = comp.setting, comp.example
    hint = f" Use a version such as {example}, or leave {name} empty to follow the release (default {comp.default})."
    if text != text.strip() or re.search(r"\s", text):
        return f"{name} must not contain spaces." + hint
    if "\x00" in text or not text.isprintable():
        return f"{name} contains characters that cannot be used." + hint
    repository, tag, digest = _split_reference(comp, text)
    if digest and not DIGEST_RE.fullmatch(digest):
        return f"{name}: the part after @ must be sha256: followed by 64 hexadecimal characters." + hint
    if comp.id == "ais-catcher":
        if not tag:
            return f"{name} must be a full image reference with a version, such as {example}."
        if not REPOSITORY_RE.fullmatch(repository):
            return f"{name}: '{repository}' is not a valid image name. Use a full reference such as {example}."
    else:
        if "/" in tag or ":" in tag:
            return f"{name} is a version only, such as {example}. It cannot name another image or registry."
    if not TAG_RE.fullmatch(tag):
        return f"{name}: '{tag}' is not a valid version." + hint
    if tag.lower() in FLOATING or not re.search(r"[0-9]", tag):
        return f"{name}: '{tag}' is not a fixed version. Floating names such as latest are not accepted; use a numbered version." + hint
    if comp.id == "ais-catcher":
        return None
    if not re.fullmatch(comp.pattern, tag):
        if comp.id == "nodered" and not tag.endswith("-debian"):
            return f"{name}: '{tag}' must end in -debian, such as {example}."
        if comp.id == "mosquitto" and not tag.endswith("-alpine"):
            return f"{name}: '{tag}' must end in -alpine, such as {example}."
        return f"{name}: '{tag}' is not a version this release can run for {comp.label}." + hint
    if comp.min_minor is not None:
        nums = _version_numbers(tag)
        if nums is None or len(nums) < 2 or nums[1] < comp.min_minor:
            return f"{name}: {comp.label} {tag} is older than the release default {comp.default}, which this release needs at least." + hint
    return None


def effective(component_id: str, value: str = "") -> str:
    """The full image reference that will run for this setting value (the release default when empty)."""
    comp = _get(component_id)
    text = str(value or "")
    if text:
        problem = validate(component_id, text)
        if problem:
            raise ValueError(problem)
    else:
        text = comp.default
    return text if comp.id == "ais-catcher" else f"{comp.repository}:{text}"


def compare(component_id: str, value: str = "") -> str:
    """One of default, newer, older, different, relative to the release default."""
    comp = _get(component_id)
    text = str(value or "")
    if text in ("", comp.default):
        return "default"
    problem = validate(component_id, text)
    if problem:
        return "different"
    d_repo, d_tag, d_digest = _split_reference(comp, comp.default)
    repo, tag, digest = _split_reference(comp, text)
    if repo != d_repo:
        return "different"
    mine, theirs = _version_numbers(tag), _version_numbers(d_tag)
    if mine is None or theirs is None:
        return "different"
    if mine == theirs:
        return "different"   # same numbers, another build (digest or suffix)
    width = max(len(mine), len(theirs))
    a = mine + (0,) * (width - len(mine))
    b = theirs + (0,) * (width - len(theirs))
    if a == b:
        return "different"
    return "newer" if a > b else "older"


def describe() -> list[dict]:
    return [{"id": c.id, "label": c.label, "setting": c.setting, "repository": c.repository, "default": c.default,
             "default_image": effective(c.id, ""), "one_way": c.one_way, "service": c.service} for c in COMPONENTS]


_ENV_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)")


def read_env_setting(path: str, key: str) -> str | None:
    """The value of KEY in an env file (the last assignment), or None. No shell evaluation."""
    import shlex
    found = None
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                match = _ENV_LINE.fullmatch(raw.rstrip("\n"))
                if match and match.group(1) == key:
                    try:
                        tokens = shlex.split(match.group(2), comments=True, posix=True)
                    except ValueError:
                        return None
                    found = tokens[0] if len(tokens) == 1 else ("" if not tokens else None)
    except OSError:
        return None
    return found

# --- report: what is configured against what is running --------------------------------------

def container_name(component_id: str, settings: dict) -> str:
    if component_id == "influxdb":
        return settings.get("INFLUXDB_CONTAINER_NAME") or "vesselstack-influxdb"
    return "vesselstack-" + component_id


def is_enabled(component_id: str, settings: dict) -> bool:
    disabled = {x.strip() for x in (settings.get("VESSELSTACK_DISABLED_COMPONENTS") or "").split(",")}
    if component_id == "signalk":
        return settings.get("SIGNALK_MODE") == "docker"
    if component_id == "ais-catcher":
        return settings.get("AIS_ENABLE") == "true"
    if component_id == "node-exporter":
        return settings.get("NODE_EXPORTER_ENABLE") == "true"
    if component_id == "nodered":
        return settings.get("NODERED_ENABLE") == "true"
    return component_id not in disabled


class DockerUnavailable(Exception):
    pass


def docker_inspect(name: str) -> dict | None:
    """{'image', 'image_id', 'running'} of a container, None when it does not exist; DockerUnavailable otherwise."""
    import subprocess
    try:
        proc = subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}|{{.Image}}|{{.State.Running}}", name],
                              capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        raise DockerUnavailable() from None
    if proc.returncode != 0:
        if "No such" in proc.stderr:
            return None
        raise DockerUnavailable()
    parts = proc.stdout.strip().split("|")
    if len(parts) != 3:
        raise DockerUnavailable()
    return {"image": parts[0], "image_id": parts[1], "running": parts[2] == "true"}


def report(settings: dict, inspect=docker_inspect) -> list[dict]:
    """One row per component. A setting that fails validation is reported as such, never run."""
    rows = []
    docker_ok = True
    for c in COMPONENTS:
        chosen = str(settings.get(c.setting) or "")
        problem = validate(c.id, chosen)
        enabled = is_enabled(c.id, settings)
        row = {"id": c.id, "label": c.label, "setting": c.setting, "release_default": c.default, "chosen": chosen,
               "enabled": enabled, "one_way": c.one_way, "effective_image": None, "running_image": None,
               "running_image_id": None, "running": None, "container": container_name(c.id, settings)}
        row["effective_image"] = effective(c.id, "") if problem else effective(c.id, chosen)
        relation = "different" if problem else compare(c.id, chosen)
        row["relation"] = relation
        row["invalid"] = problem
        if not enabled:
            row["running_state"] = "not enabled"
        elif not docker_ok:
            row["running_state"] = "unknown"
        else:
            try:
                found = inspect(row["container"])
            except DockerUnavailable:
                docker_ok = False
                found = None
                row["running_state"] = "unknown"
            else:
                if found is None:
                    row["running_state"] = "not running"
                else:
                    row.update(running_image=found["image"], running_image_id=found["image_id"], running=found["running"])
                    row["running_state"] = "running" if found["running"] else "stopped"
        drift = row["running_image"] is not None and row["running_image"] != row["effective_image"]
        row["drift"] = drift
        if drift:
            row["state"] = "drift"
        elif relation == "default":
            row["state"] = "release default"
        else:
            row["state"] = "override" if relation == "different" else f"override ({relation})"
        rows.append(row)
    return rows


STATE_TEXT = {"drift": "running a different image than configured"}


def _short(value: str) -> str:
    return re.sub(r"(@sha256:[0-9a-f]{12})[0-9a-f]+", r"\1...", value or "")


def format_table(rows: list[dict]) -> str:
    out = ["Component versions", ""]
    head = ("COMPONENT", "RELEASE DEFAULT", "CHOSEN", "RUNNING", "STATE")
    table = [head]
    for r in rows:
        name = r["label"] + (" *" if r["one_way"] else "")
        running = _short(r["running_image"].split(":", 1)[1] if r["running_image"] and ":" in r["running_image"] and r["id"] != "ais-catcher" else (r["running_image"] or r["running_state"]))
        table.append((name, _short(r["release_default"]), _short(r["chosen"]) or "release default", running, STATE_TEXT.get(r["state"], r["state"])))
    widths = [max(len(row[i]) for row in table) for i in range(4)]
    for row in table:
        out.append("  ".join(row[i].ljust(widths[i]) for i in range(4)) + "  " + row[4])
    out += ["", "* holds data that a newer version may upgrade one way: make a backup before using a different version."]
    if any(r["running_state"] == "unknown" for r in rows):
        out.append("Docker did not answer, so what is running is unknown. The configured side above is still correct.")
    if any(r["state"] != "release default" for r in rows):
        out.append("A version other than the release default is not tested with this release. To go back, empty its setting in the settings file and run the installer again.")
    return "\n".join(out)


def lines_for(rows: list[dict], kind: str) -> tuple[list[str], list[str]]:
    """(stdout lines, stderr lines) for doctor or status."""
    out, err = [], []
    odd = [r for r in rows if r["state"] != "release default"]
    if kind == "status":
        if odd:
            out.append(f"{'Component versions':<20} {len(odd)} not at the release default; see: sudo vesselstackctl component versions")
        return out, err
    for r in rows:
        if r["invalid"]:
            out.append(f"WARN {r['setting']} is not a usable version ({r['invalid']}) The release default {r['release_default']} is used by the installer only after you fix or empty it.")
        elif r["chosen"] and r["relation"] != "default":
            shown = _short(r["chosen"])
            out.append(f"WARN {r['label']} {shown} is set; the release default is {_short(r['release_default'])}. This version is not tested with this release.")
        if r["drift"]:
            line = (f"{r['label']} is running {r['running_image']} but {r['effective_image']} is configured. "
                    "Fix: sudo vesselstackctl restart, or rerun the installer.")
            if r["enabled"] and r["running"]:
                err.append("FAIL " + line)
            else:
                out.append("INFO " + line)
    return out, err


def _report_main(args) -> int:
    settings = {}
    keys = [c.setting for c in COMPONENTS] + ["SIGNALK_MODE", "AIS_ENABLE", "NODE_EXPORTER_ENABLE", "NODERED_ENABLE", "VESSELSTACK_DISABLED_COMPONENTS", "INFLUXDB_CONTAINER_NAME"]
    for key in keys:
        value = read_env_setting(args.env_file, key)
        if value is not None:
            settings[key] = value
    rows = report(settings)
    if args.format == "json":
        print(json.dumps({"components": rows, "docker": "unavailable" if any(r["running_state"] == "unknown" for r in rows) else "available"}, indent=2, sort_keys=True))
        return 0
    if args.format == "table":
        print(format_table(rows))
        return 0
    out, err = lines_for(rows, args.format)
    for line in out:
        print(line)
    for line in err:
        print(line, file=__import__("sys").stderr)
    return 1 if err else 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--json", action="store_true")
    group.add_argument("--validate", nargs=2, metavar=("ID", "VALUE"))
    group.add_argument("--effective", nargs=2, metavar=("ID", "VALUE"))
    group.add_argument("--default", metavar="ID")
    group.add_argument("--compare", nargs=2, metavar=("ID", "VALUE"))
    group.add_argument("--report", action="store_true", help="configured against running; needs --env-file")
    parser.add_argument("--env-file")
    parser.add_argument("--format", choices=("table", "json", "doctor", "status"), default="table")
    args = parser.parse_args(argv)
    if args.report:
        if not args.env_file:
            parser.error("--report needs --env-file")
        return _report_main(args)
    if args.list:
        for c in COMPONENTS:
            print(f"{c.id}\t{c.setting}\t{c.default}")
        return 0
    if args.json:
        print(json.dumps({"components": describe()}, indent=2, sort_keys=True))
        return 0
    try:
        if args.validate:
            problem = validate(*args.validate)
            if problem:
                print(problem)
                return 1
            return 0
        if args.effective:
            print(effective(*args.effective))
            return 0
        if args.compare:
            print(compare(*args.compare))
            return 0
        print(default(args.default))
        return 0
    except ValueError as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
