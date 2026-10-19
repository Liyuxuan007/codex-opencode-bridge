$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'config.json'))) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'config.example.json') -Destination (Join-Path $PSScriptRoot 'config.json')
}
$taskConfig = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json
$taskSource = (& wsl.exe -d $taskConfig.distro --exec wslpath -u $PSScriptRoot).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve source directory inside WSL.' }
& wsl.exe -d $taskConfig.distro --exec python3 "$taskSource/deploy.py"
if ($LASTEXITCODE -ne 0) { throw 'WSL runtime deployment failed.' }
$taskCodex = (Get-Command codex -ErrorAction Stop).Source
$taskCodexConfig = Join-Path $env:USERPROFILE '.codex\config.toml'
if (Test-Path -LiteralPath $taskCodexConfig) {
    $taskBackup = "$taskCodexConfig.opencode-bridge-backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item -LiteralPath $taskCodexConfig -Destination $taskBackup
}
$taskLinuxHome = (& wsl.exe -d $taskConfig.distro --exec printenv HOME).Trim()
if ($LASTEXITCODE -ne 0 -or -not $taskLinuxHome.StartsWith('/')) { throw 'Cannot resolve WSL home directory.' }
$taskRuntime = "$taskLinuxHome/.local/share/codex-opencode-bridge/bridge.py"
& $taskCodex mcp add opencode_bridge -- wsl.exe -d $taskConfig.distro --exec python3 $taskRuntime mcp
if ($LASTEXITCODE -ne 0) { throw 'Codex MCP registration failed.' }
Write-Host 'Installed. Open a NEW Codex chat to load opencode_bridge tools.'
