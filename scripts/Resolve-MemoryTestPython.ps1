$ErrorActionPreference = "Stop"

# Locate a Python interpreter with pytest installed for running Memory MCP
# tests. Searches in order:
#   1. <MemoryRoot>/.venv/Scripts/python.exe   (recommended; created by deploy.ps1)
#   2. <MemoryRoot>/.venv/bin/python           (Linux/macOS)
#   3. Matching paths under <RepoRoot>          (only if -RepoRoot is supplied)
#
# Backward-compatible: -RepoRoot is still accepted positionally, but plugin
# location is no longer assumed to be <RepoRoot>/MCP/Memory.
function Resolve-MemoryTestPython {
    param(
        [string]$MemoryRoot,
        [string]$RepoRoot
    )

    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($MemoryRoot)) {
        $candidates += (Join-Path $MemoryRoot ".venv/Scripts/python.exe")
        $candidates += (Join-Path $MemoryRoot ".venv/bin/python")
    }
    if (-not [string]::IsNullOrWhiteSpace($RepoRoot)) {
        $candidates += (Join-Path $RepoRoot ".venv/Scripts/python.exe")
        $candidates += (Join-Path $RepoRoot ".venv/bin/python")
    }
    if ($candidates.Count -eq 0) {
        throw "Resolve-MemoryTestPython requires -MemoryRoot or -RepoRoot."
    }

    foreach ($candidate in $candidates) {
        if (!(Test-Path $candidate)) {
            continue
        }
        try {
            & $candidate -c "import pytest" *> $null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        }
        catch {
            continue
        }
    }

    throw "No Python environment with pytest found. Run <MemoryRoot>/deploy.ps1 -InstallDev or install pytest into one of: $($candidates -join '; ')"
}
