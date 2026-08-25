"""依赖守卫与降级 server 的回归测试。

这条链路只在环境已经损坏时才起作用，正常跑测时永远走不到，所以每个分支都必须由
单测覆盖 —— 否则它坏了没人知道，而坏掉的表现恰好是"MCP 不可用且没有诊断"。
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from servers.memory_server import dependency_fallback as fb
from servers.memory_server import dependency_guard as dg


# --------------------------------------------------------------- requirements 解析


class TestRequirementParsing:
    def test_bare_name_has_no_bounds(self) -> None:
        req = dg._parse_requirement_line("psutil")
        assert req == dg.Requirement("psutil", None, None, False)
        assert req.describe() is None

    def test_lower_bound_only(self) -> None:
        req = dg._parse_requirement_line("mcp>=1.29")
        assert req.name == "mcp"
        assert req.min_version == "1.29"
        assert req.max_version is None
        assert req.describe() == ">=1.29"

    def test_exclusive_upper_bound(self) -> None:
        req = dg._parse_requirement_line("mcp>=1.29,<2")
        assert (req.min_version, req.max_version, req.max_inclusive) == ("1.29", "2", False)
        assert req.describe() == ">=1.29,<2"

    def test_inclusive_upper_bound(self) -> None:
        req = dg._parse_requirement_line("anyio>=4.0,<=4.14.2")
        assert (req.min_version, req.max_version, req.max_inclusive) == ("4.0", "4.14.2", True)
        assert req.describe() == ">=4.0,<=4.14.2"

    def test_extras_and_markers_do_not_leak_into_the_name(self) -> None:
        assert dg._parse_requirement_line("uvicorn[standard]>=0.30").name == "uvicorn"
        assert dg._parse_requirement_line('psutil>=7.0; sys_platform == "win32"').name == "psutil"

    def test_inline_comment_is_stripped(self) -> None:
        req = dg._parse_requirement_line("mcp>=1.29,<2  # v2 is a breaking rewrite")
        assert req == dg.Requirement("mcp", "1.29", "2", False)

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "# only a comment",
            "-r other.txt",
            "--no-binary :all:",
            "https://example.invalid/pkg-1.0-py3-none-any.whl",
        ],
    )
    def test_non_requirement_lines_are_skipped(self, line: str) -> None:
        assert dg._parse_requirement_line(line) is None


class TestParseVersion:
    """PEP 440 版本解析。

    以前这里只认纯数字点分版本，其余一律返回 None，而调用方遇到 None 就跳过整条边界
    判断 —— 于是 `<2` 会放过 `2.0.0rc1`，恰好漏掉最该抓的那种环境。
    """

    @pytest.mark.parametrize(
        ("text", "base"),
        [
            ("1", (0, (1,))),
            ("1.29", (0, (1, 29))),
            ("1.29.0", (0, (1, 29))),  # 末尾 0 归一化：1.29 == 1.29.0
            ("2.0.0rc1", (0, (2,))),
            ("2.0.0.dev0", (0, (2,))),
            ("1.26.0.post1", (0, (1, 26))),
            ("1.29.0+local", (0, (1, 29))),
            ("v1.29.0", (0, (1, 29))),
            ("1!2.0", (1, (2,))),
        ],
    )
    def test_pep440_versions_parse(self, text: str, base: tuple) -> None:
        parsed = dg._parse_version(text)
        assert parsed is not None
        assert parsed.base == base

    def test_a_local_version_sorts_above_the_plain_release(self) -> None:
        # PEP 440：`1.0+local > 1.0`。两者相等时，等号包含的上界 `<=1.0` 会把带 local
        # 的版本判成"没超"。
        plain = dg._parse_version("1.0")
        local = dg._parse_version("1.0+abc")
        assert plain.key < local.key
        assert dg._exceeds_max(local, plain, inclusive=True) is True
        assert dg._exceeds_max(plain, plain, inclusive=True) is False

    def test_the_prerelease_rule_compares_epochs_too(self) -> None:
        # 预发布规则按 base version（epoch + release）比。只比 release 时，`<1!2` 会把
        # `2.0rc1` 当成"同一个 release 的预发布"而误判成越界。
        want = dg._parse_version("1!2")
        assert dg._exceeds_max(dg._parse_version("2.0rc1"), want, inclusive=False) is False
        assert dg._exceeds_max(dg._parse_version("1!2.0rc1"), want, inclusive=False) is True

    @pytest.mark.parametrize("text", ["", "abc", "1..2", "1.2.x", "not-a-version"])
    def test_unparseable_versions_are_refused(self, text: str) -> None:
        """宁可不比较也不误判：自造版本解析器给出的错误结论比没有结论更糟。"""
        assert dg._parse_version(text) is None

    def test_trailing_zeros_do_not_change_ordering(self) -> None:
        assert dg._parse_version("1.27").key == dg._parse_version("1.27.0").key
        assert dg._parse_version("1.27").key == dg._parse_version("1.27.0.0").key

    def test_release_channels_sort_in_pep440_order(self) -> None:
        ladder = ["1.0.dev1", "1.0a1", "1.0a2", "1.0b1", "1.0rc1", "1.0", "1.0.post1", "1.1"]
        keys = [dg._parse_version(text) for text in ladder]
        assert all(key is not None for key in keys)
        assert all(keys[i].key < keys[i + 1].key for i in range(len(keys) - 1))

    @pytest.mark.parametrize("text", ["2.0.0", "2.0.0rc1", "2.0.0.dev0", "2.1.0", "3!1.0"])
    def test_an_exclusive_upper_bound_also_excludes_prereleases(self, text: str) -> None:
        """PEP 440：`<V` 不允许 V 的预发布版。

        少了这条，`mcp<2` 会放过 `2.0.0rc1` —— 而 2.x 正是这条边界要挡的 breaking 重写。
        """
        have = dg._parse_version(text)
        want = dg._parse_version("2")
        assert have is not None and want is not None
        assert dg._exceeds_max(have, want, inclusive=False) is True

    @pytest.mark.parametrize("text", ["1.9.0", "1.9.0rc1", "1.29.0+local", "1.0"])
    def test_versions_below_the_bound_are_allowed(self, text: str) -> None:
        have = dg._parse_version(text)
        want = dg._parse_version("2")
        assert have is not None and want is not None
        assert dg._exceeds_max(have, want, inclusive=False) is False

    def test_an_inclusive_upper_bound_admits_the_bound_itself(self) -> None:
        have = dg._parse_version("2.0.0")
        want = dg._parse_version("2")
        assert dg._exceeds_max(have, want, inclusive=True) is False
        assert dg._exceeds_max(dg._parse_version("2.0.1"), want, inclusive=True) is True


# --------------------------------------------------------------- 体检


def _requirements(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "requirements.txt"
    path.write_text(body, encoding="utf-8")
    return path


def _fake_versions(monkeypatch: pytest.MonkeyPatch, installed: dict[str, str]) -> None:
    """替换 importlib.metadata.version，模拟任意已装状态。"""
    from importlib import metadata

    def fake_version(name: str) -> str:
        if name in installed:
            return installed[name]
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", fake_version)


class TestCheckDependencies:
    def test_healthy_environment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        req = _requirements(tmp_path, "mcp>=1.29,<2\n")
        _fake_versions(monkeypatch, {"mcp": "1.29.0"})

        report = dg.check_dependencies(req)
        assert report["status"] == "ok"
        assert report["installed"] == {"mcp": "1.29.0"}
        assert dg.format_report(report) == "dependencies ok (mcp==1.29.0)"

    def test_missing_distribution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        req = _requirements(tmp_path, "mcp>=1.29,<2\n")
        _fake_versions(monkeypatch, {})

        report = dg.check_dependencies(req)
        assert report["status"] == "missing"
        assert report["missing"] == [{"name": "mcp", "required": ">=1.29,<2"}]
        assert "missing" in dg.format_report(report)

    def test_version_below_the_lower_bound_is_outdated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _requirements(tmp_path, "mcp>=1.29,<2\n")
        _fake_versions(monkeypatch, {"mcp": "1.26.0"})

        report = dg.check_dependencies(req)
        assert report["status"] == "outdated"
        assert report["outdated"][0]["installed"] == "1.26.0"

    def test_version_at_the_exclusive_upper_bound_is_incompatible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """这是最该抓的环境：只比对下界会把已装 2.x 判成"依赖正常"。"""
        req = _requirements(tmp_path, "mcp>=1.29,<2\n")
        _fake_versions(monkeypatch, {"mcp": "2.0.0"})

        report = dg.check_dependencies(req)
        assert report["status"] == "incompatible"
        assert report["incompatible"][0]["installed"] == "2.0.0"
        assert "OUTSIDE" in dg.format_report(report)

    def test_inclusive_upper_bound_allows_the_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _requirements(tmp_path, "anyio>=4.0,<=4.14.2\n")
        _fake_versions(monkeypatch, {"anyio": "4.14.2"})
        assert dg.check_dependencies(req)["status"] == "ok"

    def test_missing_outranks_incompatible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _requirements(tmp_path, "mcp>=1.29,<2\npsutil>=7.0\n")
        _fake_versions(monkeypatch, {"mcp": "2.0.0"})

        report = dg.check_dependencies(req)
        assert report["status"] == "missing"
        assert report["incompatible"][0]["name"] == "mcp"

    def test_a_prerelease_below_the_floor_is_reported_as_outdated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PEP 440：1.29.0rc1 < 1.29，所以它不满足 >=1.29。以前这个版本解析不出来，
        # 整条边界判断被跳过，报成"依赖正常"。
        req = _requirements(tmp_path, "mcp>=1.29,<2\n")
        _fake_versions(monkeypatch, {"mcp": "1.29.0rc1"})

        report = dg.check_dependencies(req)
        assert report["status"] == "outdated"
        assert report["installed"]["mcp"] == "1.29.0rc1"

    @pytest.mark.parametrize("installed", ["2.0.0", "2.0.0rc1", "2.0.0.dev0", "2.1.0"])
    def test_a_major_version_above_the_bound_is_incompatible_even_as_a_prerelease(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: str
    ) -> None:
        req = _requirements(tmp_path, "mcp>=1.29,<2\n")
        _fake_versions(monkeypatch, {"mcp": installed})

        report = dg.check_dependencies(req)
        assert report["status"] == "incompatible"
        assert report["incompatible"][0]["installed"] == installed

    def test_a_local_version_label_does_not_trip_the_upper_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _requirements(tmp_path, "mcp>=1.29,<2\n")
        _fake_versions(monkeypatch, {"mcp": "1.29.0+local.1"})
        assert dg.check_dependencies(req)["status"] == "ok"

    def test_a_truly_unparseable_version_is_reported_but_not_judged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _requirements(tmp_path, "mcp>=1.29,<2\n")
        _fake_versions(monkeypatch, {"mcp": "not-a-version"})

        report = dg.check_dependencies(req)
        assert report["status"] == "ok"
        assert report["installed"]["mcp"] == "not-a-version"

    def test_an_environment_marker_is_not_mistaken_for_an_upper_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `python_version<3.12` 是 marker，不是 pkg 的上界。以前 3.12 会被当成上界，
        # 于是任何 4.x 都被误报成 incompatible。
        req = _requirements(tmp_path, 'psutil>=5.9;python_version<3.12\n')
        _fake_versions(monkeypatch, {"psutil": "7.1.0"})

        parsed = dg._parse_requirement_line('psutil>=5.9;python_version<3.12')
        assert parsed.max_version is None
        assert dg.check_dependencies(req)["status"] == "ok"

    def test_unreadable_requirements_degrade_to_unknown(self, tmp_path: Path) -> None:
        report = dg.check_dependencies(tmp_path / "nope.txt")
        assert report["status"] == "unknown"
        assert "could not read requirements" in report["reason"]
        assert "skipped" in dg.format_report(report)

    def test_requirements_without_pins_are_ok(self, tmp_path: Path) -> None:
        report = dg.check_dependencies(_requirements(tmp_path, "# nothing here\n-r other.txt\n"))
        assert report["status"] == "ok"

    def test_broken_metadata_counts_as_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """元数据损坏不能让自检本身抛异常。"""
        from importlib import metadata

        def boom(name: str) -> str:
            raise RuntimeError("corrupt dist-info")

        monkeypatch.setattr(metadata, "version", boom)
        report = dg.check_dependencies(_requirements(tmp_path, "mcp>=1.29\n"))
        assert report["status"] == "missing"
        assert "RuntimeError" in report["missing"][0]["error"]

    def test_the_shipped_requirements_file_is_parsable(self) -> None:
        """守卫必须能读懂本组件真正在用的那份 requirements。"""
        report = dg.check_dependencies()
        assert report["status"] in {"ok", "missing", "outdated", "incompatible"}
        assert report["checked"] >= 1


# --------------------------------------------------------------- 离线 wheel 校验


def _vendor(tmp_path: Path, wheels: dict[str, bytes], *, sums: bool = True) -> Path:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    lines = []
    for name, payload in wheels.items():
        (vendor / name).write_bytes(payload)
        lines.append(f"{hashlib.sha256(payload).hexdigest()}  {name}")
    if sums:
        (vendor / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return vendor


class TestVerifyVendor:
    def test_matching_wheel_set_verifies(self, tmp_path: Path) -> None:
        vendor = _vendor(tmp_path, {"a-1.0-py3-none-any.whl": b"payload"})
        ok, note = dg.verify_vendor(vendor)
        assert ok is True
        assert "verified 1 wheel" in note

    def test_absent_vendor_directory(self, tmp_path: Path) -> None:
        ok, note = dg.verify_vendor(tmp_path / "missing")
        assert (ok, note) == (False, "vendor/ is not present")

    def test_missing_manifest(self, tmp_path: Path) -> None:
        vendor = _vendor(tmp_path, {"a-1.0-py3-none-any.whl": b"payload"}, sums=False)
        ok, note = dg.verify_vendor(vendor)
        assert (ok, note) == (False, "vendor/SHA256SUMS is missing")

    def test_tampered_wheel_is_refused(self, tmp_path: Path) -> None:
        vendor = _vendor(tmp_path, {"a-1.0-py3-none-any.whl": b"payload"})
        (vendor / "a-1.0-py3-none-any.whl").write_bytes(b"tampered")
        ok, note = dg.verify_vendor(vendor)
        assert ok is False
        assert "does not match its recorded digest" in note

    def test_listed_but_absent_wheel_is_refused(self, tmp_path: Path) -> None:
        vendor = _vendor(tmp_path, {"a-1.0-py3-none-any.whl": b"payload"})
        (vendor / "a-1.0-py3-none-any.whl").unlink()
        ok, note = dg.verify_vendor(vendor)
        assert ok is False
        assert "listed but missing" in note

    def test_untracked_wheel_is_refused(self, tmp_path: Path) -> None:
        """多余 wheel 会让 --no-index 装到未记录的版本。"""
        vendor = _vendor(tmp_path, {"a-1.0-py3-none-any.whl": b"payload"})
        (vendor / "b-2.0-py3-none-any.whl").write_bytes(b"stranger")
        ok, note = dg.verify_vendor(vendor)
        assert ok is False
        assert "untracked wheel" in note

    def test_malformed_manifest_line_is_refused(self, tmp_path: Path) -> None:
        vendor = _vendor(tmp_path, {"a-1.0-py3-none-any.whl": b"payload"})
        (vendor / "SHA256SUMS").write_text("garbage\n", encoding="utf-8")
        ok, note = dg.verify_vendor(vendor)
        assert ok is False
        assert "malformed" in note

    def test_empty_manifest_is_refused(self, tmp_path: Path) -> None:
        vendor = _vendor(tmp_path, {})
        (vendor / "SHA256SUMS").write_text("# nothing\n", encoding="utf-8")
        ok, note = dg.verify_vendor(vendor)
        assert (ok, note) == (False, "SHA256SUMS lists no wheels")

    def test_the_shipped_vendor_set_verifies(self) -> None:
        """随组件发布的离线 wheel 集必须自洽，否则断网机器修不了。"""
        ok, note = dg.verify_vendor()
        assert ok is True, note


# --------------------------------------------------------------- 修复


class _PipRecorder:
    """记录 pip 调用顺序，并按脚本给出结果。"""

    def __init__(self, results: list[tuple[bool, str]]) -> None:
        self.calls: list[list[str]] = []
        self._results = list(results)

    def __call__(self, args: list[str], timeout: float) -> tuple[bool, str]:
        self.calls.append(args)
        return self._results.pop(0) if self._results else (False, "unexpected pip call")


class TestRepair:
    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.setattr(dg, "in_expected_venv", lambda: True)

    def test_refuses_outside_a_virtual_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """往共享解释器里装东西会影响别的项目，用户也没要求过。"""
        monkeypatch.setattr(dg, "is_isolated_env", lambda: False)
        recorder = _PipRecorder([])
        monkeypatch.setattr(dg, "_run_pip", recorder)

        result = dg.repair(requirements=_requirements(tmp_path, "mcp>=1.29\n"))
        assert result["attempted"] is False
        assert result["repaired"] is False
        assert "not a virtual environment" in result["error"]
        assert recorder.calls == []

    def test_absent_requirements_file_is_reported(self, tmp_path: Path) -> None:
        result = dg.repair(requirements=tmp_path / "nope.txt")
        assert result["attempted"] is False
        assert "requirements file not found" in result["error"]

    def test_offline_wheels_are_tried_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _requirements(tmp_path, "mcp>=1.29\n")
        vendor = _vendor(tmp_path, {"mcp-1.29.0-py3-none-any.whl": b"wheel"})
        recorder = _PipRecorder([(True, "ok")])
        monkeypatch.setattr(dg, "_run_pip", recorder)

        result = dg.repair(requirements=req, vendor_dir=vendor)
        assert result["repaired"] is True
        assert result["method"] == "offline"
        assert len(recorder.calls) == 1
        assert "--no-index" in recorder.calls[0]
        assert str(vendor) in recorder.calls[0]

    def test_pypi_is_the_fallback_when_offline_install_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _requirements(tmp_path, "mcp>=1.29\n")
        vendor = _vendor(tmp_path, {"mcp-1.29.0-py3-none-any.whl": b"wheel"})
        recorder = _PipRecorder([(False, "pip exit=1: boom"), (True, "ok")])
        monkeypatch.setattr(dg, "_run_pip", recorder)

        result = dg.repair(requirements=req, vendor_dir=vendor)
        assert result["repaired"] is True
        assert result["method"] == "online"
        assert "--no-index" in recorder.calls[0]
        assert "--no-index" not in recorder.calls[1]

    def test_unusable_vendor_skips_straight_to_pypi(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _requirements(tmp_path, "mcp>=1.29\n")
        recorder = _PipRecorder([(True, "ok")])
        monkeypatch.setattr(dg, "_run_pip", recorder)

        result = dg.repair(requirements=req, vendor_dir=tmp_path / "absent")
        assert result["method"] == "online"
        assert len(recorder.calls) == 1
        assert result["steps"][0] == {
            "step": "verify_vendor",
            "ok": False,
            "detail": "vendor/ is not present",
        }

    def test_network_can_be_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _requirements(tmp_path, "mcp>=1.29\n")
        recorder = _PipRecorder([])
        monkeypatch.setattr(dg, "_run_pip", recorder)

        result = dg.repair(requirements=req, vendor_dir=tmp_path / "absent", allow_network=False)
        assert result["repaired"] is False
        assert "network fallback is disabled" in result["error"]
        assert recorder.calls == []

    def test_both_paths_failing_is_reported_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _requirements(tmp_path, "mcp>=1.29\n")
        vendor = _vendor(tmp_path, {"mcp-1.29.0-py3-none-any.whl": b"wheel"})
        monkeypatch.setattr(dg, "_run_pip", _PipRecorder([(False, "offline boom"), (False, "online boom")]))

        result = dg.repair(requirements=req, vendor_dir=vendor)
        assert result["repaired"] is False
        assert "both offline and online repair failed" in result["error"]
        assert [step["ok"] for step in result["steps"]] == [True, False, False]


class TestRunPip:
    def test_timeout_becomes_a_diagnosable_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """pip 卡住不能把客户端的 initialize 握手永久挂住。"""

        def fake_run(*args: Any, **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="pip", timeout=kwargs.get("timeout", 1))

        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, detail = dg._run_pip(["install", "x"], timeout=7.0)
        assert ok is False
        assert "pip timed out after 7s" in detail

    def test_missing_interpreter_becomes_a_diagnosable_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> Any:
            raise OSError("no such file")

        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, detail = dg._run_pip(["install", "x"], timeout=1.0)
        assert ok is False
        assert "could not run pip" in detail

    def test_pip_failure_keeps_the_output_tail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Proc:
            returncode = 1
            stdout = "line1\nline2\n"
            stderr = "ERROR: no matching distribution\n"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Proc())
        ok, detail = dg._run_pip(["install", "x"], timeout=1.0)
        assert ok is False
        assert "no matching distribution" in detail


# --------------------------------------------------------------- 编排与闸门


class TestProbeImports:
    """元数据齐全 ≠ 装得能用，所以放行前必须真的 import 一次。"""

    def test_no_modules_to_probe_is_ok(self) -> None:
        assert dg.probe_imports(()) == (True, None)

    def test_an_importable_module_passes(self) -> None:
        assert dg.probe_imports(("json", "pathlib")) == (True, None)

    def test_an_absent_module_is_reported(self) -> None:
        ok, detail = dg.probe_imports(("definitely_not_installed_xyz",))
        assert ok is False
        assert "ModuleNotFoundError" in detail
        assert "definitely_not_installed_xyz" in detail

    def test_a_module_raising_at_import_time_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """实测踩到的形态：分发装上了，但 import 时因为缺路径/缺 DLL 而抛。"""
        (tmp_path / "explodes_on_import.py").write_text(
            "raise ImportError('No module named \\'pywintypes\\'')\n", encoding="utf-8"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        ok, detail = dg.probe_imports(("explodes_on_import",))
        assert ok is False
        assert "pywintypes" in detail

    def test_the_real_mcp_entry_point_is_the_default(self) -> None:
        """默认探测的必须是 server 紧接着要导入的那个模块，否则探测没有意义。"""
        import inspect

        signature = inspect.signature(dg.ensure_dependencies)
        assert signature.parameters["probe_modules"].default == ("mcp.server",)


class TestEnsureDependencies:
    @pytest.fixture(autouse=True)
    def _no_inherited_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(dg._ATTEMPT_ENV, raising=False)
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)

    def test_metadata_ok_but_unusable_install_is_not_declared_healthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """踩过的坑：pywin32 装好了但 .pth 本进程未生效，元数据看起来完全正常。"""
        req = _requirements(tmp_path, "mcp>=1.29,<2\n")
        _fake_versions(monkeypatch, {"mcp": "1.29.0"})
        monkeypatch.setattr(dg, "probe_imports", lambda modules: (False, "import mcp.server failed"))

        outcome = dg.ensure_dependencies(
            req, allow_repair=False, state_file=tmp_path / "state.json"
        )
        assert outcome["ok"] is False
        assert outcome["import_error"] == "import mcp.server failed"
        assert outcome["blocked_reason"] == "auto-repair disabled"

    def test_site_packages_are_reprocessed_after_a_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """不补做 site 处理，刚装的 `.pth` 路径在本进程里永远看不到。

        这一步现在归 `repair()` 管，所有调用方（自动启动、兜底工具、`--repair`）因此都
        自动带上它 —— 原先每个调用方各自补一次，兜底工具那份就漏了。
        """
        calls: list[str] = []
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.setattr(dg, "in_expected_venv", lambda: True)
        monkeypatch.setattr(dg, "ensure_pip", lambda *a, **k: (True, "present", False))
        monkeypatch.setattr(dg, "verify_vendor", lambda _v: (True, "verified"))
        monkeypatch.setattr(dg, "_run_pip", lambda *a, **k: (True, "ok"))
        monkeypatch.setattr(dg, "_activate_site_packages", lambda: calls.append("site"))
        monkeypatch.setattr(dg, "probe_imports", lambda modules: (True, None))

        result = dg.repair(
            requirements=_requirements(tmp_path, "mcp>=1.29\n"),
            verify_modules=("mcp.server",),
        )

        assert result["repaired"]
        assert calls == ["site"]

    def test_a_repair_that_installs_but_stays_unimportable_is_not_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _requirements(tmp_path, "mcp>=1.29\n")
        installed: dict[str, str] = {}
        _fake_versions(monkeypatch, installed)
        monkeypatch.setattr(dg, "probe_imports", lambda modules: (False, "import mcp.server failed"))
        monkeypatch.setattr(
            dg,
            "repair",
            lambda *a, **k: (installed.__setitem__("mcp", "1.29.0"),
                             {"attempted": True, "repaired": True, "method": "offline", "steps": []})[1],
        )

        outcome = dg.ensure_dependencies(req, state_file=tmp_path / "state.json")
        assert outcome["after"]["status"] == "ok"
        assert outcome["ok"] is False
        assert outcome["import_error"] == "import mcp.server failed"

    def test_healthy_environment_does_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """健康路径不能起子进程、不能碰网络，否则每次启动都变慢。"""
        req = _requirements(tmp_path, "mcp>=1.29,<2\n")
        _fake_versions(monkeypatch, {"mcp": "1.29.0"})
        monkeypatch.setattr(dg, "repair", lambda *a, **k: pytest.fail("must not repair"))

        outcome = dg.ensure_dependencies(req, state_file=tmp_path / "state.json")
        assert outcome["ok"] is True
        assert outcome["repair"] is None
        assert not (tmp_path / "state.json").exists()

    def test_inconclusive_check_does_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """读不到 requirements 时不该乱装包。"""
        monkeypatch.setattr(dg, "repair", lambda *a, **k: pytest.fail("must not repair"))
        outcome = dg.ensure_dependencies(tmp_path / "nope.txt", state_file=tmp_path / "state.json")
        assert outcome["ok"] is True

    def test_repairs_then_rechecks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        req = _requirements(tmp_path, "mcp>=1.29,<2\n")
        state = tmp_path / "state.json"
        installed: dict[str, str] = {}
        _fake_versions(monkeypatch, installed)

        def fake_repair(*args: Any, **kwargs: Any) -> dict[str, Any]:
            installed["mcp"] = "1.29.0"
            return {"attempted": True, "repaired": True, "method": "offline", "steps": []}

        monkeypatch.setattr(dg, "repair", fake_repair)

        outcome = dg.ensure_dependencies(req, state_file=state)
        assert outcome["ok"] is True
        assert outcome["before"]["status"] == "missing"
        assert outcome["after"]["status"] == "ok"
        assert json.loads(state.read_text(encoding="utf-8"))["last_result"] == "ok"

    def test_failed_repair_is_reported_and_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _requirements(tmp_path, "mcp>=1.29,<2\n")
        state = tmp_path / "state.json"
        _fake_versions(monkeypatch, {})
        monkeypatch.setattr(
            dg,
            "repair",
            lambda *a, **k: {"attempted": True, "repaired": False, "method": None,
                             "steps": [], "error": "pip exploded"},
        )

        outcome = dg.ensure_dependencies(req, state_file=state)
        assert outcome["ok"] is False
        assert outcome["after"]["status"] == "missing"
        recorded = json.loads(state.read_text(encoding="utf-8"))
        assert recorded["last_result"] == "failed"
        assert recorded["last_error"] == "pip exploded"

    def test_repair_can_be_disabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_versions(monkeypatch, {})
        monkeypatch.setattr(dg, "repair", lambda *a, **k: pytest.fail("must not repair"))

        outcome = dg.ensure_dependencies(
            _requirements(tmp_path, "mcp>=1.29\n"),
            allow_repair=False,
            state_file=tmp_path / "state.json",
        )
        assert outcome["ok"] is False
        assert outcome["blocked_reason"] == "auto-repair disabled"

    def test_shared_interpreter_is_only_diagnosed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dg, "is_isolated_env", lambda: False)
        _fake_versions(monkeypatch, {})
        monkeypatch.setattr(dg, "repair", lambda *a, **k: pytest.fail("must not repair"))

        outcome = dg.ensure_dependencies(
            _requirements(tmp_path, "mcp>=1.29\n"),
            state_file=tmp_path / "state.json",
        )
        assert outcome["ok"] is False
        assert "not a virtual environment" in outcome["blocked_reason"]

    def test_one_attempt_per_process_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """客户端反复重启起不来的 server，没有这条闸门就会不停装包。"""
        monkeypatch.setenv(dg._ATTEMPT_ENV, "1")
        _fake_versions(monkeypatch, {})
        monkeypatch.setattr(dg, "repair", lambda *a, **k: pytest.fail("must not repair"))

        outcome = dg.ensure_dependencies(
            _requirements(tmp_path, "mcp>=1.29\n"),
            state_file=tmp_path / "state.json",
        )
        assert outcome["ok"] is False
        assert "already attempted" in outcome["blocked_reason"]

    def test_cooldown_blocks_a_rapid_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        state = tmp_path / "state.json"
        state.write_text(
            json.dumps({"last_attempt_at": time.time() - 10, "last_result": "failed"}),
            encoding="utf-8",
        )
        _fake_versions(monkeypatch, {})
        monkeypatch.setattr(dg, "repair", lambda *a, **k: pytest.fail("must not repair"))

        outcome = dg.ensure_dependencies(
            _requirements(tmp_path, "mcp>=1.29\n"),
            state_file=state,
        )
        assert outcome["ok"] is False
        assert "waiting" in outcome["blocked_reason"]

    def test_a_previously_successful_state_does_not_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        state = tmp_path / "state.json"
        state.write_text(
            json.dumps({"last_attempt_at": time.time(), "last_result": "ok"}),
            encoding="utf-8",
        )
        _fake_versions(monkeypatch, {})
        calls: list[int] = []
        monkeypatch.setattr(
            dg,
            "repair",
            lambda *a, **k: (calls.append(1), {"attempted": True, "repaired": False, "steps": []})[1],
        )

        dg.ensure_dependencies(_requirements(tmp_path, "mcp>=1.29\n"), state_file=state)
        assert calls == [1]

    def test_clock_moving_backwards_does_not_lock_forever(self) -> None:
        import time

        state = {"last_attempt_at": time.time() + 10_000, "last_result": "failed"}
        assert dg._cooldown_remaining(state, time.time()) == 0.0

    def test_corrupt_state_file_is_ignored(self, tmp_path: Path) -> None:
        state = tmp_path / "state.json"
        state.write_text("{not json", encoding="utf-8")
        assert dg._read_state(state) == {}


class TestEnsureReady:
    def test_healthy_environment_returns_and_lets_startup_continue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dg, "ensure_dependencies", lambda *a, **k: {"ok": True})
        monkeypatch.setattr(
            dg,
            "_log",
            lambda message: pytest.fail(f"healthy startup must stay quiet: {message}"),
        )
        assert dg.ensure_ready("generic-memory-mcp") == {"ok": True}

    def test_broken_environment_hands_off_to_the_degraded_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """修不好时必须让客户端连上一个能说清问题的 server，而不是抛 import 异常。"""
        served: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(dg, "ensure_dependencies", lambda *a, **k: {"ok": False})
        monkeypatch.setattr(fb, "serve_diagnostics", lambda name, outcome: served.append((name, outcome)))

        with pytest.raises(SystemExit) as exc:
            dg.ensure_ready("generic-memory-mcp")
        assert exc.value.code == 0
        assert served[0][0] == "generic-memory-mcp"

    def test_fallback_server_can_be_disabled_for_debugging(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMORY_MCP_NO_FALLBACK_SERVER", "1")
        monkeypatch.setattr(dg, "ensure_dependencies", lambda *a, **k: {"ok": False})
        monkeypatch.setattr(
            fb, "serve_diagnostics", lambda *a, **k: pytest.fail("must not serve")
        )
        assert dg.ensure_ready("generic-memory-mcp")["ok"] is False

    def test_auto_repair_can_be_disabled_by_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_ensure(requirements: Any = None, **kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {"ok": True}

        monkeypatch.setenv("MEMORY_MCP_NO_AUTO_REPAIR", "1")
        monkeypatch.setenv("MEMORY_MCP_NO_NETWORK_REPAIR", "1")
        monkeypatch.setattr(dg, "ensure_dependencies", fake_ensure)

        dg.ensure_ready("generic-memory-mcp")
        assert seen == {"allow_repair": False, "allow_network": False}


# --------------------------------------------------------------- 降级 server 协议


def _rpc(message: dict[str, Any], outcome: dict[str, Any] | None = None) -> dict[str, Any] | None:
    return fb._handle(message, "generic-memory-mcp", outcome if outcome is not None else {})


class TestDegradedServerProtocol:
    def test_initialize_flags_the_degraded_mode_in_the_server_name(self) -> None:
        """`-degraded` 后缀是给启动探测器用的机器可读信号。"""
        result = _rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]
        assert result["serverInfo"]["name"] == "generic-memory-mcp-degraded"
        assert result["protocolVersion"] == fb.PROTOCOL_VERSION
        assert "DEPENDENCY REPAIR MODE" in result["instructions"]

    def test_instructions_warn_against_concluding_memory_is_empty(self) -> None:
        """降级时 LLM 最危险的误判是"记忆里没有内容"，而不是"工具不可用"。"""
        assert "do not conclude" in fb._DEGRADED_BANNER.lower()

    def test_tools_list_exposes_diagnosis_and_repair(self) -> None:
        tools = _rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        assert [t["name"] for t in tools] == [
            "memory_environment_status",
            "memory_repair_environment",
        ]
        for tool in tools:
            assert tool["inputSchema"]["type"] == "object"

    def test_status_tool_reports_the_diagnosis(self, tmp_path: Path) -> None:
        outcome = {
            "ok": False,
            "before": {
                "status": "incompatible",
                "interpreter": "python.exe",
                "python_version": "3.11.8",
                "requirements": str(tmp_path / "requirements.txt"),
                "incompatible": [{"name": "mcp", "installed": "2.0.0", "required": ">=1.29,<2"}],
                "repair_hint": "run deploy",
            },
            "repair": {
                "attempted": True,
                "repaired": False,
                "method": None,
                "error": "pip exploded",
                "steps": [{"step": "verify_vendor", "ok": True, "detail": "verified 31 wheel(s)"}],
            },
        }
        response = _rpc(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "memory_environment_status", "arguments": {}}},
            outcome,
        )
        text = response["result"]["content"][0]["text"]
        assert response["result"]["isError"] is False
        assert "2.0.0" in text
        assert "pip exploded" in text
        assert "verified 31 wheel(s)" in text
        assert "restarted" in text

    def test_status_tool_explains_an_unusable_but_complete_install(self) -> None:
        """只报 status=ok 会让人以为依赖没问题，必须点明 import 失败。"""
        outcome = {
            "ok": False,
            "before": {"status": "ok", "installed": {"mcp": "1.29.0"},
                       "interpreter": "python.exe", "requirements": "requirements.txt"},
            "import_error": "import mcp.server failed: ModuleNotFoundError: No module named 'pywintypes'",
        }
        response = _rpc(
            {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
             "params": {"name": "memory_environment_status", "arguments": {}}},
            outcome,
        )
        text = response["result"]["content"][0]["text"]
        assert "import check: FAILED" in text
        assert "pywintypes" in text
        assert ".pth" in text

    def test_status_tool_survives_an_empty_outcome(self) -> None:
        response = _rpc(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "memory_environment_status", "arguments": {}}},
            {},
        )
        assert response["result"]["isError"] is False
        assert "no report available" in response["result"]["content"][0]["text"]

    def test_repair_tool_bypasses_the_cooldown_and_reports_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            dg, "repair", lambda **kwargs: {"repaired": True, "method": "offline", "steps": []}
        )
        monkeypatch.setattr(fb, "check_dependencies", lambda *a, **k: {"status": "ok", "installed": {}})

        outcome: dict[str, Any] = {"ok": False}
        response = _rpc(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "memory_repair_environment", "arguments": {}}},
            outcome,
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        assert response["result"]["isError"] is False
        assert payload["repaired"] is True
        assert "Restart this MCP server" in payload["next_step"]
        # 修好后就地更新结论，后续 status 不再报旧问题。
        assert outcome["ok"] is True

    def test_repair_tool_reports_failure_as_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            dg,
            "repair",
            lambda **kwargs: {"repaired": False, "method": None, "steps": [], "error": "boom"},
        )
        monkeypatch.setattr(
            fb,
            "check_dependencies",
            lambda *a, **k: {"status": "missing", "missing": [{"name": "mcp", "required": ">=1.29"}],
                             "interpreter": "python.exe", "repair_hint": "run deploy"},
        )

        response = _rpc(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "memory_repair_environment", "arguments": {}}},
            {"ok": False},
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        assert response["result"]["isError"] is True
        assert payload["repaired"] is False
        assert "do not retry in a loop" in payload["next_step"]

    def test_repair_tool_forwards_the_network_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def fake_repair(**kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {"repaired": True, "method": "offline", "steps": []}

        monkeypatch.setattr(dg, "repair", fake_repair)
        monkeypatch.setattr(fb, "check_dependencies", lambda *a, **k: {"status": "ok", "installed": {}})

        _rpc(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "memory_repair_environment", "arguments": {"allow_network": False}}},
            {"ok": False},
        )
        # 工具经 `locked_repair` 转发，后者会显式传齐三个参数，所以只断言开关本身。
        assert seen["allow_network"] is False

    def test_repair_tool_validates_its_argument(self) -> None:
        response = _rpc(
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
             "params": {"name": "memory_repair_environment", "arguments": {"allow_network": "yes"}}},
        )
        assert response["result"]["isError"] is True
        assert "must be a boolean" in response["result"]["content"][0]["text"]

    def test_unknown_tool_is_an_error_not_a_crash(self) -> None:
        response = _rpc(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": "memory_read", "arguments": {}}},
        )
        assert response["result"]["isError"] is True
        assert "unknown tool" in response["result"]["content"][0]["text"]

    def test_a_raising_tool_becomes_an_error_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """兜底 server 自己崩掉就又回到了"不可诊断"的原点。"""
        monkeypatch.setattr(fb, "_call_tool", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
        response = _rpc(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
             "params": {"name": "memory_environment_status", "arguments": {}}},
        )
        assert response["result"]["isError"] is True
        assert "RuntimeError: nope" in response["result"]["content"][0]["text"]

    @pytest.mark.parametrize("bad", [{"name": 42}, {"name": "x", "arguments": []}])
    def test_malformed_tool_call_params_are_rejected(self, bad: dict[str, Any]) -> None:
        response = _rpc({"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": bad})
        assert response["error"]["code"] == -32602

    def test_ping_is_answered(self) -> None:
        assert _rpc({"jsonrpc": "2.0", "id": 12, "method": "ping"})["result"] == {}

    def test_unsupported_method_returns_method_not_found(self) -> None:
        response = _rpc({"jsonrpc": "2.0", "id": 13, "method": "resources/list"})
        assert response["error"]["code"] == -32601

    def test_notifications_get_no_response(self) -> None:
        assert _rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


class TestDegradedServerLoop:
    def test_a_full_session_speaks_line_delimited_json_rpc(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
        stdout = io.StringIO()

        fb.serve_diagnostics("generic-memory-mcp", {"ok": False}, stdin=stdin, stdout=stdout)

        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        assert len(lines) == 2  # 通知不产生响应
        assert json.loads(lines[0])["result"]["serverInfo"]["name"].endswith("-degraded")
        assert json.loads(lines[1])["result"]["tools"]

    def test_garbage_input_is_answered_instead_of_silently_dropped(self) -> None:
        """静默丢弃会把兜底 server 退化成"连得上但永不回话"。

        这个 server 存在的唯一意义就是说清出了什么问题；客户端一路超时比明确报错更难
        排查。空行仍然忽略（那不是报文），坏输入也绝不能中断会话。
        """
        stdin = io.StringIO(
            "not json\n"
            "\n"
            "[1,2,3]\n"
            + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            + "\n"
        )
        stdout = io.StringIO()
        fb.serve_diagnostics("generic-memory-mcp", {}, stdin=stdin, stdout=stdout)

        replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        assert len(replies) == 3
        assert replies[0]["error"]["code"] == -32700          # 坏 JSON
        assert replies[1]["error"]["code"] == -32600          # batch 数组
        assert "batch" in replies[1]["error"]["message"]
        assert replies[2]["id"] == 1                          # 会话没被打断

    def test_a_non_object_request_is_rejected(self) -> None:
        stdin = io.StringIO('"a string"\n42\n')
        stdout = io.StringIO()
        fb.serve_diagnostics("generic-memory-mcp", {}, stdin=stdin, stdout=stdout)

        replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        assert [r["error"]["code"] for r in replies] == [-32600, -32600]

    def test_initialize_sent_as_a_notification_gets_no_reply(self) -> None:
        # 没有 id 就是通知。回一条 "id": null 的响应是协议违规，客户端可能据此断连。
        assert _rpc({"jsonrpc": "2.0", "method": "initialize", "params": {}}) is None

    def test_an_oversized_line_is_refused_instead_of_buffered(self) -> None:
        # 不设上限的话，一条畸形超长行会让这个"最后还能连上的通道"自己被内存拖死。
        huge = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "ping",
            "params": {"pad": "x" * (fb.MAX_REQUEST_CHARS + 10)},
        })
        stdin = io.StringIO(huge + "\n")
        stdout = io.StringIO()
        fb.serve_diagnostics("generic-memory-mcp", {}, stdin=stdin, stdout=stdout)

        reply = json.loads(stdout.getvalue().strip())
        assert reply["error"]["code"] == -32600
        assert "too large" in reply["error"]["message"]

    def test_the_size_limit_is_applied_while_reading_not_after(self) -> None:
        """上限必须传给 readline，事后判断等于没设。

        事后判断时整行已经在内存里了 —— 恰好是要防的那件事。这里用一个只认 size 参数的
        假流来钉住调用形式：不传 size 就读不到任何东西。
        """
        reads: list[int | None] = []

        class _SizeAwareStdin:
            def __init__(self, payload: str) -> None:
                self._buf = payload

            def readline(self, size: int | None = None) -> str:
                reads.append(size)
                if size is None:  # 旧实现会走到这里，直接把整行吞进内存
                    raise AssertionError("readline must be called with a size limit")
                chunk, self._buf = self._buf[:size], self._buf[size:]
                return chunk

        payload = "x" * (fb.MAX_REQUEST_CHARS + 50) + "\n"
        stdout = io.StringIO()
        fb.serve_diagnostics("generic-memory-mcp", {}, stdin=_SizeAwareStdin(payload), stdout=stdout)

        assert reads and all(size is not None for size in reads)
        reply = json.loads(stdout.getvalue().strip())
        assert reply["error"]["code"] == -32600

    def test_the_tail_of_an_oversized_line_is_not_parsed_as_a_new_request(self) -> None:
        # 截断读之后必须把这一行的尾巴吃掉，否则后半截会被当成下一条请求，此后条条错位。
        tail = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        stdin = io.StringIO("x" * (fb.MAX_REQUEST_CHARS + 5) + "\n" + tail + "\n")
        stdout = io.StringIO()
        fb.serve_diagnostics("generic-memory-mcp", {}, stdin=stdin, stdout=stdout)

        replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        assert replies[0]["error"]["code"] == -32600
        assert len(replies) == 2, "the following request must still be answered"
        assert replies[1]["id"] == 2
        assert "tools" in replies[1]["result"]

    def test_responses_are_ascii_safe(self, tmp_path: Path) -> None:
        """流的编码重设可能失败；转义后的报文在任何控制台编码下都写得出去。"""
        outcome = {
            "ok": False,
            "before": {"status": "missing", "interpreter": "python.exe",
                       "requirements": "requirements.txt",
                       "missing": [{"name": "mcp", "required": ">=1.29"}],
                       "repair_hint": "运行 deploy 脚本"},
        }
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "memory_environment_status", "arguments": {}}}) + "\n"
        )
        stdout = io.StringIO()
        fb.serve_diagnostics("generic-memory-mcp", outcome, stdin=stdin, stdout=stdout)

        raw = stdout.getvalue()
        assert raw.isascii()
        text = json.loads(raw)["result"]["content"][0]["text"]
        assert "运行 deploy 脚本" in text


# --------------------------------------------------------------- 并发与落盘


class TestRepairLock:
    """`pip install` 的跨进程互斥。

    Memory 的 venv 平时只有一个 server 在用，但客户端重启、手工 `--repair`、
    启动探测都可能和它撞上。pip 没有跨进程锁：同时往一套 site-packages 里装同一批
    wheel 会互相删改对方正在写的文件，结果正是本模块要修的那种半安装环境。
    `_ATTEMPT_ENV` 只在单个进程树内有效，挡不住这个。
    """

    @staticmethod
    def _is_free(target: Path) -> bool:
        """锁是否真的空闲。

        不能用"锁文件存在"判断：锁由内核持有，文件在 `acquire()` 打开时就被创建，
        释放时也**不删除**。唯一可靠的判断就是再抢一次。
        """
        probe = dg._RepairLock(target, wait_sec=0.0)
        free = probe.acquire() == dg._RepairLock.ACQUIRED
        probe.release()
        return free

    def test_only_one_holder_at_a_time(self, tmp_path: Path) -> None:
        target = tmp_path / "repair.lock"
        first = dg._RepairLock(target, wait_sec=0.4)
        assert first.acquire() == dg._RepairLock.ACQUIRED

        second = dg._RepairLock(target, wait_sec=0.4)
        assert second.acquire() == dg._RepairLock.TIMEOUT
        assert second.waited_sec >= 0.4
        assert second.contended is True

        first.release()
        third = dg._RepairLock(target, wait_sec=0.4)
        assert third.acquire() == dg._RepairLock.ACQUIRED
        third.release()

    def test_a_leftover_lock_file_does_not_block_a_new_acquire(self, tmp_path: Path) -> None:
        """锁文件不再被删除，所以"文件存在"绝不能等于"被持有"。

        持锁进程被硬杀会留下文件。把文件存在当成持锁，自动修复就被这个空文件永久挡住了。
        """
        target = tmp_path / "repair.lock"
        target.write_text(json.dumps({"pid": 1, "at": 0}), encoding="utf-8")

        lock = dg._RepairLock(target, wait_sec=0.0)
        assert lock.acquire() == dg._RepairLock.ACQUIRED
        lock.release()

    def test_a_killed_holder_releases_the_lock_with_no_stale_threshold(
        self, tmp_path: Path
    ) -> None:
        """内核锁的全部意义：持锁进程一死，锁立刻可用。

        这条回归针对上一版实现的 blocker：那一版用"锁文件存在 + 陈旧阈值"表达持锁，于是
        必须自己判断持锁者是否已死。而那个判断天生有竞态 —— 两个等待者可以先后判定同一个
        锁陈旧，前者回收并持锁，后者随即把前者刚建好的锁当成陈旧的搬走，两个 pip 一起写
        同一套 site-packages。改成内核锁后，"被硬杀"和"还在装"由内核区分。
        """
        target = tmp_path / "repair.lock"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                "-c",
                f"import sys, time; sys.path.insert(0, {str(dg.MEMORY_ROOT)!r});"
                "from pathlib import Path;"
                "from servers.memory_server import dependency_guard as g;"
                f"l = g._RepairLock(Path({str(target)!r}), wait_sec=5);"
                "print(l.acquire(), flush=True); time.sleep(120)",
            ],
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            assert (holder.stdout.readline() or "").strip() == "acquired"
            assert self._is_free(target) is False
        finally:
            holder.kill()
            holder.wait(timeout=30)

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not self._is_free(target):
            time.sleep(0.1)
        assert self._is_free(target) is True

    def test_release_does_not_free_a_lock_we_never_held(self, tmp_path: Path) -> None:
        target = tmp_path / "repair.lock"
        owner = dg._RepairLock(target, wait_sec=0.1)
        assert owner.acquire() == dg._RepairLock.ACQUIRED

        stranger = dg._RepairLock(target, wait_sec=0.0)
        assert stranger.acquire() == dg._RepairLock.TIMEOUT
        stranger.release()

        assert self._is_free(target) is False
        owner.release()
        assert self._is_free(target) is True

    def test_an_unusable_lock_path_reports_unavailable_not_timeout(self, tmp_path: Path) -> None:
        # 建不了锁文件是环境问题，不能让一个不可写目录把自动修复彻底关掉；但它和
        # "别人正在装"必须区分开，后者绝不允许继续装。
        blocker = tmp_path / "in-the-way"
        blocker.write_text("not a directory", encoding="utf-8")

        lock = dg._RepairLock(blocker / "repair.lock", wait_sec=0.1)
        assert lock.acquire() == dg._RepairLock.UNAVAILABLE
        assert lock.acquired is False

    def test_the_wait_budget_covers_the_worst_case_repair(self) -> None:
        # 持锁的一方可能先用 `ensurepip` 把 pip 装回来（残留元数据让第一次空转时会清理后
        # 再跑一次，所以是两次），再离线装一遍、失败后联网装一遍；如果装完仍然 import
        # 不起来，还会带 `--force-reinstall` 把后两步再走一遍。这些超时串行相加。等待上限
        # 小于这个和，等待者就会在对方还在装的时候放弃，然后各自开装。
        one_pass = dg.OFFLINE_TIMEOUT_SEC + dg.ONLINE_TIMEOUT_SEC
        worst = 2 * dg.ENSUREPIP_TIMEOUT_SEC + 2 * one_pass
        assert dg.MAX_REPAIR_SEC == worst
        assert dg.LOCK_WAIT_SEC > worst

    def test_a_locked_pip_hold_is_bounded_so_it_cannot_park_the_lock_forever(self) -> None:
        # `--locked-pip` 是持锁跑的。没有上界的话，一个挂死的 pip 会把锁一直占着，
        # 而内核锁不会被别人抢走。
        assert 0 < dg.LOCKED_PIP_TIMEOUT_SEC < float("inf")
        # 上界还必须容得下一次正常的联网安装，否则合法的慢安装会被自己掐掉。
        assert dg.LOCKED_PIP_TIMEOUT_SEC >= dg.ONLINE_TIMEOUT_SEC
        assert dg.DEFAULT_LOCKED_PIP_WAIT_SEC < dg.LOCK_WAIT_SEC

    def test_a_wait_timeout_refuses_to_start_a_second_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """等超时说明别人还在装，此时自己再跑 pip 就等于这把锁白加了。"""
        target = tmp_path / "repair.lock"
        blocker = dg._RepairLock(target, wait_sec=0.1)
        assert blocker.acquire() == dg._RepairLock.ACQUIRED

        monkeypatch.setattr(dg, "check_dependencies", lambda *a, **k: {"status": "missing"})
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.setattr(dg, "LOCK_WAIT_SEC", 0.3)
        monkeypatch.setattr(
            dg, "repair", lambda *a, **k: pytest.fail("must not run a concurrent install")
        )
        monkeypatch.delenv(dg._ATTEMPT_ENV, raising=False)
        try:
            outcome = dg.ensure_dependencies(state_file=tmp_path / "state.json", lock_file=target)
        finally:
            blocker.release()

        assert outcome["ok"] is False
        assert "another process" in outcome["blocked_reason"]
        assert outcome["lock"]["status"] == dg._RepairLock.TIMEOUT

    def test_repair_runs_under_the_lock_and_releases_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "repair.lock"
        held: list[bool] = []
        statuses = [{"status": "missing"}, {"status": "ok"}]

        monkeypatch.setattr(dg, "check_dependencies", lambda *a, **k: statuses.pop(0))
        monkeypatch.setattr(dg, "probe_imports", lambda *a, **k: (True, None))
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.delenv(dg._ATTEMPT_ENV, raising=False)

        # 判据必须是"此刻别人抢不到锁"，不能是"锁文件存在"：锁由内核持有，文件在
        # acquire 打开时就已创建、release 后也不删除。
        def _repair(*_a: Any, **_k: Any) -> dict[str, Any]:
            held.append(not TestRepairLock._is_free(target))
            return {"repaired": True, "method": "offline"}

        monkeypatch.setattr(dg, "repair", _repair)
        outcome = dg.ensure_dependencies(state_file=tmp_path / "state.json", lock_file=target)

        assert outcome["ok"] is True
        assert held == [True]
        assert TestRepairLock._is_free(target) is True
        assert outcome["lock"]["acquired"] is True

    def test_waiting_for_the_lock_then_finding_a_healthy_env_skips_the_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 等到锁说明刚有别的进程改过同一套 site-packages。它很可能已经修好了，
        # 此时再装一遍纯属浪费，还会把刚装好的文件再动一次。
        import threading

        target = tmp_path / "repair.lock"
        blocker = dg._RepairLock(target, wait_sec=0.1)
        assert blocker.acquire() == dg._RepairLock.ACQUIRED
        threading.Timer(0.3, blocker.release).start()

        statuses = [{"status": "missing"}, {"status": "ok"}]
        monkeypatch.setattr(dg, "check_dependencies", lambda *a, **k: statuses.pop(0))
        monkeypatch.setattr(dg, "probe_imports", lambda *a, **k: (True, None))
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.setattr(
            dg, "repair", lambda *a, **k: pytest.fail("must not reinstall after waiting")
        )
        monkeypatch.delenv(dg._ATTEMPT_ENV, raising=False)

        outcome = dg.ensure_dependencies(state_file=tmp_path / "state.json", lock_file=target)

        assert outcome["ok"] is True
        assert outcome["repair"] == {"method": "another process", "ok": True}
        assert outcome["lock"]["waited_sec"] > 0


class TestExplicitRepairTakesTheLock:
    """显式修复也必须走同一把跨进程锁。

    显式入口（兜底 server 的 `memory_repair_environment`、`--repair` 命令行、部署脚本里的
    pip）刻意绕过冷却与"本进程树已试过"两道闸门 —— 那两道只为防客户端自动重启导致的装包
    循环。但绕过跨进程锁就等于允许"一次人工重试"和"server 的启动期自动修复"同时往同一套
    site-packages 跑 pip，正是这把锁要防的半安装状态。
    """

    @staticmethod
    def _fake_pip(returncode: int, seen: list[list[str]], target: Path):
        """替掉 Popen，记录 argv 并断言此刻锁真的被持有。"""

        def _popen(cmd: list[str], **kwargs: Any) -> Any:
            seen.append(list(cmd))
            assert TestRepairLock._is_free(target) is False, "pip must hold the lock"
            assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
            return SimpleNamespace(returncode=returncode, wait=lambda timeout=None: returncode)

        return _popen

    def test_locked_repair_holds_the_lock_while_pip_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "repair.lock"
        held: list[bool] = []

        def _repair(**_k: Any) -> dict[str, Any]:
            held.append(not TestRepairLock._is_free(target))
            return {"repaired": True, "method": "offline", "steps": []}

        monkeypatch.setattr(dg, "repair", _repair)
        result = dg.locked_repair(lock_file=target)

        assert held == [True]
        assert TestRepairLock._is_free(target) is True
        assert result["lock"]["status"] == dg._RepairLock.ACQUIRED

    def test_locked_repair_refuses_while_another_process_installs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "repair.lock"
        blocker = dg._RepairLock(target, wait_sec=0.1)
        assert blocker.acquire() == dg._RepairLock.ACQUIRED

        monkeypatch.setattr(dg, "repair", lambda **_k: pytest.fail("must not install"))
        try:
            result = dg.locked_repair(lock_file=target, lock_wait_sec=0.3)
        finally:
            blocker.release()

        assert result["attempted"] is False
        assert result["repaired"] is False
        assert "another process" in result["error"]
        # 结构必须和 `repair()` 一致：兜底工具和 CLI 都会遍历 steps。
        assert result["steps"] == []
        assert "requirements" in result and "vendor" in result

    def test_the_fallback_repair_tool_uses_the_locked_wrapper(self) -> None:
        # 回归：兜底 server 里的重试工具曾直接调 `repair()`，完全绕过这把锁。
        from servers.memory_server import dependency_fallback

        source = Path(dependency_fallback.__file__).read_text(encoding="utf-8")
        assert "locked_repair as run_repair" in source
        assert "import repair as run_repair" not in source

    def test_the_cli_repair_path_uses_the_locked_wrapper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 回归：`--repair`（部署脚本和启动探测器都用它）曾直接调 `repair()`。
        calls: list[dict[str, Any]] = []

        monkeypatch.setattr(dg, "check_dependencies", lambda *a, **k: {"status": "missing"})
        monkeypatch.setattr(dg, "repair", lambda **_k: pytest.fail("must go through the lock"))
        monkeypatch.setattr(
            dg,
            "locked_repair",
            lambda **kw: (calls.append(kw), {"repaired": True, "method": "offline", "steps": []})[1],
        )
        dg._main(["--repair"])

        assert len(calls) == 1

    def test_locked_pip_runs_pip_under_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 部署脚本原先直接调 pip，绕过了锁 —— 而"重新部署"和"server 自动修复"同时发生
        # 完全可能。
        target = tmp_path / "repair.lock"
        seen: list[list[str]] = []

        monkeypatch.setattr(dg, "lock_path", lambda *a, **k: target)
        monkeypatch.setattr(dg.subprocess, "Popen", self._fake_pip(0, seen, target))

        code = dg._main(["--locked-pip", "install", "-r", "req.txt", "--no-index"])

        assert code == 0
        assert seen[0][1:] == ["-m", "pip", "install", "-r", "req.txt", "--no-index"]
        assert TestRepairLock._is_free(target) is True

    def test_locked_pip_reports_busy_instead_of_installing_concurrently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "repair.lock"
        blocker = dg._RepairLock(target, wait_sec=0.1)
        assert blocker.acquire() == dg._RepairLock.ACQUIRED

        monkeypatch.setattr(dg, "lock_path", lambda *a, **k: target)
        monkeypatch.setattr(dg.subprocess, "Popen", lambda *a, **k: pytest.fail("must not run pip"))
        try:
            code = dg._main(["--locked-pip", "--lock-wait", "0.3", "install", "-r", "req.txt"])
        finally:
            blocker.release()

        assert code == dg.LOCKED_PIP_BUSY_EXIT

    def test_locked_pip_passes_pip_flags_through_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 回归：交给 argparse 解析会把 `-r`/`--no-index` 当成本模块自己的选项而报错退出。
        target = tmp_path / "repair.lock"
        seen: list[list[str]] = []

        monkeypatch.setattr(dg, "lock_path", lambda *a, **k: target)
        monkeypatch.setattr(dg.subprocess, "Popen", self._fake_pip(7, seen, target))

        code = dg._main(["--locked-pip", "install", "--upgrade", "pip", "--json", "--repair"])

        assert code == 7, "pip's exit code must be propagated verbatim"
        assert seen[0][-4:] == ["--upgrade", "pip", "--json", "--repair"]

    def test_lock_wait_is_consumed_and_not_forwarded_to_pip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 部署脚本需要"只短等，然后立刻拿到 75"，否则脚本自己的超时会先到，用户看到的是
        # "pip 超时"而不是"别人正在装"。这个选项必须被吃掉，不能混进 pip 的参数里。
        target = tmp_path / "repair.lock"
        seen: list[list[str]] = []

        monkeypatch.setattr(dg, "lock_path", lambda *a, **k: target)
        monkeypatch.setattr(dg.subprocess, "Popen", self._fake_pip(0, seen, target))

        code = dg._main(["--locked-pip", "--lock-wait", "5", "install", "-r", "req.txt"])

        assert code == 0
        assert seen[0][1:] == ["-m", "pip", "install", "-r", "req.txt"]

    def test_a_bad_lock_wait_is_rejected_instead_of_silently_defaulted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dg.subprocess, "Popen", lambda *a, **k: pytest.fail("must not run pip"))
        assert dg._main(["--locked-pip", "--lock-wait", "soon"]) == 2

    def test_a_hung_pip_is_killed_with_its_children_and_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 这条命令是持锁跑的，而内核锁不会被别人抢走。所以挂死的 pip 必须有上界，否则
        # server 的自动修复会永远排在它后面。
        target = tmp_path / "repair.lock"
        killed: list[int] = []

        class _Hung:
            pid = 4321

            def wait(self, timeout: float | None = None) -> int:
                if not killed:
                    raise subprocess.TimeoutExpired(cmd="pip", timeout=timeout or 0)
                return -1

        monkeypatch.setattr(dg, "lock_path", lambda *a, **k: target)
        monkeypatch.setattr(dg.subprocess, "Popen", lambda *a, **k: _Hung())
        monkeypatch.setattr(dg, "_kill_tree", lambda p: killed.append(p.pid))

        code = dg._main(["--locked-pip", "install", "-r", "req.txt"])

        assert killed == [4321], "the whole process tree must be killed"
        assert code == 124, "a timeout must be distinguishable from a pip failure"
        assert TestRepairLock._is_free(target) is True


class TestDeploymentScriptEncoding:
    """含非 ASCII 文本的 .ps1 必须带 UTF-8 BOM。

    Windows PowerShell 5.1 对没有 BOM 的脚本按当前 ANSI 代码页解码。在 GBK（936）机器上，
    UTF-8 的中文字节会被两两误拼成汉字，落单的那个字节可能把紧随其后的 `"` 当成双字节字符的
    后半吞掉 —— 引号从此不配对，整个脚本连语法都过不了。实测 `scripts/bootstrap.ps1` 就是这样
    直接跑不起来的，而症状（"字符串缺少终止符"指向文件末尾某行）完全看不出与编码有关。
    """

    def test_scripts_with_non_ascii_text_start_with_a_utf8_bom(self) -> None:
        root = Path(dg.MEMORY_ROOT)
        scripts = [p for p in root.rglob("*.ps1") if ".venv" not in p.parts]
        assert scripts, "expected to find the Memory PowerShell scripts"

        offenders = [
            str(p.relative_to(root))
            for p in scripts
            if not p.read_bytes().startswith(b"\xef\xbb\xbf")
            and any(byte > 0x7F for byte in p.read_bytes())
        ]
        assert offenders == [], "these scripts need a UTF-8 BOM or ASCII-only text"


class TestDeploymentScriptLockWiring:
    """部署脚本自己的超时必须容得下"等锁 + 一次安装"，且每一步都要查退出码。

    脚本超时到点会杀进程树。如果它比"等锁上限 + pip 上限"还短，一次正常的竞争就会被
    脚本先掐掉，用户看到的是"pip 超时"而不是"别人正在装"。
    """

    def _deploy(self) -> str:
        return (Path(dg.MEMORY_ROOT) / "deploy.ps1").read_text(encoding="utf-8-sig")

    def _bootstrap(self) -> str:
        return (Path(dg.MEMORY_ROOT) / "scripts" / "bootstrap.ps1").read_text(
            encoding="utf-8-sig"
        )

    def test_every_pip_timeout_covers_the_lock_wait_plus_the_pip_budget(self) -> None:
        import re

        text = self._deploy()
        waits = [int(m) for m in re.findall(r"\$LockWaitSeconds\s*=\s*(\d+)", text)]
        assert len(waits) == 1, "expected exactly one lock-wait budget"
        floor = waits[0] + dg.LOCKED_PIP_TIMEOUT_SEC

        declared = [int(m) for m in re.findall(r"TimeoutSeconds\s*=\s*(\d+)", text)]
        passed = [int(m) for m in re.findall(r"-TimeoutSeconds\s+(\d+)", text)]
        assert declared and passed
        for value in declared + passed:
            assert value >= floor, f"TimeoutSeconds {value} is below {floor}"

    def test_a_timeout_kills_the_whole_process_tree(self) -> None:
        # 只 Kill() 包装进程会留下 pip 那一层，而此时锁已经随包装进程一起释放 ——
        # 一个不持锁却还在写 site-packages 的 pip 比一开始不加锁更糟。
        assert "taskkill.exe /T /F /PID" in self._deploy()

    def test_every_pip_call_goes_through_the_lock(self) -> None:
        # 回归：pip 自升级和开发依赖曾直接 `& $venvPython -m pip install`，绕过了这把锁。
        text = self._deploy()
        assert "-m pip install" not in text
        assert "'--lock-wait', $script:LockWaitSeconds" in text

    def test_every_pip_step_handles_the_busy_exit_code(self) -> None:
        text = self._deploy()
        assert "$PipBusyExit = 75" in text
        handled = text.count("-eq $PipBusyExit") + text.count("-eq $script:PipBusyExit")
        calls = text.count("Invoke-PipInstall -PipExePath")
        assert handled >= calls, "every pip step must handle the busy exit code"

    def test_bootstrap_checks_the_exit_code_of_each_step(self) -> None:
        # 回归：只在最后一条之后查 $LASTEXITCODE，于是 pip 自升级被锁挡住（75）或直接
        # 失败都会被丢掉，脚本却照样打印 "dependencies OK"。
        text = self._bootstrap()
        assert "foreach ($step in $steps)" in text
        assert text.count("throw") >= 4

    def test_every_venv_deletion_checks_for_live_users_first(self) -> None:
        """删一个正在运行的 venv 会留下半个 venv。

        Windows 删不掉 server 打开着的 `.pyd`，其余文件却已经删了 —— pip 和一半
        site-packages 没了，python.exe 留着。之后任何 pip 都只会报 `No module named
        pip`，而运行中的 server 活在一个已经被抽走的解释器目录上。
        """
        import re

        text = self._deploy()
        deletes = re.findall(r"Remove-Item -Recurse -Force \$venvDir", text)
        assert deletes, "expected the script to be able to rebuild a venv"
        assert text.count("Assert-VenvNotInUse") >= len(deletes)

    def test_a_reused_venv_gets_pip_restored_before_installing(self) -> None:
        # 复用的 venv 里 pip 可能已经不在了（上一次删了一半，或当初 --without-pip）。
        assert "ensurepip --default-pip" in self._deploy()


class TestPipRecovery:
    """pip 自己就是缺的那一块时，修复必须先把 pip 装回来。"""

    def test_a_missing_pip_is_restored_with_ensurepip(self, monkeypatch) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        available = iter([False, True])
        monkeypatch.setattr(dg, "_pip_is_available", lambda: next(available))
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.setattr(dg.subprocess, "run", fake_run)

        ok, note, changed = dg.ensure_pip()

        assert ok, note
        assert changed is True, "restoring pip is a change to the environment"
        assert len(calls) == 1
        assert "ensurepip" in calls[0]
        assert "--default-pip" in calls[0]

    def test_an_untouched_pip_is_reported_as_unchanged(self, monkeypatch) -> None:
        """调用方靠这个布尔值决定要不要记一条步骤，而不是去比对诊断文案。

        原先它比的是 `note != "pip already present"`；那种写法下改一次措辞，就会让每次
        修复都凭空多出一条 `ensure_pip` 步骤，而没有任何测试会失败。
        """
        monkeypatch.setattr(dg, "_pip_is_available", lambda: True)

        def explode(*_a, **_k):
            raise AssertionError("must not run")

        monkeypatch.setattr(dg.subprocess, "run", explode)

        ok, _note, changed = dg.ensure_pip()
        assert ok is True
        assert changed is False

    def test_stale_pip_metadata_is_cleared_so_ensurepip_stops_no_opping(
        self, monkeypatch
    ) -> None:
        """实测踩到的坑：`ensurepip` 退出码 0，pip 依然不可导入。

        `ensurepip` 内部是用捆绑的 wheel 跑 `pip install pip`，而那个 pip 只看元数据。
        venv 被删了一半时 `pip/` 目录没了、`pip-24.0.dist-info` 还在，于是它报
        `Requirement already satisfied: pip`、退出码 0、什么都不装 —— 修复在第一步就
        静默卡死，且诊断只会说"报成功但仍不可导入"，不指向真正的原因。
        """
        runs: list[int] = []
        available = iter([False, False, True])
        monkeypatch.setattr(dg, "_pip_is_available", lambda: next(available))
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.setattr(
            dg, "_run_ensurepip", lambda _t: (runs.append(1), (True, "ensurepip completed"))[1]
        )
        monkeypatch.setattr(
            dg, "_clear_stale_pip_metadata", lambda: ["pip-24.0.dist-info"]
        )

        ok, note, changed = dg.ensure_pip()

        assert ok, note
        assert changed is True
        assert len(runs) == 2, "expected a retry after clearing the metadata"
        assert "pip-24.0.dist-info" in note

    def test_a_no_op_ensurepip_with_nothing_stale_reports_the_original_failure(
        self, monkeypatch
    ) -> None:
        # 没有残留元数据可清时不要谎报修好，也不要白跑第二次 ensurepip。
        runs: list[int] = []
        monkeypatch.setattr(dg, "_pip_is_available", lambda: False)
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.setattr(
            dg, "_run_ensurepip", lambda _t: (runs.append(1), (True, "ensurepip completed"))[1]
        )
        monkeypatch.setattr(dg, "_clear_stale_pip_metadata", lambda: [])

        ok, note, _changed = dg.ensure_pip()

        assert ok is False
        assert len(runs) == 1
        assert "still not importable" in note

    def test_metadata_is_only_cleared_when_the_package_directory_is_really_gone(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """`pip/` 还在时不能碰它的元数据 —— 那是个能用的安装。"""
        (tmp_path / "pip").mkdir()
        (tmp_path / "pip-24.0.dist-info").mkdir()
        monkeypatch.setattr(
            "sysconfig.get_paths",
            lambda *_a, **_k: {"purelib": str(tmp_path), "platlib": str(tmp_path)},
        )

        assert dg._clear_stale_pip_metadata() == []
        assert (tmp_path / "pip-24.0.dist-info").is_dir()

    def test_orphaned_metadata_is_removed(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / "pip-24.0.dist-info").mkdir()
        monkeypatch.setattr(
            "sysconfig.get_paths", lambda *_a, **_k: {"purelib": str(tmp_path)}
        )

        assert dg._clear_stale_pip_metadata() == ["pip-24.0.dist-info"]
        assert not (tmp_path / "pip-24.0.dist-info").exists()

    def test_ensurepip_is_not_run_on_a_shared_interpreter(self, monkeypatch) -> None:
        # 理由和不往共享解释器装包一样：那会改引擎或系统 Python。
        monkeypatch.setattr(dg, "_pip_is_available", lambda: False)
        monkeypatch.setattr(dg, "is_isolated_env", lambda: False)

        def explode(*_a, **_k):
            raise AssertionError("must not run")

        monkeypatch.setattr(dg.subprocess, "run", explode)

        ok, note, changed = dg.ensure_pip()
        assert ok is False
        assert changed is False
        assert "not a virtual environment" in note

    def test_a_claimed_success_that_did_not_restore_pip_is_a_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(dg, "_pip_is_available", lambda: False)
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.setattr(
            dg.subprocess,
            "run",
            lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        ok, note, changed = dg.ensure_pip()
        assert ok is False
        assert changed is True, "ensurepip ran, so the environment may have been touched"
        assert "still not importable" in note

    def test_repair_stops_with_a_usable_reason_when_pip_cannot_be_restored(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        req = tmp_path / "requirements.txt"
        req.write_text("mcp>=1.27,<2\n", encoding="utf-8")
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.setattr(dg, "in_expected_venv", lambda: True)
        monkeypatch.setattr(
            dg, "ensure_pip", lambda: (False, "ensurepip exit=1: boom", True)
        )

        def explode(*_a, **_k):
            raise AssertionError("no pip yet")

        monkeypatch.setattr(dg, "_run_pip", explode)

        result = dg.repair(requirements=req)

        assert result["repaired"] is False
        assert "pip is unavailable" in result["error"]
        assert [step["step"] for step in result["steps"]] == ["ensure_pip"]


class TestUnusableEnvironmentEscalatesToForceReinstall:
    """元数据齐全但 import 不起来时，普通 `install -r` 是空操作。

    pip 只看 `dist-info` 判断"已满足"。venv 被删了一半时，包目录没了而 `dist-info`
    留着，于是 `pip install -r` 什么都不装、报成功，体检也报"依赖正常" —— 而
    `import mcp.server` 照样炸。这条路径实测发生过。
    """

    def test_the_second_attempt_forces_a_reinstall(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        installs: list[list[str]] = []
        probes = iter([(False, "No module named 'dotenv'"), (True, None)])

        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.setattr(dg, "in_expected_venv", lambda: True)
        monkeypatch.setattr(dg, "ensure_pip", lambda *a, **k: (True, "present", False))
        monkeypatch.setattr(dg, "verify_vendor", lambda _v: (True, "verified"))
        monkeypatch.setattr(dg, "_activate_site_packages", lambda: None)
        monkeypatch.setattr(dg, "probe_imports", lambda _m: next(probes))
        monkeypatch.setattr(
            dg,
            "_run_pip",
            lambda args, _t: (installs.append(list(args)), (True, "ok"))[1],
        )

        result = dg.repair(
            requirements=_requirements(tmp_path, "mcp>=1.29\n"),
            verify_modules=("mcp.server",),
        )

        assert result["repaired"] is True
        assert len(installs) == 2, "expected a forced retry"
        assert "--force-reinstall" not in installs[0]
        assert "--force-reinstall" in installs[1]
        assert [step["step"] for step in result["steps"]] == [
            "verify_vendor",
            "install_offline",
            "verify_imports",
            "install_offline_forced",
            "verify_imports_forced",
        ]

    def test_the_forced_retry_happens_while_the_lock_is_still_held(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """升级重装必须在锁内，否则两轮安装之间锁是放开的。

        这个顺序是这条修复的全部意义：调用方各自"装完→验证→再装一次"时，第二轮 pip 跑在
        锁外，别的进程正好能在那个窗口里挤进来开第二个 pip —— 也就是这把锁本来要防的
        半安装状态。
        """
        events: list[str] = []
        probes = iter([(False, "broken"), (True, None)])
        real_release = dg._RepairLock.release

        def spy_release(self):
            events.append("release")
            return real_release(self)

        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.setattr(dg, "in_expected_venv", lambda: True)
        monkeypatch.setattr(dg, "ensure_pip", lambda *a, **k: (True, "present", False))
        monkeypatch.setattr(dg, "verify_vendor", lambda _v: (True, "verified"))
        monkeypatch.setattr(dg, "_activate_site_packages", lambda: None)
        monkeypatch.setattr(dg, "probe_imports", lambda _m: next(probes))
        monkeypatch.setattr(
            dg, "_run_pip", lambda args, _t: (events.append("install"), (True, "ok"))[1]
        )
        monkeypatch.setattr(dg._RepairLock, "release", spy_release)

        dg.locked_repair(
            requirements=_requirements(tmp_path, "mcp>=1.29\n"),
            lock_file=tmp_path / "repair.lock",
            verify_modules=("mcp.server",),
        )

        assert events == ["install", "install", "release"]

    def test_a_still_broken_environment_is_not_reported_as_ok(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(dg, "check_dependencies", lambda *_a, **_k: {"status": "ok"})
        monkeypatch.setattr(
            dg, "probe_imports", lambda _m: (False, "No module named 'dotenv'")
        )
        monkeypatch.setattr(
            dg,
            "repair",
            lambda *_a, **_k: {
                "attempted": True, "repaired": True, "method": "offline", "steps": []
            },
        )
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.delenv(dg._ATTEMPT_ENV, raising=False)

        outcome = dg.ensure_dependencies(
            state_file=tmp_path / "state.json", lock_file=tmp_path / "repair.lock"
        )

        assert outcome["ok"] is False
        assert "dotenv" in outcome["import_error"]

    def test_the_probe_targets_what_the_server_actually_imports(self) -> None:
        # `import mcp` 只跑一个 `__init__`；把 fastmcp → pydantic-settings →
        # python-dotenv 整条链拉起来的是 `mcp.server`。
        assert "mcp.server" in dg.PROBE_MODULES


class TestStateDurability:
    """状态文件必须原子落盘。

    `write_text` 会先截断再写；此刻进程被杀就留下空/半截 JSON，`_read_state` 把它当成
    "没有状态"，冷却随之失效，于是又开始"修-崩-修"。
    """

    def test_a_failed_write_leaves_the_previous_state_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "state.json"
        good = {"last_attempt_at": 123.0, "last_result": "failed"}
        dg._write_state(target, good)

        monkeypatch.setattr(dg.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
        dg._write_state(target, {"last_result": "ok"})

        assert dg._read_state(target) == good

    def test_no_temporary_files_are_left_behind(self, tmp_path: Path) -> None:
        dg._write_state(tmp_path / "state.json", {"last_result": "ok"})
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    def test_the_cooldown_clock_starts_when_the_repair_finishes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 一次离线安装可能跑几十秒。用"进入函数的时刻"计时会让冷却提前到期，
        # 客户端重启时又能立刻再装一次。
        target = tmp_path / "state.json"
        monkeypatch.setattr(dg, "check_dependencies", lambda *a, **k: {"status": "missing"})
        monkeypatch.setattr(dg, "probe_imports", lambda *a, **k: (False, "still broken"))
        monkeypatch.setattr(dg, "is_isolated_env", lambda: True)
        monkeypatch.delenv(dg._ATTEMPT_ENV, raising=False)

        def _slow(*_a: Any, **_k: Any) -> dict[str, Any]:
            time.sleep(0.2)
            return {"repaired": False, "error": "boom"}

        monkeypatch.setattr(dg, "repair", _slow)
        started = time.time()
        dg.ensure_dependencies(state_file=target, lock_file=tmp_path / "l.lock")

        state = dg._read_state(target)
        assert state["last_attempt_at"] >= started + 0.2
        assert state["last_attempt_started_at"] < state["last_attempt_at"]


class TestPipEnvironmentIsolation:
    """离线优先必须名副其实。"""

    def test_inherited_pip_index_variables_are_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 继承来的 PIP_INDEX_URL / PIP_FIND_LINKS 能把 `--no-index` 那一步重新指向网络，
        # 于是"只用校验过的 vendor wheel"这条保证就没了。
        monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
        monkeypatch.setenv("PIP_FIND_LINKS", "C:/somewhere/else")
        monkeypatch.setenv("PATH", "keep-me")

        env = dg._pip_env()

        assert [key for key in env if key.startswith("PIP_")] == []
        assert env["PATH"] == "keep-me"
        assert env["PYTHONIOENCODING"] == "utf-8"


class TestVendorManifestPaths:
    """SHA256SUMS 只能点名 vendor/ 里的纯文件名。"""

    @pytest.mark.parametrize("entry", ["../outside.whl", "sub/dir.whl", "/abs/x.whl"])
    def test_a_path_outside_vendor_is_refused(self, tmp_path: Path, entry: str) -> None:
        (tmp_path / "SHA256SUMS").write_text("0" * 64 + f"  {entry}\n", encoding="utf-8")

        ok, detail = dg.verify_vendor(tmp_path)

        assert ok is False
        assert "not a plain file name" in detail

    def test_a_binary_mode_star_prefix_is_still_accepted(self, tmp_path: Path) -> None:
        # `sha256sum -b` 会在文件名前加 `*`，那是合法清单，不该当成路径穿越。
        wheel = tmp_path / "pkg-1.0-py3-none-any.whl"
        wheel.write_bytes(b"payload")
        digest = hashlib.sha256(b"payload").hexdigest()
        (tmp_path / "SHA256SUMS").write_text(f"{digest}  *{wheel.name}\n", encoding="utf-8")

        ok, detail = dg.verify_vendor(tmp_path)

        assert ok is True, detail


# --------------------------------------------------------------- 坏环境里的可用性


class TestUsableWhenMcpIsBroken:
    """守卫的全部价值都在"`mcp` 已经坏了"这个前提下，所以它绝不能依赖 `mcp`。"""

    def test_the_guard_modules_have_no_mcp_imports(self) -> None:
        for module in (dg, fb):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                assert not stripped.startswith("import mcp")
                assert not stripped.startswith("from mcp")

    def test_importing_the_guard_does_not_pull_in_mcp(self) -> None:
        """用子进程实测，避免被本进程里已经导入过的 mcp 掩盖。"""
        code = (
            "import sys;"
            "import servers.memory_server.dependency_guard as g;"
            "import servers.memory_server.dependency_fallback as f;"
            "print(any(m == 'mcp' or m.startswith('mcp.') for m in sys.modules))"
        )
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(dg.MEMORY_ROOT),
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False"

    def test_the_guard_runs_as_a_standalone_cli(self) -> None:
        """部署脚本要能对任意解释器发起体检，即使那个环境的 mcp 已经损坏。"""
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "servers.memory_server.dependency_guard", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(dg.MEMORY_ROOT),
            timeout=120,
        )
        assert proc.returncode in {0, 1}
        payload = json.loads(proc.stdout) if proc.returncode == 0 else None
        if payload is not None:
            assert payload["status"] in {"ok", "unknown"}


class TestEntryPointOrdering:
    def test_the_guard_runs_before_the_server_import(self) -> None:
        """顺序错了守卫就白写了：`server` 一被导入就会 import mcp 并崩掉。

        按 AST 比较真实语句的先后，而不是搜文本 —— docstring 里同样会出现这两个名字。
        """
        import ast

        source = (Path(dg.__file__).parent / "__main__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        guard_line = next(
            node.lineno
            for node in tree.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", None) == "ensure_ready"
        )
        server_import_line = next(
            node.lineno
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "server"
        )
        assert guard_line < server_import_line
