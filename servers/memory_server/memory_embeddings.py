"""Embedding provider abstraction for the local RAG / vector tier (P5 Phase 1).

============================================================================
⚠️  EXPERIMENTAL — FROZEN  (DesignDoc §15.5 / §15.x slim-down decision)
----------------------------------------------------------------------------
Provider layer of the frozen vector tier.  See ``memory_vector_search``
for the full freeze rationale.  Default config keeps this dormant; do not
build on it unless a §15.5 activation threshold is met.
============================================================================

Design contract (see MemorySystemDesignDocument.md §15.4):

* All embedding work must run **locally on CPU** with no required network
  call.  GPU / remote API providers are optional and never the default.
* Providers expose an identical interface so the index format
  (``.ai-memory/vector_index/<provider_id>__<model_hash>/``) stays portable
  and any provider/model change deterministically invalidates the index.
* This module ships a zero-dependency :class:`DeterministicHashProvider` so
  the vector tier is always operable (mainly for tests and as the ultimate
  fallback when no real embedding model is installed).  ONNX-backed
  providers land in Phase 2.

Failure policy:

* Provider construction must never raise on import; missing optional deps
  are reported via :func:`available_providers`.
* Any per-call failure surfaces as :class:`EmbeddingError` so callers can
  cleanly skip the vector tier (per §15.4.1 "可选 + 可降级").
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmbeddingError(RuntimeError):
    """Raised when an embedding provider cannot fulfil a request.

    Callers (retrieval / key_documents embedding renderer) MUST treat this
    as a soft signal to skip the vector tier, never as a hard fault that
    blocks the main read/write/FTS pipeline.
    """


class ProviderUnavailableError(EmbeddingError):
    """Raised when a provider is requested but its runtime is missing."""


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingMetadata:
    """Identity + shape information that must be persisted with every index.

    ``provider_id`` and ``model_hash`` together form the on-disk directory
    name.  Any change in either field invalidates the index and triggers a
    full rebuild (see §15.4.4).
    """

    provider_id: str
    model_hash: str
    dim: int
    normalized: bool

    def index_dir_name(self) -> str:
        return f"{self.provider_id}__{self.model_hash}"

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "model_hash": self.model_hash,
            "dim": self.dim,
            "normalized": self.normalized,
        }


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class EmbeddingProvider(ABC):
    """Minimal embedding provider interface.

    Implementations MUST be deterministic with respect to (text, metadata):
    given the same text and the same ``metadata.model_hash`` the returned
    vector must be byte-identical across runs and machines.  This is what
    lets the on-disk index survive process restarts and be diff-friendly.
    """

    @property
    @abstractmethod
    def metadata(self) -> EmbeddingMetadata: ...

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per input text.

        Implementations MUST raise :class:`EmbeddingError` (or subclass) on
        any failure so the caller can fall back to the FTS-only path.
        """


# ---------------------------------------------------------------------------
# Deterministic hash provider (zero-dependency baseline)
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    """Tokenise into ASCII words + individual CJK characters.

    Mirrors the FTS5 CJK bigram/trigram tokenisation philosophy used
    elsewhere in this server: never relies on any external tokenizer, so it
    works identically inside CI sandboxes and on user machines.
    """

    if not text:
        return []
    return [match.group(0).lower() for match in _TOKEN_RE.finditer(text)]


class DeterministicHashProvider(EmbeddingProvider):
    """Zero-dependency embedding provider used as test baseline + fallback.

    The vector is built by hashing each token into ``dim`` buckets (signed
    feature hashing).  This gives a true vector — supports cosine
    similarity, additive composition, etc. — without pulling any model.

    It captures **lexical** similarity only; semantic synonym recall is
    weak.  Phase 2 ONNX providers replace this for serious queries.  The
    provider stays in the registry so:

    * Tests run with no extra deps.
    * Users with no model installed still get long-tail alias matching
      instead of an error (per §15.4.1 "可选 + 可降级").
    """

    DEFAULT_DIM = 64

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        if dim <= 0:
            raise ValueError("dim must be a positive integer")
        self._dim = int(dim)
        # model_hash is purely a function of (provider_id, dim) so the
        # on-disk index dir name is stable across runs.
        digest = hashlib.sha256(f"deterministic-hash:dim={self._dim}".encode("utf-8")).hexdigest()
        self._metadata = EmbeddingMetadata(
            provider_id="deterministic-hash",
            model_hash=digest[:16],
            dim=self._dim,
            normalized=True,
        )

    @property
    def metadata(self) -> EmbeddingMetadata:
        return self._metadata

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not isinstance(texts, (list, tuple)):
            raise EmbeddingError("texts must be a list or tuple of strings")
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise EmbeddingError(f"text must be str, got {type(text).__name__}")
        vector = [0.0] * self._dim
        tokens = _tokenize(text)
        if not tokens:
            return vector
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % self._dim
            sign = 1.0 if (digest[4] & 1) == 0 else -1.0
            vector[bucket] += sign
        # L2 normalise so cosine similarity reduces to dot product.
        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0.0:
            vector = [v / norm for v in vector]
        return vector


# ---------------------------------------------------------------------------
# Local ONNX provider (Phase 2c — optional, CPU EP only)
# ---------------------------------------------------------------------------


def _onnxruntime_available() -> bool:
    """Return True iff ``onnxruntime`` can be imported in the current env.

    Kept as a function (not a module-level boolean) so tests can monkey-patch
    or invalidate the result by reloading the module.
    """

    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False
    return True


def _file_sha256(path: Path, *, chunk: int = 1 << 20) -> str:
    """Stream a file through SHA-256 — used to derive the model_hash.

    The hash is truncated to 16 hex chars when persisted in the index dir
    name (matches :class:`DeterministicHashProvider`).  We return the full
    digest here so the model-download CLI can verify the artefact too.
    """

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class LocalOnnxProvider(EmbeddingProvider):
    """CPU-only ONNX Runtime embedding provider (DesignDoc \u00a715.4.1).

    Hard rules baked into the constructor:

    * ``providers=["CPUExecutionProvider"]`` is forced; we never request a
      GPU EP even if onnxruntime-gpu is installed.  Phase 3 may relax this
      if \u00a715.4.2 thresholds ever get hit.
    * Inter-op + intra-op thread pools are capped to keep the editor and
      MCP server responsive on developer machines.
    * The model file path is resolved from ``MemoryConfig.embeddings_model_path``
      by the caller \u2014 the provider itself only takes a concrete ``Path``.
    * ``model_hash`` is the SHA-256 of the .onnx bytes (truncated to 16 hex
      chars in the index dir name) so swapping models forces a clean rebuild.

    Tokenizer (\u00a715.1-A): a real tokenizer must accompany the model.

    * ``tokenizer="auto"`` (default) probes the directory next to
      ``model_path`` for ``tokenizer.json`` (HuggingFace ``tokenizers``)
      and falls back to ``*.model`` / ``spiece.model`` (SentencePiece).
    * An explicit ``Path`` skips probing and loads the file as-is.
    * A ``callable(list[str]) -> tuple[list[list[int]], list[list[int]]]``
      (returns ``(input_ids, attention_mask)``) bypasses tokenizer-file
      loading entirely \u2014 used by tests with toy ONNX models.
    * If the tokenizer cannot be resolved, the constructor raises
      :class:`ProviderUnavailableError` so ``get_provider("auto", ...)``
      cleanly degrades to the deterministic-hash baseline (no stub output).

    The provider stays import-safe: ``onnxruntime`` is imported lazily in
    ``__init__`` and any failure is wrapped in :class:`ProviderUnavailableError`.
    Per-call failures wrap as :class:`EmbeddingError` so callers degrade.
    """

    DEFAULT_DIM_HINT = 384  # bge-small / e5-small family
    DEFAULT_MAX_TOKENS = 256
    _CPU_PROVIDERS = ("CPUExecutionProvider",)

    def __init__(
        self,
        model_path: Path,
        *,
        tokenizer: "str | Path | Callable[[list[str]], tuple[list[list[int]], list[list[int]]]]" = "auto",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        intra_op_threads: int | None = None,
        inter_op_threads: int | None = None,
    ) -> None:
        if not isinstance(model_path, Path):
            raise ProviderUnavailableError(
                f"model_path must be a Path, got {type(model_path).__name__}"
            )
        if not model_path.is_file():
            raise ProviderUnavailableError(
                f"ONNX model file not found: {model_path}"
            )
        try:
            import onnxruntime as ort
        except Exception as exc:
            raise ProviderUnavailableError(
                f"onnxruntime is not importable: {type(exc).__name__}: {exc}"
            ) from exc

        self._model_path = model_path
        self._max_tokens = max(1, int(max_tokens))
        self._lock = threading.Lock()  # ort sessions are not strictly thread-safe

        # Resolve tokenizer BEFORE building the ORT session so a missing
        # tokenizer fails fast and ``get_provider("auto", ...)`` falls back
        # to deterministic-hash without any side effects.
        self._tokenize_fn, self._tokenizer_kind, self._tokenizer_path = (
            self._resolve_tokenizer(model_path, tokenizer)
        )

        try:
            options = ort.SessionOptions()
            if intra_op_threads is not None:
                options.intra_op_num_threads = max(1, int(intra_op_threads))
            if inter_op_threads is not None:
                options.inter_op_num_threads = max(1, int(inter_op_threads))
            options.graph_optimization_level = (
                ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            )
            self._session = ort.InferenceSession(
                str(model_path),
                sess_options=options,
                providers=list(self._CPU_PROVIDERS),
            )
        except Exception as exc:
            raise ProviderUnavailableError(
                f"failed to create ONNX session for {model_path.name}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        # Probe the output shape with a single dummy token so we capture the
        # real embedding dim from the model itself (bge-small=384, e5-small=384,
        # bge-base=768, \u2026).  Falls back to DEFAULT_DIM_HINT if probing fails.
        self._inputs = [inp.name for inp in self._session.get_inputs()]
        try:
            probe = self._run_session([[101, 102]], [[1, 1]])
            dim = int(probe.shape[-1])
        except Exception:
            dim = self.DEFAULT_DIM_HINT

        digest_full = _file_sha256(model_path)
        # Fold the tokenizer file hash into the model hash so swapping
        # tokenizers also invalidates the on-disk index.
        if self._tokenizer_path is not None and self._tokenizer_path.is_file():
            try:
                tok_digest = _file_sha256(self._tokenizer_path)
                digest_full = hashlib.sha256(
                    (digest_full + ":" + tok_digest).encode("utf-8")
                ).hexdigest()
            except OSError:
                pass
        self._metadata = EmbeddingMetadata(
            provider_id="local-onnx",
            model_hash=digest_full[:16],
            dim=dim,
            normalized=True,
        )

    @property
    def metadata(self) -> EmbeddingMetadata:
        return self._metadata

    @property
    def model_path(self) -> Path:
        return self._model_path

    @property
    def tokenizer_path(self) -> Path | None:
        return self._tokenizer_path

    @property
    def tokenizer_kind(self) -> str:
        return self._tokenizer_kind

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not isinstance(texts, (list, tuple)):
            raise EmbeddingError("texts must be a list or tuple of strings")
        if not texts:
            return []
        try:
            input_ids, attention_mask = self._tokenise_batch(list(texts))
            output = self._run_session(input_ids, attention_mask)
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(
                f"ONNX inference failed: {type(exc).__name__}: {exc}"
            ) from exc
        return self._postprocess(output, attention_mask)

    # \u2500\u2500 internals \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    @staticmethod
    def _resolve_tokenizer(
        model_path: Path,
        tokenizer,
    ) -> tuple["Callable[[list[str]], tuple[list[list[int]], list[list[int]]]]", str, "Path | None"]:
        """Return ``(tokenize_fn, kind, path)`` or raise ProviderUnavailableError.

        ``kind`` \u2208 {``"huggingface"``, ``"sentencepiece"``, ``"callable"``}.
        """

        if callable(tokenizer):
            return tokenizer, "callable", None

        # Resolve a concrete file path.
        candidate: Path | None = None
        if isinstance(tokenizer, Path):
            candidate = tokenizer
        elif isinstance(tokenizer, str) and tokenizer != "auto":
            candidate = Path(tokenizer)
        else:
            # auto-discovery: look in the model directory.
            parent = model_path.parent
            for name in ("tokenizer.json",):
                p = parent / name
                if p.is_file():
                    candidate = p
                    break
            if candidate is None:
                for name in ("spiece.model", "sentencepiece.model", "tokenizer.model"):
                    p = parent / name
                    if p.is_file():
                        candidate = p
                        break

        if candidate is None or not candidate.is_file():
            raise ProviderUnavailableError(
                f"tokenizer file not found alongside {model_path.name}; "
                "expected tokenizer.json (HuggingFace) or spiece.model "
                "(SentencePiece) in the same directory"
            )

        suffix = candidate.suffix.lower()
        if suffix == ".json":
            try:
                from tokenizers import Tokenizer  # type: ignore[import-not-found]
            except Exception as exc:
                raise ProviderUnavailableError(
                    f"HuggingFace 'tokenizers' package is required for {candidate.name}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            try:
                tok = Tokenizer.from_file(str(candidate))
            except Exception as exc:
                raise ProviderUnavailableError(
                    f"failed to load tokenizer.json {candidate}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            def _hf_tokenise(texts: list[str]):
                encs = tok.encode_batch([t or "" for t in texts])
                ids = [list(e.ids) for e in encs]
                masks = [list(e.attention_mask) for e in encs]
                return ids, masks

            return _hf_tokenise, "huggingface", candidate

        # SentencePiece fallback.
        try:
            import sentencepiece as spm  # type: ignore[import-not-found]
        except Exception as exc:
            raise ProviderUnavailableError(
                f"'sentencepiece' package is required for {candidate.name}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        try:
            sp = spm.SentencePieceProcessor()
            sp.Load(str(candidate))
        except Exception as exc:
            raise ProviderUnavailableError(
                f"failed to load sentencepiece model {candidate}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        def _sp_tokenise(texts: list[str]):
            ids = [list(sp.EncodeAsIds(t or "")) for t in texts]
            masks = [[1] * len(row) for row in ids]
            return ids, masks

        return _sp_tokenise, "sentencepiece", candidate

    def _tokenise_batch(
        self, texts: list[str]
    ) -> tuple[list[list[int]], list[list[int]]]:
        """Tokenise via the resolved real tokenizer.

        \u00a715.1-A: the previous byte-level hash stub has been removed; missing
        tokenizers now surface as :class:`ProviderUnavailableError` at
        construction time so we never return bogus token IDs to the model.
        """

        ids, masks = self._tokenize_fn(texts)
        # Truncate + pad. Truncation must happen before padding so the mask
        # stays consistent with the batch width.
        ids = [list(row)[: self._max_tokens] or [0] for row in ids]
        masks = [list(row)[: self._max_tokens] or [0] for row in masks]
        for row, mask in zip(ids, masks):
            # Defensive: keep id and mask lengths aligned even if a custom
            # callable returns slightly mismatched rows.
            if len(mask) < len(row):
                mask.extend([1] * (len(row) - len(mask)))
            elif len(mask) > len(row):
                del mask[len(row):]
        width = max((len(r) for r in ids), default=1)
        for row, mask in zip(ids, masks):
            pad = width - len(row)
            if pad:
                row.extend([0] * pad)
                mask.extend([0] * pad)
        return ids, masks

    def _run_session(
        self, input_ids: list[list[int]], attention_mask: list[list[int]]
    ):
        feeds: dict[str, object] = {}
        if "input_ids" in self._inputs:
            feeds["input_ids"] = self._as_int64(input_ids)
        if "attention_mask" in self._inputs:
            feeds["attention_mask"] = self._as_int64(attention_mask)
        if "token_type_ids" in self._inputs:
            feeds["token_type_ids"] = self._as_int64(
                [[0] * len(row) for row in input_ids]
            )
        if not feeds:
            raise EmbeddingError(
                f"ONNX model exposes no recognised inputs: {self._inputs!r}"
            )
        with self._lock:
            outputs = self._session.run(None, feeds)
        return outputs[0]

    @staticmethod
    def _as_int64(matrix: list[list[int]]):
        # numpy is a hard transitive dep of onnxruntime, so importing it is
        # safe inside this branch.
        import numpy as np

        return np.asarray(matrix, dtype=np.int64)

    def _postprocess(
        self, raw, attention_mask: list[list[int]]
    ) -> list[list[float]]:
        import numpy as np

        arr = np.asarray(raw)
        if arr.ndim == 3:
            # token-level output → mean-pool over valid tokens
            mask = np.asarray(attention_mask, dtype=arr.dtype)[..., None]
            summed = (arr * mask).sum(axis=1)
            counts = np.maximum(mask.sum(axis=1), 1)
            arr = summed / counts
        elif arr.ndim != 2:
            raise EmbeddingError(
                f"unexpected ONNX output shape: {arr.shape}; expected 2D or 3D"
            )
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms = np.where(norms == 0.0, 1.0, norms)
        normed = (arr / norms).astype(np.float32, copy=False)
        return normed.tolist()


# ---------------------------------------------------------------------------
# Similarity helpers (CPU only, pure stdlib)
# ---------------------------------------------------------------------------


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    Used by callers that only have a few thousand vectors to compare
    (current corpus is well under §15.4.2's 100k threshold, so brute-force
    cosine in Python is fast enough — see design doc for the budget).
    """

    if len(a) != len(b):
        raise EmbeddingError(f"vector dim mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


def available_providers() -> list[str]:
    """Return the list of provider IDs that can be instantiated right now.

    Phase 1 only ships ``deterministic-hash``.  Phase 2c appends
    ``local-onnx`` whenever ``onnxruntime`` is importable; the actual
    construction still requires a model file (resolved by the caller
    from ``MemoryConfig.embeddings_model_path``).
    """

    providers = ["deterministic-hash"]
    if _onnxruntime_available():
        providers.append("local-onnx")
    return providers


def get_provider(name: str = "auto", **kwargs) -> EmbeddingProvider:
    """Construct an embedding provider by name.

    ``name="auto"`` prefers ``local-onnx`` when both ``onnxruntime`` and a
    valid ``model_path`` (passed in via ``kwargs``) are available, otherwise
    falls through to :class:`DeterministicHashProvider`.  Any failure to
    construct the ONNX provider degrades silently to the hash baseline so
    the caller never has to handle exceptions for the optional tier.
    """

    if name == "local-onnx":
        model_path = kwargs.get("model_path")
        if model_path is None:
            raise ProviderUnavailableError(
                "local-onnx provider requires model_path=Path(...)"
            )
        return LocalOnnxProvider(
            Path(model_path),
            tokenizer=kwargs.get("tokenizer", "auto"),
            max_tokens=int(kwargs.get("max_tokens", LocalOnnxProvider.DEFAULT_MAX_TOKENS)),
            intra_op_threads=kwargs.get("intra_op_threads"),
            inter_op_threads=kwargs.get("inter_op_threads"),
        )
    if name == "auto":
        model_path = kwargs.get("model_path")
        if (
            _onnxruntime_available()
            and model_path is not None
            and Path(model_path).is_file()
        ):
            try:
                return LocalOnnxProvider(
                    Path(model_path),
                    tokenizer=kwargs.get("tokenizer", "auto"),
                )
            except ProviderUnavailableError:
                pass  # fall through to deterministic fallback
        dim = int(kwargs.get("dim", DeterministicHashProvider.DEFAULT_DIM))
        return DeterministicHashProvider(dim=dim)
    if name == "deterministic-hash":
        dim = int(kwargs.get("dim", DeterministicHashProvider.DEFAULT_DIM))
        return DeterministicHashProvider(dim=dim)
    raise ProviderUnavailableError(
        f"unknown or unavailable embedding provider: {name!r}; "
        f"available={available_providers()}"
    )


__all__ = [
    "EmbeddingError",
    "ProviderUnavailableError",
    "EmbeddingMetadata",
    "EmbeddingProvider",
    "DeterministicHashProvider",
    "cosine_similarity",
    "available_providers",
    "get_provider",
]
