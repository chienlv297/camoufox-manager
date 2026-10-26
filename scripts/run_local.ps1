param(
    [switch]$BuildWebUi
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$dataPath = Join-Path $repoRoot 'data'
$dbPath = Join-Path $dataPath 'profiles.db'
$profilesPath = Join-Path $dataPath 'profiles'
$webUiPath = Join-Path $repoRoot 'src\camoufox_pm\webui'
$launcher = Join-Path $repoRoot '.venv\Scripts\camoufox-pm.exe'

if (-not (Test-Path -LiteralPath $dbPath)) { throw "Missing existing database $dbPath" }
if (-not (Test-Path -LiteralPath $launcher)) { throw "Missing $launcher; run uv sync first." }

if ($BuildWebUi) {
    $python = Join-Path $repoRoot '.venv\Scripts\python.exe'
    & $python (Join-Path $repoRoot 'scripts\build_webui.py')
    if ($LASTEXITCODE -ne 0) { throw 'Web UI build failed.' }
}

if (-not (Test-Path -LiteralPath (Join-Path $webUiPath 'index.html'))) {
    throw 'Static UI missing. Rerun with -BuildWebUi.'
}

$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listener) {
    throw "Port 8000 is already used by PID $($listener.OwningProcess). Stop that server before launching this repo."
}

$env:CPM_HOST = '127.0.0.1'
$env:CPM_PORT = '8000'
$env:CPM_API_KEY = ''
$env:CPM_DB_PATH = $dbPath
$env:CPM_PROFILES_DIR = $profilesPath
$env:CPM_WEBUI_DIR = $webUiPath

Write-Host "Starting Camoufox Profile Manager from $repoRoot on http://127.0.0.1:8000"
Write-Host 'API key disabled; manager stays bound to loopback. Press Ctrl+C to stop.'
& $launcher --host 127.0.0.1 --port 8000 --no-browser
exit $LASTEXITCODE
