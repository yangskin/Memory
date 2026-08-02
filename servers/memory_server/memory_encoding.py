from __future__ import annotations

import codecs
import hashlib
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_locks import file_lock
from .memory_paths import PathManager, PathSecurityError
from .memory_record_io import _atomic_write_text
from .memory_result import error_result, ok_result

_TEXT_SUFFIXES = {".md", ".json", ".jsonl", ".yaml", ".yml", ".txt"}
_MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "â€™", "ï»¿", "锟斤拷")
_REPAIR_MODES = {"utf8_bom", "latin1_mojibake", "cp1252_mojibake", "gb18030"}


def _atomic_write_bytes(target: Path, content: bytes, *, fsync_strict: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex[:8]}.tmp"
    descriptor = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                if fsync_strict:
                    raise
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _memory_text_paths(config: MemoryConfig) -> list[Path]:
    roots = [config.repo_root / "memory-bank", config.repo_root / ".ai-context", config.repo_root / ".ai-memory"]
    paths: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(config.repo_root)
            except ValueError:
                # rglob 可跟随文件符号链接；审计不得越过工作区读取链接目标。
                continue
            if any(part in {"backups", "locks", "cache"} for part in relative.parts):
                continue
            paths.append(resolved)
    config_path = config.config_path.resolve()
    if config_path.is_file() and config_path not in paths:
        paths.append(config_path)
    return sorted(set(paths))


def _audit_bytes(path: Path, raw: bytes, *, repo_root: Path) -> dict[str, Any]:
    try:
        rel = path.relative_to(repo_root).as_posix()
    except ValueError:
        rel = str(path)
    issues: list[dict[str, Any]] = []
    if raw.startswith(codecs.BOM_UTF8):
        issues.append({"code": "utf8_bom", "severity": "warning", "message": "UTF-8 BOM should be removed"})
    if b"\x00" in raw:
        issues.append({"code": "nul_byte", "severity": "error", "message": "text file contains NUL bytes"})
    try:
        text = raw.decode("utf-8-sig" if raw.startswith(codecs.BOM_UTF8) else "utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        issues.append(
            {
                "code": "invalid_utf8",
                "severity": "error",
                "message": str(exc),
                "byte_offset": exc.start,
            }
        )
        text = ""
    if "\ufffd" in text:
        issues.append(
            {
                "code": "replacement_character",
                "severity": "error",
                "message": "text contains U+FFFD replacement characters",
                "count": text.count("\ufffd"),
            }
        )
    markers = {marker: text.count(marker) for marker in _MOJIBAKE_MARKERS if marker in text}
    if markers:
        issues.append(
            {
                "code": "probable_mojibake",
                "severity": "warning",
                "message": "text contains common mojibake markers; repair requires an explicit codec mode",
                "markers": markers,
            }
        )
    return {"path": rel, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "issues": issues}


def audit_memory_encoding(config: MemoryConfig, *, paths: list[str] | None = None) -> dict[str, Any]:
    manager = PathManager(config)
    selected: list[Path] = []
    try:
        if paths:
            selected = [manager.resolve(path, must_exist=True, must_be_file=True) for path in paths]
        else:
            selected = _memory_text_paths(config)
    except (PathSecurityError, FileNotFoundError, IsADirectoryError) as exc:
        return error_result("invalid_path", str(exc))
    files: list[dict[str, Any]] = []
    read_errors: list[dict[str, str]] = []
    for path in selected:
        try:
            files.append(_audit_bytes(path, path.read_bytes(), repo_root=config.repo_root))
        except (OSError, ValueError) as exc:
            read_errors.append({"path": str(path), "error": str(exc)})
    issue_count = sum(len(item["issues"]) for item in files)
    errors = sum(1 for item in files for issue in item["issues"] if issue["severity"] == "error")
    warnings = issue_count - errors
    return ok_result(
        "memory encoding audit completed",
        files=files,
        stats={
            "scanned": len(files),
            "issues": issue_count,
            "errors": errors,
            "warnings": warnings,
            "read_errors": len(read_errors),
        },
        read_errors=read_errors,
        healthy=errors == 0 and not read_errors,
    )


def _repair_content(raw: bytes, mode: str) -> str:
    if mode == "utf8_bom":
        if not raw.startswith(codecs.BOM_UTF8):
            raise ValueError("file does not contain a UTF-8 BOM")
        return raw[len(codecs.BOM_UTF8) :].decode("utf-8", errors="strict")
    if mode == "gb18030":
        return raw.decode("gb18030", errors="strict")
    current = raw.decode("utf-8", errors="strict")
    source_codec = "latin-1" if mode == "latin1_mojibake" else "cp1252"
    return current.encode(source_codec, errors="strict").decode("utf-8", errors="strict")


def repair_memory_encoding(
    config: MemoryConfig,
    *,
    path: str,
    mode: str,
    apply: bool = False,
    if_match: str | None = None,
) -> dict[str, Any]:
    """Repair one file only after an explicit codec choice and optional CAS."""

    if mode not in _REPAIR_MODES:
        return error_result("invalid_mode", f"mode must be one of: {sorted(_REPAIR_MODES)}")
    manager = PathManager(config)
    try:
        target = manager.resolve(path, must_exist=True, must_be_file=True)
        raw = target.read_bytes()
        repaired = _repair_content(raw, mode)
        repaired.encode("utf-8", errors="strict")
    except (PathSecurityError, FileNotFoundError, IsADirectoryError) as exc:
        return error_result("invalid_path", str(exc))
    except (OSError, UnicodeError, ValueError) as exc:
        return error_result("repair_not_applicable", str(exc))
    # CAS 必须基于原始字节。有损的 errors="replace" 会让不同坏字节映射到
    # 相同 U+FFFD 文本，从而错误接受已经变化的文件。
    current_sha = hashlib.sha256(raw).hexdigest()
    repaired_bytes = repaired.encode("utf-8")
    payload = {
        "path": manager.to_repo_relative(target),
        "mode": mode,
        "dry_run": not apply,
        "before_sha": current_sha,
        "after_sha": hashlib.sha256(repaired_bytes).hexdigest(),
        "changed": raw != repaired_bytes,
        "candidate_content": repaired,
    }
    if not apply:
        return ok_result("encoding repair dry run completed", **payload)
    if if_match is not None and if_match != current_sha:
        return error_result("source_changed", "file changed after it was inspected", current_sha=current_sha)
    try:
        with file_lock(config.repo_root, target):
            latest_raw = target.read_bytes()
            if latest_raw != raw:
                return error_result("source_changed", "file changed before encoding repair commit")
            backup_name = (
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-"
                f"{hashlib.sha256(raw).hexdigest()[:12]}-{target.name}.bin"
            )
            backup_path = config.repo_root / ".ai-memory" / "backups" / "encoding" / backup_name
            _atomic_write_bytes(backup_path, raw, fsync_strict=config.mcp_fsync_strict)
            _atomic_write_text(target, repaired, fsync_strict=config.mcp_fsync_strict)
    except OSError as exc:
        return error_result("write_failed", f"failed to commit encoding repair: {exc}")
    payload.pop("candidate_content", None)
    return ok_result(
        "encoding repair applied",
        **payload,
        backup_path=backup_path.relative_to(config.repo_root).as_posix(),
        backup_sha256=hashlib.sha256(raw).hexdigest(),
    )


__all__ = ["audit_memory_encoding", "repair_memory_encoding"]
