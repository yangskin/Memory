$ErrorActionPreference = "Stop"
$memoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "Resolve-MemoryTestPython.ps1")
$venvPython = Resolve-MemoryTestPython -MemoryRoot $memoryRoot
Push-Location $memoryRoot
try {
    & $venvPython -m pytest tests/memory_server -q
}
finally {
    Pop-Location
}
