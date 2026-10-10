param(
    [ValidateSet('probe', 'serve', 'test')][string]$Action = 'probe',
    [switch]$Reuse
)
$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvPath = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
if (-not (Test-Path -LiteralPath $uvPath)) { throw '未找到 uv，请先安装 uv。' }
& $uvPath --directory $appRoot sync --locked
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
switch ($Action) {
    'probe' {
        $probeArgs = @('--directory', $appRoot, 'run', 'python', '-m', 'backend.app.probe')
        if ($Reuse) { $probeArgs += '--reuse' }
        & $uvPath @probeArgs
    }
    'serve' { & $uvPath --directory $appRoot run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --no-access-log }
    'test' {
        & $uvPath --directory $appRoot run python -m pytest -q
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $uvPath --directory $appRoot run ruff check $appRoot
    }
}
exit $LASTEXITCODE
