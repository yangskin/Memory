function ConvertTo-PowerShellSingleQuotedLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)

    return "'" + $Value.Replace("'", "''") + "'"
}

function New-WorkspaceAwareMemoryServerEntry {
    <#
    .SYNOPSIS
    创建不会绑定到某个工作副本绝对路径的用户级 MCP 配置。

    .DESCRIPTION
    Codex、Cursor 等客户端从哪个工作区启动 MCP，子进程就会继承哪个工作目录。
    启动命令从该目录向上寻找 Memory 组件，因此 Project-A 与 Project-B 可以共用同一份
    用户级配置；两个工作副本反复运行 setup_mcp.ps1 也不会互相覆盖根目录。
    #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$MemoryRelativePath
    )

    $normalized = ($MemoryRelativePath -replace "\\", "/").Trim("/")
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        throw "MemoryRelativePath must not be empty."
    }
    if ([System.IO.Path]::IsPathRooted($normalized) -or ($normalized.Split("/") -contains "..")) {
        throw "MemoryRelativePath must be a safe path relative to the repository root: $MemoryRelativePath"
    }

    $runnerRelativePath = "$normalized/scripts/run_memory_server.ps1"
    $runnerLiteral = ConvertTo-PowerShellSingleQuotedLiteral $runnerRelativePath
    $launchCommand = @(
        '$cursor = [System.IO.DirectoryInfo]::new((Get-Location).Path)'
        "while (`$null -ne `$cursor) { `$runner = Join-Path -Path `$cursor.FullName -ChildPath $runnerLiteral; if (Test-Path -LiteralPath `$runner -PathType Leaf) { & `$runner; if (`$null -eq `$LASTEXITCODE) { exit 0 }; exit `$LASTEXITCODE }; `$cursor = `$cursor.Parent }"
        "[Console]::Error.WriteLine('project-memory-mcp: could not find $runnerRelativePath from workspace ' + (Get-Location).Path)"
        'exit 1'
    ) -join "; "

    return @{
        command = "powershell.exe"
        args = @(
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            $launchCommand
        )
        env = @{
            PYTHONUTF8 = "1"
        }
    }
}
