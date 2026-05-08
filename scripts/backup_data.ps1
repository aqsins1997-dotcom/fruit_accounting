$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$dbPath = Join-Path $projectRoot "db.sqlite3"
$backupRoot = Join-Path $env:USERPROFILE "Documents\fruit_accounting_backups"
$timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$dailyDir = Join-Path $backupRoot (Get-Date -Format "yyyy-MM-dd")

if (-not (Test-Path $dbPath)) {
    throw "Файл базы данных не найден: $dbPath"
}

New-Item -ItemType Directory -Path $dailyDir -Force | Out-Null

$dbBackupPath = Join-Path $dailyDir "db_$timestamp.sqlite3"
$jsonBackupPath = Join-Path $dailyDir "data_$timestamp.json"

Copy-Item -LiteralPath $dbPath -Destination $dbBackupPath -Force

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pythonCommand = if (Test-Path $venvPython) { $venvPython } else { "python" }

Push-Location $projectRoot
try {
    & $pythonCommand manage.py dumpdata `
        --natural-foreign `
        --natural-primary `
        --exclude contenttypes `
        --exclude auth.permission `
        --exclude sessions.session `
        --indent 2 | Out-File -FilePath $jsonBackupPath -Encoding utf8
}
finally {
    Pop-Location
}

Write-Host "Бэкап базы создан:" -ForegroundColor Green
Write-Host $dbBackupPath
Write-Host "JSON-дамп создан:" -ForegroundColor Green
Write-Host $jsonBackupPath
