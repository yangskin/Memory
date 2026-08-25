param(
    [string]$WorkspaceRoot,
    [string]$RepoRoot,
    [switch]$AbsolutePath
)

$ErrorActionPreference = "Stop"

function Convert-ToPosixPath([string]$PathValue) {
    return $PathValue -replace "\\", "/"
}

function Convert-ToHashtable($Value) {
    if ($null -eq $Value) {
        return $null
    }

    if ($Value -is [string] -or $Value -is [ValueType]) {
        return $Value
    }

    if ($Value -is [System.Collections.IDictionary]) {
        $ht = @{}
        foreach ($key in $Value.Keys) {
            $ht[$key] = Convert-ToHashtable $Value[$key]
        }
        return $ht
    }

    if (($Value -is [System.Collections.IEnumerable]) -and !($Value -is [string])) {
        $arr = @()
        foreach ($item in $Value) {
            $arr += ,(Convert-ToHashtable $item)
        }
        return $arr
    }

    if ($Value.PSObject -and $Value.PSObject.Properties.Count -gt 0) {
        $ht = @{}
        foreach ($prop in $Value.PSObject.Properties) {
            $ht[$prop.Name] = Convert-ToHashtable $prop.Value
        }
        return $ht
    }

    return $Value
}

$memoryRoot = (Resolve-Path $PSScriptRoot).Path

# Resolve repo root via the shared helper (supports -RepoRoot, $env:MEMORY_REPO_ROOT,
# .git/.svn/.hg/pyproject.toml/*.uproject/*.code-workspace marker auto-detect).
. (Join-Path $memoryRoot "scripts\_Resolve-MemoryRoots.ps1")
if ([string]::IsNullOrWhiteSpace($RepoRoot) -and -not [string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    # -WorkspaceRoot is the legacy alias of -RepoRoot.
    $RepoRoot = $WorkspaceRoot
}
$roots = Resolve-MemoryRoots -MemoryRoot $memoryRoot -RepoRoot $RepoRoot
$WorkspaceRoot   = $roots.RepoRoot
$memoryRelToRepo = $roots.MemoryRelToRepo  # posix relpath, e.g. "MCP/Memory" or "Tools/Memory"

# Path inside mcp.json:
#   - default: ${workspaceFolder}/<MemoryRelToRepo>/...   (portable across team)
#   - -AbsolutePath OR plugin lives outside repo (rel = $null): write absolute paths
$useWorkspaceFolderVar = (-not $AbsolutePath) -and ($null -ne $memoryRelToRepo)

$vscodeDir = Join-Path $WorkspaceRoot ".vscode"
$mcpJsonPath = Join-Path $vscodeDir "mcp.json"
$venvPython = Join-Path $memoryRoot ".venv\Scripts\python.exe"

if (!(Test-Path $venvPython)) {
    throw "Python venv not found: $venvPython. Run <MemoryRoot>/deploy.ps1 first."
}

if (!(Test-Path $vscodeDir)) {
    New-Item -ItemType Directory -Path $vscodeDir -Force | Out-Null
}

$mcpConfig = @{}
if (Test-Path $mcpJsonPath) {
    $raw = Get-Content $mcpJsonPath -Raw -Encoding UTF8
    if (![string]::IsNullOrWhiteSpace($raw)) {
        $parsed = $raw | ConvertFrom-Json
        $mcpConfig = Convert-ToHashtable $parsed
    }
}

if ($null -eq $mcpConfig -or $mcpConfig.Count -eq 0) {
    $mcpConfig = @{}
}

if (!$mcpConfig.ContainsKey("servers") -or $null -eq $mcpConfig["servers"]) {
    $mcpConfig["servers"] = @{}
}

$servers = $mcpConfig["servers"]

function Test-IsMemoryServerEntry($Entry) {
    if ($null -eq $Entry) { return $false }
    $entryMap = Convert-ToHashtable $Entry
    if ($null -eq $entryMap -or -not $entryMap.ContainsKey("args")) { return $false }
    foreach ($arg in $entryMap["args"]) {
        if ([string]$arg -eq "servers.memory_server") { return $true }
    }
    return $false
}

# Path stored in mcp.json: prefer ${workspaceFolder}/<rel> so the file is
# portable across machines and team members. -AbsolutePath (or plugin not
# under the repo) forces absolute paths instead.
$rootArg = if ($useWorkspaceFolderVar) { '${workspaceFolder}' } else { Convert-ToPosixPath $WorkspaceRoot }
$pythonArg = if ($useWorkspaceFolderVar) {
    '${workspaceFolder}/' + $memoryRelToRepo + '/.venv/Scripts/python.exe'
} else {
    Convert-ToPosixPath $venvPython
}
$pythonPathArg = if ($useWorkspaceFolderVar) {
    '${workspaceFolder}/' + $memoryRelToRepo
} else {
    Convert-ToPosixPath $memoryRoot
}

$serverEntry = @{
    command = $pythonArg
    args = @(
        "-m",
        "servers.memory_server",
        "--root",
        $rootArg
    )
    env = @{
        PYTHONPATH = $pythonPathArg
        PYTHONUTF8 = "1"
    }
}

$servers["project-memory-mcp"] = $serverEntry

if ($servers.ContainsKey("memory-mcp") -and (Test-IsMemoryServerEntry $servers["memory-mcp"])) {
    $servers["memory-mcp"] = $serverEntry
    Write-Host "Migrated legacy server: memory-mcp"
}

$compactJson = $mcpConfig | ConvertTo-Json -Depth 50 -Compress
$tempJsonPath = Join-Path $env:TEMP ("mcp_setup_" + [System.Guid]::NewGuid().ToString("N") + ".json")
[System.IO.File]::WriteAllText($tempJsonPath, $compactJson, [System.Text.UTF8Encoding]::new($false))

$prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$fmtOutput = & $venvPython -c "import json, pathlib, sys; src=pathlib.Path(sys.argv[1]); dst=pathlib.Path(sys.argv[2]); obj=json.loads(src.read_text(encoding='utf-8')); dst.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')" $tempJsonPath $mcpJsonPath 2>&1
$fmtExitCode = $LASTEXITCODE
$ErrorActionPreference = $prevEAP
if ($fmtExitCode -ne 0) {
    Remove-Item $tempJsonPath -Force -ErrorAction SilentlyContinue
    $fmtOutput | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    throw "Failed to format mcp.json with Python."
}

Remove-Item $tempJsonPath -Force

Write-Host "Updated VS Code MCP config: $mcpJsonPath"
Write-Host "Registered server: project-memory-mcp"

function Get-UserHomePath {
    $candidates = @(
        $env:USERPROFILE,
        $HOME,
        [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

    foreach ($candidate in $candidates) {
        $resolved = Resolve-Path -LiteralPath $candidate -ErrorAction SilentlyContinue
        if ($resolved) { return $resolved.Path }
    }
    return $null
}

function Update-McpJsonServerEntry {
    param(
        [string]$Path,
        [string]$TopKey,
        [string]$Label,
        [hashtable]$Entry,
        [switch]$CreateIfMissing
    )

    $exists = Test-Path $Path
    if (-not $exists -and -not $CreateIfMissing) {
        Write-Host "Skipped $Label MCP config (not detected): $Path"
        return
    }

    $config = @{}
    if ($exists) {
        try {
            $raw = Get-Content $Path -Raw -Encoding UTF8
            if (![string]::IsNullOrWhiteSpace($raw)) {
                $config = Convert-ToHashtable ($raw | ConvertFrom-Json)
            }
        } catch {
            $backupPath = "$Path.bak.$(Get-Date -Format 'yyyyMMddHHmmss')"
            Copy-Item $Path $backupPath -Force
            Write-Host "WARNING: $Label config JSON is invalid; backed up to $backupPath"
            $config = @{}
        }
    }

    if (!$config.ContainsKey($TopKey) -or $null -eq $config[$TopKey]) {
        $config[$TopKey] = @{}
    }
    $config[$TopKey]["project-memory-mcp"] = $Entry

    $dir = Split-Path -Parent $Path
    if (!(Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    $json = $config | ConvertTo-Json -Depth 50
    [System.IO.File]::WriteAllText($Path, $json, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Updated $Label MCP config: $Path"
}

function Format-TomlString {
    param([string]$Value)
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

function Format-TomlStringArray {
    param([string[]]$Values)
    $items = @($Values | ForEach-Object { Format-TomlString $_ })
    return "[" + ($items -join ", ") + "]"
}

function Remove-TomlSection {
    param(
        [string]$Content,
        [string]$SectionName
    )
    $escaped = [regex]::Escape($SectionName)
    return [regex]::Replace($Content, "(?ms)^\[$escaped\]\r?\n.*?(?=^\[|\z)", "").TrimEnd()
}

function Get-CodexConfigPath {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
        $candidates += (Join-Path $env:CODEX_HOME "config.toml")
    }

    $userHome = Get-UserHomePath
    if ($userHome) {
        $candidates += (Join-Path $userHome ".codex\config.toml")
    }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-Path $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
        return (Join-Path ([System.IO.Path]::GetFullPath($env:CODEX_HOME)) "config.toml")
    }
    if ($userHome) {
        return (Join-Path $userHome ".codex\config.toml")
    }
    return $null
}

function Update-CodexMemoryServer {
    param(
        [string]$Path,
        [hashtable]$Entry
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        Write-Host "WARNING: Codex user config path not detected; skipped."
        return
    }

    $content = ""
    if (Test-Path $Path) {
        $content = Get-Content $Path -Raw -Encoding UTF8
    }
    $content = Remove-TomlSection -Content $content -SectionName "mcp_servers.project-memory-mcp"
    $content = Remove-TomlSection -Content $content -SectionName "mcp_servers.project-memory-mcp.env"

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("[mcp_servers.project-memory-mcp]") | Out-Null
    $lines.Add("command = $(Format-TomlString $Entry.command)") | Out-Null
    $lines.Add("args = $(Format-TomlStringArray $Entry.args)") | Out-Null
    $lines.Add("enabled = true") | Out-Null
    $lines.Add("") | Out-Null
    $lines.Add("[mcp_servers.project-memory-mcp.env]") | Out-Null
    foreach ($envName in ($Entry.env.Keys | Sort-Object)) {
        $lines.Add("$envName = $(Format-TomlString $Entry.env[$envName])") | Out-Null
    }

    $newContent = $content.TrimEnd()
    if (-not [string]::IsNullOrWhiteSpace($newContent)) {
        $newContent += "`r`n`r`n"
    }
    $newContent += ($lines -join "`r`n") + "`r`n"

    $dir = Split-Path -Parent $Path
    if (!(Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    [System.IO.File]::WriteAllText($Path, $newContent, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Updated Codex MCP config: $Path"
}

$userServerEntry = @{
    command = Convert-ToPosixPath $venvPython
    args = @(
        "-m",
        "servers.memory_server",
        "--root",
        (Convert-ToPosixPath $WorkspaceRoot)
    )
    env = @{
        PYTHONPATH = Convert-ToPosixPath $memoryRoot
        PYTHONUTF8 = "1"
    }
}

Update-McpJsonServerEntry `
    -Path (Join-Path $WorkspaceRoot ".cursor\mcp.json") `
    -TopKey "mcpServers" `
    -Label "Cursor (project)" `
    -Entry $userServerEntry `
    -CreateIfMissing

$userHomePath = Get-UserHomePath
if ($userHomePath) {
    Update-McpJsonServerEntry `
        -Path (Join-Path $userHomePath ".gongfeng-copilot\mcp.json") `
        -TopKey "mcpServers" `
        -Label "gongfeng-copilot" `
        -Entry $userServerEntry `
        -CreateIfMissing

    Update-McpJsonServerEntry `
        -Path (Join-Path $userHomePath ".codebuddy\mcp.json") `
        -TopKey "mcpServers" `
        -Label "CodeBuddy" `
        -Entry $userServerEntry `
        -CreateIfMissing

    $cursorDir = Join-Path $userHomePath ".cursor"
    if (Test-Path $cursorDir) {
        Update-McpJsonServerEntry `
            -Path (Join-Path $cursorDir "mcp.json") `
            -TopKey "mcpServers" `
            -Label "Cursor" `
            -Entry $userServerEntry
    }
}

if (-not [string]::IsNullOrWhiteSpace($env:APPDATA)) {
    $vscodeUserDir = Join-Path $env:APPDATA "Code\User"
    if (Test-Path $vscodeUserDir) {
        Update-McpJsonServerEntry `
            -Path (Join-Path $vscodeUserDir "mcp.json") `
            -TopKey "servers" `
            -Label "VS Code Copilot user" `
            -Entry $userServerEntry
    }
}

Update-CodexMemoryServer -Path (Get-CodexConfigPath) -Entry $userServerEntry
