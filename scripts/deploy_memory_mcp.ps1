param(
    [string]$PythonExe = "python",
    [string]$RepoRoot,
    [switch]$InstallDev
)

$ErrorActionPreference = "Stop"

$mcpRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "_Resolve-MemoryRoots.ps1")
$roots = Resolve-MemoryRoots -MemoryRoot $mcpRoot -RepoRoot $RepoRoot
$RepoRoot = $roots.RepoRoot

$venvDir = Join-Path $mcpRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

Push-Location $RepoRoot
try {
    if (!(Test-Path $venvPython)) {
        & $PythonExe -m venv $venvDir
    }

    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r (Join-Path $mcpRoot "requirements.txt")

    if ($InstallDev) {
        & $venvPython -m pip install -r (Join-Path $mcpRoot "requirements-dev.txt")
    }

    $env:PYTHONPATH = $mcpRoot
    & $venvPython -m servers.memory_server --help | Out-Null
    Write-Host "Memory MCP deploy completed."
    Write-Host "Repo root  : $RepoRoot"
    Write-Host "Plugin root: $mcpRoot"
    Write-Host "Python     : $venvPython"
    Write-Host "PYTHONPATH should be set to: $mcpRoot"
}
finally {
    Pop-Location
}
