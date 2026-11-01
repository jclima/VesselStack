#!/usr/bin/env python3
"""Archive admission tests for browser release extraction."""

from __future__ import annotations

import importlib.util
import io
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_updates():
    spec = importlib.util.spec_from_file_location("archive_updates", ROOT / "control-panel" / "browser_updates.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArchiveAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.updates = load_updates()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="vesselstack-archive-test-")
        self.root = Path(self.tmp.name)
        self.payloads: dict[int, bytes] = {}
        self.case_number = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def archive(self, entries: list[tarfile.TarInfo], name: str = "fixture.tar.gz") -> Path:
        path = self.root / name
        with tarfile.open(path, "w:gz") as handle:
            for entry in entries:
                data = self.payloads.get(id(entry))
                if data is not None:
                    entry.size = len(data)
                    handle.addfile(entry, io.BytesIO(data))
                else:
                    handle.addfile(entry)
        return path

    def file_entry(self, name: str, data: bytes = b"x", mode: int = 0o644) -> tarfile.TarInfo:
        entry = tarfile.TarInfo(name)
        entry.mode = mode
        self.payloads[id(entry)] = data
        return entry

    def dir_entry(self, name: str, mode: int = 0o755) -> tarfile.TarInfo:
        entry = tarfile.TarInfo(name.rstrip("/") + "/")
        entry.type = tarfile.DIRTYPE
        entry.mode = mode
        return entry

    def valid_entries(self, version: str = "0.5.4") -> list[tarfile.TarInfo]:
        top = f"vesselstack-{version}"
        entries = [self.dir_entry(top), self.dir_entry(f"{top}/scripts"), self.dir_entry(f"{top}/systemd"), self.dir_entry(f"{top}/templates"), self.dir_entry(f"{top}/control-panel")]
        entries += [
            self.file_entry(f"{top}/VERSION", f"{version}\n".encode()),
            self.file_entry(f"{top}/install.sh", b"#!/bin/sh\n", 0o755),
            self.file_entry(f"{top}/scripts/vesselstackctl", b"#!/bin/sh\n", 0o755),
            self.file_entry(f"{top}/systemd/vesselstack-control-panel.service.in"),
            self.file_entry(f"{top}/templates/update.env.example"),
            self.file_entry(f"{top}/control-panel/app.py"),
        ]
        return entries

    def assert_rejected_without_writes(self, entries: list[tarfile.TarInfo], **kwargs: object) -> None:
        archive = self.archive(entries)
        self.case_number += 1
        destination = self.root / f"extract-{self.case_number}"
        destination.mkdir()
        with self.assertRaises(ValueError):
            self.updates.safe_extract(archive, destination, **kwargs)
        self.assertEqual(list(destination.iterdir()), [], "rejected archive wrote files")

    def test_valid_full_layout_extracts_with_expected_version(self) -> None:
        archive = self.archive(self.valid_entries())
        destination = self.root / "extract"
        destination.mkdir()
        self.updates.safe_extract(archive, destination, expected_version="0.5.4")
        self.assertEqual((destination / "vesselstack-0.5.4" / "VERSION").read_text(), "0.5.4\n")
        self.assertTrue((destination / "vesselstack-0.5.4" / "install.sh").stat().st_mode & stat.S_IXUSR)
        for directory in ("scripts", "systemd", "templates", "control-panel"):
            self.assertTrue((destination / "vesselstack-0.5.4" / directory).is_dir())

    def test_path_traversal_absolute_backslash_dot_and_wrong_top_are_rejected(self) -> None:
        cases = [
            [self.dir_entry("vesselstack-0.5.4"), self.file_entry("vesselstack-0.5.4/../escape")],
            [self.file_entry("/absolute")],
            [self.file_entry(r"vesselstack-0.5.4\..\escape")],
            [self.file_entry("vesselstack-0.5.4/./escape")],
            self.valid_entries("0.5.3"),
        ]
        for entries in cases:
            with self.subTest(entries=[entry.name for entry in entries]):
                self.assert_rejected_without_writes(entries, expected_version="0.5.4")

    def test_duplicate_and_normalized_duplicate_paths_are_rejected(self) -> None:
        top = "vesselstack-0.5.4"
        self.assert_rejected_without_writes([self.dir_entry(top), self.file_entry(f"{top}/x"), self.file_entry(f"{top}/x")])
        self.assert_rejected_without_writes([self.dir_entry(top), self.file_entry(f"{top}/x"), self.file_entry(f"{top}/x/")])

    def test_links_devices_fifo_modes_and_ownership_are_rejected(self) -> None:
        top = "vesselstack-0.5.4"
        symlink = tarfile.TarInfo(f"{top}/link"); symlink.type = tarfile.SYMTYPE; symlink.linkname = "/etc/passwd"
        hardlink = tarfile.TarInfo(f"{top}/hard"); hardlink.type = tarfile.LNKTYPE; hardlink.linkname = f"{top}/target"
        device = tarfile.TarInfo(f"{top}/device"); device.type = tarfile.CHRTYPE
        fifo = tarfile.TarInfo(f"{top}/fifo"); fifo.type = tarfile.FIFOTYPE
        cases = [
            [self.dir_entry(top), symlink], [self.dir_entry(top), hardlink],
            [self.dir_entry(top), device], [self.dir_entry(top), fifo],
        ]
        mode = self.file_entry(f"{top}/setid", mode=0o4755)
        writable = self.file_entry(f"{top}/writable", mode=0o644 | 0o022)
        nonroot = self.file_entry(f"{top}/owner"); nonroot.uid = 123; nonroot.gid = 456
        cases += [[self.dir_entry(top), mode], [self.dir_entry(top), writable], [self.dir_entry(top), nonroot]]
        for entries in cases:
            with self.subTest(kind=entries[-1].type):
                self.assert_rejected_without_writes(entries)

    def test_destination_lexical_symlink_is_rejected(self) -> None:
        real = self.root / "real"
        real.mkdir()
        destination = self.root / "extract"
        destination.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.updates.safe_extract(self.archive(self.valid_entries()), destination, expected_version="0.5.4")
        self.assertEqual(list(real.iterdir()), [])

    def test_member_count_and_expanded_size_limits_reject_before_writes(self) -> None:
        entries = self.valid_entries() + [self.file_entry("vesselstack-0.5.4/extra", b"large")]
        archive = self.archive(entries)
        destination = self.root / "count"; destination.mkdir()
        with mock.patch.object(self.updates, "MAX_MEMBERS", len(entries) - 1):
            with self.assertRaises(ValueError): self.updates.safe_extract(archive, destination)
        self.assertEqual(list(destination.iterdir()), [])

        destination = self.root / "size"; destination.mkdir()
        with mock.patch.object(self.updates, "MAX_ARCHIVE_EXPANDED", 1):
            with self.assertRaises(ValueError): self.updates.safe_extract(self.archive(self.valid_entries()), destination)
        self.assertEqual(list(destination.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
