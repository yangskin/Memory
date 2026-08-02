# vendor/models/ — offline embedding model bundling

This directory is the **offline source** consumed by
`scripts/download_embedding_model.py --vendor-dir <here>` (the deploy
scripts default to it automatically).

## Layout

```
vendor/models/
    <preset>/
        <model_filename>      # e.g. model_quantized.onnx
        <tokenizer_filename>  # e.g. tokenizer.json
        <extra_filename>      # e.g. model_quantized.onnx_data (when present)
```

`<preset>` matches the preset name used by `download_embedding_model.py`
(see `--list`). Currently shipped presets:

- `bge-small-zh-v1.5/`  ≈ **120 MB** total
  - `model_quantized.onnx` (sha256 `99a6e522…`)
  - `tokenizer.json`        (sha256 `3d09c84e…`)
  - `model_quantized.onnx_data` (sha256 `95262348…`)
- `paraphrase-multilingual-MiniLM-L12-v2/`  ≈ **47 MB** total
  - `model_int8.onnx`       (sha256 `d6ea442f…`)
  - `tokenizer.json`        (sha256 `b60b6b43…`)

The downloader verifies every file against the embedded sha256 (see
`scripts/download_embedding_model.py::PRESETS`); mismatched files are
ignored and the deploy will either fall back to HTTP or fail loudly.

## How to populate this directory

Choose **one** path:

1. **Auto-fetch then commit (small models only)** — run
   `scripts/download_embedding_model.py --preset paraphrase-multilingual-MiniLM-L12-v2`
   once, then move `.ai-memory/models/<preset>/` into
   `vendor/models/<preset>/`.
2. **Git LFS (any size)** — `git lfs track "vendor/models/**"` and commit.
   Recommended for the 120 MB `bge-small-zh-v1.5` bundle.
3. **Internal mirror / offline transfer** — copy from a shared drive or
   USB stick that mirrors HuggingFace.

## Why files in this directory are gitignored by default

Plain Git would bloat history with ~120 MB blobs. The repository ignores
`vendor/models/*/*` so the README and any explicit `.gitkeep` survive but
the binary payloads do not enter the index unless you opt-in via Git LFS
or by removing the ignore rule.

## Verifying contents

```powershell
.venv\Scripts\python.exe scripts\download_embedding_model.py `
    --repo . --preset bge-small-zh-v1.5 --no-network
```

`--no-network` refuses HTTP fallback, so the command succeeds only when
every preset artefact is present in `vendor/models/<preset>/` with the
expected sha256.
