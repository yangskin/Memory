#!/usr/bin/env python3
"""Fail when the publishable source tree contains private project material.

The check intentionally scans both tracked files and source-like untracked files.
Local credentials and runtime state are skipped for content inspection, but fail
the check if Git tracks them.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCAL_ONLY_NAMES = {"llm_config.local.json", "user_config.local.json"}
IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "htmlcov",
}
FORBIDDEN_TRACKED_PARTS = {".ai-memory", ".ai-context", *IGNORED_DIRS}
TEXT_NAMES = {".gitignore", "LICENSE"}
TEXT_SUFFIXES = {
    ".bat",
    ".cfg",
    ".cmd",
    ".cpp",
    ".cs",
    ".h",
    ".hpp",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".pyi",
    ".rst",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

FORBIDDEN_CONTENT = {
    "private project id": re.compile(r"(?i)\bP1{2}[01]\b"),
    "private subsystem id": re.compile(r"(?i)(?:\bLJ[C](?:Editor)?\b|A?LJ[C]Domain)"),
    "private game title": re.compile(r"(?i)Cras[h]!?\s*Crash|CrashCras[h]"),
    "private feature fixture": re.compile(
        r"(?i)SpawnRuntimeRewardDropGrou[p]|Packed[ _-]?Chun[k]|PolygonGrou[p]|L_TestLeve[l]|AutoFractur[e]"
    ),
    "private repository path": re.compile(r"(?i)[A-Za-z]:[/\\](?:GIT|UGit)[/\\]P1{2}[01]\b"),
    "private identity": re.compile(r"(?i)\b(?:mengzhoyan[g]|yaominglon[g]|tian[y]|diy[e])\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "API token": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
}


def _tracked_files() -> set[Path]:
    try:
        top = Path(
            subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        ).resolve()
        raw = subprocess.check_output(
            ["git", "-C", str(top), "ls-files", "-z"],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return set()

    result: set[Path] = set()
    for value in raw.decode("utf-8", errors="strict").split("\0"):
        if not value:
            continue
        path = (top / value).resolve()
        try:
            path.relative_to(ROOT)
        except ValueError:
            continue
        if path.is_file():
            result.add(path)
    return result


def _source_files(tracked: set[Path]) -> set[Path]:
    files = set(tracked)
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.name in LOCAL_ONLY_NAMES:
            continue
        relative = path.relative_to(ROOT)
        if any(part in IGNORED_DIRS or part == ".ai-memory" for part in relative.parts):
            continue
        if path.name in TEXT_NAMES or path.suffix.lower() in TEXT_SUFFIXES:
            files.add(path)
    return files


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _audit_vendor() -> list[str]:
    vendor = ROOT / "vendor"
    manifest = vendor / "SHA256SUMS"
    wheels = {path.name: path for path in vendor.glob("*.whl")}
    if not manifest.is_file():
        return ["missing vendor/SHA256SUMS"]

    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            return ["invalid vendor/SHA256SUMS format"]
        expected[parts[1].strip()] = parts[0].lower()

    failures: list[str] = []
    if set(expected) != set(wheels):
        failures.append("vendor wheel set does not match SHA256SUMS")
    for name in sorted(set(expected).intersection(wheels)):
        digest = hashlib.sha256(wheels[name].read_bytes()).hexdigest()
        if digest != expected[name]:
            failures.append(f"vendor hash mismatch: {name}")
    return failures


def audit() -> list[str]:
    tracked = _tracked_files()
    failures: list[str] = []

    for path in sorted(tracked):
        relative = path.relative_to(ROOT)
        if path.name in LOCAL_ONLY_NAMES or any(part in FORBIDDEN_TRACKED_PARTS for part in relative.parts):
            failures.append(f"tracked local/runtime path: {relative.as_posix()}")

    for path in sorted(_source_files(tracked)):
        text = _read_text(path)
        if text is None:
            continue
        relative = path.relative_to(ROOT).as_posix()
        for label, pattern in FORBIDDEN_CONTENT.items():
            if pattern.search(text):
                failures.append(f"{label}: {relative}")

    failures.extend(_audit_vendor())
    return sorted(set(failures))


def main() -> int:
    failures = audit()
    if failures:
        print("Public-tree audit failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Public-tree audit passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
