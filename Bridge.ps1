param(
    [ValidateSet('doctor','models','list','restart','stop','test','call')]
    [string]$Action = 'doctor',
    [string]$Tool,
    [string]$ArgumentsJson = '{}'
)
$ErrorActionPreference = 'Stop'
$taskConfig = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json
$taskLinuxHome = (& wsl.exe -d $taskConfig.distro --exec printenv HOME).Trim()
if ($LASTEXITCODE -ne 0 -or -not $taskLinuxHome.StartsWith('/')) { throw 'Cannot resolve WSL home directory.' }
$taskRuntime = "$taskLinuxHome/.local/share/codex-opencode-bridge"
$taskScript = "$taskRuntime/bridge.py"
switch ($Action) {
    'doctor' { & wsl.exe -d $taskConfig.distro --exec python3 $taskScript doctor }
    'restart' { & wsl.exe -d $taskConfig.distro --exec python3 $taskScript restart }
    'stop' { & wsl.exe -d $taskConfig.distro --exec python3 $taskScript stop }
    'models' { & wsl.exe -d $taskConfig.distro --exec python3 $taskScript call bridge_models }
    'list' { & wsl.exe -d $taskConfig.distro --exec python3 $taskScript call job_list }
    'test' { & wsl.exe -d $taskConfig.distro --exec python3 -m unittest discover -s $taskRuntime -p test_bridge.py -v }
    'call' {
        if (-not $Tool) { throw 'Provide -Tool and optionally -ArgumentsJson.' }
        $taskArgFile = Join-Path $env:TEMP "codex-opencode-$([guid]::NewGuid().ToString('N')).json"
        try {
            [System.IO.File]::WriteAllText($taskArgFile, $ArgumentsJson, (New-Object System.Text.UTF8Encoding($false)))
            $taskWslArgFile = (& wsl.exe -d $taskConfig.distro --exec wslpath -u $taskArgFile).Trim()
            if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve argument file in WSL.' }
            & wsl.exe -d $taskConfig.distro --exec python3 $taskScript call $Tool --arguments-file $taskWslArgFile
            if ($LASTEXITCODE -ne 0) { throw 'Bridge call failed.' }
        } finally {
            if (Test-Path -LiteralPath $taskArgFile) { Remove-Item -LiteralPath $taskArgFile }
        }
    }
}
if ($LASTEXITCODE -ne 0) { throw "Bridge action failed: $Action" }
