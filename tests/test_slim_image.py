"""tools/slim_image.py: removing unused Debian packages from the image, on a made-up system."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import slim_image

STATUS = """\
Package: tar
Status: install ok installed
Architecture: arm64

Package: libblkid1
Status: install ok installed
Architecture: arm64
Multi-Arch: same
Source: util-linux (2.41-5)

Package: login
Status: install ok installed
Architecture: arm64
Source: util-linux

Package: login.defs
Status: install ok installed
Architecture: all
Source: login.defs-maker

Package: bash
Status: install ok installed
Architecture: arm64

Package: libc6
Status: install ok installed
Architecture: arm64
Multi-Arch: same
Source: glibc
"""

LISTS = {
    "tar": [
        "/.",
        "/bin",
        "/bin/tar",
        "/usr/share/doc/tar",
        "/usr/share/doc/tar/copyright",
        "/usr/libexec/rmt",
        "/bin/shared",
    ],
    "libblkid1:arm64": ["/.", "/usr/lib/libblkid.so.1"],
    "login": ["/.", "/usr/bin/login", "/etc/login.conf", "/usr/share/doc"],
    "login.defs": ["/.", "/etc/login.defs"],
    "bash": ["/.", "/bin", "/usr/bin/bash", "/usr/share/doc", "/usr/share/doc/bash", "/usr/bin/shared"],
    "libc6:arm64": ["/.", "/usr/lib/libc.so.6"],
}


class SlimImageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for directory in ("usr/bin", "usr/lib", "usr/libexec", "etc", "var/lib/dpkg/info"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "bin").symlink_to("usr/bin")  # merged /usr, as in Debian 13
        dpkg = self.root / "var/lib/dpkg"
        (dpkg / "status").write_text(STATUS)
        (dpkg / "status-old").write_text(STATUS)
        for name, paths in LISTS.items():
            (dpkg / "info" / f"{name}.list").write_text("\n".join(paths) + "\n")
            (dpkg / "info" / f"{name}.md5sums").write_text("")
            for path in paths[1:]:
                full = self.root / path.lstrip("/")
                if path in ("/bin", "/usr/share/doc", "/usr/share/doc/tar", "/usr/share/doc/bash"):
                    if not os.path.lexists(full):
                        full.mkdir(parents=True)
                elif not full.exists():
                    full.parent.mkdir(parents=True, exist_ok=True)
                    full.write_text("x")
        (dpkg / "info" / "tar.postrm").write_text("#!/bin/sh\n")

    def tearDown(self):
        self._tmp.cleanup()

    def exists(self, path: str) -> bool:
        return os.path.lexists(self.root / path.lstrip("/"))

    def test_removes_whole_source_packages_and_nothing_else(self):
        removed = slim_image.remove_sources(["tar", "util-linux"], root=self.root)
        self.assertEqual(sorted(removed), ["libblkid1", "login", "tar"])
        for path in ("/usr/bin/tar", "/usr/libexec/rmt", "/usr/lib/libblkid.so.1", "/usr/bin/login", "/etc/login.conf"):
            self.assertFalse(self.exists(path), path)
        # Kept: other packages' files, a file both lists name (as /bin/... and /usr/bin/...),
        # a package whose name only starts the same, and the /bin symlink itself.
        for path in ("/usr/bin/bash", "/usr/lib/libc.so.6", "/etc/login.defs", "/usr/bin/shared"):
            self.assertTrue(self.exists(path), path)
        self.assertTrue((self.root / "bin").is_symlink())
        # Emptied directories go; ones other packages use, or protected ones, stay.
        self.assertFalse(self.exists("/usr/share/doc/tar"))
        self.assertTrue(self.exists("/usr/share/doc/bash"))
        self.assertTrue(self.exists("/usr/libexec"))

    def test_dpkg_records_are_updated(self):
        slim_image.remove_sources(["tar", "util-linux"], root=self.root)
        dpkg = self.root / "var/lib/dpkg"
        left = slim_image.stanzas((dpkg / "status").read_text())
        self.assertEqual([slim_image.field(s, "Package") for s in left], ["login.defs", "bash", "libc6"])
        info = sorted(p.name for p in (dpkg / "info").iterdir())
        self.assertEqual(
            info,
            [
                "bash.list",
                "bash.md5sums",
                "libc6:arm64.list",
                "libc6:arm64.md5sums",
                "login.defs.list",
                "login.defs.md5sums",
            ],
        )
        self.assertFalse((dpkg / "status-old").exists())

    def test_dry_run_changes_nothing(self):
        before = (self.root / "var/lib/dpkg/status").read_text()
        self.assertEqual(sorted(slim_image.remove_sources(["tar"], root=self.root, dry_run=True)), ["tar"])
        self.assertEqual((self.root / "var/lib/dpkg/status").read_text(), before)
        self.assertTrue(self.exists("/usr/bin/tar"))

    def test_names(self):
        stanza = "Package: libblkid1\nArchitecture: arm64\nMulti-Arch: same\nSource: util-linux (2.41-5)"
        self.assertEqual(
            (slim_image.source_of(stanza), slim_image.info_name(stanza)), ("util-linux", "libblkid1:arm64")
        )
        self.assertEqual(slim_image.source_of("Package: tar\nArchitecture: arm64"), "tar")

    def test_refuses_outside_the_docker_build(self):
        with redirect_stderr(io.StringIO()) as said:
            self.assertEqual(slim_image.main([]), 2)
        self.assertIn("only runs inside the Docker image build", said.getvalue())
        with self.assertRaises(slim_image.SlimError):
            slim_image.remove_sources(["tar"], root=self.root / "nowhere")


if __name__ == "__main__":
    unittest.main()
