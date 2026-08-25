param(
    [string]$PythonExe,
    [switch]$InstallDeps,       # kept for backward compat (install is default-on)
    [switch]$SkipInstall,       # opt-out of dependency install
    [switch]$InstallDevDeps,
    [switch]$ForceRecreate,
    [switch]$RegisterVSCode,
    [switch]$Verify,
    [switch]$NoVerify,
    [string]$RepoRoot,
    # Embedding model bootstrap (off by default; opt-in to keep deploy hermetic).
    [switch]$DownloadModel,
    [switch]$SkipDownloadModel,
    [string]$ModelPreset = "bge-small-zh-v1.5",
    [string]$ModelVendorDir,
    [switch]$ModelOffline,
    [switch]$ModelOptional
)

# Default behaviour: ensure venv + ensure deps + verify imports.
# Use -SkipInstall to opt out of installing deps; -NoVerify to skip the import check.

$ErrorActionPreference = "Stop"

$memoryRoot = (Resolve-Path $PSScriptRoot).Path
$venvDir    = Join-Path $memoryRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$vendorDir  = Join-Path $memoryRoot "vendor"
$runtimeReq = Join-Path $memoryRoot "requirements.txt"

# ---------------------------------------------------------------------------
# 1) Detect required CPython version from vendored wheels
#    Vendor wheels look like: pkg-1.0-cp311-cp311-win_amd64.whl  → cp311
#    If only "py3-none-any" wheels exist, any Python 3 works.
# ---------------------------------------------------------------------------
function Get-RequiredCpTag {
    param([string]$VendorPath)
    if (-not (Test-Path $VendorPath)) { return $null }
    $cpTags = @(Get-ChildItem -Path $VendorPath -Filter *.whl |
        ForEach-Object { if ($_.Name -match '-(cp\d{2,3})-cp\d{2,3}-') { $Matches[1] } } |
        Sort-Object -Unique)
    if ($cpTags.Count -eq 0) { return $null }
    if ($cpTags.Count -gt 1) {
        Write-Host "WARNING: vendor contains mixed cp tags: $($cpTags -join ', '). Using $($cpTags[0])." -ForegroundColor Yellow
    }
    return [string]$cpTags[0]   # e.g. "cp311"
}

function Get-PythonCpTag {
    param([string]$Exe)
    if (-not (Test-Path $Exe)) { return $null }
    try {
        $out = & $Exe -c "import sys;print(f'cp{sys.version_info.major}{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -eq 0) { return ($out | Select-Object -First 1).Trim() }
    } catch {}
    return $null
}

function Test-PythonMatch {
    param([string]$Exe, [string]$WantTag)
    $tag = Get-PythonCpTag -Exe $Exe
    if (-not $tag) { return $false }
    if (-not $WantTag) { return $true }
    return ($tag -eq $WantTag)
}

# ---------------------------------------------------------------------------
# 2) Search candidate Python interpreters in priority order:
#    a) -PythonExe (user override, takes precedence)
#    b) UE bundled Python (UE 5.7 ships 3.11; matches our cp311 wheels)
#    c) py launcher with required version (e.g. py -3.11)
#    d) python / python3 on PATH
# ---------------------------------------------------------------------------
function Find-CandidatePythons {
    param([string]$WantTag)

    $list = New-Object System.Collections.Generic.List[string]

    # b) UE bundled python  (Engine\Binaries\ThirdParty\Python3\Win64\python.exe)
    $ueRoots = @(
        $env:UE_ROOT,
        "C:\Program Files\Epic Games\UE_5.7",
        "C:\Program Files\Epic Games\UE_5.6",
        "C:\Program Files\Epic Games\UE_5.5",
        "C:\Program Files\Epic Games\UE_5.4"
    ) | Where-Object { $_ -and (Test-Path $_) }
    foreach ($r in $ueRoots) {
        $p = Join-Path $r "Engine\Binaries\ThirdParty\Python3\Win64\python.exe"
        if (Test-Path $p) { $list.Add($p) | Out-Null }
    }

    # c) py launcher with desired version
    if ($WantTag -and ($WantTag -match '^cp(\d)(\d+)$')) {
        $major = $Matches[1]; $minor = $Matches[2]
        $pyLauncher = (Get-Command py.exe -ErrorAction SilentlyContinue)
        if ($pyLauncher) {
            try {
                $found = & py.exe "-$major.$minor" -c "import sys;print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $found) { $list.Add(($found | Select-Object -First 1).Trim()) | Out-Null }
            } catch {}
        }
    }

    # d) generic python / python3 on PATH
    foreach ($name in @('python.exe','python3.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $list.Add($cmd.Source) | Out-Null }
    }

    return ($list | Select-Object -Unique)
}

function Resolve-Python {
    param([string]$Override, [string]$WantTag)

    if ($Override) {
        if (-not (Test-Path $Override)) {
            Write-Host "ERROR: -PythonExe path does not exist: $Override" -ForegroundColor Red
            exit 1
        }
        $tag = Get-PythonCpTag -Exe $Override
        if ($WantTag -and $tag -ne $WantTag) {
            Write-Host "WARNING: -PythonExe is $tag but vendor wheels require $WantTag." -ForegroundColor Yellow
            Write-Host "         Install may fall back to online or fail. Continuing with user override." -ForegroundColor Yellow
        }
        return $Override
    }

    $candidates = Find-CandidatePythons -WantTag $WantTag
    if ($candidates.Count -eq 0) {
        Write-Host "ERROR: No Python interpreter found." -ForegroundColor Red
        Write-Host "       Install Python $($WantTag -replace 'cp(\d)(\d+)','$1.$2') (recommended) or pass -PythonExe <path>." -ForegroundColor Yellow
        exit 1
    }

    # Prefer one that matches the required cp tag
    foreach ($c in $candidates) {
        if (Test-PythonMatch -Exe $c -WantTag $WantTag) {
            Write-Host "Using Python (matches $WantTag): $c" -ForegroundColor Green
            return $c
        }
    }

    # No exact match → fail loudly with guidance
    Write-Host "ERROR: Found Python(s) but none match required tag '$WantTag':" -ForegroundColor Red
    foreach ($c in $candidates) {
        $t = Get-PythonCpTag -Exe $c
        Write-Host "       $c  ($t)" -ForegroundColor DarkYellow
    }
    Write-Host "Install matching Python or pass -PythonExe <path>." -ForegroundColor Yellow
    Write-Host "Tip: UE 5.7 ships Python 3.11 at:" -ForegroundColor DarkGray
    Write-Host "     C:\Program Files\Epic Games\UE_5.7\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" -ForegroundColor DarkGray
    exit 1
}

# ---------------------------------------------------------------------------
# 3) (Re)create venv, ensure its tag matches vendor wheels
# ---------------------------------------------------------------------------
$wantTag = Get-RequiredCpTag -VendorPath $vendorDir
if ($wantTag) {
    Write-Host "Vendor wheels require Python tag: $wantTag" -ForegroundColor DarkGray
} else {
    Write-Host "No cp-tagged wheels in vendor (pure-python only)." -ForegroundColor DarkGray
}

# 删一个正在运行的 venv 在 Windows 上不会干净失败：server 打开着的 `.pyd` 删不掉，其余
# 文件已经删了，于是 venv 变成"python.exe 还在、pip 和一半包没了"的状态。删除前先确认没人在用。
function Assert-VenvNotInUse {
    param([string]$Reason)
    if (-not (Test-Path $venvDir)) { return }
    $prefix = (Resolve-Path $venvDir).Path
    $users = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Id -ne $PID -and $_.Path -and $_.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($users.Count -eq 0) { return }
    Write-Host "ERROR: cannot delete this venv ($Reason) while it is in use." -ForegroundColor Red
    Write-Host "Windows cannot delete the .pyd files these processes have open, which" -ForegroundColor Yellow
    Write-Host "would leave a half-removed venv behind:" -ForegroundColor Yellow
    foreach ($p in $users) { Write-Host "  PID $($p.Id)  $($p.Path)" -ForegroundColor Yellow }
    Write-Host "Close the Memory MCP server (or the IDE hosting it) and re-run." -ForegroundColor Yellow
    exit 1
}

if ((Test-Path $venvPython) -and -not $ForceRecreate) {
    if ($wantTag -and -not (Test-PythonMatch -Exe $venvPython -WantTag $wantTag)) {
        $existing = Get-PythonCpTag -Exe $venvPython
        Write-Host "Existing venv ($existing) does not match required $wantTag → recreating." -ForegroundColor Yellow
        Assert-VenvNotInUse -Reason "python tag $existing does not match $wantTag"
        Remove-Item -Recurse -Force $venvDir
    }
}

if ($ForceRecreate -and (Test-Path $venvDir)) {
    Assert-VenvNotInUse -Reason "-ForceRecreate"
    Remove-Item -Recurse -Force $venvDir
}

if (-not (Test-Path $venvPython)) {
    $py = Resolve-Python -Override $PythonExe -WantTag $wantTag
    Write-Host "Creating virtual environment with: $py" -ForegroundColor Cyan
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $py -m venv $venvDir 2>&1 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    $ErrorActionPreference = $prevEAP
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        Write-Host "ERROR: Failed to create virtual environment." -ForegroundColor Red
        exit 1
    }
    Write-Host "Created venv at: $venvDir" -ForegroundColor Green
} else {
    Write-Host "Virtual environment already exists: $venvDir" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 4) Install dependencies
#    Default-on: install whenever venv is missing required packages,
#    or whenever the user explicitly asks (-InstallDeps / -InstallDevDeps / -ForceRecreate).
#    Opt-out via -SkipInstall.
# ---------------------------------------------------------------------------
function Test-DepsInstalled {
    param([string]$VenvPy)
    if (-not (Test-Path $VenvPy)) { return $false }
    $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        & $VenvPy -c "import mcp, mcp.server, pydantic, httpx, anyio, starlette" *> $null
        return ($LASTEXITCODE -eq 0)
    }
    finally {
        $ErrorActionPreference = $prevEAP
    }
}

$needInstall = -not $SkipInstall -and (
    $InstallDeps -or $InstallDevDeps -or $ForceRecreate -or -not (Test-DepsInstalled -VenvPy $venvPython)
)

if ($needInstall) {
    # 等锁的上限（秒）。必须明显小于下面每一步的进程超时：本脚本超时会杀进程树，
    # 如果"等锁"能耗到超时之后，用户看到的就是"pip 超时"而不是"别人正在装"。
    $LockWaitSeconds = 60
    $PipBusyExit = 75   # 与 dependency_guard.LOCKED_PIP_BUSY_EXIT 一致

    function Invoke-PipInstall {
        param([string]$PipExePath, [string[]]$Arguments, [int]$TimeoutSeconds = 900)
        $psi = [System.Diagnostics.ProcessStartInfo]::new()

        # 优先经 dependency_guard 的 `--locked-pip` 转发：Memory server 会在启动时自动
        # 修复依赖，直接调 pip.exe 会绕过那把跨进程锁，于是"手动重新部署"和"server 自动
        # 修复"可能同时写同一套 site-packages。该模块只用标准库，mcp 损坏时也跑得起来。
        $guardModule = Join-Path $script:memoryRoot "servers\memory_server\dependency_guard.py"
        if ((Test-Path $script:venvPython) -and (Test-Path $guardModule)) {
            $psi.FileName  = $script:venvPython
            $psi.Arguments = (@(
                '-m', 'servers.memory_server.dependency_guard', '--locked-pip',
                '--lock-wait', $script:LockWaitSeconds
            ) + $Arguments) -join ' '
            $psi.WorkingDirectory = $script:memoryRoot
        }
        else {
            $psi.FileName  = $PipExePath
            $psi.Arguments = $Arguments -join ' '
        }
        $psi.UseShellExecute = $false
        $proc = [System.Diagnostics.Process]::Start($psi)
        $exited = $proc.WaitForExit($TimeoutSeconds * 1000)
        if (-not $exited) {
            # 必须连子孙一起杀。只 Kill() 包装进程的话，pip 那一层会活下来，而此时包装
            # 进程已死、锁也随之释放 —— 留下一个不持锁却还在写 site-packages 的 pip。
            try { & taskkill.exe /T /F /PID $proc.Id 2>&1 | Out-Null } catch {}
            try { if (-not $proc.HasExited) { $proc.Kill() } } catch {}
            Write-Host "  ERROR: pip timed out after $TimeoutSeconds s." -ForegroundColor Red
            return 124
        }
        if ($proc.ExitCode -eq $script:PipBusyExit) {
            Write-Host "  Another process is installing into this venv right now." -ForegroundColor Yellow
            Write-Host "  Close the Memory MCP server in your client, then re-run." -ForegroundColor Yellow
        }
        return $proc.ExitCode
    }

    $pipExe = Join-Path $venvDir "Scripts\pip.exe"

    # 复用的 venv 里 pip 可能已经不见了（venv 被删到一半、或当初用 --without-pip 建的）。
    # 不先补回来，后面每一步都只会报 `No module named pip`，而那条消息不指向任何解法。
    $prevEAP0 = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & $venvPython -m pip --version 2>&1 | Out-Null
    $pipMissing = ($LASTEXITCODE -ne 0)
    if ($pipMissing) {
        Write-Host "pip is missing from this venv; restoring it with ensurepip..." -ForegroundColor Yellow
        & $venvPython -m ensurepip --default-pip 2>&1 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        $ensureCode = $LASTEXITCODE
    }
    $ErrorActionPreference = $prevEAP0
    if ($pipMissing -and $ensureCode -ne 0) {
        Write-Host "ERROR: could not restore pip (ensurepip exit=$ensureCode)." -ForegroundColor Red
        Write-Host "Close the Memory MCP server and re-run with -ForceRecreate." -ForegroundColor Yellow
        exit 1
    }

    # pip 自升级也要走同一把锁：它同样往这套 site-packages 里写。
    $pipUpgrade = Invoke-PipInstall -PipExePath $pipExe -Arguments @('install', '--upgrade', 'pip')
    if ($pipUpgrade -eq $PipBusyExit) {
        Write-Host "Nothing was installed because another process holds the install lock." -ForegroundColor Yellow
        exit 75
    }
    if ($pipUpgrade -ne 0) {
        Write-Host "  WARN pip self-upgrade failed (exit=$pipUpgrade); continuing." -ForegroundColor Yellow
    }

    if (Test-Path $runtimeReq) {
        $installed = $false

        if (Test-Path $vendorDir) {
            Write-Host "Installing runtime deps from offline vendor (cp tag $wantTag)..." -ForegroundColor Cyan
            $offlineArgs = @('install', '-r', "`"$runtimeReq`"", '--no-index', '--find-links', "`"$vendorDir`"")
            $exitCode = Invoke-PipInstall -PipExePath $pipExe -Arguments $offlineArgs -TimeoutSeconds 900
            if ($exitCode -eq 0) { $installed = $true }
            elseif ($exitCode -eq $PipBusyExit) {
                # 别人正在往这个 venv 里装，不是"离线源不行"。改走在线只会撞上同一把锁，
                # 还会让人误以为 vendor/ 坏了。
                Write-Host "Nothing was installed because another process holds the install lock." -ForegroundColor Yellow
                exit 75
            }
            else { Write-Host "  Offline install failed (exit=$exitCode), trying online..." -ForegroundColor Yellow }
        }

        if (-not $installed) {
            Write-Host "Installing runtime deps from PyPI (online)..." -ForegroundColor Cyan
            $onlineArgs = @('install', '-r', "`"$runtimeReq`"", '--retries', '3', '--timeout', '120', '--progress-bar', 'on')
            $exitCode2 = Invoke-PipInstall -PipExePath $pipExe -Arguments $onlineArgs -TimeoutSeconds 900
            if ($exitCode2 -eq 0) { $installed = $true }
            elseif ($exitCode2 -eq $PipBusyExit) {
                Write-Host "Nothing was installed because another process holds the install lock." -ForegroundColor Yellow
                exit 75
            }
        }

        if (-not $installed) {
            Write-Host "ERROR: Failed to install runtime dependencies." -ForegroundColor Red
            exit 1
        }
    }

    if ($InstallDevDeps) {
        $devReq = Join-Path $memoryRoot "requirements-dev.txt"
        if (Test-Path $devReq) {
            # 开发依赖同样写这套 site-packages，必须走同一把锁。
            $devExit = Invoke-PipInstall -PipExePath $pipExe -Arguments @('install', '-r', "`"$devReq`"")
            if ($devExit -eq $PipBusyExit) {
                Write-Host "Dev dependencies were skipped: another process holds the install lock." -ForegroundColor Yellow
            }
            elseif ($devExit -ne 0) {
                Write-Host "  WARN dev dependencies failed to install (exit=$devExit); tests will not run here." -ForegroundColor Yellow
            }
        }
    }
}

# ---------------------------------------------------------------------------
# 5) Verify (import all top-level deps from requirements.txt)
#    Default-on; suppress with -NoVerify.
# ---------------------------------------------------------------------------
if (-not $NoVerify) {
    Write-Host "Verifying critical imports..." -ForegroundColor Cyan
    $check = & $venvPython -c "import mcp, mcp.server, pydantic, httpx, anyio, starlette; from importlib.metadata import version; print('OK mcp', version('mcp'))" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  $check" -ForegroundColor Green
    } else {
        Write-Host "  Verification FAILED:" -ForegroundColor Red
        $check | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
        Write-Host "  Hint: rerun with -ForceRecreate to rebuild the venv." -ForegroundColor Yellow
        exit 1
    }
}

# ---------------------------------------------------------------------------
# 6) Optional: register VS Code mcp.json
# ---------------------------------------------------------------------------
if ($RegisterVSCode) {
    if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
        & (Join-Path $memoryRoot "setup_mcp.ps1")
    } else {
        & (Join-Path $memoryRoot "setup_mcp.ps1") -RepoRoot $RepoRoot
    }
}

# ---------------------------------------------------------------------------
# 7) Optional: bootstrap embedding model (P5 RAG provider)
#    Off by default. Triggers when:
#      * -DownloadModel switch is set, OR
#      * env MEMORY_DOWNLOAD_MODEL=1 is set
#    Suppressed by -SkipDownloadModel.
#    Default source: vendor/models/<preset>/  (offline, sha256-verified copy)
#    Falls back to https download unless -ModelOffline is set.
# ---------------------------------------------------------------------------
$envWantDownload = $env:MEMORY_DOWNLOAD_MODEL -and ($env:MEMORY_DOWNLOAD_MODEL -ne "0")
if (-not $SkipDownloadModel -and ($DownloadModel -or $envWantDownload)) {
    $downloadScript = Join-Path $memoryRoot "scripts\download_embedding_model.py"
    if (-not (Test-Path $downloadScript)) {
        Write-Host "WARNING: model download requested but $downloadScript not found." -ForegroundColor Yellow
    } else {
        $modelRepo = if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $memoryRoot } else { $RepoRoot }
        $argsList = @("`"$downloadScript`"", "--repo", "`"$modelRepo`"", "--preset", $ModelPreset)
        if ($ModelVendorDir) { $argsList += @("--vendor-dir", "`"$ModelVendorDir`"") }
        if ($ModelOffline)   { $argsList += "--no-network" }
        Write-Host "Bootstrapping embedding model preset: $ModelPreset" -ForegroundColor Cyan
        $prevEAP4 = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        & $venvPython @argsList
        $modelExit = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP4
        if ($modelExit -ne 0) {
            Write-Host "" -ForegroundColor Yellow
            Write-Host "Model bootstrap FAILED (exit=$modelExit)." -ForegroundColor Yellow
            Write-Host "  Manual retry:" -ForegroundColor Yellow
            Write-Host "    `"$venvPython`" `"$downloadScript`" --repo `"$modelRepo`" --preset $ModelPreset" -ForegroundColor DarkYellow
            Write-Host "  Offline / bundled deployment:" -ForegroundColor Yellow
            Write-Host "    1) Pre-stage files under $memoryRoot\vendor\models\$ModelPreset\ (sha256 must match preset)" -ForegroundColor DarkYellow
            Write-Host "    2) Re-run with -DownloadModel -ModelOffline (refuses HTTP fallback)" -ForegroundColor DarkYellow
            Write-Host "  See vendor\models\README.md for the full bundling layout." -ForegroundColor DarkYellow
            if (-not $ModelOptional) { exit $modelExit }
            Write-Host "  -ModelOptional set: continuing without embedding model (RAG falls back to deterministic-hash)." -ForegroundColor Yellow
        }
    }
}

Write-Host ""
Write-Host "Python: $venvPython"
Write-Host "Done."
