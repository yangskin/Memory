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
LOCAL_ONLY_NAMES = {"llm_config.local.json", "user_config.local.json", "shared_memory.local.json"}
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
    # 本项目 Hub 自己发的 token 格式。少了这条，前面几条通用模式一个都匹配不上，
    # "内容里有凭据"这类检查对我们唯一真正会签发的凭据形同不存在。
    "Memory Hub token": re.compile(r"\bmem_v\d+\.tok_[A-Za-z0-9._\-]{20,}"),
}


def _tracked_files(root: Path) -> tuple[set[Path], str | None]:
    """枚举 root 下被 Git 跟踪的文件。返回 (文件集, 错误说明)。

    两处都必须显式按 UTF-8 解码：Git 以 UTF-8 输出路径，而 `text=True` 会用本机
    locale（中文 Windows 上是 GBK）。仓库路径含非 ASCII 字符时，locale 解码会把
    toplevel 解成乱码，于是每个跟踪文件都落在 root 之外被过滤掉，
    "tracked local/runtime path" 这一整类检查静默变成零命中。

    枚举失败必须显式上报：静默返回空集会让门禁只剩内容检查，看起来仍然通过。
    """
    try:
        raw_top = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        )
        top = Path(raw_top.decode("utf-8").strip()).resolve()
        raw = subprocess.check_output(
            ["git", "-C", str(top), "ls-files", "-z"],
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return set(), "git executable not found"
    except subprocess.CalledProcessError:
        return set(), "not a Git working tree"
    except OSError as exc:
        return set(), f"could not run git: {exc}"
    except UnicodeDecodeError as exc:
        return set(), f"git printed a non-UTF-8 path: {exc}"

    result: set[Path] = set()
    try:
        entries = raw.decode("utf-8").split("\0")
    except UnicodeDecodeError as exc:
        return set(), f"git printed a non-UTF-8 path: {exc}"
    for value in entries:
        if not value:
            continue
        path = (top / value).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if path.is_file():
            result.add(path)
    return result, None


def _is_local_or_runtime(root: Path, path: Path) -> bool:
    """本地凭据与运行时状态：不做内容检查（被跟踪则由 `audit` 单独判定）。"""
    if path.name in LOCAL_ONLY_NAMES:
        return True
    relative = path.relative_to(root)
    return any(part in IGNORED_DIRS or part == ".ai-memory" for part in relative.parts)


def _source_files(root: Path, tracked: set[Path]) -> set[Path]:
    files = {path for path in tracked if not _is_local_or_runtime(root, path)}
    for path in root.rglob("*"):
        if not path.is_file() or _is_local_or_runtime(root, path):
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


def _audit_vendor(root: Path) -> list[str]:
    vendor = root / "vendor"
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


def audit(root: Path | None = None) -> list[str]:
    root = ROOT if root is None else root.resolve()
    tracked, tracked_error = _tracked_files(root)
    failures: list[str] = []

    if tracked_error is not None:
        failures.append(f"cannot enumerate tracked files: {tracked_error}")

    for path in sorted(tracked):
        relative = path.relative_to(root)
        is_local_config = path.name in LOCAL_ONLY_NAMES
        in_runtime_dir = any(part in FORBIDDEN_TRACKED_PARTS for part in relative.parts)
        if is_local_config or in_runtime_dir:
            failures.append(f"tracked local/runtime path: {relative.as_posix()}")

    for path in sorted(_source_files(root, tracked)):
        text = _read_text(path)
        if text is None:
            continue
        relative = path.relative_to(root).as_posix()
        for label, pattern in FORBIDDEN_CONTENT.items():
            if pattern.search(text):
                failures.append(f"{label}: {relative}")

    failures.extend(_audit_vendor(root))
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
