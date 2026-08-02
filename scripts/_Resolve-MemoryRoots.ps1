# _Resolve-MemoryRoots.ps1
#
# Shared helper used by every Memory MCP shell entry point so the plugin
# can be deployed at ANY relative path inside a host project (legacy
# `MCP/Memory/`, generic `Tools/Memory/`, monorepo `vendor/memory-mcp/`,
# etc.) without changing any code.
#
# Usage (any caller):
#     . (Join-Path $PSScriptRoot "_Resolve-MemoryRoots.ps1")  # if in scripts/
#     . (Join-Path $PSScriptRoot "scripts\_Resolve-MemoryRoots.ps1")  # if at MCP/Memory root
#     $r = Resolve-MemoryRoots -MemoryRoot <plugin root> -RepoRoot $RepoRoot
#     $r.RepoRoot         # absolute path to host project root
#     $r.MemoryRoot       # absolute path to this plugin (= parent of scripts/)
#     $r.MemoryRelToRepo  # posix relpath from RepoRoot to MemoryRoot, or $null
#                         # if the plugin lives outside the repo (symlink etc).
#
# Repo-root resolution priority:
#   1. -RepoRoot parameter
#   2. $env:MEMORY_REPO_ROOT
#   3. Walk up from MemoryRoot looking for marker files:
#        .git / .svn / .hg / pyproject.toml / *.uproject / *.code-workspace
#   4. Fall back to MemoryRoot/../.. (legacy MCP/Memory layout) with a warning.

$ErrorActionPreference = "Stop"

function Resolve-MemoryRoots {
    param(
        [Parameter(Mandatory = $true)][string]$MemoryRoot,
        [string]$RepoRoot
    )

    $MemoryRoot = (Resolve-Path $MemoryRoot).Path

    if ([string]::IsNullOrWhiteSpace($RepoRoot) -and -not [string]::IsNullOrWhiteSpace($env:MEMORY_REPO_ROOT)) {
        $RepoRoot = $env:MEMORY_REPO_ROOT
    }

    if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
        $current = Split-Path $MemoryRoot -Parent
        $markerFiles = @('.git', '.svn', '.hg', 'pyproject.toml')
        $markerGlobs = @('*.uproject', '*.code-workspace', '*.sln')
        while ($current -and ($current -ne (Split-Path $current -Parent))) {
            $hit = $false
            foreach ($m in $markerFiles) {
                if (Test-Path (Join-Path $current $m)) { $hit = $true; break }
            }
            if (-not $hit) {
                foreach ($g in $markerGlobs) {
                    $found = Get-ChildItem $current -Filter $g -File -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($found) { $hit = $true; break }
                }
            }
            if ($hit) { $RepoRoot = $current; break }
            $current = Split-Path $current -Parent
        }
    }

    if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
        $RepoRoot = (Resolve-Path (Join-Path $MemoryRoot "..\..")).Path
        Write-Warning "[MemoryMCP] Could not auto-detect repo root (looked for .git / .svn / .hg / pyproject.toml / *.uproject / *.code-workspace / *.sln). Falling back to legacy layout: $RepoRoot. Pass -RepoRoot <path> or set `$env:MEMORY_REPO_ROOT to disable this warning."
    }
    else {
        $RepoRoot = (Resolve-Path $RepoRoot).Path
    }

    $rel = $null
    try {
        $repoUri   = [Uri]($RepoRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar)
        $memoryUri = [Uri]$MemoryRoot
        $relRaw    = [Uri]::UnescapeDataString($repoUri.MakeRelativeUri($memoryUri).ToString())
        if (-not $relRaw.StartsWith('..') -and -not [System.IO.Path]::IsPathRooted($relRaw)) {
            $rel = ($relRaw -replace '\\', '/').TrimEnd('/')
        }
    }
    catch {
        $rel = $null
    }

    return [pscustomobject]@{
        MemoryRoot      = $MemoryRoot
        RepoRoot        = $RepoRoot
        MemoryRelToRepo = $rel
    }
}
