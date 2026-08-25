"""`scripts/check_public_tree.py` 的回归测试。

审计脚本是发布门禁：它决定私有项目材料会不会随公开树一起发出去。
脚本本身只依赖标准库（CI 在装依赖之前就要跑它），所以这里也只用标准库 + pytest。

注意：本文件也会被公开树审计扫描。所有违规样本必须用分片拼装，
源码里绝不能出现完整的禁止字符串，否则这个测试文件自己就会让门禁失败
（`TestScriptContract::test_the_real_tree_has_no_content_violations` 会抓到）。
审计脚本里的 `FORBIDDEN_CONTENT` 正则出于同样原因写成 `P1{2}[01]`、`mengzhoyan[g]` 这类形式。
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

MEMORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = MEMORY_ROOT / "scripts" / "check_public_tree.py"

# 分片拼装的违规样本，见模块 docstring。
IDENTITY = "mengzhoyan" + "g"
PROJECT_ID = "P1" + "11"
SUBSYSTEM_ID = "LJ" + "CEditor"
GAME_TITLE = "Crash" + "!Crash"
FEATURE_FIXTURE = "L_TestLeve" + "l"
REPO_PATH = "D:/Gi" + "t/" + PROJECT_ID
PRIVATE_KEY = "-----BEGIN PRIVATE KEY" + "-----"
GITHUB_TOKEN = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz01"
API_TOKEN = "sk" + "-" + "abcdefghijklmnopqrstuvwxyz01"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_check_public_tree_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_script = _load_script()


def _git(root: Path, *args: str) -> None:
    subprocess.check_call(
        ["git", "-C", str(root), *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_valid_vendor(root: Path) -> None:
    """审计要求 vendor/SHA256SUMS 与 wheel 集合完全一致。"""
    vendor = root / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    wheel = vendor / "example_pkg-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"not a real wheel, only needs a stable digest")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (vendor / "SHA256SUMS").write_text(f"{digest}  {wheel.name}\n", encoding="utf-8")


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """一棵干净、可通过审计的公开树（已 git init 并 add）。"""
    root = tmp_path / "memory"
    root.mkdir()
    _write(root / "README.md", "# Public readme\n\nNothing private here.\n")
    _write(root / "servers" / "memory_server" / "server.py", "def main() -> None:\n    pass\n")
    _write_valid_vendor(root)
    _git(root, "init")
    _git(root, "add", "-A")
    return root


def _labels(failures: list[str]) -> set[str]:
    return {failure.split(":", 1)[0] for failure in failures}


class TestCleanTree:
    def test_a_clean_tree_passes(self, tree: Path) -> None:
        assert audit_script.audit(tree) == []

    def test_untracked_source_files_are_still_scanned(self, tree: Path) -> None:
        # 未提交但看起来是源码的文件也必须被扫，否则"先写后提交"能绕过门禁。
        _write(tree / "notes.md", f"contact {IDENTITY} for details\n")
        assert audit_script.audit(tree) == ["private identity: notes.md"]

    def test_untracked_non_source_files_are_not_scanned(self, tree: Path) -> None:
        _write(tree / "scratch.log", f"{IDENTITY}\n")
        assert audit_script.audit(tree) == []


class TestForbiddenContent:
    @pytest.mark.parametrize(
        ("payload", "label"),
        [
            (f"owner is {IDENTITY}", "private identity"),
            (f"the {PROJECT_ID} project", "private project id"),
            (f"see {SUBSYSTEM_ID} for details", "private subsystem id"),
            (f"game title {GAME_TITLE}", "private game title"),
            (f"open {FEATURE_FIXTURE} now", "private feature fixture"),
            (f"path {REPO_PATH}/Source", "private repository path"),
            (PRIVATE_KEY, "private key"),
            (f"token {GITHUB_TOKEN}", "GitHub token"),
            (f"token {API_TOKEN}", "API token"),
        ],
    )
    def test_each_forbidden_pattern_is_detected(self, tree: Path, payload: str, label: str) -> None:
        _write(tree / "doc.md", payload + "\n")

        failures = audit_script.audit(tree)

        # 部分样本会同时命中多条规则（私有仓库路径本身就含私有项目 ID），
        # 所以断言目标标签命中、且所有命中都指向被检文件。
        assert label in _labels(failures)
        assert all(failure.endswith(": doc.md") for failure in failures)

    def test_detection_is_case_insensitive(self, tree: Path) -> None:
        _write(tree / "doc.md", f"Owner Is {IDENTITY.upper()}\n")
        assert audit_script.audit(tree) == ["private identity: doc.md"]

    def test_one_file_can_report_multiple_labels(self, tree: Path) -> None:
        _write(tree / "doc.md", f"{IDENTITY} works on {PROJECT_ID}\n")
        assert audit_script.audit(tree) == [
            "private identity: doc.md",
            "private project id: doc.md",
        ]

    def test_a_substring_of_an_identity_does_not_match(self, tree: Path) -> None:
        # 规则带 \b 词边界，避免把无关长单词误判成私有身份。
        _write(tree / "doc.md", f"prefix{IDENTITY}suffix\n")
        assert audit_script.audit(tree) == []

    def test_binary_files_are_skipped(self, tree: Path) -> None:
        # 含 NUL 的文件按二进制处理：避免把随机字节误判成命中。
        (tree / "blob.json").write_bytes(IDENTITY.encode() + b"\x00binary")
        assert audit_script.audit(tree) == []

    def test_non_utf8_files_are_skipped(self, tree: Path) -> None:
        (tree / "gbk.md").write_bytes(f"私有 {IDENTITY}".encode("gbk"))
        assert audit_script.audit(tree) == []


class TestTrackedLocalAndRuntimePaths:
    @pytest.mark.parametrize(
        "name",
        ["llm_config.local.json", "user_config.local.json", "shared_memory.local.json"],
    )
    def test_tracking_a_local_credential_file_fails(self, tree: Path, name: str) -> None:
        _write(tree / name, '{"user_name": "someone"}\n')
        _git(tree, "add", "-A")
        assert audit_script.audit(tree) == [f"tracked local/runtime path: {name}"]

    @pytest.mark.parametrize(
        "name",
        ["llm_config.local.json", "user_config.local.json", "shared_memory.local.json"],
    )
    def test_an_untracked_local_credential_file_is_allowed(self, tree: Path, name: str) -> None:
        # 本地凭据留在磁盘上是正常用法，只有被 Git 跟踪才算问题。
        _write(tree / name, '{"user_name": "someone"}\n')
        assert audit_script.audit(tree) == []

    @pytest.mark.parametrize("part", [".ai-memory", ".ai-context", ".venv", "__pycache__"])
    def test_tracking_runtime_directories_fails(self, tree: Path, part: str) -> None:
        _write(tree / part / "state.json", "{}\n")
        _git(tree, "add", "-Af")
        assert audit_script.audit(tree) == [f"tracked local/runtime path: {part}/state.json"]

    def test_a_tracked_local_file_reports_only_the_tracked_failure(self, tree: Path) -> None:
        """本地凭据文件跳过内容检查，只报被跟踪。

        脚本 docstring 承诺 "Local credentials and runtime state are skipped for
        content inspection, but fail the check if Git tracks them"。之前 `_source_files`
        以全部 tracked 文件为种子，本地文件仍被内容扫描，导致同一个文件既报被跟踪、
        又报内容命中。被跟踪本身已经让审计失败，重复的内容标签只是噪音。
        """
        _write(tree / "user_config.local.json", f'{{"project_id": "{PROJECT_ID}"}}\n')
        _git(tree, "add", "-A")

        failures = audit_script.audit(tree)

        assert failures == ["tracked local/runtime path: user_config.local.json"]
        assert "private project id" not in _labels(failures)

    def test_the_credential_pattern_still_covers_every_other_file(self, tree: Path) -> None:
        # Hub 自己签发的 token 格式必须被抓到，否则通用的 ghp_/sk- 模式一个都匹配不上它。
        _write(tree / "docs" / "notes.md", "token: mem_v1.tok_" + "b" * 40 + "\n")
        _git(tree, "add", "-A")

        assert "Memory Hub token" in _labels(audit_script.audit(tree))

    def test_a_local_config_name_is_rejected_at_any_relative_path(self, tree: Path) -> None:
        _write(tree / "servers" / "shared_memory.local.json", "{}\n")
        _git(tree, "add", "-A")

        assert audit_script.audit(tree) == [
            "tracked local/runtime path: servers/shared_memory.local.json"
        ]

    def test_a_tracked_runtime_dir_file_is_not_content_scanned(self, tree: Path) -> None:
        _write(tree / ".ai-memory" / "state.json", f'{{"owner": "{IDENTITY}"}}\n')
        _git(tree, "add", "-Af")

        failures = audit_script.audit(tree)

        assert failures == ["tracked local/runtime path: .ai-memory/state.json"]


class TestScopeBoundary:
    def test_files_outside_the_audit_root_are_ignored(self, tmp_path: Path) -> None:
        """Memory 以 subtree 形式嵌在宿主仓库里，宿主自己的私有文件不该让 Memory 审计失败。"""
        host = tmp_path / "host"
        root = host / "MCP" / "Memory"
        root.mkdir(parents=True)
        _write(root / "README.md", "# Public readme\n")
        _write_valid_vendor(root)
        _write(host / "HostPrivate.md", f"{IDENTITY} owns {PROJECT_ID} and {SUBSYSTEM_ID}\n")
        _git(host, "init")
        _git(host, "add", "-A")

        assert audit_script.audit(root) == []


class TestTrackedEnumerationCannotSilentlyDegrade:
    """跟踪枚举一旦失效，整个 "tracked local/runtime path" 类别都会变成零命中。

    这类退化必须显式报错，否则门禁只剩内容检查、看起来仍然通过。
    """

    def test_a_non_ascii_repository_path_still_finds_tracked_files(self, tmp_path: Path) -> None:
        # 仓库路径含中文时，若按本机 locale 解码 git 的 toplevel 输出就会得到乱码，
        # 每个跟踪文件都会被判定在 root 之外，跟踪类检查静默失效。
        root = tmp_path / "仓库-项目" / "memory"
        root.mkdir(parents=True)
        _write(root / "README.md", "# Public readme\n")
        _write_valid_vendor(root)
        _write(root / "user_config.local.json", '{"user_name": "someone"}\n')
        _git(root, "init")
        _git(root, "add", "-A")

        assert audit_script.audit(root) == [
            "tracked local/runtime path: user_config.local.json"
        ]

    def test_a_tree_outside_git_is_reported_not_silently_passed(self, tmp_path: Path) -> None:
        root = tmp_path / "plain"
        root.mkdir()
        _write(root / "README.md", "# Public readme\n")
        _write_valid_vendor(root)

        failures = audit_script.audit(root)

        assert failures == ["cannot enumerate tracked files: not a Git working tree"]

    def test_a_missing_git_executable_is_reported(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_git(*args: object, **kwargs: object) -> bytes:
            raise FileNotFoundError("git")

        monkeypatch.setattr(audit_script.subprocess, "check_output", _no_git)

        assert audit_script.audit(tree) == [
            "cannot enumerate tracked files: git executable not found"
        ]

    def test_non_utf8_git_output_is_reported(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bad_bytes(*args: object, **kwargs: object) -> bytes:
            return b"\xff\xfe not utf-8"

        monkeypatch.setattr(audit_script.subprocess, "check_output", _bad_bytes)

        failures = audit_script.audit(tree)

        assert len(failures) == 1
        assert failures[0].startswith("cannot enumerate tracked files: git printed a non-UTF-8 path")


class TestVendorManifest:
    def test_missing_manifest_fails(self, tree: Path) -> None:
        (tree / "vendor" / "SHA256SUMS").unlink()
        assert audit_script.audit(tree) == ["missing vendor/SHA256SUMS"]

    def test_hash_mismatch_fails(self, tree: Path) -> None:
        wheel = tree / "vendor" / "example_pkg-1.0.0-py3-none-any.whl"
        wheel.write_bytes(b"tampered payload")
        assert audit_script.audit(tree) == [
            "vendor hash mismatch: example_pkg-1.0.0-py3-none-any.whl"
        ]

    def test_an_unlisted_wheel_fails(self, tree: Path) -> None:
        (tree / "vendor" / "sneaky-9.9.9-py3-none-any.whl").write_bytes(b"unlisted")
        assert audit_script.audit(tree) == ["vendor wheel set does not match SHA256SUMS"]

    def test_a_listed_but_absent_wheel_fails(self, tree: Path) -> None:
        (tree / "vendor" / "example_pkg-1.0.0-py3-none-any.whl").unlink()
        assert audit_script.audit(tree) == ["vendor wheel set does not match SHA256SUMS"]

    def test_malformed_manifest_line_fails(self, tree: Path) -> None:
        (tree / "vendor" / "SHA256SUMS").write_text("only-one-column\n", encoding="utf-8")
        assert audit_script.audit(tree) == ["invalid vendor/SHA256SUMS format"]

    def test_blank_manifest_lines_are_tolerated(self, tree: Path) -> None:
        manifest = tree / "vendor" / "SHA256SUMS"
        body = manifest.read_text(encoding="utf-8").strip()
        manifest.write_text(f"\n{body}\n\n", encoding="utf-8")
        assert audit_script.audit(tree) == []

    def test_uppercase_digests_are_accepted(self, tree: Path) -> None:
        manifest = tree / "vendor" / "SHA256SUMS"
        digest, name = manifest.read_text(encoding="utf-8").split()
        manifest.write_text(f"{digest.upper()}  {name}\n", encoding="utf-8")
        assert audit_script.audit(tree) == []


class TestFailureReporting:
    def test_failures_are_deduplicated_and_sorted(self, tree: Path) -> None:
        _write(tree / "b.md", f"{PROJECT_ID}\n")
        _write(tree / "a.md", f"{PROJECT_ID}\n")
        assert audit_script.audit(tree) == [
            "private project id: a.md",
            "private project id: b.md",
        ]

    def test_main_returns_zero_on_a_clean_real_repo(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(audit_script, "ROOT", tree)
        assert audit_script.main() == 0
        assert "audit passed" in capsys.readouterr().out

    def test_main_returns_one_and_lists_failures(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(tree / "doc.md", f"{PROJECT_ID}\n")
        monkeypatch.setattr(audit_script, "ROOT", tree)

        assert audit_script.main() == 1

        out = capsys.readouterr().out
        assert "audit failed" in out
        assert "- private project id: doc.md" in out


class TestScriptContract:
    def test_the_script_imports_only_standard_library(self) -> None:
        """CI 在 pip install 之前就跑这个脚本，任何第三方 import 都会让门禁本身崩掉。"""
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        third_party = {"pytest", "pydantic", "mcp", "requests", "yaml", "httpx"}
        for line in source.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            top = stripped.split()[1].split(".")[0]
            assert top not in third_party, f"unexpected third-party import: {stripped}"

    def test_audit_defaults_to_the_real_memory_root(self) -> None:
        assert audit_script.ROOT == MEMORY_ROOT

    def test_the_real_tree_has_no_content_violations(self) -> None:
        """真实公开树的内容检查必须干净。

        这里刻意不断言整体通过：`tracked local/runtime path` 取决于本机 Git 索引状态，
        而内容类命中是源码问题，任何一条都必须为零。本测试文件自身也在扫描范围内，
        所以它同时守住"测试样本不得写成完整字面量"这条约束。
        """
        failures = [
            failure
            for failure in audit_script.audit()
            if not failure.startswith("tracked local/runtime path:")
        ]
        assert failures == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
