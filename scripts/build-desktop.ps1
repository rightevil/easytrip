param()
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backendRoot = Join-Path $repoRoot 'backend'
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvPath = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
if (-not (Test-Path -LiteralPath $uvPath -PathType Leaf)) { throw 'uv not found.' }
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'npm not found.' }
& $uvPath --directory $backendRoot sync --locked --group desktop
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Push-Location (Join-Path $repoRoot 'frontend')
try {
    & npm.cmd ci
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally { Pop-Location }
& $uvPath --directory $backendRoot run --locked --group desktop python -m PyInstaller --noconfirm --clean --distpath (Join-Path $repoRoot 'dist') --workpath (Join-Path $repoRoot 'build/desktop') (Join-Path $PSScriptRoot 'EasyTrip.spec')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output ('Ready: ' + (Join-Path $repoRoot 'dist/EasyTrip.exe'))
