"""跨 Agent 周日志；副本身份只保存在本地，不使用硬件或用户指纹。"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .memory_locks import file_lock


def replica_home(repo_root: Path) -> Path:
    """独立克隆各自持有身份，linked worktree 使用同一 git common dir。"""
    # 普通目录不必每次启动 Git 再等它失败；仍检查祖先以支持仓库内的子项目。
    root = repo_root.resolve()
    if not os.environ.get("GIT_DIR") and not any((parent / ".git").exists() for parent in (root, *root.parents)):
        return repo_root / ".ai-memory"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
    except FileNotFoundError:
        # 无 Git 的独立部署有显式本地身份目录。
        return repo_root / ".ai-memory"
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Git common directory discovery timed out") from exc
    if result.returncode == 0:
        return Path(result.stdout.strip()).resolve()
    if (repo_root / ".git").exists():
        raise RuntimeError("Git common directory could not be resolved")
    return repo_root / ".ai-memory"


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
