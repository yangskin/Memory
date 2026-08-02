param(
    [string]$RepoRoot
)

$ErrorActionPreference = "Stop"

$memoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "_Resolve-MemoryRoots.ps1")
$roots = Resolve-MemoryRoots -MemoryRoot $memoryRoot -RepoRoot $RepoRoot
$RepoRoot = $roots.RepoRoot

$venvPython = Join-Path $memoryRoot ".venv\Scripts\python.exe"

if (!(Test-Path $venvPython)) {
    throw "Memory MCP venv not found at $venvPython. Run <MemoryRoot>/deploy.ps1 first."
}

Push-Location $RepoRoot
try {
    $env:PYTHONPATH = $memoryRoot
    & $venvPython -m servers.memory_server --root $RepoRoot
}
finally {
    Pop-Location
}
