param(
    [ValidateSet('probe', 'serve', 'test')][string]$Action = 'probe',
    [switch]$Reuse
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$backendRoot = Join-Path $repoRoot "backend"
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvPath = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
if (-not (Test-Path -LiteralPath $uvPath)) { throw '未找到 uv，请先安装 uv。' }
& $uvPath --directory $backendRoot sync --locked
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
switch ($Action) {
    'probe' {
        $probeArgs = @('--directory', $backendRoot, 'run', 'python', '-m', 'easytrip.probe')
        if ($Reuse) { $probeArgs += '--reuse' }
        & $uvPath @probeArgs
    }
    'serve' { & $uvPath --directory $backendRoot run uvicorn easytrip.main:app --host 127.0.0.1 --port 8000 --no-access-log }
    'test' {
        & $uvPath --directory $backendRoot run python -m pytest -q
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $uvPath --directory $backendRoot run ruff check $backendRoot
    }
}
exit $LASTEXITCODE
