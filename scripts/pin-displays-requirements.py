#!/usr/bin/env python3
"""Regenerate displays/requirements.txt with sha256 hashes taken from PyPI.

Usage: scripts/pin-displays-requirements.py > displays/requirements.txt

The versions below are the single place the bridge dependencies are chosen. The
output lists the sdist, the pure-Python wheel and every Linux wheel of each
release, so pip (which verifies a hash only for the file it picks) works on
x86_64, aarch64 and armv7 hosts. The hashes are as published by PyPI at the time
the command runs: review the diff like any other dependency change.
"""
import json
import sys
import urllib.request

PINS = (("paho-mqtt", "2.1.0"), ("websockets", "17.3"))
HEADER = """\
# Exact, hash-pinned dependencies of the displays bridge (installed with --require-hashes).
# Regenerate with: python3 scripts/pin-displays-requirements.py > displays/requirements.txt
"""


def wanted(filename: str) -> bool:
    if filename.endswith(".tar.gz") or filename.endswith("-py3-none-any.whl"):
        return True
    return filename.endswith(".whl") and ("manylinux" in filename or "musllinux" in filename)


def main() -> int:
    out = [HEADER]
    for name, version in PINS:
        url = f"https://pypi.org/pypi/{name}/{version}/json"
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 (fixed https URL)
            files = json.load(response)["urls"]
        hashes = sorted({item["digests"]["sha256"] for item in files if wanted(item["filename"])})
        if not hashes:
            raise SystemExit(f"no files found for {name}=={version}")
        out.append(f"{name}=={version} \\\n" + " \\\n".join(f"    --hash=sha256:{h}" for h in hashes) + "\n")
    sys.stdout.write("".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
