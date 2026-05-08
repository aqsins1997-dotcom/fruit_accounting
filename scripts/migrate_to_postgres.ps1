$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$dbPath = Join-Path $projectRoot "db.sqlite3"
$dataPath = Join-Path $projectRoot "data.json"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pythonCommand = if (Test-Path $venvPython) { $venvPython } else { "python" }
$backupRoot = Join-Path $env:USERPROFILE "Documents\fruit_accounting_backups"
$timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$dailyDir = Join-Path $backupRoot (Get-Date -Format "yyyy-MM-dd")
$dbBackupPath = Join-Path $dailyDir "db_$timestamp.sqlite3"

if (-not (Test-Path $dbPath)) {
    throw "SQLite database file not found: $dbPath"
}

if (-not $env:DATABASE_URL) {
    throw "DATABASE_URL environment variable is not set."
}

$postgresUrl = $env:DATABASE_URL

New-Item -ItemType Directory -Path $dailyDir -Force | Out-Null
Copy-Item -LiteralPath $dbPath -Destination $dbBackupPath -Force

$sqliteUrl = "sqlite:///" + (($dbPath -replace "\\", "/"))

Push-Location $projectRoot
try {
    $env:DATABASE_URL = $sqliteUrl
    $env:PYTHONUTF8 = "1"
    & $pythonCommand manage.py dumpdata --natural-foreign --natural-primary --output $dataPath

    $env:DATABASE_URL = $postgresUrl
    & $pythonCommand manage.py migrate
    & $pythonCommand manage.py loaddata $dataPath
    & $pythonCommand manage.py check
}
finally {
    Pop-Location
}

Write-Host "SQLite backup saved: $dbBackupPath" -ForegroundColor Green
Write-Host "Data dump updated: $dataPath" -ForegroundColor Green
Write-Host "PostgreSQL migration completed." -ForegroundColor Green
