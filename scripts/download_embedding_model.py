"""Download an embedding model for the local-onnx provider (P5 Phase 2c).

DesignDoc §15.4 hard rules baked in:

* **Local + CPU only** — the script never installs onnxruntime or any
  Python dep; the user is expected to have ``onnxruntime`` in their venv
  before they ask the provider to load the artefact.
* **Verified by SHA-256** — every download checks against an expected
  digest before the file is moved into ``.ai-memory/models/``.  A
  mismatched download is left at ``<dest>.partial`` for inspection.
* **Auditable** — a ``model_downloaded`` event is appended to
  ``.ai-memory/events.jsonl`` with the truncated model_hash so future
  index rebuilds can correlate which artefact was active.
* **Resumable / idempotent** — if the destination already exists with the
  expected hash the script no-ops (returns exit 0); ``--force`` will
  re-download.

Usage::

    python scripts/download_embedding_model.py \
        --repo C:\\path\\to\\workspace \
        --url https://huggingface.co/.../bge-small-zh.onnx \
        --sha256 <hex>

When ``--url`` / ``--sha256`` are omitted the script falls back to a
preset by ``--preset bge-small-zh-v1.5`` (default).  Presets are kept in
this file (no network metadata fetch) so the script is hermetic.  Presets
download the ONNX model and its real tokenizer side-by-side; external
ONNX data files are also pinned when the upstream model requires them.

Exit codes:
    0  success or already up-to-date
    2  invalid arguments / missing config
    3  download failed (network)
    4  hash mismatch (artefact left at <dest>.partial)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Allow running from inside MCP/Memory/scripts/ without installing the package.
_HERE = Path(__file__).resolve()
_MEMORY_ROOT = _HERE.parents[1]
if str(_MEMORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_MEMORY_ROOT))


# ── Presets ────────────────────────────────────────────────────────────


# Each preset MUST carry sha256 values the user can re-verify out-of-band.
# We refuse to download anything without a sha256 — see ``_validate_sha``.
#
# Notes on source repos:
# * ``onnx-community/bge-small-zh-v1.5-ONNX`` stores the quantized ONNX graph
#   as ``model_quantized.onnx`` plus external data; both files must be kept
#   together in the same directory for ONNX Runtime to load the model.
# * ``Xenova/paraphrase-multilingual-MiniLM-L12-v2`` offers a single-file
#   int8 ONNX artefact plus ``tokenizer.json``.
PRESETS: dict[str, dict[str, object]] = {
    "bge-small-zh-v1.5": {
        "install_dir": "bge-small-zh-v1.5",
        "model": {
            "url": "https://huggingface.co/onnx-community/bge-small-zh-v1.5-ONNX/resolve/main/onnx/model_quantized.onnx",
            "sha256": "99a6e522710c00220c89f8c52e0cc5aa09d4cbb1c34c0e932eab3a9dfdc65df3",
            "filename": "model_quantized.onnx",
        },
        "tokenizer": {
            "url": "https://huggingface.co/onnx-community/bge-small-zh-v1.5-ONNX/resolve/main/tokenizer.json",
            "sha256": "3d09c84ebd10306706a79a8276b3ab736a40d8ec03251c7639f4e52c3a1a4f8e",
            "filename": "tokenizer.json",
        },
        "extra_files": [
            {
                "role": "onnx_data",
                "url": "https://huggingface.co/onnx-community/bge-small-zh-v1.5-ONNX/resolve/main/onnx/model_quantized.onnx_data",
                "sha256": "952623481ca8beea884e3d3c9ecaf8a3c7bf1d0c21de29e970cd31af9d37a90b",
                "filename": "model_quantized.onnx_data",
            }
        ],
    },
    # Backwards-compatible alias used by v0.9.x/v0.10.x docs.
    "bge-small-zh": {
        "alias_for": "bge-small-zh-v1.5",
    },
    "paraphrase-multilingual-MiniLM-L12-v2": {
        "install_dir": "paraphrase-multilingual-MiniLM-L12-v2",
        "model": {
            "url": "https://huggingface.co/Xenova/paraphrase-multilingual-MiniLM-L12-v2/resolve/main/onnx/model_int8.onnx",
            "sha256": "d6ea442ff6a891daefed7c83b2f596fc5dc66bf697e4d006236f64f34bbcf4c8",
            "filename": "model_int8.onnx",
        },
        "tokenizer": {
            "url": "https://huggingface.co/Xenova/paraphrase-multilingual-MiniLM-L12-v2/resolve/main/tokenizer.json",
            "sha256": "b60b6b43406a48bf3638526314f3d232d97058bc93472ff2de930d43686fa441",
            "filename": "tokenizer.json",
        },
    },
    "paraphrase-multilingual-minilm": {
        "alias_for": "paraphrase-multilingual-MiniLM-L12-v2",
    },
}


@dataclass(frozen=True)
class Artifact:
    role: str
    url: str
    sha256: str
    filename: str


# ── Helpers ────────────────────────────────────────────────────────────


def _file_sha256(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, dest: Path, *, timeout: float) -> None:
    """Stream ``url`` to ``dest`` via urllib (stdlib only).

    Writes to a sibling ``<dest>.partial`` first so the destination only
    appears once the bytes are fully on disk.
    """

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".partial")
    if partial.exists():
        partial.unlink()

    req = urllib.request.Request(url, headers={"User-Agent": "memory-mcp/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with partial.open("wb") as fh:
                while True:
                    block = resp.read(1 << 20)
                    if not block:
                        break
                    fh.write(block)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Leave the partial file for the caller to inspect.
        raise SystemExit(3) from exc

    os.replace(partial, dest)


def _emit_event(repo: Path, payload: dict) -> None:
    """Append a ``model_downloaded`` event to ``.ai-memory/events.jsonl``.

    We deliberately bypass ``memory_events.append_event`` here because that
    helper requires a fully-loaded ``MemoryConfig``, and this CLI may run
    before the layout has been touched in any other way.  Format stays
    compatible: one JSON object per line, UTF-8.
    """

    events_path = repo / ".ai-memory/events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "type": "model_downloaded",
        "status": "ok",
        "payload": payload,
    }
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _validate_sha(role: str, sha: object) -> str:
    value = str(sha or "").strip().lower()
    if "<fill" in value or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        print(
            f"error: {role} has no verified sha256; pass explicit URLs/hashes or update the preset.",
            file=sys.stderr,
        )
        sys.exit(2)
    return value


def _canonical_preset_name(name: str) -> str:
    seen: set[str] = set()
    current = name
    while True:
        preset = PRESETS.get(current)
        if not preset:
            print(
                f"error: unknown preset {name!r}; available: {sorted(_real_preset_names())}",
                file=sys.stderr,
            )
            sys.exit(2)
        alias_for = preset.get("alias_for")
        if not alias_for:
            return current
        if current in seen:
            print(f"error: preset alias loop at {current!r}", file=sys.stderr)
            sys.exit(2)
        seen.add(current)
        current = str(alias_for)


def _real_preset_names() -> list[str]:
    return [name for name, preset in PRESETS.items() if "alias_for" not in preset]


def _artifact_from_mapping(role: str, data: object) -> Artifact:
    if not isinstance(data, dict):
        print(f"error: malformed preset artifact: {role}", file=sys.stderr)
        sys.exit(2)
    return Artifact(
        role=role,
        url=str(data.get("url") or "").strip(),
        sha256=_validate_sha(role, data.get("sha256")),
        filename=str(data.get("filename") or Path(str(data.get("url") or "")).name).strip(),
    )


def _resolve_artifacts(args: argparse.Namespace) -> tuple[str | None, list[Artifact]]:
    """Pick artefacts from explicit flags or preset.

    Returns ``(preset_name, artifacts)``. ``preset_name`` is ``None`` for the
    explicit ``--url`` path.
    """
    if args.url and args.sha256:
        artifacts = [
            Artifact(
                role="model",
                url=args.url,
                sha256=_validate_sha("model", args.sha256),
                filename=args.filename or Path(args.url).name,
            )
        ]
        if args.tokenizer_url or args.tokenizer_sha256:
            if not args.tokenizer_url or not args.tokenizer_sha256:
                print("error: --tokenizer-url requires --tokenizer-sha256", file=sys.stderr)
                sys.exit(2)
            artifacts.append(
                Artifact(
                    role="tokenizer",
                    url=args.tokenizer_url,
                    sha256=_validate_sha("tokenizer", args.tokenizer_sha256),
                    filename=args.tokenizer_filename or Path(args.tokenizer_url).name,
                )
            )
        return None, artifacts

    preset_name = _canonical_preset_name(args.preset)
    preset = PRESETS[preset_name]
    artifacts = [
        _artifact_from_mapping("model", preset.get("model")),
        _artifact_from_mapping("tokenizer", preset.get("tokenizer")),
    ]
    for item in preset.get("extra_files", []) if isinstance(preset.get("extra_files", []), list) else []:
        role = str(item.get("role") or "extra") if isinstance(item, dict) else "extra"
        artifacts.append(_artifact_from_mapping(role, item))
    return preset_name, artifacts


def _resolve_dest(args: argparse.Namespace, artifact: Artifact, *, preset_name: str | None, multi: bool) -> Path:
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"error: --repo not a directory: {repo}", file=sys.stderr)
        sys.exit(2)
    if args.dest:
        dest = Path(args.dest)
        resolved = dest.resolve() if dest.is_absolute() else (repo / dest).resolve()
        if multi:
            return resolved / artifact.filename
        return resolved
    if preset_name:
        install_dir = str(PRESETS[preset_name].get("install_dir") or preset_name)
        return repo / ".ai-memory" / "models" / install_dir / artifact.filename
    return repo / ".ai-memory" / "models" / artifact.filename


def _print_presets() -> None:
    print("available presets:")
    for name in _real_preset_names():
        preset = PRESETS[name]
        artifacts = [
            _artifact_from_mapping("model", preset.get("model")),
            _artifact_from_mapping("tokenizer", preset.get("tokenizer")),
        ]
        extra_raw = preset.get("extra_files", [])
        if isinstance(extra_raw, list):
            for item in extra_raw:
                role = str(item.get("role") or "extra") if isinstance(item, dict) else "extra"
                artifacts.append(_artifact_from_mapping(role, item))
        bits = [f"{a.role}:{a.filename}:sha256={a.sha256[:12]}..." for a in artifacts]
        print(f"  {name} verified {' '.join(bits)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo", help="Workspace root containing .ai-memory/")
    parser.add_argument("--preset", default="bge-small-zh-v1.5", help="Named preset to download")
    parser.add_argument("--list", action="store_true", help="List built-in presets and exit")
    parser.add_argument("--url", help="Override preset URL")
    parser.add_argument("--sha256", help="Expected SHA-256 (required when --url is set)")
    parser.add_argument("--filename", help="Override target filename")
    parser.add_argument("--tokenizer-url", help="Optional tokenizer URL for explicit downloads")
    parser.add_argument("--tokenizer-sha256", help="Expected tokenizer SHA-256")
    parser.add_argument("--tokenizer-filename", help="Override tokenizer target filename")
    parser.add_argument("--dest", help="Override destination path (relative to --repo or absolute)")
    parser.add_argument("--force", action="store_true", help="Re-download even if hash matches")
    parser.add_argument("--timeout", type=float, default=180.0, help="Network timeout in seconds")
    parser.add_argument("--quiet", action="store_true", help="Only print on error")
    parser.add_argument(
        "--vendor-dir",
        help=(
            "Optional offline source directory. Layout: <vendor-dir>/<install_dir>/<filename>. "
            "Files matching the expected sha256 are copied locally without touching the network. "
            "Defaults to <memory_root>/vendor/models when omitted."
        ),
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Refuse to fall back to HTTP download; require all artefacts to be present in --vendor-dir.",
    )
    args = parser.parse_args(argv)

    if args.list:
        _print_presets()
        return 0

    if not args.repo:
        print("error: --repo is required unless --list is used", file=sys.stderr)
        return 2

    if args.url and not args.sha256:
        print("error: --url requires --sha256", file=sys.stderr)
        return 2
    if not args.url and (args.tokenizer_url or args.tokenizer_sha256):
        print("error: --tokenizer-url is only valid with explicit --url", file=sys.stderr)
        return 2

    preset_name, artifacts = _resolve_artifacts(args)
    repo = Path(args.repo).resolve()
    multi = len(artifacts) > 1

    # Resolve optional vendor (offline) source directory. Convention:
    #   <vendor_dir>/<install_dir>/<filename>
    # Files that pass sha256 here are copied without any network access.
    vendor_root: Path | None = None
    if args.vendor_dir:
        vendor_root = Path(args.vendor_dir).expanduser().resolve()
    else:
        default_vendor = _MEMORY_ROOT / "vendor" / "models"
        if default_vendor.is_dir():
            vendor_root = default_vendor.resolve()

    def _vendor_candidate(artifact_: Artifact) -> Path | None:
        if vendor_root is None:
            return None
        if preset_name:
            install_dir = str(PRESETS[preset_name].get("install_dir") or preset_name)
            return vendor_root / install_dir / artifact_.filename
        return vendor_root / artifact_.filename

    downloaded_or_verified: list[tuple[Artifact, Path, str]] = []
    any_downloaded = False
    for artifact in artifacts:
        dest = _resolve_dest(args, artifact, preset_name=preset_name, multi=multi)
        if dest.exists() and not args.force:
            actual = _file_sha256(dest)
            if actual.lower() == artifact.sha256:
                if not args.quiet:
                    print(f"already up-to-date: {dest} (sha256={actual[:16]}...)")
                downloaded_or_verified.append((artifact, dest, actual))
                continue
            if not args.quiet:
                print(
                    f"existing {artifact.role} hash mismatch "
                    f"({actual[:16]}... != {artifact.sha256[:16]}...); refreshing"
                )

        # Try vendor (offline) source first.
        vendor_path = _vendor_candidate(artifact)
        copied_from_vendor = False
        if vendor_path is not None and vendor_path.is_file():
            actual_vendor = _file_sha256(vendor_path)
            if actual_vendor.lower() == artifact.sha256:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(vendor_path, dest)
                if not args.quiet:
                    print(
                        f"copied {artifact.role} from vendor: {vendor_path}\n  -> {dest}"
                    )
                copied_from_vendor = True
                any_downloaded = True
            elif not args.quiet:
                print(
                    f"vendor {artifact.role} sha256 mismatch "
                    f"({actual_vendor[:16]}... != {artifact.sha256[:16]}...); ignoring"
                )

        if not copied_from_vendor:
            if args.no_network:
                where = vendor_path if vendor_path is not None else "(no --vendor-dir set)"
                print(
                    f"error: --no-network set and {artifact.role} not available in vendor: {where}",
                    file=sys.stderr,
                )
                return 3
            if not args.quiet:
                print(f"downloading {artifact.role} {artifact.url}\n  -> {dest}")
            _download(artifact.url, dest, timeout=args.timeout)
            any_downloaded = True

        actual = _file_sha256(dest)
        if actual.lower() != artifact.sha256:
            partial = dest.with_suffix(dest.suffix + ".mismatch")
            os.replace(dest, partial)
            print(
                f"error: SHA-256 mismatch for {artifact.role}\n"
                f"  expected: {artifact.sha256}\n"
                f"  actual:   {actual}\n"
                f"  saved to: {partial} (please inspect or delete)",
                file=sys.stderr,
            )
            return 4
        downloaded_or_verified.append((artifact, dest, actual))

    if not any_downloaded:
        return 0

    model_entry = next((entry for entry in downloaded_or_verified if entry[0].role == "model"), downloaded_or_verified[0])

    _emit_event(
        repo,
        {
            "url": model_entry[0].url,
            "dest": str(model_entry[1].relative_to(repo)) if model_entry[1].is_relative_to(repo) else str(model_entry[1]),
            "sha256": model_entry[2],
            "model_hash": model_entry[2][:16],
            "size_bytes": model_entry[1].stat().st_size,
            "preset": preset_name,
            "artifacts": [
                {
                    "role": artifact.role,
                    "url": artifact.url,
                    "dest": str(dest.relative_to(repo)) if dest.is_relative_to(repo) else str(dest),
                    "sha256": actual,
                    "size_bytes": dest.stat().st_size,
                }
                for artifact, dest, actual in downloaded_or_verified
            ],
        },
    )
    if not args.quiet:
        for artifact, dest, actual in downloaded_or_verified:
            print(f"verified {artifact.role} sha256={actual[:16]}... ({dest.stat().st_size} bytes)")
        print(
            "done. enable in .ai-memory/config.json:\n"
            f'  "embeddings": {{ "enabled": true, "provider": "local-onnx", '
            f'"model_path": "{model_entry[1].relative_to(repo) if model_entry[1].is_relative_to(repo) else model_entry[1]}" }}'
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
