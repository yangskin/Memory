$ErrorActionPreference = "Stop"

$memoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $memoryRoot "scripts\New-WorkspaceAwareMemoryServerEntry.ps1")

function Assert-Equal {
    param($Expected, $Actual, [string]$Message)

    if ($Expected -ne $Actual) {
        throw "$Message Expected=[$Expected] Actual=[$Actual]"
    }
}

$entry = New-WorkspaceAwareMemoryServerEntry -MemoryRelativePath "MCP/Memory"
Assert-Equal "powershell.exe" $entry.command "The user-level launcher must not bind to a worktree."

$serialized = $entry | ConvertTo-Json -Depth 20
if ($serialized -match '[A-Za-z]:[\\/]') {
    throw "The user-level launcher contains an absolute Windows path: $serialized"
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("memory-mcp-worktree-root-" + [Guid]::NewGuid().ToString("N"))
try {
    foreach ($name in @("Project-A", "Project-B")) {
        $repoRoot = Join-Path $tempRoot $name
        $nestedWorkingDirectory = Join-Path $repoRoot "Source\Nested"
        $runnerDirectory = Join-Path $repoRoot "MCP\Memory\scripts"
        New-Item -ItemType Directory -Path $nestedWorkingDirectory -Force | Out-Null
        New-Item -ItemType Directory -Path $runnerDirectory -Force | Out-Null

        $runnerPath = Join-Path $runnerDirectory "run_memory_server.ps1"
        [System.IO.File]::WriteAllText(
            $runnerPath,
            "[Console]::Out.WriteLine('$name')`r`n",
            [System.Text.UTF8Encoding]::new($false)
        )

        Push-Location $nestedWorkingDirectory
        try {
            $output = & $entry.command @($entry.args)
            $exitCode = $LASTEXITCODE
        }
        finally {
            Pop-Location
        }

        Assert-Equal 0 $exitCode "$name launcher returned a non-zero exit code."
        Assert-Equal $name (($output | Out-String).Trim()) "$name did not resolve its own worktree."
    }
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}

Write-Host "setup_mcp worktree-root regression test passed."