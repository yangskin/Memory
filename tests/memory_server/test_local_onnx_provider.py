"""P5 Phase 2c — LocalOnnxProvider + model-download CLI.

These tests exercise the optional ONNX tier WITHOUT requiring the real
``onnxruntime`` package or any model file:

* registry-level behaviour (``available_providers``, ``get_provider``)
  is verified with monkey-patching;
* the CLI is exercised against a local ``http.server`` so no real network
  call is made; it covers the happy path, hash-mismatch quarantine, and
  the up-to-date short-circuit.

Tests that require an actual ``onnxruntime`` install are guarded with
``pytest.importorskip("onnxruntime")`` so the suite stays green on CI
machines that have not opted into the optional dependency.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from servers.memory_server import memory_embeddings as me
from servers.memory_server.memory_embeddings import (
    DeterministicHashProvider,
    ProviderUnavailableError,
    available_providers,
    get_provider,
)


# ---------------------------------------------------------------------------
# Registry / availability
# ---------------------------------------------------------------------------


def test_available_providers_always_lists_deterministic() -> None:
    assert "deterministic-hash" in available_providers()


def test_available_providers_lists_local_onnx_when_runtime_present(monkeypatch) -> None:
    monkeypatch.setattr(me, "_onnxruntime_available", lambda: True)
    assert "local-onnx" in available_providers()


def test_available_providers_omits_local_onnx_without_runtime(monkeypatch) -> None:
    monkeypatch.setattr(me, "_onnxruntime_available", lambda: False)
    assert "local-onnx" not in available_providers()


def test_get_provider_local_onnx_requires_model_path() -> None:
    with pytest.raises(ProviderUnavailableError, match="model_path"):
        get_provider("local-onnx")


def test_get_provider_local_onnx_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.onnx"
    with pytest.raises(ProviderUnavailableError, match="not found"):
        get_provider("local-onnx", model_path=missing)


def test_get_provider_auto_falls_back_when_runtime_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(me, "_onnxruntime_available", lambda: False)
    fake = tmp_path / "fake.onnx"
    fake.write_bytes(b"not really an onnx model")
    provider = get_provider("auto", model_path=fake)
    assert isinstance(provider, DeterministicHashProvider)


def test_get_provider_auto_falls_back_when_model_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(me, "_onnxruntime_available", lambda: True)
    provider = get_provider("auto", model_path=tmp_path / "absent.onnx")
    assert isinstance(provider, DeterministicHashProvider)


def test_get_provider_unknown_name_raises() -> None:
    with pytest.raises(ProviderUnavailableError, match="unknown or unavailable"):
        get_provider("remote-openai-api")


# ---------------------------------------------------------------------------
# \u00a715.1-A: tokenizer must be resolvable; missing tokenizer \u2192 unavailable
# ---------------------------------------------------------------------------


def test_local_onnx_provider_missing_tokenizer_raises_unavailable(tmp_path: Path) -> None:
    """``LocalOnnxProvider`` must NOT silently fall back to a stub tokenizer.

    We construct a syntactically-present .onnx file (real bytes are not
    required because tokenizer resolution happens *before* the ORT session
    is built) and assert the constructor raises ``ProviderUnavailableError``
    when no tokenizer.json / spiece.model / sentencepiece.model is found
    next to it.
    """
    pytest.importorskip("onnxruntime")
    fake_model = tmp_path / "model.onnx"
    fake_model.write_bytes(b"\x00" * 16)

    with pytest.raises(ProviderUnavailableError, match="tokenizer file not found"):
        me.LocalOnnxProvider(fake_model)


def test_get_provider_auto_falls_back_when_tokenizer_missing(tmp_path: Path) -> None:
    """``get_provider("auto", ...)`` must degrade to deterministic-hash
    when the tokenizer is missing rather than surfacing the error.
    """
    pytest.importorskip("onnxruntime")
    fake_model = tmp_path / "model.onnx"
    fake_model.write_bytes(b"\x00" * 16)
    provider = get_provider("auto", model_path=fake_model)
    assert isinstance(provider, DeterministicHashProvider)


# ---------------------------------------------------------------------------
# LocalOnnxProvider — only when onnxruntime is actually installed
# ---------------------------------------------------------------------------


def test_local_onnx_provider_loads_real_model(tmp_path: Path) -> None:
    """End-to-end smoke test using a tiny in-memory ONNX graph.

    Skipped unless ``onnxruntime`` is importable.  Builds a one-op model
    with ``onnx`` (also optional) so we never rely on a downloaded file.
    """

    ort = pytest.importorskip("onnxruntime")
    onnx = pytest.importorskip("onnx")
    helper = onnx.helper
    TensorProto = onnx.TensorProto

    # Tiny model: takes input_ids[B,T] int64 and emits a [B,T,D] float32
    # tensor by casting + tiling.  D=4 keeps the artefact small.
    input_ids = helper.make_tensor_value_info("input_ids", TensorProto.INT64, [None, None])
    attention_mask = helper.make_tensor_value_info(
        "attention_mask", TensorProto.INT64, [None, None]
    )
    output = helper.make_tensor_value_info(
        "last_hidden_state", TensorProto.FLOAT, [None, None, 4]
    )

    cast_node = helper.make_node("Cast", ["input_ids"], ["x_float"], to=TensorProto.FLOAT)
    unsq_node = helper.make_node("Unsqueeze", ["x_float", "axes"], ["x_3d"])
    axes_init = helper.make_tensor("axes", TensorProto.INT64, [1], [2])
    repeats_init = helper.make_tensor("repeats", TensorProto.INT64, [3], [1, 1, 4])
    tile_node = helper.make_node("Tile", ["x_3d", "repeats"], ["last_hidden_state"])

    graph = helper.make_graph(
        [cast_node, unsq_node, tile_node],
        "tiny",
        [input_ids, attention_mask],
        [output],
        initializer=[axes_init, repeats_init],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 7
    model_path = tmp_path / "tiny.onnx"
    onnx.save(model, str(model_path))

    provider = me.LocalOnnxProvider(
        model_path,
        # \u00a715.1-A: real tokenizers are now mandatory; for this toy ONNX
        # model we feed a stable callable so the test does not depend on
        # the optional ``tokenizers`` / ``sentencepiece`` packages.
        tokenizer=lambda texts: (
            [[1, 2, 3] for _ in texts],
            [[1, 1, 1] for _ in texts],
        ),
    )
    assert provider.metadata.provider_id == "local-onnx"
    assert provider.metadata.dim == 4
    assert provider.metadata.normalized is True
    # model_hash is deterministic = sha256 of the file truncated to 16 chars
    expected = hashlib.sha256(model_path.read_bytes()).hexdigest()[:16]
    assert provider.metadata.model_hash == expected

    vectors = provider.embed(["alpha bravo", "charlie"])
    assert len(vectors) == 2
    assert all(len(v) == 4 for v in vectors)
    # L2-normalised
    for v in vectors:
        norm = sum(x * x for x in v) ** 0.5
        assert abs(norm - 1.0) < 1e-5 or norm == 0.0


# ---------------------------------------------------------------------------
# Model-download CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def http_serving(tmp_path: Path):
    """Spin up a local HTTP server that serves files from ``tmp_path``."""

    handler_factory = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(tmp_path), **kw
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = socketserver.TCPServer(("127.0.0.1", port), handler_factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "download_embedding_model.py"
    )
    # 必须显式定死编码：text=True 会用本机 locale 解码（中文 Windows 上是 GBK），
    # 而子进程的输出编码取决于继承来的 PYTHONIOENCODING。两者不一致时 reader 线程
    # 抛 UnicodeDecodeError，subprocess 把 stdout/stderr 交回 None，断言会以
    # "argument of type 'NoneType' is not iterable" 这种与真实原因无关的形式失败。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(script), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def test_cli_downloads_and_records_event(http_serving, tmp_path: Path) -> None:
    base_url, serve_root = http_serving
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".ai-memory").mkdir()

    payload = b"this-is-a-tiny-fake-model-payload" * 32
    (serve_root / "model.onnx").write_bytes(payload)
    expected_sha = hashlib.sha256(payload).hexdigest()

    result = _run_cli(
        [
            "--repo",
            str(repo),
            "--url",
            f"{base_url}/model.onnx",
            "--sha256",
            expected_sha,
            "--filename",
            "tiny.onnx",
            "--quiet",
        ]
    )
    assert result.returncode == 0, result.stderr

    dest = repo / ".ai-memory/models/tiny.onnx"
    assert dest.is_file()
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == expected_sha

    events_path = repo / ".ai-memory/events.jsonl"
    assert events_path.is_file()
    last_event = json.loads(events_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert last_event["type"] == "model_downloaded"
    assert last_event["payload"]["sha256"] == expected_sha
    assert last_event["payload"]["model_hash"] == expected_sha[:16]


def test_cli_quarantines_hash_mismatch(http_serving, tmp_path: Path) -> None:
    base_url, serve_root = http_serving
    repo = tmp_path / "repo"
    repo.mkdir()
    (serve_root / "bad.onnx").write_bytes(b"the actual bytes")

    result = _run_cli(
        [
            "--repo",
            str(repo),
            "--url",
            f"{base_url}/bad.onnx",
            "--sha256",
            "0" * 64,
            "--filename",
            "bad.onnx",
        ]
    )
    assert result.returncode == 4
    quarantine = repo / ".ai-memory/models/bad.onnx.mismatch"
    assert quarantine.is_file()
    assert not (repo / ".ai-memory/models/bad.onnx").exists()


def test_cli_short_circuits_when_already_up_to_date(http_serving, tmp_path: Path) -> None:
    base_url, serve_root = http_serving
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = b"already-here" * 16
    sha = hashlib.sha256(payload).hexdigest()
    dest = repo / ".ai-memory/models/preexisting.onnx"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(payload)
    # Note: serve_root has NO file by that name → if the CLI tried to
    # download we would get a 404 / non-zero exit.  Up-to-date check must
    # short-circuit before any HTTP traffic.
    result = _run_cli(
        [
            "--repo",
            str(repo),
            "--url",
            f"{base_url}/never-fetched.onnx",
            "--sha256",
            sha,
            "--filename",
            "preexisting.onnx",
        ]
    )
    assert result.returncode == 0, result.stderr
    assert "already up-to-date" in result.stdout


def test_cli_output_survives_a_restrictive_console_codepage(tmp_path: Path) -> None:
    """CLI 的正常输出必须是纯 ASCII。

    这个脚本不做任何 console 兜底（没有 reconfigure，也不强制 PYTHONIOENCODING），
    所以打印任何非 ASCII 字符都会在装不下该字符的代码页上抛 UnicodeEncodeError。
    曾经用过的省略号 U+2026 在 cp437 上就会崩，而且是在模型已经校验通过之后才崩。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = b"already-here" * 16
    sha = hashlib.sha256(payload).hexdigest()
    dest = repo / ".ai-memory/models/preexisting.onnx"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(payload)

    script = Path(__file__).resolve().parents[2] / "scripts" / "download_embedding_model.py"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp437"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--repo",
            str(repo),
            "--url",
            "http://127.0.0.1:1/never-fetched.onnx",
            "--sha256",
            sha,
            "--filename",
            "preexisting.onnx",
        ],
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode("cp437", "replace")
    assert b"UnicodeEncodeError" not in result.stderr
    assert result.stdout.decode("ascii")  # 非 ASCII 会让 decode 抛错


def test_cli_rejects_url_without_sha(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    result = _run_cli(["--repo", str(repo), "--url", "http://example.com/x.onnx"])
    assert result.returncode == 2
    assert "requires --sha256" in result.stderr


def test_cli_lists_verified_presets() -> None:
    result = _run_cli(["--list"])
    assert result.returncode == 0, result.stderr
    assert "bge-small-zh-v1.5" in result.stdout
    assert "paraphrase-multilingual-MiniLM-L12-v2" in result.stdout
    assert "verified" in result.stdout
    assert "<fill" not in result.stdout


def test_cli_downloads_explicit_model_and_tokenizer(http_serving, tmp_path: Path) -> None:
    base_url, serve_root = http_serving
    repo = tmp_path / "repo"
    repo.mkdir()

    model_payload = b"fake-model" * 32
    tokenizer_payload = b'{"fake":"tokenizer"}'
    (serve_root / "model.onnx").write_bytes(model_payload)
    (serve_root / "tokenizer.json").write_bytes(tokenizer_payload)

    result = _run_cli(
        [
            "--repo",
            str(repo),
            "--url",
            f"{base_url}/model.onnx",
            "--sha256",
            hashlib.sha256(model_payload).hexdigest(),
            "--filename",
            "model.onnx",
            "--tokenizer-url",
            f"{base_url}/tokenizer.json",
            "--tokenizer-sha256",
            hashlib.sha256(tokenizer_payload).hexdigest(),
            "--dest",
            ".ai-memory/models/custom",
            "--quiet",
        ]
    )
    assert result.returncode == 0, result.stderr
    assert (repo / ".ai-memory/models/custom/model.onnx").is_file()
    assert (repo / ".ai-memory/models/custom/tokenizer.json").is_file()
