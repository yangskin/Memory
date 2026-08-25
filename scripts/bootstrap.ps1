#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Memory MCP one-shot bootstrap (P1-1 / v0.6.0 OOTB hardening).

.DESCRIPTION
    Single deploy entry. Idempotent. Performs:
        1. venv create + pip install
        2. prompt (or accept -UserName) for stable user id
        3. write user_config.local.json into the Memory project root
        4. merge memory-mcp entry into .vscode/mcp.json
        5. final green-light health check (validate user + load config)

    All file mutations go through Python helpers in
    servers.memory_server.memory_bootstrap so the risky JSON merges are
    covered by pytest.

.PARAMETER UserName
    Stable user id for multi-user scoping. If omitted and the current
    user_config.local.json already has user_name, that value is kept. Legacy
    .vscode/settings.json memory-mcp.userName is reused only when no local
    Memory user config exists; otherwise the script prompts.

.PARAMETER PythonExe
    Bootstrapping interpreter (only used to create the venv).

.EXAMPLE
    pwsh ./bootstrap.ps1
    pwsh ./bootstrap.ps1 -UserName alice
#>

param(
    [string]$UserName,
    [string]$PythonExe = "python",
    [string]$RepoRoot
)

$ErrorActionPreference = "Stop"

$mcpRoot   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "_Resolve-MemoryRoots.ps1")
$roots     = Resolve-MemoryRoots -MemoryRoot $mcpRoot -RepoRoot $RepoRoot
$repoRoot  = $roots.RepoRoot
$venvDir   = Join-Path $mcpRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

Write-Host "[bootstrap] repo root : $repoRoot"
Write-Host "[bootstrap] mcp  root : $mcpRoot"
if ($roots.MemoryRelToRepo) {
    Write-Host "[bootstrap] mcp  rel  : $($roots.MemoryRelToRepo)"
}

# ── Step 1: venv + deps ────────────────────────────────────────────────
if (-not (Test-Path $venvPython)) {
    Write-Host "[bootstrap] creating venv ..."
    & $PythonExe -m venv $venvDir
}
# 经 dependency_guard 的 `--locked-pip` 转发，和 server 启动期自动修复共用同一把跨进程
# 锁；否则重新 bootstrap 一个已存在的 venv 时可能与正在自动修复的 server 撞上同一套
# site-packages。该模块只用标准库，mcp 已损坏时同样跑得起来。
$guardModule = Join-Path $mcpRoot "servers\memory_server\dependency_guard.py"
if (Test-Path $guardModule) {
    # 每一步都要单独查退出码。只看最后一条时，前一条被锁挡住（75）或直接失败都会被丢掉，
    # 脚本却照样打印 "dependencies OK"。
    $steps = @(
        @('install', '--upgrade', 'pip', '--quiet'),
        @('install', '-r', (Join-Path $mcpRoot "requirements.txt"), '--quiet')
    )
    Push-Location $mcpRoot
    try {
        foreach ($step in $steps) {
            & $venvPython -m servers.memory_server.dependency_guard --locked-pip --lock-wait 60 @step
            if ($LASTEXITCODE -eq 75) {
                throw "another process is installing into $venvDir right now; close the Memory MCP server and re-run"
            }
            if ($LASTEXITCODE -ne 0) {
                throw "pip step failed (exit=$LASTEXITCODE): $($step -join ' ')"
            }
        }
    }
    finally {
        Pop-Location
    }
}
else {
    & $venvPython -m pip install --upgrade pip --quiet
    if ($LASTEXITCODE -ne 0) { throw "pip self-upgrade failed (exit=$LASTEXITCODE)" }
    & $venvPython -m pip install -r (Join-Path $mcpRoot "requirements.txt") --quiet
    if ($LASTEXITCODE -ne 0) { throw "installing requirements.txt failed (exit=$LASTEXITCODE)" }
}
Write-Host "[bootstrap] dependencies OK"

$env:PYTHONPATH = $mcpRoot

# ── Step 2: resolve user id ────────────────────────────────────────────
$settingsPath = Join-Path $repoRoot ".vscode\settings.json"
$localUserConfigPath = Join-Path $mcpRoot "user_config.local.json"
if (-not $UserName) {
    if (Test-Path $localUserConfigPath) {
        try {
            $existingLocal = (Get-Content $localUserConfigPath -Raw | ConvertFrom-Json)
            $existingUser = $existingLocal.user_name
            if (-not $existingUser -and $existingLocal.user) {
                $existingUser = $existingLocal.user.name
            }
            if ($existingUser) {
                $UserName = $existingUser
                Write-Host "[bootstrap] reusing existing user_config.local.json user_name='$UserName'"
            }
        } catch {}
    }
}
if (-not $UserName) {
    if (Test-Path $settingsPath) {
        try {
            $existing = (Get-Content $settingsPath -Raw | ConvertFrom-Json)
            $existingUser = $existing.'memory-mcp.userName'
            if ($existingUser) {
                $UserName = $existingUser
                Write-Host "[bootstrap] reusing legacy memory-mcp.userName='$UserName'"
            }
        } catch {}
    }
}
if (-not $UserName) {
    $UserName = Read-Host "[bootstrap] Enter your stable user id (e.g. alice)"
}
if (-not $UserName) {
    throw "[bootstrap] user id is required for multi-user safety"
}

# ── Step 3-5: hand off to Python helpers ───────────────────────────────
$bootstrapScript = @"
import json, sys
from pathlib import Path

sys.path.insert(0, r'$mcpRoot')
from servers.memory_server.memory_bootstrap import (
    write_local_user_config, merge_mcp_json, health_green_light,
)

repo_root = Path(r'$repoRoot')

r1 = write_local_user_config(Path(r'$mcpRoot'), '$UserName')
print('write_local_user_config:', json.dumps(r1, ensure_ascii=False))

r2 = merge_mcp_json(
    repo_root,
    server_name='project-memory-mcp',
    python_exe=r'$venvPython',
    memory_root=r'$mcpRoot',
)
print('merge_mcp_json   :', json.dumps(r2, ensure_ascii=False))

r3 = health_green_light(repo_root)
print('health_green_light:', json.dumps(r3, ensure_ascii=False))

sys.exit(0 if r1.get('ok') and r2.get('ok') and r3.get('ok') else 1)
"@

$tmpScript = [System.IO.Path]::GetTempFileName() + ".py"
Set-Content -Path $tmpScript -Value $bootstrapScript -Encoding UTF8
try {
    & $venvPython $tmpScript
    if ($LASTEXITCODE -ne 0) {
        throw "[bootstrap] green-light health check failed; see output above"
    }
} finally {
    Remove-Item $tmpScript -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "[bootstrap] DONE ✔"
Write-Host "  user        : $UserName"
Write-Host "  venv python : $venvPython"
Write-Host "  user_config.local.json updated, mcp.json updated, health green."
