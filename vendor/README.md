# Offline Python wheels

`vendor/` contains the complete Windows CPython 3.11 runtime dependency set
for `requirements.txt`. Deployment prefers these wheels with `--no-index`, so
the directory must contain one coherent resolver output rather than a mixture
of old and new package versions.

Current baseline:

- Python: CPython 3.11 / Windows x64
- MCP Python SDK: 1.29.x
- MCP 2.x: intentionally excluded until an explicit breaking-change migration

Refresh the wheel set in a temporary directory, review the resolver output,
then replace all root-level `*.whl` files together:

```powershell
python -m pip download `
    --dest <temp-dir> `
    --only-binary=:all: `
    --platform win_amd64 `
    --python-version 311 `
    --implementation cp `
    --abi cp311 `
    "mcp==1.29.0"
```

After replacement, regenerate `SHA256SUMS`, create a clean Python 3.11 virtual
environment, install with `--no-index --find-links vendor`, and run the full
test suite. Embedding model payloads belong under `vendor/models/` and remain
ignored unless a project deliberately adopts Git LFS.
