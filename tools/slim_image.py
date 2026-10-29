"""Slim the Docker image down at build time: take out what the sync never uses, so
the known vulnerabilities in those parts leave with them.

The Dockerfile runs this once, as root, before the final image is assembled:

    python /tmp/slim_image.py --docker-build

It does three things:

1. Uninstalls pip (and setuptools and wheel, if present), and deletes the spare
   copy of pip that Python keeps for ensurepip. The sync only needs pip while the
   image is being built. pip carries its own copies of urllib3, requests, msgpack
   and setuptools code, which scanners report on their own.
2. Removes whole Debian source packages the sync never runs (the shell, apt,
   coreutils, tar and so on): their files and their entries in dpkg's database,
   the way `dpkg --purge` would. Their install scripts aren't run, because those
   would need the very tools being removed. Everything Python needs (glibc,
   OpenSSL, SQLite, zlib, libffi, the CA certificates, time zones...) is kept.
3. Checks, in a fresh Python process, that what the sync needs still works.

It refuses to run anywhere but inside a Debian image as root, so it can't touch a
real computer by accident.
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

# Debian source packages to remove. Scanners match vulnerabilities by source
# package, so every binary package built from one of these goes.
REMOVE_SOURCES = (
    "apt",  # the package manager: nothing gets installed after the build
    "coreutils",  # ls, cp, cat... (Python does all of this itself)
    "dash",  # /bin/sh: the sync never starts a shell
    "diffutils",
    "perl",  # perl-base
    "shadow",  # passwd, useradd (the sync's user is created before this runs)
    "systemd",  # libsystemd0, libudev1: only apt and util-linux use them
    "tar",  # Python's tarfile module is pure Python
    "util-linux",  # mount, login, libuuid... (Python's uuid module works without libuuid)
)

# Directories that are never removed, even if empty.
PROTECTED_DIRS = {
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/home",
    "/lib",
    "/lib64",
    "/media",
    "/mnt",
    "/opt",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/srv",
    "/sys",
    "/tmp",
    "/usr",
    "/usr/bin",
    "/usr/lib",
    "/usr/lib64",
    "/usr/libexec",
    "/usr/local",
    "/usr/sbin",
    "/usr/share",
    "/usr/share/doc",
    "/usr/share/man",
    "/var",
    "/var/lib",
    "/var/lib/dpkg",
    "/var/lib/dpkg/info",
    "/var/log",
}

CHECK = r"""
import bz2, ctypes, decimal, email, hashlib, http.server, imaplib, json, lzma, os, smtplib
import sqlite3, ssl, tarfile, uuid, zlib
from zoneinfo import ZoneInfo

ZoneInfo("America/New_York")
assert zlib.decompress(zlib.compress(b"x" * 1000)) == b"x" * 1000
assert bz2.decompress(bz2.compress(b"x")) == b"x" and lzma.decompress(lzma.compress(b"x")) == b"x"
assert sqlite3.connect(":memory:").execute("select 41 + 1").fetchone() == (42,)
paths = ssl.get_default_verify_paths()
cafile, capath = paths.cafile or "", paths.capath or ""
has_file = bool(cafile) and os.path.isfile(cafile) and os.path.getsize(cafile) > 50_000
has_dir = bool(capath) and os.path.isdir(capath) and len(os.listdir(capath)) > 50
assert has_file or has_dir, f"no CA certificates at {cafile!r} or {capath!r}"
ssl.create_default_context()
hashlib.sha256(b"x").hexdigest()
uuid.uuid4()
import importlib.util
assert importlib.util.find_spec("pip") is None, "pip is still installed"
for name in MODULES:
    __import__(name)
print("Runtime check passed: TLS certificates, SQLite, zlib, time zones and every library load.")
"""
THIRD_PARTY = ("aiohttp", "requests", "openpyxl", "huckleberry_api", "google.auth", "google.cloud.firestore", "grpc")


class SlimError(Exception):
    pass


# ---------------------------------------------------------------------------
# dpkg's database
# ---------------------------------------------------------------------------


def stanzas(text: str) -> list[str]:
    return [block.strip("\n") for block in text.split("\n\n") if block.strip()]


def field(stanza: str, name: str) -> str:
    prefix = name.lower() + ":"
    for line in stanza.splitlines():
        if line.lower().startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def source_of(stanza: str) -> str:
    """The source package: the Source field's name (it may carry a version), else the package itself."""
    source = field(stanza, "Source").split(" ")[0]
    return source or field(stanza, "Package")


def info_name(stanza: str) -> str:
    """How dpkg names this package's files in /var/lib/dpkg/info (libfoo1:arm64 for Multi-Arch: same)."""
    package = field(stanza, "Package")
    if field(stanza, "Multi-Arch").lower() == "same" and field(stanza, "Architecture"):
        return f"{package}:{field(stanza, 'Architecture')}"
    return package


def _under(root: Path, path: str) -> Path:
    return root / path.lstrip("/")


def _canonical(root: Path, path: str) -> str:
    """The same file, however it's spelled (/bin/x and /usr/bin/x when /bin is a symlink)."""
    full = _under(root, path)
    return str(Path(os.path.realpath(full.parent)) / full.name)


def _listed(info: Path, name: str) -> list[str]:
    listing = info / f"{name}.list"
    if not listing.exists():
        return []
    return [line for line in listing.read_text(encoding="utf-8", errors="surrogateescape").splitlines() if line]


def remove_sources(sources, root: Path = Path("/"), *, dry_run: bool = False) -> list[str]:
    """Remove every installed binary package built from these source packages.
    Returns the package names removed."""
    dpkg = root / "var/lib/dpkg"
    status_path, info = dpkg / "status", dpkg / "info"
    if not status_path.exists():
        raise SlimError(f"{status_path} doesn't exist: this isn't a Debian system.")
    wanted = set(sources)
    entries = stanzas(status_path.read_text(encoding="utf-8", errors="surrogateescape"))
    removed = [s for s in entries if source_of(s) in wanted]
    kept = [s for s in entries if source_of(s) not in wanted]
    names = [field(s, "Package") for s in removed]
    if dry_run or not removed:
        return names

    kept_paths = {_canonical(root, p) for s in kept for p in _listed(info, info_name(s))}
    directories: set[str] = set()
    for stanza in removed:
        for path in _listed(info, info_name(stanza)):
            if path == "/." or _canonical(root, path) in kept_paths:
                continue
            full = _under(root, path)
            if os.path.isdir(full):  # also true for a symlink to a directory, like /bin: never removed
                directories.add(path)
            elif os.path.lexists(full):
                os.unlink(full)
    # Directories they leave empty, deepest first. rmdir only ever removes empty ones.
    for path in sorted(directories, key=lambda p: p.count("/"), reverse=True):
        full = _under(root, path)
        if path.rstrip("/") in PROTECTED_DIRS or os.path.islink(full):
            continue
        try:
            os.rmdir(full)
        except OSError:
            pass

    # dpkg's own records: the info files (list, md5sums, scripts...) and the status entries.
    gone = {info_name(s) for s in removed}
    for entry in info.iterdir():
        if entry.name.rsplit(".", 1)[0] in gone:
            entry.unlink()
    temporary = status_path.with_name("status-new")
    temporary.write_text("".join(s + "\n\n" for s in kept), encoding="utf-8", errors="surrogateescape")
    os.replace(temporary, status_path)
    (dpkg / "status-old").unlink(missing_ok=True)  # the backup still lists them
    return names


# ---------------------------------------------------------------------------
# pip
# ---------------------------------------------------------------------------


def remove_pip() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "uninstall",
            "--yes",
            "--disable-pip-version-check",
            "setuptools",
            "wheel",
            "pip",
        ],
        check=True,
    )
    for wheel in glob.glob(os.path.join(sysconfig.get_paths()["stdlib"], "ensurepip", "_bundled", "*.whl")):
        os.unlink(wheel)
    scripts = Path(sysconfig.get_paths()["scripts"])
    for entry in scripts.iterdir():  # links like pip -> pip3 that now point nowhere
        if entry.is_symlink() and not entry.exists():
            entry.unlink()


def check_runtime(modules=THIRD_PARTY) -> None:
    program = f"MODULES = {tuple(modules)!r}\n" + CHECK
    subprocess.run([sys.executable, "-c", program], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docker-build", action="store_true", help="required: confirms this runs in the image build")
    parser.add_argument("--dry-run", action="store_true", help="only list the Debian packages that would go")
    args = parser.parse_args(argv)
    if args.dry_run:
        print("Would remove:", " ".join(remove_sources(REMOVE_SOURCES, dry_run=True)) or "nothing")
        return 0
    if not args.docker_build or os.geteuid() != 0 or not Path("/var/lib/dpkg/status").exists():
        print("This only runs inside the Docker image build (as root, with --docker-build).", file=sys.stderr)
        return 2
    remove_pip()
    names = remove_sources(REMOVE_SOURCES)
    print(f"Removed {len(names)} Debian packages the sync never uses: {' '.join(sorted(names))}")
    for leftover in (
        "/var/log/apt",
        "/var/cache/apt",
        "/var/lib/apt",
        "/var/log/dpkg.log",
        "/var/log/alternatives.log",
    ):
        target = Path(leftover)
        if target.is_dir() and not target.is_symlink():
            for path in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                path.rmdir() if path.is_dir() and not path.is_symlink() else path.unlink()
            target.rmdir()
        elif target.exists():
            target.unlink()
    check_runtime()
    Path(__file__).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
