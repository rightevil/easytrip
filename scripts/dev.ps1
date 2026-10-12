param(
    [ValidateSet('setup', 'build', 'serve', 'test')][string]$Action = 'serve',
    [ValidateRange(1024, 65535)][int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backendRoot = Join-Path $repoRoot 'backend'
$frontendRoot = Join-Path $repoRoot 'frontend'

if ($Action -eq 'serve' -and -not (Test-Path -LiteralPath (Join-Path $frontendRoot 'dist/index.html') -PathType Leaf)) {
    throw 'Frontend build missing. Run: .\scripts\dev.ps1 setup, then .\scripts\dev.ps1 build.'
}

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvPath = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
if (-not (Test-Path -LiteralPath $uvPath -PathType Leaf)) { throw 'uv not found. Install uv before running this script.' }

& $uvPath --directory $backendRoot sync --locked
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

switch ($Action) {
    'setup' { $npmArguments = @('ci') }
    'build' { $npmArguments = @('run', 'build') }
    'test' {
        & $uvPath --directory $backendRoot run --locked python -m pytest -q
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $uvPath --directory $backendRoot run --locked ruff check $backendRoot
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        $npmArguments = @('run', 'test')
    }
    'serve' {
        & $uvPath --directory $backendRoot run --locked uvicorn easytrip.main:app --host 127.0.0.1 --port $Port --workers 1 --no-access-log
        exit $LASTEXITCODE
    }
}

$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCommand) { throw 'npm not found. Install a Node.js version supported by frontend/package.json.' }
Push-Location $frontendRoot
try {
    & $npmCommand.Source @npmArguments
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
exit 0
