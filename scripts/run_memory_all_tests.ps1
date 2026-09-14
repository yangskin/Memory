$ErrorActionPreference = "Stop"
$memoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "Resolve-MemoryTestPython.ps1")
$venvPython = Resolve-MemoryTestPython -MemoryRoot $memoryRoot
Push-Location $memoryRoot
try {
    & $venvPython -m pytest tests/memory_server -q
    if ($LASTEXITCODE -ne 0) {
        throw "Memory Python tests failed with exit code $LASTEXITCODE."
    }

    & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $memoryRoot "tests\setup_mcp_worktree_root.Tests.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Memory PowerShell tests failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
