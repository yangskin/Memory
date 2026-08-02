from __future__ import annotations

import fnmatch
import os
import sys
from pathlib import Path
from typing import Iterable

from .memory_config import MemoryConfig


class PathSecurityError(ValueError):
    pass


# Windows extended-length path prefix. ``Path.resolve()`` may produce paths
# with this prefix when the original string is long enough or when certain
# tmp / junction structures are involved (notably under pytest's tmp_path on
# some Windows configurations). Mixing prefixed and non-prefixed paths in
# the same comparison breaks ``Path.relative_to`` with ``ValueError``.
_WIN_LONG_PREFIX = "\\\\?\\"
_WIN_LONG_UNC_PREFIX = "\\\\?\\UNC\\"


def _strip_long_path_prefix(value: str) -> str:
    """Return ``value`` with any Windows ``\\\\?\\`` prefix stripped."""
    if sys.platform != "win32":
        return value
    if value.startswith(_WIN_LONG_UNC_PREFIX):
        # \\?\UNC\server\share\... → \\server\share\...
        return "\\\\" + value[len(_WIN_LONG_UNC_PREFIX):]
    if value.startswith(_WIN_LONG_PREFIX):
        return value[len(_WIN_LONG_PREFIX):]
    return value


def _normalize_path(path: Path) -> Path:
    """Resolve ``path`` and strip any ``\\\\?\\`` extended-length prefix.

    Used at every PathManager boundary so two paths that refer to the same
    on-disk file always compare equal regardless of which API produced
    them.
    """
    resolved_str = str(path.resolve())
    return Path(_strip_long_path_prefix(resolved_str))


def _is_within(path: Path, root: Path) -> bool:
    try:
        _normalize_path(path).relative_to(_normalize_path(root))
        return True
    except ValueError:
        return False


class PathManager:
    def __init__(self, config: MemoryConfig) -> None:
        self.config = config

    def resolve(
        self,
        path_value: str,
        *,
        must_exist: bool = True,
        must_be_file: bool = True,
    ) -> Path:
        # Reject pathological inputs early so they can't reach pathlib /
        # os.replace and raise unhelpful ValueError / OSError downstream.
        if not isinstance(path_value, str) or "\x00" in path_value:
            raise PathSecurityError(f"path contains illegal character: {path_value!r}")
        candidate = Path(path_value)
        if candidate.is_absolute():
            resolved = _normalize_path(candidate)
        else:
            resolved = _normalize_path(self.config.repo_root / candidate)

        if not any(_is_within(resolved, root) for root in self.config.allowed_roots):
            raise PathSecurityError(f"path is outside allowed_roots: {path_value}")
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"path does not exist: {path_value}")
        if must_be_file and resolved.exists() and not resolved.is_file():
            raise IsADirectoryError(f"path is not a file: {path_value}")
        return resolved

    def to_repo_relative(self, path: Path) -> str:
        normalized = _normalize_path(path)
        repo_root = _normalize_path(self.config.repo_root)
        return normalized.relative_to(repo_root).as_posix()

    def _is_excluded(self, repo_rel_path: str) -> bool:
        normalized = repo_rel_path.replace("\\", "/").strip("/")
        for excluded in self.config.excluded_dirs:
            prefix = excluded.strip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
        return False

    def _matches_patterns(self, repo_rel_path: str, patterns: list[str] | None) -> bool:
        if not patterns:
            return True
        normalized = repo_rel_path.replace("\\", "/")
        return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)

    def _scope_roots(self, scopes: list[str] | None) -> list[Path]:
        if not scopes:
            return [root for root in self.config.allowed_roots if root.exists()]
        resolved: list[Path] = []
        for scope in scopes:
            scope_path = self.resolve(scope, must_exist=True, must_be_file=False)
            resolved.append(scope_path)
        return resolved

    def iter_files(
        self,
        *,
        scopes: list[str] | None = None,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> Iterable[tuple[Path, str]]:
        for root in self._scope_roots(scopes):
            if root.is_file():
                rel_path = self.to_repo_relative(root)
                if self._is_excluded(rel_path):
                    continue
                if not self._matches_patterns(rel_path, include_paths):
                    continue
                if exclude_paths and self._matches_patterns(rel_path, exclude_paths):
                    continue
                yield root, rel_path
                continue

            for current_dir, dirs, files in os.walk(root):
                current = Path(current_dir)
                kept_dirs: list[str] = []
                for dir_name in dirs:
                    candidate_dir = _normalize_path(current / dir_name)
                    try:
                        rel_dir = self.to_repo_relative(candidate_dir)
                    except ValueError:
                        # Path leaked out of repo_root after normalization
                        # (symlink to elsewhere, or a stale ``\\?\`` mismatch
                        # we couldn't reconcile). Treat as excluded so the
                        # iteration never blows up downstream callers.
                        continue
                    if not self._is_excluded(rel_dir):
                        kept_dirs.append(dir_name)
                dirs[:] = kept_dirs

                for file_name in files:
                    abs_path = _normalize_path(current / file_name)
                    try:
                        rel_path = self.to_repo_relative(abs_path)
                    except ValueError:
                        continue
                    if self._is_excluded(rel_path):
                        continue
                    if not self._matches_patterns(rel_path, include_paths):
                        continue
                    if exclude_paths and self._matches_patterns(rel_path, exclude_paths):
                        continue
                    yield abs_path, rel_path


def resolve_user_path(config: MemoryConfig, path: str, user: str) -> str:
    """将 user_scoped 路径重定向到用户分区目录。

    例如：
        memory-bank/activeContext.md → memory-bank/activeContext/{user}.md

    当 path 在 user_scoped_paths 列表中时生效；多人安全策略始终开启。
    否则原样返回。

    自动迁移：如果旧的单文件存在且新的用户分区文件不存在，
    自动将旧文件内容复制到新路径（旧文件保留不删除，供其他用户迁移）。
    """
    if not config.multi_user:
        return path
    scoped_paths = config.multi_user.user_scoped_paths
    if not scoped_paths:
        return path

    normalized = path.replace("\\", "/").strip("/")
    for scoped in scoped_paths:
        scoped_norm = scoped.replace("\\", "/").strip("/")
        if scoped_norm == normalized or normalized.endswith(scoped_norm):
            # memory-bank/activeContext.md → memory-bank/activeContext/{user}.md
            stem = Path(scoped_norm).stem       # "activeContext"
            parent = str(Path(scoped_norm).parent).replace("\\", "/")  # "memory-bank"
            new_path = f"{parent}/{stem}/{user}.md"

            # 自动迁移：旧单文件 → 新用户分区文件
            old_abs = (config.repo_root / scoped_norm).resolve()
            new_abs = (config.repo_root / new_path).resolve()
            if old_abs.is_file() and not new_abs.exists():
                try:
                    new_abs.parent.mkdir(parents=True, exist_ok=True)
                    content = old_abs.read_text(encoding="utf-8", errors="replace")
                    # 历史归属警告：旧单文件可能由多人覆盖过，第一个登场的用户
                    # 不应被默认为唯一作者。加一条注释让人/工具明确这条
                    # 内容的真实归属未知，必须人工核对后再继续编辑。
                    banner = (
                        "<!-- migrated-from-shared: this content was copied from "
                        f"{scoped_norm} during multi-user activation. The original "
                        "file may have been edited by multiple users; attribution to "
                        f"'{user}' is NOT verified. Please review before continuing. -->\n\n"
                    )
                    new_abs.write_text(banner + content, encoding="utf-8")
                except OSError:
                    pass  # 迁移失败不阻塞正常流程

            return new_path
    return path
