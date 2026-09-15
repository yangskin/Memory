"""跨 Agent 周日志；副本身份只保存在本地，不使用硬件或用户指纹。"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .memory_locks import file_lock


def _metadata_path(base: Path, value: str) -> Path:
    if not value or any(char in value for char in "\0\r\n"):
        raise ValueError("Git directory metadata contains an invalid path")
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def _read_git_path(path: Path, prefix: str = "") -> str:
    # 只读取小型路径指针；损坏时不能换目录生成另一个副本身份。
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Git directory metadata exceeds the size limit")
    value = os.fsdecode(raw).rstrip("\r\n")
    if not value.startswith(prefix):
        raise ValueError("Git directory metadata has an invalid prefix")
    return value[len(prefix):]


def replica_home(repo_root: Path) -> Path:
    """直接解析 Git 元数据：同克隆 worktree 共用身份，写入不启动子进程。"""
    root = repo_root.resolve()
    explicit = os.environ.get("GIT_DIR")
    git_dir = _metadata_path(root, explicit) if explicit is not None else None
    if git_dir is None:
        ceilings = {Path(value).resolve() for value in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep)
                    if value and Path(value).is_absolute()}
        across = os.environ.get("GIT_DISCOVERY_ACROSS_FILESYSTEM", "false").lower()
        if across not in ("", "0", "false", "no", "off", "1", "true", "yes", "on"):
            raise ValueError("GIT_DISCOVERY_ACROSS_FILESYSTEM is not a valid boolean")
        device = root.stat().st_dev
        for parent in (root, *root.parents):
            if parent != root and (parent in ceilings or
                    (across not in ("1", "true", "yes", "on") and parent.stat().st_dev != device)):
                break
            marker = parent / ".git"
            # lexists 保证损坏的符号链接也会明确失败，不误判为独立部署。
            if os.path.lexists(marker):
                git_dir = marker
                break
    if git_dir is None:
        if "GIT_COMMON_DIR" in os.environ:
            raise RuntimeError("GIT_COMMON_DIR is set but no Git directory was found")
        return root / ".ai-memory"
    if git_dir.is_file():
        git_dir = _metadata_path(git_dir.parent, _read_git_path(git_dir, "gitdir: "))
    if not git_dir.is_dir() or not (git_dir / "HEAD").is_file():
        raise RuntimeError("Git directory metadata is missing or invalid")
    common = os.environ.get("GIT_COMMON_DIR")
    if common is not None:
        home = _metadata_path(root, common)
    elif os.path.lexists(git_dir / "commondir"):
        home = _metadata_path(git_dir, _read_git_path(git_dir / "commondir"))
    else:
        home = git_dir.resolve()
    if not home.is_dir():
        raise RuntimeError("Git common directory is missing or invalid")
    return home


def get_replica_id(repo_root: Path) -> str:
    home = replica_home(repo_root)
    home.mkdir(parents=True, exist_ok=True)
    target = home / "memory-replica-id"
    with file_lock(home, target):
        if target.exists():
            value = target.read_text(encoding="ascii").strip()
            if not re.fullmatch(r"[0-9a-f]{32}", value):
                raise ValueError("local Memory replica ID is invalid; restore it instead of silently changing identity")
            return value
        from .memory_record_io import _atomic_write_text
        value = uuid.uuid4().hex
        _atomic_write_text(target, value + "\n", fsync_strict=True)
        return value


def journal_path(single_path: str, *, author: str, scope: str, now: datetime,
                 replica_id: str, index: int) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", replica_id) or not 1 <= index <= 999:
        raise ValueError("invalid journal replica or volume")
    if not re.fullmatch(r"[a-z_]+", scope):
        raise ValueError("invalid journal scope")
    year, week, _ = now.astimezone(timezone.utc).isocalendar()
    # 共享目录及非 ASCII 作者的 slug 可能相同，额外摘要避免混装不同作者。
    author_key = hashlib.sha256(author.encode("utf-8")).hexdigest()[:16]
    # 旧维护器已跳过归档包目录；借用该稳定容器以免旧端搬走/重打包活跃周日志。
    # 活跃状态与可见范围始终来自逐记录元数据，不从目录名推断。
    return (Path("memory-bank/archive/record-packs/journal") / f"{scope}-{author_key}"
            / f"{year}-W{week:02d}" / f"{replica_id}-{index:03d}.md").as_posix()
